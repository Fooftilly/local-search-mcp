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
import secrets
import socket
import sqlite3
from urllib.parse import urlsplit, urlunsplit
from public_suffix import is_public_suffix, registrable_domain
from request_cache import invalidate_fetch

UI_URL = "http://127.0.0.1:8791/"
SCHEMA_VERSION = 6
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
                first_requested_at TEXT NOT NULL DEFAULT '',
                last_requested_url TEXT NOT NULL DEFAULT '',
                explicit_rule INTEGER NOT NULL DEFAULT 0 CHECK(explicit_rule IN (0,1)),
                match_scope TEXT NOT NULL DEFAULT 'subdomains' CHECK(match_scope IN ('exact','subdomains')),
                expires_at TEXT NOT NULL DEFAULT '',
                request_id TEXT NOT NULL DEFAULT ''
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
            if "request_id" not in columns:
                conn.execute("ALTER TABLE domains ADD COLUMN request_id TEXT NOT NULL DEFAULT ''")
            if "first_requested_at" not in columns:
                conn.execute("ALTER TABLE domains ADD COLUMN first_requested_at TEXT NOT NULL DEFAULT ''")
                # Exactly one historical request has a known first timestamp.
                # For 2+ older requests the original first time is unknown.
                conn.execute("""UPDATE domains
                    SET first_requested_at=last_requested_at
                    WHERE request_count=1 AND last_requested_at!=''""")
            # Preserve the same opaque ID on existing requests across restart.
            for row in conn.execute("SELECT domain FROM domains WHERE request_id=''").fetchall():
                conn.execute("UPDATE domains SET request_id=? WHERE domain=?",
                             (secrets.token_hex(12), row["domain"]))
            conn.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("""CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL, domain TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('allow', 'deny', 'delete', 'rename')),
                request_id TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                requested_url TEXT NOT NULL DEFAULT '',
                first_requested_at TEXT NOT NULL DEFAULT '',
                last_requested_at TEXT NOT NULL DEFAULT '',
                request_count INTEGER NOT NULL DEFAULT 0
            )""")
            decision_columns = {col["name"] for col in conn.execute(
                "PRAGMA table_info(decisions)")}
            if "request_id" not in decision_columns:
                conn.execute("ALTER TABLE decisions ADD COLUMN request_id TEXT NOT NULL DEFAULT ''")
            if "reason" not in decision_columns:
                conn.execute("ALTER TABLE decisions ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
            for name, sql_type in (
                ("requested_url", "TEXT NOT NULL DEFAULT ''"),
                ("first_requested_at", "TEXT NOT NULL DEFAULT ''"),
                ("last_requested_at", "TEXT NOT NULL DEFAULT ''"),
                ("request_count", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in decision_columns:
                    conn.execute(f"ALTER TABLE decisions ADD COLUMN {name} {sql_type}")
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
            # One-use capabilities live apart from effective domain rules.
            # Staging one must NEVER implicitly make request() return allowed.
            conn.execute("""CREATE TABLE IF NOT EXISTS once_grants (
                domain TEXT PRIMARY KEY,
                grant_id TEXT NOT NULL UNIQUE,
                match_scope TEXT NOT NULL CHECK(match_scope IN ('exact','subdomains')),
                expires_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                consumed_at TEXT NOT NULL DEFAULT ''
            )""")
            # Separate append-only audit because old decisions table has a
            # CHECK constraint limiting its action names. No bearer IDs here.
            conn.execute("""CREATE TABLE IF NOT EXISTS once_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL, domain TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('stage','consume','revoke')),
                scope TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL DEFAULT '',
                request_id TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                requested_url TEXT NOT NULL DEFAULT '',
                first_requested_at TEXT NOT NULL DEFAULT '',
                last_requested_at TEXT NOT NULL DEFAULT '',
                request_count INTEGER NOT NULL DEFAULT 0
            )""")
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
    # Browser UTS #46 non-transitional handling diverges from Python's
    # IDNA2003 codec on deviation characters. Reject those hostnames instead
    # of letting a grant to fass.de authorize navigation to faß.de.
    # This check has no third-party dependencies: bootstrap validates domain
    # configuration before the virtualenv is available.
    if any(char in domain for char in ("ß", "ς", "\u200c", "\u200d")):
        raise ValueError("Ambiguous IDN domain: browser and policy hostnames differ")
    try:
        domain = domain.encode("idna").decode("ascii")
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
                conn.execute("INSERT OR IGNORE INTO domains(domain,status,created_at,updated_at,request_id) VALUES(?,?,?,?,?)",
                             (domain,'allowed',now,now,secrets.token_hex(12)))
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
    safe_url = safe_display_url(safe_url)
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
                             last_requested_at,first_requested_at,
                             last_requested_url,request_id)
                            VALUES (?,'pending',?,?,1,?,?,?,?)""",
                            (domain, now, now, now, now, safe_url[:512],
                             secrets.token_hex(12)))
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
                    first_requested_at=CASE
                        WHEN request_count=0 AND first_requested_at='' THEN ?
                        ELSE first_requested_at END,
                    last_requested_at=?,last_requested_url=? WHERE domain=?""",
                    (now, now, safe_url[:512], target))
            conn.commit()
            return status, matched
        except BaseException:
            conn.rollback()
            raise





def _validate_once_reason(reason: str) -> None:
    if (not isinstance(reason, str) or len(reason) > 160
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in reason)):
        raise ValueError("Reason must be at most 160 characters without control characters")


