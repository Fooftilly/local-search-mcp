"""Versioned, redacted and transactional domain-rule transfer.

The only portable records are explicit allow/deny rules and their exact scope
and absolute UTC expiry. Never export pending requests, audit trails, tokens,
one-use proofs, local paths or credentials. No MCP tool calls this module.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import re
import secrets
import time

import domain_policy as policy

SCHEMA = "local-search-mcp/domain-rules/v1"
MAX_RULES = 250
MAX_JSON_BYTES = 48_000
PREVIEW_TTL_SECONDS = 600
_PREVIEW_SECRET = secrets.token_bytes(32)  # Process-local, never persisted.
REQUIRED = frozenset(("domain", "status", "scope", "expires_at"))
UTC_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _now_dt() -> datetime:
    return datetime.fromisoformat(policy.utcnow().replace("Z", "+00:00"))


def _expiry(value: str, now: datetime) -> str:
    if not isinstance(value, str):
        raise ValueError("Expiration must be a UTC timestamp or empty string")
    if not value:
        return ""
    if not UTC_SECONDS.fullmatch(value):
        raise ValueError("Expiration must use YYYY-MM-DDTHH:MM:SSZ")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid UTC expiration") from exc
    if dt <= now:
        raise ValueError("Import contains an expired rule")
    return value


def _rules_from_text(payload: str) -> list[dict]:
    if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("Rule document exceeds 48 KB")
    try:
        doc = json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid rule JSON") from exc
    if not isinstance(doc, dict) or set(doc) != {"schema", "rules"} or doc["schema"] != SCHEMA:
        raise ValueError("Unsupported version or unexpected top-level fields")
    rules = doc["rules"]
    if not isinstance(rules, list) or not 1 <= len(rules) <= MAX_RULES:
        raise ValueError("Import must contain 1–250 explicit rules")
    now = _now_dt()
    seen: set[str] = set()
    validated = []
    for item in rules:
        if not isinstance(item, dict) or set(item) != REQUIRED:
            raise ValueError("Rule fields must be domain, status, scope and expires_at")
        domain_raw = item["domain"]
        if not isinstance(domain_raw, str):
            raise ValueError("Rule domain must be text")
        domain = policy.normalize_domain(domain_raw)
        if domain != domain_raw:
            raise ValueError("Rule domains must be canonical lowercase ASCII hostnames")
        if domain in seen:
            raise ValueError("Duplicate domain in imported rules")
        seen.add(domain)
        if item["status"] not in ("allowed", "denied") or type(item["status"]) is not str:
            raise ValueError("Only explicit allowed/denied rules can be imported")
        if item["scope"] not in ("exact", "subdomains") or type(item["scope"]) is not str:
            raise ValueError("Invalid rule scope")
        validated.append({
            "domain": domain, "status": item["status"],
            "scope": item["scope"], "expires_at": _expiry(item["expires_at"], now),
        })
    return sorted(validated, key=lambda x: x["domain"])


def _snapshot(conn) -> str:
    """Fingerprint all policy records, including pending rows and request IDs.

    A preview is invalid if another process or browser tab changes state.
    Apply checks the fingerprint *inside* a write-locked SQLite transaction.
    """
    rows = [tuple(row) for row in conn.execute(
        """SELECT domain,status,explicit_rule,match_scope,expires_at,request_id
           FROM domains ORDER BY domain""").fetchall()]
    return sha256(json.dumps(rows, ensure_ascii=True,
                             separators=(",", ":")).encode("ascii")).hexdigest()


def _plan(conn, rules: list[dict]) -> dict:
    added, unchanged, conflicts = [], [], []
    for item in rules:
        existing = conn.execute(
            "SELECT status,explicit_rule,match_scope,expires_at FROM domains WHERE domain=?",
            (item["domain"],)).fetchone()
        if existing and existing["explicit_rule"]:
            if (existing["status"] == item["status"]
                    and existing["match_scope"] == item["scope"]
                    and existing["expires_at"] == item["expires_at"]):
                unchanged.append(item["domain"])
            else:
                conflicts.append(item["domain"])
        elif (item["status"] == "allowed"
              and (inherited := policy._match(conn, item["domain"], policy.utcnow()))
              and inherited["status"] == "denied"):
            # An explicit child allow would punch a hole through an existing
            # local parent denial. Require the operator to adjust the denial
            # manually before importing; never broaden silently.
            conflicts.append(item["domain"])
        else:
            added.append(item["domain"])
    return {"add": added, "unchanged": unchanged, "conflicts": conflicts}


def export_rules() -> dict:
    """Export active explicit decisions only; no private metadata or proofs."""
    with policy._db() as conn:
        policy._seed(conn)
        now = policy.utcnow()
        rows = conn.execute(
            """SELECT domain,status,match_scope,expires_at FROM domains
               WHERE explicit_rule=1 AND status IN ('allowed','denied')
                 AND (expires_at='' OR expires_at>?)
               ORDER BY domain LIMIT ?""", (now, MAX_RULES + 1)).fetchall()
        if len(rows) > MAX_RULES:
            raise ValueError("Too many rules to export in one file")
        return {"schema": SCHEMA, "rules": [
            {"domain": r["domain"], "status": r["status"],
             "scope": r["match_scope"], "expires_at": r["expires_at"]}
            for r in rows]}


def _canonical(rules: list[dict]) -> str:
    return json.dumps({"schema": SCHEMA, "rules": rules},
                      separators=(",", ":"), ensure_ascii=True)


def _ticket(canonical: str, state: str, issued: int) -> str:
    message = (state + "\n" + str(issued) + "\n" + canonical).encode("ascii")
    signature = hmac.new(_PREVIEW_SECRET, message, "sha256").hexdigest()
    return str(issued) + "." + signature


def preview_import(payload: str) -> dict:
    """Validated, read-only import plan. Conflicting decisions block apply."""
    rules = _rules_from_text(payload)
    with policy._db() as conn:
        policy._seed(conn)
        # Both reads share one consistent SQLite snapshot. Without BEGIN,
        # a policy mutation between classification and fingerprinting could
        # make a stale preview appear current at apply time.
        conn.execute("BEGIN")
        try:
            plan = _plan(conn, rules)
            state = _snapshot(conn)
        finally:
            conn.rollback()
    # The canonical serialization is safe to round-trip into escaped HTML.
    canonical = _canonical(rules)
    ticket = _ticket(canonical, state, int(time.monotonic()))
    return {"state": state, "ticket": ticket, "canonical": canonical,
            "rules": rules, **plan}


def apply_import(payload: str, expected_state: str,
                 ticket: str) -> int:
    """Apply exactly the approved preview or nothing, under BEGIN IMMEDIATE."""
    if not isinstance(expected_state, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_state):
        raise ValueError("Missing or invalid import preview")
    rules = _rules_from_text(payload)
    if not isinstance(ticket, str) or not re.fullmatch(r"[0-9]{1,15}\.[a-f0-9]{64}", ticket):
        raise ValueError("Missing or invalid import confirmation")
    issued = int(ticket.split(".", 1)[0])
    age = int(time.monotonic()) - issued
    if age < 0 or age > PREVIEW_TTL_SECONDS:
        raise ValueError("Import preview expired; preview again")
    expected_ticket = _ticket(_canonical(rules), expected_state, issued)
    if not hmac.compare_digest(expected_ticket, ticket):
        raise ValueError("Confirmation does not match the previewed rules")
    with policy._db() as conn:
        policy._seed(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _snapshot(conn) != expected_state:
                raise ValueError("Domain rules changed since preview; preview again")
            plan = _plan(conn, rules)
            if plan["conflicts"]:
                raise ValueError("Import conflicts with existing explicit rules or inherited denials")
            # Validate AFTER BEGIN IMMEDIATE, not only before opening SQLite:
            # the database write lock may be held until an imported TTL expires.
            locked_now = _now_dt()
            for item in rules:
                _expiry(item["expires_at"], locked_now)
            now = policy.utcnow()
            imported_domains = {item["domain"] for item in rules}
            for item in rules:
                if item["domain"] not in plan["add"]:
                    continue
                domain = item["domain"]
                existing = conn.execute(
                    "SELECT * FROM domains WHERE domain=?", (domain,)).fetchone()
                request_id = (existing["request_id"] if existing and existing["request_id"]
                              else secrets.token_hex(12))
                conn.execute(
                    """INSERT INTO domains (domain,status,created_at,updated_at,
                        explicit_rule,match_scope,expires_at,request_id)
                       VALUES (?,?,?,?,1,?,?,?)
                       ON CONFLICT(domain) DO UPDATE SET status=excluded.status,
                           updated_at=excluded.updated_at,explicit_rule=1,
                           match_scope=excluded.match_scope,
                           expires_at=excluded.expires_at,
                           request_id=CASE WHEN domains.request_id=''
                               THEN excluded.request_id ELSE domains.request_id END""",
                    (domain, item["status"], now, now, item["scope"],
                     item["expires_at"], request_id))
                # Preserve audit evidence and stable IDs when parent rules
                # supersede automatically queued child requests.
                if item["scope"] == "subdomains":
                    children = [
                        child for child in conn.execute(
                            """SELECT * FROM domains WHERE explicit_rule=0
                               AND domain!=? AND domain LIKE '%.' || ?""",
                            (domain, domain)).fetchall()
                        if child["domain"] not in imported_domains
                    ]
                    for child in children:
                        conn.execute(
                            """INSERT INTO decisions
                               (at,domain,action,request_id,reason,requested_url,
                                first_requested_at,last_requested_at,request_count)
                               VALUES (?,?,'delete',?,?,?,?,?,?)""",
                            (now, child["domain"], child["request_id"],
                             "Superseded by imported parent rule",
                             policy.safe_request_origin(child["last_requested_url"])
                             if child["last_requested_url"] else "",
                             child["first_requested_at"], child["last_requested_at"],
                             child["request_count"]))
                    conn.executemany(
                        "DELETE FROM domains WHERE domain=? AND explicit_rule=0",
                        [(child["domain"],) for child in children])
                # An imported approval of an existing pending request must
                # retain its original sanitized origin, timestamps and count
                # in the decision audit, just like a local dashboard decision.
                conn.execute(
                    """INSERT INTO decisions
                       (at,domain,action,request_id,reason,requested_url,
                        first_requested_at,last_requested_at,request_count)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (now, domain,
                     "allow" if item["status"] == "allowed" else "deny",
                     request_id, "Imported from validated portable rule file",
                     policy.safe_request_origin(existing["last_requested_url"])
                     if existing and existing["last_requested_url"] else "",
                     existing["first_requested_at"] if existing else "",
                     existing["last_requested_at"] if existing else "",
                     existing["request_count"] if existing else 0))
            if plan["add"]:
                policy.invalidate_fetch()
            conn.commit()
            return len(plan["add"])
        except BaseException:
            conn.rollback()
            raise
