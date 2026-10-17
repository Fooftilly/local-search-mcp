"""Local-only, persistent domain approval policy for Crawl4AI MCP requests.

Only the local dashboard can modify decisions. All remote MCP tool calls may do is
create or refresh a *pending* request. Never accept approvals via MCP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import ipaddress
import os
from pathlib import Path
import re
import socket
import sqlite3
from urllib.parse import urlsplit, urlunsplit
from public_suffix import is_public_suffix, registrable_domain
from request_cache import invalidate_fetch

UI_URL = "http://127.0.0.1:8791/"
SCHEMA_VERSION = 4
# Fixed safeguards for agent-created pending requests (not explicit rules).
MAX_PENDING_TOTAL = 512
MAX_NEW_PENDING_GLOBAL_PER_HOUR = 128
MAX_PENDING_PER_SITE = 32
MAX_NEW_PENDING_PER_SITE_PER_HOUR = 8


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
    try:
        conn.row_factory = sqlite3.Row
        os.chmod(path, 0o600)
        conn.execute("PRAGMA busy_timeout = 10000")
        # The dashboard and bridge can open the same v1 database at startup.
        # Serialize schema discovery and ALTER so the second connection
        # rechecks after the first commit instead of adding a duplicate column.
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS domains (
                domain TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK(status IN ('allowed', 'pending', 'denied')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                last_requested_at TEXT NOT NULL DEFAULT '',
                last_requested_url TEXT NOT NULL DEFAULT '',
                explicit_rule INTEGER NOT NULL DEFAULT 0 CHECK(explicit_rule IN (0,1)),
                match_scope TEXT NOT NULL DEFAULT 'subdomains' CHECK(match_scope IN ('exact','subdomains')),
                expires_at TEXT NOT NULL DEFAULT ''
            )""")
            if not any(col["name"] == "explicit_rule" for col in conn.execute("PRAGMA table_info(domains)")):
                conn.execute("""ALTER TABLE domains ADD COLUMN explicit_rule INTEGER
                    NOT NULL DEFAULT 0 CHECK(explicit_rule IN (0,1))""")
            columns = {col["name"] for col in conn.execute("PRAGMA table_info(domains)")}
            if "match_scope" not in columns:
                conn.execute("""ALTER TABLE domains ADD COLUMN match_scope TEXT
                    NOT NULL DEFAULT 'subdomains'
                    CHECK(match_scope IN ('exact','subdomains'))""")
            if "expires_at" not in columns:
                conn.execute("ALTER TABLE domains ADD COLUMN expires_at TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("""CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL, domain TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('allow', 'deny', 'delete', 'rename'))
            )""")
            # Retain hourly admission history even after an operator resolves
            # or deletes pending domains. A request row is not an audit log.
            conn.execute("""CREATE TABLE IF NOT EXISTS pending_admissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT NOT NULL,
                admitted_at TEXT NOT NULL
            )""")
            conn.execute("""CREATE INDEX IF NOT EXISTS
                idx_pending_admissions_site_time
                ON pending_admissions(site, admitted_at)""")
            conn.execute("""CREATE INDEX IF NOT EXISTS
                idx_pending_admissions_time
                ON pending_admissions(admitted_at)""")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
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
            # was later allowed or deleted. A deleted or renamed child rule
            # could also be recreated by request() as an inherited denial.
            # Thus only the latest audit action for the current name may
            # prove an explicit deny; historical denies are not sufficient.
            # Rename audits store the source but not the destination, so
            # preserve destination rules by their updated timestamp
            # (conservatively retaining ties).
            conn.execute("""UPDATE domains SET explicit_rule=1
                WHERE status='allowed' OR
                (status='denied' AND (
                    (SELECT d.action FROM decisions d
                     WHERE d.domain=domains.domain ORDER BY d.id DESC LIMIT 1) = 'deny'
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


def entries(search: str = "", limit: int = 1000, status: str | None = None):
    """Show pending requests and explicit decisions independently."""
    if status not in (None, "pending", "decided"):
        raise ValueError("Invalid domain entry filter")
    with _db() as conn:
        _seed(conn)
        clauses = []
        args = []
        if status == "pending":
            clauses.append("status='pending'")
        elif status == "decided":
            clauses.append("status IN ('allowed','denied')")
        if search:
            s = "%" + search.lower().replace("%", "\\%").replace("_", "\\_") + "%"
            clauses.append("domain LIKE ? ESCAPE '\\'")
            args.append(s)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(
            "SELECT * FROM domains" + where +
            " ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'allowed' THEN 1 ELSE 2 END, updated_at DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
        now = utcnow()
        return [dict(r) | {"expired": bool(
            r["explicit_rule"] and r["expires_at"] and r["expires_at"] <= now)}
            for r in rows]

def counts():
    with _db() as conn:
        _seed(conn)
        now = utcnow()
        return {s: conn.execute(
            "SELECT count(*) FROM domains WHERE status=? AND "
            "(?='pending' OR expires_at='' OR expires_at>?)",
            (s, s, now)).fetchone()[0] for s in ('pending','allowed','denied')}


def _match(conn, domain: str, now: str):
    """Most-specific currently valid rule; exact-host scope never matches children."""
    pieces = domain.split('.')
    for i in range(len(pieces) - 1):
        candidate = '.'.join(pieces[i:])
        row = conn.execute('SELECT * FROM domains WHERE domain=?', (candidate,)).fetchone()
        if not row or not row['explicit_rule'] or row['status'] not in ('allowed','denied'):
            continue
        if row['match_scope'] == 'exact' and candidate != domain:
            continue
        # ISO8601 UTC seconds are fixed width, so lexical order is chronological.
        if row['expires_at'] and row['expires_at'] <= now:
            continue
        return dict(row)
    return None


def request(domain: str, safe_url: str):
    """Apply explicit parent/child rules while recording requests as metadata.

    An automatically created pending child cannot override a later parent
    decision. Only a local user's explicit decision can create a child rule.
    """
    domain = normalize_domain(domain)
    with _db() as conn:
        _seed(conn)
        conn.execute('BEGIN IMMEDIATE')
        try:
            # Read time only after acquiring the write lock. A request may
            # wait for SQLite while a finite permission reaches its expiry.
            now = utcnow()
            rule = _match(conn, domain, now)
            exact = conn.execute('SELECT * FROM domains WHERE domain=?', (domain,)).fetchone()
            expired_exact = bool(
                rule is None and exact is not None and exact['explicit_rule']
                and exact['expires_at'] and exact['expires_at'] <= now)
            status = rule['status'] if rule else 'pending'
            matched = rule['domain'] if rule else domain
            if rule is None and (exact is None or expired_exact):
                # Both new hosts AND expired exact decisions consume the same
                # bounded admission quota. Never let mass expiry inflate the
                # pending table or bypass per-site/hourly limits.
                site = registrable_domain(domain)
                cutoff = (datetime.fromisoformat(now.replace("Z", "+00:00")) -
                          timedelta(hours=1)).isoformat(
                              timespec="seconds").replace("+00:00", "Z")
                conn.execute("DELETE FROM pending_admissions WHERE admitted_at < ?",
                             (cutoff,))
                total = conn.execute(
                    "SELECT count(*) FROM domains WHERE status='pending'"
                ).fetchone()[0]
                per_site = conn.execute(
                    """SELECT count(*) FROM domains WHERE status='pending'
                       AND (domain=? OR domain LIKE ?)""",
                    (site, "%." + site),
                ).fetchone()[0]
                recent_site_admissions = conn.execute(
                    """SELECT count(*) FROM pending_admissions
                       WHERE site=? AND admitted_at >= ?""",
                    (site, cutoff),
                ).fetchone()[0]
                recent_global_admissions = conn.execute(
                    "SELECT count(*) FROM pending_admissions WHERE admitted_at >= ?",
                    (cutoff,),
                ).fetchone()[0]
                if (total >= MAX_PENDING_TOTAL or
                        recent_global_admissions >= MAX_NEW_PENDING_GLOBAL_PER_HOUR or
                        per_site >= MAX_PENDING_PER_SITE or
                        recent_site_admissions >= MAX_NEW_PENDING_PER_SITE_PER_HOUR):
                    # Leave an expired explicit rule visible as expired instead
                    # of silently exceeding admission limits or claiming it
                    # was queued. The operator can decide it manually.
                    status = 'queue_full'
                else:
                    if exact is None:
                        conn.execute("""INSERT INTO domains
                            (domain,status,created_at,updated_at,request_count,
                             last_requested_at,last_requested_url)
                            VALUES (?,'pending',?,?,1,?,?)""",
                            (domain, now, now, now, safe_url[:512]))
                    else:
                        # Preserve its chosen scope as the default for
                        # local reapproval. _match already ignored the expired
                        # rule; converting its display state does not revoke a
                        # previously active authorization. Do not bump the
                        # process-wide fetch-cache epoch from an agent request.
                        conn.execute("""UPDATE domains
                            SET status='pending',explicit_rule=0,updated_at=?
                            WHERE domain=?""", (now, domain))
                    conn.execute("""INSERT INTO pending_admissions(site,admitted_at)
                        VALUES (?,?)""", (site, now))
            if exact is not None or rule is not None:
                # Preserve existing request metadata on a known row even when
                # the expired rule cannot be added to the bounded queue.
                target = domain if exact else matched
                conn.execute("""UPDATE domains SET request_count=request_count+1,
                    last_requested_at=?,last_requested_url=? WHERE domain=?""",
                    (now, safe_url[:512], target))
            conn.commit()
            return status, matched
        except BaseException:
            conn.rollback()
            raise



def expired_grant_without_active_fallback(domain: str) -> bool:
    """Read-only post-fetch safeguard for in-flight finite permissions.

    Authorization is checked at admission by evaluate_fetch_url(). A slow
    crawler may finish after a timed rule expires. Recheck under SQLite without
    changing the pending queue or globally invalidating unrelated fetches.
    Pending rows retain their old expiry timestamp after requeue so concurrent
    requests cannot erase evidence of the deadline.
    """
    domain = normalize_domain(domain)
    with _db() as conn:
        _seed(conn)
        now = utcnow()
        if _match(conn, domain, now) is not None:
            return False
        labels = domain.split('.')
        for i in range(len(labels) - 1):
            candidate = '.'.join(labels[i:])
            row = conn.execute(
                "SELECT explicit_rule,match_scope,expires_at,status FROM domains "
                "WHERE domain=?", (candidate,)
            ).fetchone()
            if (row and row['expires_at'] and row['expires_at'] <= now
                    and (row['match_scope'] == 'subdomains' or candidate == domain)):
                return True
        return False


def decide(domain: str, action: str, new_domain: str = "",
           *, scope: str = "subdomains", expires_minutes: int = 0):
    """Only the dashboard can write explicit decisions and their limits.

    0 minutes means permanent. Allowed finite durations are deliberately
    bounded to avoid arbitrary, malformed or never-ending timestamps.
    """
    domain = normalize_domain(domain)
    if action not in ('allow','deny','delete','rename'):
        raise ValueError('Unrecognized domain action')
    if action == 'rename':
        new_domain = normalize_domain(new_domain)
    if scope not in ('exact', 'subdomains'):
        raise ValueError('Invalid rule scope')
    if type(expires_minutes) is not int or expires_minutes not in (0, 60, 1440, 10080):
        raise ValueError('Invalid permission duration')
    now = utcnow()
    expires_at = ((datetime.fromisoformat(now.replace('Z', '+00:00')) +
                   timedelta(minutes=expires_minutes)).isoformat(
                       timespec='seconds').replace('+00:00', 'Z')
                  if expires_minutes else '')
    with _db() as conn:
        _seed(conn)
        conn.execute('BEGIN IMMEDIATE')
        try:
            if action in ('allow','deny'):
                status = 'allowed' if action == 'allow' else 'denied'
                conn.execute("""INSERT INTO domains
                    (domain,status,created_at,updated_at,explicit_rule,match_scope,expires_at)
                    VALUES(?,?,?,?,1,?,?) ON CONFLICT(domain) DO UPDATE
                    SET status=excluded.status,updated_at=excluded.updated_at,
                        explicit_rule=1,match_scope=excluded.match_scope,
                        expires_at=excluded.expires_at""",
                    (domain,status,now,now,scope,expires_at))
                # Remove only obsolete auto-queued descendants covered by the
                # new rule. Exact-host rules must leave children in the queue.
                if scope == 'subdomains':
                    conn.execute("""DELETE FROM domains WHERE explicit_rule=0
                        AND domain != ? AND domain LIKE '%.' || ?""", (domain,domain))
            elif action == 'delete':
                conn.execute('DELETE FROM domains WHERE domain=?',(domain,))
            else:
                if not conn.execute('SELECT 1 FROM domains WHERE domain=?',(domain,)).fetchone():
                    raise ValueError('Domain no longer exists')
                if domain != new_domain:
                    conn.execute('UPDATE domains SET domain=?,updated_at=? WHERE domain=?',
                                 (new_domain,now,domain))
            conn.execute('INSERT INTO decisions(at,domain,action) VALUES(?,?,?)',(now,domain,action))
            # As before, invalidate before the write is visible to readers.
            invalidate_fetch()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def clear_pending_requests() -> int:
    """Delete pending queue entries only, preserving explicit decisions.

    This is a local dashboard operation, never an MCP tool. Record deletions
    in the decision audit; admission quotas intentionally remain unchanged.
    """
    now = utcnow()
    with _db() as conn:
        _seed(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""INSERT INTO decisions(at,domain,action)
                SELECT ?,domain,'delete' FROM domains
                WHERE status='pending' AND explicit_rule=0""", (now,))
            result = conn.execute("""DELETE FROM domains
                WHERE status='pending' AND explicit_rule=0""")
            conn.commit()
            return result.rowcount
        except BaseException:
            conn.rollback()
            raise


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
    if status == 'queue_full':
        return {'success':False,'status':'approval_queue_full','url':shown,'domain':host,'reason':'Domain was not queued: automatic approval requests are capacity/rate limited. Ask the local user to approve the hostname manually in the dashboard.','approval_dashboard':UI_URL}
    if status == 'denied':
        return {'success':False,'status':'denied','url':shown,'domain':host,'rule':matched,'reason':'Domain denied in local dashboard. Only the local user can change this decision.','approval_dashboard':UI_URL}
    try:
        addresses=socket.getaddrinfo(host, port or (443 if parsed.scheme=='https' else 80), type=socket.SOCK_STREAM)
    except socket.gaierror:
        return {'success': False, 'status': 'blocked', 'url': shown, 'domain':host, 'reason': 'DNS lookup failed'}
    if not addresses or not all(ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        return {'success': False, 'status': 'blocked', 'url': shown, 'domain':host, 'reason': 'Non-public resolved address blocked'}
    return None