def _record_once_event(conn, domain: str, action: str, now: str, *,
                       scope: str = "", expires_at: str = "",
                       reason: str = "") -> None:
    """Append a sanitized operator/consumption event inside the same DB txn.

    Private grant IDs, URL paths, queries and page content are never stored.
    """
    row = conn.execute("SELECT * FROM domains WHERE domain=?", (domain,)).fetchone()
    conn.execute("""INSERT INTO once_events
        (at,domain,action,scope,expires_at,request_id,reason,requested_url,
         first_requested_at,last_requested_at,request_count)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (now, domain, action, scope, expires_at,
         row["request_id"] if row else "", reason,
         safe_request_origin(row["last_requested_url"])
         if row and row["last_requested_url"] else "",
         row["first_requested_at"] if row else "",
         row["last_requested_at"] if row else "",
         row["request_count"] if row else 0))


def once_grant_entries(search: str = "", limit: int = 200) -> list[dict]:
    """Local dashboard status; never reveal private one-use grant IDs."""
    if not isinstance(search, str) or len(search) > 100:
        raise ValueError("Invalid one-use filter")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("Invalid one-use list limit")
    with _db() as conn:
        _seed(conn)
        now = utcnow()
        rows = conn.execute("""SELECT domain,match_scope,expires_at,created_at,
                                      consumed_at FROM once_grants
                               WHERE domain LIKE ? ESCAPE '\\'
                               ORDER BY created_at DESC,domain LIMIT ?""",
                            ("%" + search.lower().replace("%", "\\%")
                             .replace("_", "\\_") + "%", limit)).fetchall()
        return [{**dict(row), "state": (
            "consumed" if row["consumed_at"] else
            "expired" if row["expires_at"] and row["expires_at"] <= now
            else "available")} for row in rows]


def recent_once_events(limit: int = 25, search: str = "") -> list[dict]:
    """Bounded local audit view; excludes bearer grant identifiers."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid one-use audit limit")
    if not isinstance(search, str) or len(search) > 100:
        raise ValueError("Invalid one-use audit filter")
    with _db() as conn:
        _seed(conn)
        return [dict(r) for r in conn.execute(
            """SELECT at,domain,action,scope,expires_at,reason,request_id,
                      requested_url,first_requested_at,last_requested_at,
                      request_count FROM once_events
               WHERE domain LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?""",
            ("%" + search.lower().replace("%", "\\%")
             .replace("_", "\\_") + "%", limit)).fetchall()]


