"""Local-only, persistent domain approval policy for Crawl4AI MCP requests.

Only the local dashboard can modify decisions. All remote MCP tool calls may do is
create or refresh a *pending* request. Never accept approvals via MCP.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import ipaddress
import os
from pathlib import Path
import re
import socket
import sqlite3
from urllib.parse import urlsplit, urlunsplit
from public_suffix import is_public_suffix

UI_URL = "http://127.0.0.1:8791/"
SCHEMA_VERSION = 2


def storage_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")).expanduser()
    return base / "local-search-mcp"


def database_path() -> Path:
    return storage_dir() / "domains.sqlite3"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@contextmanager
def _db():
    directory = storage_dir()
    if directory.is_symlink():
        raise RuntimeError("Refusing symlinked local-search-mcp state directory")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    path = database_path()
    if path.is_symlink():
        raise RuntimeError("Refusing symlinked domains database")
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    os.chmod(path, 0o600)
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("""CREATE TABLE IF NOT EXISTS domains (
        domain TEXT PRIMARY KEY,
        status TEXT NOT NULL CHECK(status IN ('allowed', 'pending', 'denied')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        request_count INTEGER NOT NULL DEFAULT 0,
        last_requested_at TEXT NOT NULL DEFAULT '',
        last_requested_url TEXT NOT NULL DEFAULT '',
        explicit_rule INTEGER NOT NULL DEFAULT 0 CHECK(explicit_rule IN (0,1))
    )""")
    # Older installs have domains but no separate marker for intentional
    # dashboard decisions versus automatically created request rows.
    if not any(col["name"] == "explicit_rule" for col in conn.execute("PRAGMA table_info(domains)")):
        conn.execute("ALTER TABLE domains ADD COLUMN explicit_rule INTEGER NOT NULL DEFAULT 0 CHECK(explicit_rule IN (0,1))")
    conn.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("""CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL, domain TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('allow', 'deny', 'delete', 'rename'))
    )""")
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def normalize_domain(domain: str) -> str:
    if not isinstance(domain, str):
        raise ValueError("Domain must be text")
    domain = domain.strip().lower().rstrip(".")
    if not domain or any(c in domain for c in '/:@?#\\ \t\n'):
        raise ValueError("Enter only a domain name, not a URL")
    try:
        domain = domain.encode('idna').decode('ascii')
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Invalid IDN domain") from exc
    if len(domain) > 253 or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", domain):
        raise ValueError("Domain must be a valid public DNS name")
    if domain.endswith((".local", ".localhost", ".internal", ".test", ".invalid", ".onion", ".home", ".lan")):
        raise ValueError("Private or special-use domains cannot be approved")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise ValueError("IP addresses are not permitted")
    if is_public_suffix(domain):
        raise ValueError("Public suffixes (including shared hosting domains) cannot be approved")
    return domain


def _seed(conn):
    """Import existing domain settings once, never overwrite UI edits on restart."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute("SELECT value FROM metadata WHERE key='domain_seed_v1'").fetchone()
        if existing is None:
            raw = os.environ.get("ALLOWED_CRAWL_DOMAINS", "example.com")
            for entry in raw.split(','):
                try:
                    domain = normalize_domain(entry)
                except ValueError:
                    continue
                now = utcnow()
                conn.execute("INSERT OR IGNORE INTO domains(domain,status,created_at,updated_at) VALUES(?,?,?,?)", (domain,'allowed',now,now))
            conn.execute("INSERT INTO metadata (key, value) VALUES ('domain_seed_v1', '1')")
        # Preserve intentional decisions when upgrading older databases. Older
        # requests copied a parent's denial into child rows without user action.
        # Such inherited rows must never become independent deny rules.
        if conn.execute("SELECT 1 FROM metadata WHERE key='explicit_rules_v1'").fetchone() is None:
            # Parent status is NOT evidence of a child's own decision:
            # the old request() copied denials to children even if the parent
            # was later allowed or deleted. The old rename audit stores only
            # the source name, not the destination. Preserve renamed rules via
            # their updated timestamp (conservatively retaining ties), while
            # classifying unreviewed, request-derived denials as pending.
            conn.execute("""UPDATE domains SET explicit_rule=1
                WHERE status='allowed' OR
                (status='denied' AND (
                    EXISTS (SELECT 1 FROM decisions d WHERE d.domain=domains.domain
                            AND d.action='deny')
                    OR (request_count=0 AND last_requested_at='')
                    OR updated_at != created_at
                    OR EXISTS (SELECT 1 FROM decisions d
                               WHERE d.action='rename' AND d.at=domains.updated_at)
                ))""")
            # The only remaining denials are auto-inherited bookkeeping rows.
            # Keep their requests pending until a local decision is made.
            conn.execute("UPDATE domains SET status='pending' WHERE status='denied' AND explicit_rule=0")
            # Old releases also left pending child requests behind after the
            # operator changed a parent rule. Those are not actionable anymore.
            conn.execute("""DELETE FROM domains WHERE explicit_rule=0
                AND EXISTS (
                    SELECT 1 FROM domains parent
                    WHERE parent.explicit_rule=1
                      AND parent.status IN ('allowed','denied')
                      AND domains.domain != parent.domain
                      AND domains.domain LIKE '%.' || parent.domain
                )""")
            conn.execute("INSERT INTO metadata(key,value) VALUES('explicit_rules_v1','1')")
        # Existing databases may contain insecure broad grants from older versions.
        # Remove unsafe rules once and preserve a decision audit trail.
        if conn.execute("SELECT 1 FROM metadata WHERE key='psl_cleanup_v1'").fetchone() is None:
            for row in conn.execute("SELECT domain FROM domains").fetchall():
                try:
                    normalize_domain(row["domain"])
                except ValueError:
                    conn.execute("DELETE FROM domains WHERE domain=?", (row["domain"],))
                    conn.execute("INSERT INTO decisions(at,domain,action) VALUES(?,?,'delete')", (utcnow(), row["domain"]))
            conn.execute("INSERT INTO metadata(key,value) VALUES('psl_cleanup_v1','1')")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def initialize():
    with _db() as conn:
        _seed(conn)


def entries(search: str = "", limit: int = 1000):
    with _db() as conn:
        _seed(conn)
        if search:
            s = '%' + search.lower().replace('%','\\%').replace('_','\\_') + '%'
            rows=conn.execute("SELECT * FROM domains WHERE domain LIKE ? ESCAPE '\\' ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'allowed' THEN 1 ELSE 2 END, updated_at DESC LIMIT ?", (s, limit)).fetchall()
        else:
            rows=conn.execute("SELECT * FROM domains ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'allowed' THEN 1 ELSE 2 END, updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def counts():
    with _db() as conn:
        _seed(conn)
        return {s: conn.execute('SELECT count(*) FROM domains WHERE status=?',(s,)).fetchone()[0] for s in ('pending','allowed','denied')}


def _match(conn, domain: str):
    """Return the most specific *explicit* policy rule, never request metadata."""
    pieces = domain.split('.')
    for i in range(len(pieces) - 1):
        candidate = '.'.join(pieces[i:])
        row = conn.execute('SELECT * FROM domains WHERE domain=?', (candidate,)).fetchone()
        if row and row['explicit_rule'] and row['status'] in ('allowed', 'denied'):
            return dict(row)
    return None


def request(domain: str, safe_url: str):
    """Apply explicit parent/child rules while recording requests as metadata.

    An automatically created pending child cannot override a later parent
    decision. Only a local user's explicit decision can create a child rule.
    """
    domain = normalize_domain(domain)
    now = utcnow()
    with _db() as conn:
        _seed(conn)
        conn.execute('BEGIN IMMEDIATE')
        try:
            rule = _match(conn, domain)
            exact = conn.execute('SELECT domain FROM domains WHERE domain=?', (domain,)).fetchone()
            status = rule['status'] if rule else 'pending'
            matched = rule['domain'] if rule else domain
            if exact is None and rule is None:
                conn.execute("""INSERT INTO domains
                    (domain,status,created_at,updated_at,request_count,last_requested_at,last_requested_url)
                    VALUES (?,'pending',?,?,1,?,?)""", (domain, now, now, now, safe_url[:512]))
            else:
                # Keep per-host request counts when already present; otherwise
                # count against the matching explicit parent without creating
                # inherited allow/deny rows for every agent-requested host.
                target = domain if exact else matched
                conn.execute("""UPDATE domains SET request_count=request_count+1,
                    last_requested_at=?,last_requested_url=? WHERE domain=?""",
                    (now, safe_url[:512], target))
            conn.commit()
            return status, matched
        except BaseException:
            conn.rollback()
            raise


def decide(domain: str, action: str, new_domain: str = ""):
    domain=normalize_domain(domain)
    if action not in ('allow','deny','delete','rename'):
        raise ValueError('Unrecognized domain action')
    if action == 'rename':
        new_domain=normalize_domain(new_domain)
    now=utcnow()
    with _db() as conn:
        _seed(conn)
        conn.execute('BEGIN IMMEDIATE')
        try:
            if action in ('allow','deny'):
                status = 'allowed' if action == 'allow' else 'denied'
                conn.execute("""INSERT INTO domains(domain,status,created_at,updated_at,explicit_rule)
                    VALUES(?,?,?,?,1) ON CONFLICT(domain) DO UPDATE
                    SET status=excluded.status,updated_at=excluded.updated_at,explicit_rule=1""",
                    (domain,status,now,now))
                # Pending descendant requests were never independent rules.
                # After an explicit parent decision their queue entries are
                # obsolete; explicit child allow/deny exceptions are preserved.
                conn.execute("""DELETE FROM domains WHERE explicit_rule=0
                    AND domain != ? AND domain LIKE '%.' || ?""", (domain,domain))
            elif action == 'delete':
                conn.execute('DELETE FROM domains WHERE domain=?',(domain,))
            else:
                if not conn.execute('SELECT 1 FROM domains WHERE domain=?',(domain,)).fetchone():
                    raise ValueError('Domain no longer exists')
                if domain != new_domain:
                    conn.execute('UPDATE domains SET domain=?,updated_at=? WHERE domain=?',(new_domain,now,domain))
            conn.execute('INSERT INTO decisions(at,domain,action) VALUES(?,?,?)',(now,domain,action))
            conn.commit()
        except BaseException:
            conn.rollback(); raise


def safe_display_url(url: str) -> str:
    """Discard URL query parameters, fragments, and credentials from stored requests."""
    try:
        p=urlsplit(url)
        return urlunsplit((p.scheme, p.hostname or '', p.path[:350], '', ''))[:500]
    except ValueError:
        return '[invalid URL]'


def evaluate_fetch_url(url: str) -> dict | None:
    """None = allowed; otherwise return a safe, structured MCP response.

    Validation against private DNS is defense in depth, not a substitute for
    outbound network isolation on Crawl4AI, which resolves destinations later.
    """
    shown=safe_display_url(url)
    try:
        parsed=urlsplit(url)
        hostname=parsed.hostname or ''
        host=normalize_domain(hostname)
        port=parsed.port
        if parsed.scheme not in ('http','https') or parsed.username or parsed.password or port not in (None,80,443):
            raise ValueError('Only standard public HTTP(S) URLs are permitted')
    except (ValueError, TypeError) as exc:
        return {'success': False, 'status': 'blocked', 'url': shown, 'reason': str(exc)}
    status, matched=request(host, shown)
    if status == 'pending':
        return {'success':False,'status':'approval_required','url':shown,'domain':host,'rule':matched,'reason':'Domain approval required. A request is waiting in the local dashboard. Ask the user to approve it, then retry this tool call.','approval_dashboard':UI_URL}
    if status == 'denied':
        return {'success':False,'status':'denied','url':shown,'domain':host,'rule':matched,'reason':'Domain denied in local dashboard. Only the local user can change this decision.','approval_dashboard':UI_URL}
    try:
        addresses=socket.getaddrinfo(host, port or (443 if parsed.scheme=='https' else 80), type=socket.SOCK_STREAM)
    except socket.gaierror:
        return {'success': False, 'status': 'blocked', 'url': shown, 'domain':host, 'reason': 'DNS lookup failed'}
    if not addresses or not all(ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        return {'success': False, 'status': 'blocked', 'url': shown, 'domain':host, 'reason': 'Non-public resolved address blocked'}
    return None