def _stage_once_grant(domain: str, *, scope: str = "exact",
                      expires_minutes: int = 60,
                      reason: str = "",
                      expected_request_id: str | None = None) -> str:
    """Stage a local-only one-use grant; never make a reusable allow rule.

    Only the loopback dashboard exposes this mutation. A stale pending-row
    request ID aborts rather than granting from an obsolete browser tab.
    """
    domain = normalize_domain(domain)
    if scope not in ("exact", "subdomains"):
        raise ValueError("Invalid one-use rule scope")
    if type(expires_minutes) is not int or expires_minutes not in (0, 60, 1440, 10080):
        raise ValueError("Invalid one-use grant duration")
    _validate_once_reason(reason)
    if (expected_request_id is not None and
            (not isinstance(expected_request_id, str)
             or not re.fullmatch(r"[0-9a-f]{24}", expected_request_id))):
        raise ValueError("Invalid pending request ID")
    with _db() as conn:
        _seed(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = utcnow()
            active = _match(conn, domain, now)
            if active and active["status"] == "denied":
                raise ValueError("An active explicit denial must be changed first")
            if active and active["status"] == "allowed":
                raise ValueError("Domain already has reusable approval")
            if expected_request_id is not None:
                pending = conn.execute(
                    "SELECT status,explicit_rule,request_id FROM domains WHERE domain=?",
                    (domain,)).fetchone()
                if (not pending or pending["status"] != "pending"
                        or pending["explicit_rule"]
                        or pending["request_id"] != expected_request_id):
                    raise ValueError("Pending request changed; refresh before approving")
            expires = ((datetime.fromisoformat(now.replace("Z", "+00:00"))
                        + timedelta(minutes=expires_minutes)).isoformat(
                            timespec="seconds").replace("+00:00", "Z")
                       if expires_minutes else "")
            grant_id = secrets.token_hex(16)
            conn.execute("""INSERT INTO once_grants
                (domain,grant_id,match_scope,expires_at,created_at,consumed_at)
                VALUES(?,?,?,?,?,'') ON CONFLICT(domain) DO UPDATE SET
                grant_id=excluded.grant_id,
                match_scope=excluded.match_scope,
                expires_at=excluded.expires_at,
                created_at=excluded.created_at,consumed_at=''""",
                (domain, grant_id, scope, expires, now))
            _record_once_event(conn, domain, "stage", now, scope=scope,
                               expires_at=expires, reason=reason)
            invalidate_fetch()
            conn.commit()
            return grant_id
        except BaseException:
            conn.rollback()
            raise


def _matching_once_grant(conn, domain: str, now: str):
    """Return most-specific unspent, unexpired grant (read under write lock)."""
    labels = domain.split(".")
    for i in range(len(labels) - 1):
        candidate = ".".join(labels[i:])
        grant = conn.execute("SELECT * FROM once_grants WHERE domain=?",
                             (candidate,)).fetchone()
        if (grant is None or grant["consumed_at"] or
                (grant["expires_at"] and grant["expires_at"] <= now) or
                (grant["match_scope"] == "exact" and candidate != domain)):
            continue
        return grant
    return None


def _consume_once_grant(domain: str) -> str | None:
    """One atomic SQLite claim across concurrent connections and processes.

    Return a *private* opaque grant ID, never a tool-visible permission. The
    caller is responsible for DNS validation before claim, for ensuring a
    restricted operation context, and for checking the proof at content release.
    """
    domain = normalize_domain(domain)
    with _db() as conn:
        _seed(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = utcnow()  # After lock acquisition, not before.
            active = _match(conn, domain, now)
            # Recheck *both* decisions after BEGIN IMMEDIATE. An operator
            # may have approved the host after the caller's read-only hint;
            # never consume its one-use fallback when reusable permission
            # already exists (or an explicit deny has taken precedence).
            if active and active["status"] in ("allowed", "denied"):
                conn.commit()
                return None
            grant = _matching_once_grant(conn, domain, now)
            if not grant:
                conn.commit()
                return None
            updated = conn.execute("""UPDATE once_grants SET consumed_at=?
                WHERE domain=? AND grant_id=? AND consumed_at=''
                  AND (expires_at='' OR expires_at>?)""",
                (now, grant["domain"], grant["grant_id"], now)).rowcount
            if updated == 1:
                _record_once_event(conn, domain, "consume", now,
                                   scope=grant["match_scope"],
                                   expires_at=grant["expires_at"])
            conn.commit()
            return grant["grant_id"] if updated == 1 else None
        except BaseException:
            conn.rollback()
            raise



def _once_grant_available(domain: str) -> bool:
    """Read-only hint: avoid DNS preflights for hosts without one-use grants.

    This is not an authorization decision. The atomic claim still rechecks
    active explicit denials, validity, scope and expiration under a write lock.
    """
    domain = normalize_domain(domain)
    with _db() as conn:
        _seed(conn)
        now = utcnow()
        active = _match(conn, domain, now)
        if active and active["status"] in ("allowed", "denied"):
            return False
        return _matching_once_grant(conn, domain, now) is not None


def _public_url_host(url: str) -> str | None:
    """Validate a public unicast HTTP(S) target without changing permissions.

    Internal-only: use for redirect safety; it is not a reusable approval.
    """
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        host = normalize_domain(parsed.hostname or "")
        if (parsed.scheme not in ("http", "https") or
                parsed.username is not None or parsed.password is not None or
                parsed.port not in (None, 80, 443)):
            return None
    except (ValueError, TypeError):
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not records:
            return None
        # is_global alone includes multicast in Python's ipaddress library.
        addresses = [ipaddress.ip_address(rec[4][0]) for rec in records]
        if not all(addr.is_global and not addr.is_multicast
                   for addr in addresses):
            return None
    except (socket.gaierror, ValueError, IndexError, TypeError):
        return None
    return host


def _claim_once_for_public_url(url: str) -> tuple[str, str] | None:
    """Return a private atomic once-claim only after public-target validation.

    This internal primitive never returns a tool-visible policy decision.
    The caller must revalidate the proof and final redirect at content release.
    """
    host = _public_url_host(url)
    if host is None:
        return None
    # _consume_once_grant takes BEGIN IMMEDIATE, rechecks live explicit denials
    # and expiration AFTER locking, and marks one winner atomically.
    grant_id = _consume_once_grant(host)
    return (host, grant_id) if grant_id else None


def _once_proof_current(domain: str, grant_id: str) -> bool:
    """Fail-closed post-fetch check: revoke, replacement, expiry, or deny."""
    if not isinstance(grant_id, str) or not re.fullmatch(r"[a-f0-9]{32}", grant_id):
        return False
    try:
        domain = normalize_domain(domain)
    except ValueError:
        return False
    with _db() as conn:
        _seed(conn)
        now = utcnow()
        active = _match(conn, domain, now)
        if active and active["status"] == "denied":
            return False
        grant = conn.execute(
            "SELECT * FROM once_grants WHERE grant_id=?", (grant_id,)
        ).fetchone()
        return bool(grant and grant["consumed_at"] and
                    (not grant["expires_at"] or grant["expires_at"] > now) and
                    (domain == grant["domain"] or (
                        grant["match_scope"] == "subdomains" and
                        domain.endswith("." + grant["domain"]))))


def _revoke_once_grant(domain: str, *, reason: str = "") -> None:
    """Explicit operator primitive. In-flight proof becomes invalid."""
    domain = normalize_domain(domain)
    _validate_once_reason(reason)
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            previous = conn.execute(
                "SELECT match_scope,expires_at FROM once_grants WHERE domain=?",
                (domain,)).fetchone()
            removed = conn.execute("DELETE FROM once_grants WHERE domain=?",
                                   (domain,)).rowcount
            if removed:
                _record_once_event(conn, domain, "revoke", utcnow(),
                                   scope=previous["match_scope"],
                                   expires_at=previous["expires_at"],
                                   reason=reason)
                invalidate_fetch()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def is_domain_currently_allowed(domain: str) -> bool:
    """Read-only effective permission for already-fetched document content.

    Unlike request(), this never admits new pending entries, increments
    request counts, or repeats DNS resolution. The caller separately checks
    the policy epoch for concurrent dashboard mutations.
    """
    domain = normalize_domain(domain)
    with _db() as conn:
        _seed(conn)
        rule = _match(conn, domain, utcnow())
        return rule is not None and rule["status"] == "allowed"


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
        active_rule = _match(conn, domain, now)
        if active_rule is not None:
            # A still-valid deny fallback must never release content that
            # started under a now-expired, more-specific allow rule.
            return active_rule['status'] != 'allowed'
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
           *, scope: str = "subdomains", expires_minutes: int = 0,
           reason: str = ""):
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
    if (not isinstance(reason, str) or len(reason) > 160
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in reason)):
        raise ValueError("Reason must be at most 160 characters without control characters")
    now = utcnow()
    expires_at = ((datetime.fromisoformat(now.replace('Z', '+00:00')) +
                   timedelta(minutes=expires_minutes)).isoformat(
                       timespec='seconds').replace('+00:00', 'Z')
                  if expires_minutes else '')
    with _db() as conn:
        _seed(conn)
        conn.execute('BEGIN IMMEDIATE')
        try:
            existing = conn.execute("SELECT * FROM domains WHERE domain=?",
                                    (domain,)).fetchone()
            request_id = (existing["request_id"] if existing and existing["request_id"]
                          else secrets.token_hex(12))
            snapshot_url = (safe_request_origin(existing["last_requested_url"])
                            if existing and existing["last_requested_url"] else "")
            first_requested_at = existing["first_requested_at"] if existing else ""
            last_requested_at = existing["last_requested_at"] if existing else ""
            request_count = existing["request_count"] if existing else 0
            if action in ('allow','deny'):
                status = 'allowed' if action == 'allow' else 'denied'
                conn.execute("""INSERT INTO domains
                    (domain,status,created_at,updated_at,explicit_rule,match_scope,expires_at,request_id)
                    VALUES(?,?,?,?,1,?,?,?) ON CONFLICT(domain) DO UPDATE
                    SET status=excluded.status,updated_at=excluded.updated_at,
                        explicit_rule=1,match_scope=excluded.match_scope,
                        expires_at=excluded.expires_at,
                        request_id=CASE WHEN domains.request_id=''
                                        THEN excluded.request_id ELSE domains.request_id END""",
                    (domain,status,now,now,scope,expires_at,request_id))
                # Remove only obsolete auto-queued descendants covered by the
                # new rule. Exact-host rules must leave children in the queue.
                if scope == 'subdomains':
                    # Reconcile covered pending children without erasing their
                    # stable request IDs or sanitized decision evidence.
                    children = conn.execute("""SELECT * FROM domains
                        WHERE explicit_rule=0 AND domain != ?
                          AND domain LIKE '%.' || ?""",
                        (domain, domain)).fetchall()
                    conn.executemany("""INSERT INTO decisions
                        (at,domain,action,request_id,reason,requested_url,
                         first_requested_at,last_requested_at,request_count)
                        VALUES(?,?,'delete',?,?,?,?,?,?)""",
                        [(now, row["domain"], row["request_id"], reason,
                          safe_request_origin(row["last_requested_url"])
                          if row["last_requested_url"] else "",
                          row["first_requested_at"], row["last_requested_at"],
                          row["request_count"]) for row in children])
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
            conn.execute(
                """INSERT INTO decisions
                   (at,domain,action,request_id,reason,requested_url,
                    first_requested_at,last_requested_at,request_count)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (now,domain,action,request_id,reason,snapshot_url,
                 first_requested_at,last_requested_at,request_count))
            # As before, invalidate before the write is visible to readers.
            invalidate_fetch()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise



MAX_BULK_DECISIONS = 25


def bulk_decide(selected: list[str], action: str, *,
                scope: str = "subdomains", expires_minutes: int = 0,
                reason: str = "") -> int:
    """Atomically decide on 1-25 selected *pending* requests from the local UI.

    Each selection carries the opaque request ID displayed with its domain.
    A stale/recreated request, missing row or changed decision aborts the whole
    transaction instead of granting access based on an obsolete browser tab.
    """
    if not isinstance(selected, list) or not 1 <= len(selected) <= MAX_BULK_DECISIONS:
        raise ValueError("Select between 1 and 25 pending requests")
    if action not in ("allow", "deny"):
        raise ValueError("Bulk action must be Allow or Deny")
    if scope not in ("exact", "subdomains"):
        raise ValueError("Invalid rule scope")
    if type(expires_minutes) is not int or expires_minutes not in (0, 60, 1440, 10080):
        raise ValueError("Invalid permission duration")
    if (not isinstance(reason, str) or len(reason) > 160
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in reason)):
        raise ValueError("Reason must be at most 160 characters without control characters")

    selections: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in selected:
        if not isinstance(item, str) or item.count("|") != 1:
            raise ValueError("Invalid pending request selection")
        raw_domain, request_id = item.split("|", 1)
        domain = normalize_domain(raw_domain)
        if (domain != raw_domain or not re.fullmatch(r"[a-f0-9]{24}", request_id)
                or domain in seen):
            raise ValueError("Invalid or duplicate pending request selection")
        selections.append((domain, request_id))
        seen.add(domain)

    with _db() as conn:
        _seed(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Check the complete selection before applying ANY permission.
            rows = {}
            for domain, request_id in selections:
                row = conn.execute(
                    "SELECT * FROM domains WHERE domain=?", (domain,)
                ).fetchone()
                if (row is None or row["status"] != "pending"
                        or row["explicit_rule"] != 0
                        or row["request_id"] != request_id):
                    raise ValueError("Selection is stale; refresh the dashboard")
                rows[domain] = row

            now = utcnow()
            expires_at = ((datetime.fromisoformat(now.replace("Z", "+00:00")) +
                           timedelta(minutes=expires_minutes)).isoformat(
                               timespec="seconds").replace("+00:00", "Z")
                          if expires_minutes else "")
            status = "allowed" if action == "allow" else "denied"
            # Process children before ancestors. An explicitly selected child
            # remains its own rule when a selected parent covers subdomains.
            for domain, request_id in sorted(
                    selections, key=lambda pair: (-pair[0].count("."), pair[0])):
                row = rows[domain]
                conn.execute(
                    """UPDATE domains SET status=?,explicit_rule=1,
                       match_scope=?,expires_at=?,updated_at=?
                       WHERE domain=? AND request_id=?""",
                    (status, scope, expires_at, now, domain, request_id))
                if scope == "subdomains":
                    children = conn.execute(
                        """SELECT * FROM domains WHERE explicit_rule=0
                           AND domain != ? AND domain LIKE '%.' || ?""",
                        (domain, domain)).fetchall()
                    conn.executemany(
                        """INSERT INTO decisions
                           (at,domain,action,request_id,reason,requested_url,
                            first_requested_at,last_requested_at,request_count)
                           VALUES (?,?,'delete',?,?,?,?,?,?)""",
                        [(now, child["domain"], child["request_id"], reason,
                          safe_request_origin(child["last_requested_url"])
                          if child["last_requested_url"] else "",
                          child["first_requested_at"], child["last_requested_at"],
                          child["request_count"]) for child in children])
                    conn.execute(
                        """DELETE FROM domains WHERE explicit_rule=0
                           AND domain != ? AND domain LIKE '%.' || ?""",
                        (domain, domain))
                conn.execute(
                    """INSERT INTO decisions
                       (at,domain,action,request_id,reason,requested_url,
                        first_requested_at,last_requested_at,request_count)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (now, domain, action, request_id, reason,
                     safe_request_origin(row["last_requested_url"])
                     if row["last_requested_url"] else "",
                     row["first_requested_at"], row["last_requested_at"],
                     row["request_count"]))
            # One global invalidation before committing an atomic policy change.
            invalidate_fetch()
            conn.commit()
            return len(selections)
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
            # A timed grant can expire while an earlier crawl is still in
            # flight. Requeue preserves its expiry evidence on the pending row.
            # Clearing that row otherwise erases the only evidence that the
            # earlier crawl must not release its content after expiry.
            #
            # Unlike ordinary pending queue cleanup, dropping such evidence is
            # a security-relevant policy change. Invalidate before publishing
            # the deletion, so concurrent fetches see a changed epoch and fail
            # closed without exposing results admitted under the old grant.
            drops_expiry_evidence = conn.execute(
                """SELECT 1 FROM domains WHERE status='pending'
                   AND explicit_rule=0 AND expires_at != '' LIMIT 1"""
            ).fetchone() is not None
            pending = conn.execute("""SELECT * FROM domains
                WHERE status='pending' AND explicit_rule=0""").fetchall()
            conn.executemany("""INSERT INTO decisions
                (at,domain,action,request_id,requested_url,
                 first_requested_at,last_requested_at,request_count)
                VALUES (?,?,'delete',?,?,?,?,?)""",
                [(now, row["domain"], row["request_id"],
                  safe_request_origin(row["last_requested_url"])
                  if row["last_requested_url"] else "",
                  row["first_requested_at"], row["last_requested_at"],
                  row["request_count"]) for row in pending])
            result = conn.execute("""DELETE FROM domains
                WHERE status='pending' AND explicit_rule=0""")
            if drops_expiry_evidence:
                invalidate_fetch()
            conn.commit()
            return result.rowcount
        except BaseException:
            conn.rollback()
            raise


def recent_decisions(limit: int = 25, search: str = "") -> list[dict]:
    """Return a bounded operator-only decision audit, with no URL or tokens."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid decision audit limit")
    if not isinstance(search, str) or len(search) > 100:
        raise ValueError("Invalid decision audit filter")
    term = "%" + search.lower().replace("%", "\\%").replace("_", "\\_") + "%"
    with _db() as conn:
        return [dict(row) for row in conn.execute(
            """SELECT at,domain,action,request_id,reason,requested_url,
                      first_requested_at,last_requested_at,request_count FROM decisions
               WHERE domain LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?""",
            (term, limit)).fetchall()]


def safe_request_origin(url: str) -> str:
    """Render only scheme and hostname: both paths and queries may be secrets."""
    try:
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return "[invalid URL]"
        return urlunsplit((p.scheme, p.hostname, "", "", ""))
    except (ValueError, TypeError):
        return "[invalid URL]"


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
