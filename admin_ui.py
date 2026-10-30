"""Loopback-only management dashboard for local-search-mcp domain approvals."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import sqlite3
import threading
import time
import unicodedata
from urllib.parse import parse_qs, quote, urlsplit

import domain_policy as policy
import policy_transfer
import service_status
import safe_settings
import upstream_settings
import cache_settings
import portable_settings
import history_index
from web_assets import LIVE_SCRIPT, STYLESHEET
from web_templates import render_template
from runtime_ports import MCP_PORT

HOST='127.0.0.1'
PORT=int(os.environ.get('LOCAL_SEARCH_MCP_DASHBOARD_PORT','8791'))
CSRF=secrets.token_urlsafe(32)
PENDING_PAGE_SIZE = 200
MAX_PENDING_PAGE = 20
_LIVE_SNAPSHOT_LOCK = threading.Lock()
_LAST_LIVE_SNAPSHOT_US = 0
# Ephemeral ID distinguishes dashboard process restarts even if system time
# moves backward. The browser rejects delayed responses from retired runs.
_LIVE_SNAPSHOT_RUN = secrets.token_hex(16)

# Browser CSS is a fixed, same-origin asset under web/static.



def esc(value): return html.escape(str(value), quote=True)


def recent_history(n=25):
    try:
        from history_log import history_path
        path=history_path()
        if not path.is_file():return []
        latest=deque(maxlen=n)
        with path.open('r',encoding='utf-8') as f:
            for line in f:
                try: evt=json.loads(line)
                except (json.JSONDecodeError, ValueError):continue
                if isinstance(evt,dict):latest.append(evt)
        return list(reversed(latest))
    except (ImportError,OSError):return []


def _safe_query_display(query):
    """Make control/bidi characters inert while retaining full printable text."""
    if not isinstance(query, str):
        return "— (unavailable)"
    rendered = []
    for char in query:
        if unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp"):
            rendered.append(f"\\\\x{ord(char):02x}" if ord(char) <= 255
                            else f"\\\\u{ord(char):04x}")
        else:
            rendered.append(char)
    return "".join(rendered)

def history_items_html(kind="", status="", domain="", hours=0, show_queries=False):
    try:
        items = history_index.search_history(kind=kind, status=status,
                                              domain=domain, hours=hours,
                                              reveal_queries=show_queries)
    except (OSError, ValueError, sqlite3.Error):
        return '<tr><td colspan="5">Indexed history is unavailable; check local configuration.</td></tr>'
    out = []
    for event in items:
        stamp = datetime.fromtimestamp(
            event["at_ms"] / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
        elapsed = (str(event["duration_ms"]) if event["duration_ms"] is not None else "—")
        display = (_safe_query_display(event.get("query"))
                   if show_queries and event["kind"] == "search"
                   else _safe_query_display(event.get("url"))
                   if show_queries and event["kind"] == "fetch"
                   else (event["domain"] or "—") if show_queries
                   else event["domain"])
        css_class = 'domain history-query' if show_queries else 'domain'
        out.append(
            f'<tr><td class="small">{esc(stamp)}</td><td>{esc(event["kind"])}</td>'
            f'<td>{esc(event["status"])}</td>'
            f'<td class="{css_class}">{esc(display)}</td>'
            f'<td>{esc(elapsed)}</td></tr>')
    return ''.join(out) or '<tr><td colspan="5">No matching MCP history.</td></tr>'


def recent_rows(kind="", status="", domain="", hours=0, show_queries=False):
    column = "Search query / page URL / domain" if show_queries else "Domain"
    return ('<div class="scroll"><table><thead><tr><th>UTC time</th>'
            '<th>Operation</th><th>Status</th><th>' + column + '</th>'
            '<th>Duration (ms)</th></tr></thead>'
            '<tbody id="live-history">' +
            history_items_html(kind, status, domain, hours, show_queries) +
            '</tbody></table></div>')


def history_summary_html(metrics):
    """An accessible, CSP-compatible bar chart using native progress elements."""
    peak = max((int(x["count"]) for x in metrics), default=1)
    out = []
    for row in metrics:
        count = int(row["count"])
        kind = esc(row["kind"])
        status = esc(row["status"])
        duration = esc(row["avg_duration_ms"]
                       if row["avg_duration_ms"] is not None else "—")
        out.append(
            f'<tr><td>{kind}</td><td>{status}</td>'
            f'<td><progress aria-label="{kind} {status} count" '
            f'value="{count}" max="{peak}">{count}</progress></td>'
            f'<td>{count}</td><td>{duration}</td></tr>')
    return ''.join(out) or '<tr><td colspan="5">No matching events.</td></tr>'


def _history_filters(query, *, validate_text=True):
    """Parse bounded GET filters; preserve form input on text-validation errors."""
    values = {key: query.get(key, [''])[0][:253 if key == 'domain' else 48]
              for key in ('kind', 'status', 'domain')}
    raw = query.get('hours', ['0'])[0]
    if raw not in ('0', '1', '24', '168'):
        raise ValueError("Invalid history time window")
    values['hours'] = int(raw)
    visible = query.get('show_queries', ['0'])
    if len(visible) != 1 or visible[0] not in ('0', '1'):
        raise ValueError("Invalid history query visibility")
    values['show_queries'] = visible[0] == '1'
    # Invalid text filters may be displayed, but never passed to a query.
    if validate_text:
        history_index._history_filter(**{k: values[k] for k in
                                        ('kind', 'status', 'domain', 'hours')})
    return values


def _pending_page(value):
    if (not isinstance(value, str) or not value.isascii() or
            not value.isdecimal() or len(value) > 2):
        raise ValueError("Invalid pending review page")
    page = int(value)
    if not 1 <= page <= MAX_PENDING_PAGE:
        raise ValueError("Invalid pending review page")
    return page


def _pending_sort(value):
    if type(value) is not str or value not in policy.PENDING_SORT_MODES:
        raise ValueError("Invalid pending review sort")
    return value


def pending_sort_options(selected):
    labels = (("recent", "Newest activity"), ("oldest", "Oldest waiting"),
              ("frequent", "Most requests"))
    return ''.join('<option value="' + mode + '"' +
                   (' selected' if mode == selected else '') + '>' +
                   label + '</option>' for mode, label in labels)


def _last_pending_page(total):
    return min(MAX_PENDING_PAGE, max(
        1, (total + PENDING_PAGE_SIZE - 1) // PENDING_PAGE_SIZE))


def pending_pagination_html(query, page, total, pending_sort='recent'):
    """Accessible navigation over bounded pending rows; never alters rules."""
    pages = _last_pending_page(total)
    first = (page - 1) * PENDING_PAGE_SIZE + 1 if total and page <= pages else 0
    last = min(page * PENDING_PAGE_SIZE, total) if first else 0

    def href(number):
        return esc('/?q=' + quote(query, safe='') + '&page=' + str(number) +
                   ('' if pending_sort == 'recent' else
                    '&sort=' + pending_sort))

    links = []
    if page > 1:
        links.append('<a rel="prev" href="' + href(page - 1) +
                     '">Previous 200</a>')
    if page < pages:
        links.append('<a rel="next" href="' + href(page + 1) +
                     '">Next 200</a>')
    return ('<nav class="pending-pages" aria-label="Pending request pages">'
            '<span role="status">Showing ' + str(first) + '–' + str(last) +
            ' of ' + str(total) + ' matching pending domains · page ' +
            str(page) + ' of ' + str(pages) + '</span>' +
            ''.join(links) + '</nav>')


def live_snapshot(query='', view='policy', history_filters=None,
                  include_overview=False, pending_page=1, pending_sort='recent'):
    """Order loopback snapshots across threads, including out-of-order delivery.

    Each policy snapshot has a strictly increasing, safely representable
    microsecond sequence number; browser tabs discard stale responses rather
    than regressing a newer shared pending-queue baseline.
    """
    if view == 'history':
        filters = history_filters or {}
        try:
            metrics = history_index.summarize_history(**{
                k: filters[k] for k in ('kind', 'status', 'domain', 'hours')
                if k in filters})
        except (OSError, ValueError, sqlite3.Error):
            metrics = []
        return {'history_html': history_items_html(**filters),
                'history_summary_html': history_summary_html(metrics)}
    # Aggregate history is read only on an explicit, less-frequent overview
    # poll. Avoid repeating a full history sync on every ten-second refresh.
    overview = (_overview_history_metrics()
                if include_overview else None)
    global _LAST_LIVE_SNAPSHOT_US
    with _LIVE_SNAPSHOT_LOCK:
        pending_total = policy.pending_match_count(query)
        pending_page = min(pending_page, _last_pending_page(pending_total))
        pending_rows = policy.entries(
            query, limit=PENDING_PAGE_SIZE, status='pending',
            offset=(pending_page - 1) * PENDING_PAGE_SIZE,
            pending_sort=pending_sort)
        decisions = policy.entries(query, limit=2000, status='decided')
        domains = [row['domain'] for row in policy.entries(
            limit=policy.MAX_PENDING_TOTAL, status='pending')]
        counts = policy.counts()
        _LAST_LIVE_SNAPSHOT_US = max(
            time.time_ns() // 1000, _LAST_LIVE_SNAPSHOT_US + 1)
        result = {
            'snapshot_run': _LIVE_SNAPSHOT_RUN,
            'snapshot_us': _LAST_LIVE_SNAPSHOT_US,
            'counts': counts,
            'pending_html': ''.join(row_html(
                r, query=query, page=pending_page, pending_sort=pending_sort)
                for r in pending_rows)
                or '<tr><td colspan="4">No pending requests.</td></tr>',
            'decided_html': ''.join(row_html(
                r, query=query, page=pending_page, pending_sort=pending_sort)
                for r in decisions)
                or '<tr><td colspan="4">No matching domains.</td></tr>',
            'audit_html': decision_audit_rows(query),
            'activity_html': history_items_html(show_queries=False),
            'service_rows_html': service_overview_rows(),
            'pending_domains': domains,
            'pending_pages_html': pending_pagination_html(
                query, pending_page, pending_total, pending_sort),
            'pending_page': pending_page,
        }
        if include_overview:
            result['operations_html'] = operations_overview_html(overview)
            result['activity_summary_html'] = overview_summary_html(overview)
        return result


def _overview_history_metrics():
    try:
        return history_index.summarize_history(hours=24)
    except (OSError, ValueError, sqlite3.Error):
        return None


def overview_summary_html(metrics):
    if metrics is None:
        return ('<tr><td colspan="5">Operational history unavailable; '
                'check local configuration.</td></tr>')
    return history_summary_html(metrics)


def operations_overview_html(metrics):
    """Aggregate, redacted 24h counts only; no URLs, domains or queries."""
    if metrics is None:
        return ('<p class="subtle" role="status">Operational history is '
                'unavailable. Existing permissions and recent activity '
                'remain accessible.</p>')
    totals = {
        'Operations': sum(int(x['count']) for x in metrics),
        'Searches': sum(int(x['count']) for x in metrics
                        if x['kind'] == 'search'),
        'Fetches': sum(int(x['count']) for x in metrics
                       if x['kind'] == 'fetch'),
        'Non-success outcomes': sum(int(x['count']) for x in metrics
                                    if x['status'] != 'success'),
    }
    return ''.join(
        '<div class="stat"><span>' + label +
        '</span><b>' + str(value) + '</b></div>'
        for label, value in totals.items())


def service_overview_rows():
    """Bounded local TCP-only view, not proof of service health."""
    return ''.join(
        '<tr><td>' + esc(item['service']) + '</td><td>' +
        esc(item['status']) + '</td><td>' + esc(item['source']) +
        '</td></tr>' for item in service_status.probe_services())


def row_html(r, query='', page=1, pending_sort='recent'):
    dom = esc(r['domain'])
    # This value only restores the local review filter after POST. It never
    # affects the permission decision or its stale-request/CSRF checks.
    review_context = (f'<input type="hidden" name="review_query" '
                      f'value="{esc(query[:100])}">'
                      f'<input type="hidden" name="review_page" '
                      f'value="{page}">'
                      f'<input type="hidden" name="review_sort" '
                      f'value="{pending_sort}">')
    stat = esc('expired' if r.get('expired') else r['status'])
    token = esc(CSRF)
    scope = r.get('match_scope', 'subdomains')
    scope_label = 'Exact host' if scope == 'exact' else 'Host + subdomains'
    expires = r.get('expires_at') or 'Never'
    limits = ('' if r['status'] == 'pending' else
              f'<div class="subtle">{esc(scope_label)} · Expires: '
              f'{esc(expires)}</div>')
    request_meta = (
        f'<div class="subtle">ID: {esc(r.get("request_id") or "—")}</div>'
        f'<div class="subtle">First request: {esc(r.get("first_requested_at") or "—")}</div>'
        f'<div class="subtle">Last: {esc(r.get("last_requested_at") or "—")}</div>'
        f'<div class="subtle">{esc(policy.safe_request_origin(r["last_requested_url"]) if r.get("last_requested_url") else "—")}</div>')
    opts = ('<label class="small">Scope <select name="scope">'
            + '<option value="subdomains"' + (' selected' if scope != 'exact' else '') +
            '>Host + subdomains</option><option value="exact"' +
            (' selected' if scope == 'exact' else '') +
            '>Exact host only</option></select></label>'
            '<label class="small">Duration <select name="expires_minutes" required>'
            '<option value="" selected disabled>Choose duration</option>'
            '<option value="0">Permanent</option><option value="60">1 hour</option>'
            '<option value="1440">24 hours</option>'
            '<option value="10080">7 days</option></select></label>')
    def action(name, label, cls=''):
        controls = ((opts if name in ('allow', 'deny') else '') +
                    '<label class="small">Reason (optional) '
                    '<input name="reason" maxlength="160" '
                    'placeholder="Short local note"></label>')
        return (f'<form method="post" action="/domain">'
                f'<input type="hidden" name="csrf" value="{token}">' + review_context +
                f'<input type="hidden" name="domain" value="{dom}">'
                + controls +
                f'<button class="{cls}" type="submit" name="action" value="{name}">'
                f'{label}</button></form>')
    # A pending request can be decided with one deliberate click. Default
    # selections are bounded to one hour and the exact host; permanent and
    # subdomain-wide rules require an operator to change a visible selector.
    # Never default to the indefinite ("0") permission from policy.decide().
    quick = (
        f'<form class="quick-decision" method="post" action="/domain">'
        f'<input type="hidden" name="csrf" value="{token}">' + review_context +
        f'<input type="hidden" name="domain" value="{dom}">'
        f'<input type="hidden" name="request_id" value="{esc(r.get("request_id") or "")}">'
        '<label class="small">Scope <select name="scope" aria-label="Rule scope">'
        '<option value="exact" selected>Exact host</option>'
        '<option value="subdomains">Host + subdomains</option></select></label>'
        '<label class="small">Duration <select name="expires_minutes" '
        'aria-label="Rule duration" required>'
        '<option value="60" selected>1 hour</option>'
        '<option value="1440">24 hours</option>'
        '<option value="10080">7 days</option>'
        '<option value="0">Permanent</option></select></label>'
        '<button class="allow" type="submit" name="action" value="allow">'
        'Allow</button>'
        '<button class="deny" type="submit" name="action" value="deny">'
        'Deny</button>'
        '<details class="inline-options"><summary>Note (optional)</summary>'
        '<label class="small">Reason '
        '<input name="reason" maxlength="160" '
        'placeholder="Short local note"></label></details></form>'
        if r['status'] == 'pending' and r.get('request_id') else ''
    )
    # One-use grants retain the stale-request ID check, are exact-host and
    # one hour by default, and never become durable permission rules.
    once = (
        f'<form class="once-action" method="post" action="/once">'
        f'<input type="hidden" name="csrf" value="{token}">' + review_context +
        f'<input type="hidden" name="domain" value="{dom}">'
        f'<input type="hidden" name="request_id" value="{esc(r.get("request_id") or "")}">'
        '<button class="allow" name="action" value="stage">Allow once</button>'
        '<details class="inline-options"><summary>One-use options</summary>'
        '<label class="small">Scope <select name="scope">'
        '<option value="exact" selected>Exact host</option>'
        '<option value="subdomains">Host + subdomains</option></select></label>'
        '<label class="small">Valid for <select name="expires_minutes">'
        '<option value="60" selected>1 hour</option>'
        '<option value="1440">24 hours</option>'
        '<option value="10080">7 days</option></select></label>'
        '<label class="small">Reason (optional) '
        '<input name="reason" maxlength="160" placeholder="Short local note">'
        '</label></details></form>'
        if r['status'] == 'pending' and not r.get('explicit_rule') and r.get('request_id')
        else ''
    )
    edit = (f'<form method="post" action="/domain">'
            f'<input type="hidden" name="csrf" value="{token}">' + review_context +
            f'<input type="hidden" name="domain" value="{dom}">'
            f'<label class="small">Reason (optional) '
            f'<input name="reason" maxlength="160" placeholder="Short local note"></label>'
            f'<input aria-label="Rename {dom}" name="new_domain" value="{dom}"'
            f' style="width:195px"><button name="action" value="rename">'
            f'Rename</button></form>')
    select = (f'<label class="small"><input type="checkbox" name="selected" '
              f'form="bulk-form" aria-label="Select {dom}" '
              f'value="{dom}|{esc(r.get("request_id") or "")}"> Bulk</label> '
              if (r['status'] == 'pending' and not r.get('explicit_rule')
                  and r.get('request_id')) else '')
    # Keep rare rule maintenance out of the fast review path without
    # removing or changing any POST endpoints and their CSRF protection.
    maintenance = (
        '<details class="secondary-actions"><summary>More actions</summary>' +
        (action('allow', 'Allow', 'allow') +
         action('deny', 'Deny', 'deny')
         if r['status'] != 'pending' else '') +
        action('delete', 'Remove', 'delete') + edit + '</details>'
    )
    actions = quick + once + maintenance
    return (f'<tr><td class="domain">{select}{dom}</td>'
            f'<td><span class="pill {stat}">{stat}</span>{limits}</td>'
            f'<td>{int(r["request_count"])}{request_meta}</td>'
            f'<td class="actions">{actions}</td></tr>')

def decision_audit_rows(search=""):
    """Render escaped, filtered decision rows for initial and live views."""
    rows = policy.recent_decisions(search=search)
    cells = ''.join(
        '<tr><td class="small">' + esc(r['at']) + '</td>'
        '<td class="domain">' + esc(r['domain']) + '</td>'
        '<td>' + esc(r['action']) + '</td>'
        '<td class="small">' + esc(r['request_id'] or '—') + '</td>'
        '<td>' + esc(r['reason'] or '—') + '</td>'
        '<td>' + esc(policy.safe_request_origin(r['requested_url']) if r['requested_url'] else '—') + '</td>'
        '<td>' + esc(r['first_requested_at'] or '—') + '</td>'
        '<td>' + esc(r['last_requested_at'] or '—') + '</td>'
        '<td>' + str(int(r['request_count'])) + '</td></tr>'
        for r in rows)
    return cells or '<tr><td colspan="9">No decisions recorded.</td></tr>'


def decision_audit_html(search=""):
    return ('<div class="scroll"><table><thead><tr><th>UTC time</th>'
            '<th>Domain</th><th>Action</th><th>Request ID</th>'
            '<th>Operator reason</th><th>Request origin</th>'
            '<th>First request</th><th>Last request</th><th>Count</th>'
            '</tr></thead><tbody id="live-audit">' +
            decision_audit_rows(search) + '</tbody></table></div>')


def once_grants_html(search="", page=1, pending_sort='recent'):
    """Escaped, bounded local status; never expose bearer grant IDs."""
    rows = policy.once_grant_entries(search=search)
    cells = ''.join(
        '<tr><td class="domain">' + esc(r['domain']) + '</td>'
        '<td>' + esc(r['state']) + '</td>'
        '<td>' + esc('Exact host' if r['match_scope'] == 'exact'
                   else 'Host + subdomains') + '</td>'
        '<td>' + esc(r['expires_at'] or 'Never') + '</td>'
        '<td><form method="post" action="/once">'
        '<input type="hidden" name="csrf" value="' + esc(CSRF) + '">'
        '<input type="hidden" name="review_query" value="' + esc(search[:100]) + '">'
        '<input type="hidden" name="review_page" value="' + str(page) + '">'
        '<input type="hidden" name="review_sort" value="' + pending_sort + '">'
        '<input type="hidden" name="domain" value="' + esc(r['domain']) + '">'
        '<label class="small">Reason (optional) <input name="reason" '
        'maxlength="160" placeholder="Short local note"></label>'
        '<button class="delete" name="action" value="revoke">Revoke</button>'
        '</form></td></tr>' for r in rows
    )
    return ('<div class="scroll"><table><thead><tr><th>Domain</th>'
            '<th>Use</th><th>Scope</th><th>Expiry (UTC)</th><th>Manage</th>'
            '</tr></thead><tbody id="once-grants">' +
            (cells or '<tr><td colspan="5">No single-use grants stored.</td></tr>') +
            '</tbody></table></div>')


def once_audit_html(search=""):
    """Separate action log avoids changing the old decisions CHECK schema."""
    rows = policy.recent_once_events(search=search)
    cells = ''.join(
        '<tr><td>' + esc(r['at']) + '</td><td class="domain">' +
        esc(r['domain']) + '</td><td>' + esc(r['action']) +
        '</td><td>' + esc(r['scope'] or '—') +
        '</td><td>' + esc(r['expires_at'] or 'Never') +
        '</td><td>' + esc(r['request_id'] or '—') +
        '</td><td>' + esc(r['reason'] or '—') +
        '</td><td>' + esc(r['requested_url'] or '—') +
        '</td></tr>' for r in rows
    )
    return ('<div class="scroll"><table><thead><tr><th>UTC</th>'
            '<th>Domain</th><th>Action</th><th>Scope</th><th>Expiry</th>'
            '<th>Request ID</th><th>Reason</th><th>Request origin</th>'
            '</tr></thead><tbody id="once-audit">' +
            (cells or '<tr><td colspan="8">No single-use events.</td></tr>') +
            '</tbody></table></div>')


def import_preview_html(plan: dict) -> str:
    """Read-only validated preview with explicit, stale-state-checked apply."""
    new = set(plan["add"])
    unchanged = set(plan["unchanged"])
    rows = ''.join(
        '<tr><td class="domain">' + esc(x["domain"]) + '</td><td>' +
        esc("New" if x["domain"] in new else
            "Unchanged" if x["domain"] in unchanged else "Conflict") +
        '</td><td>' + esc(x["status"]) + '</td><td>' + esc(x["scope"]) +
        '</td><td>' + esc(x["expires_at"] or "Permanent") + '</td></tr>'
        for x in plan["rules"])
    conflicts = plan["conflicts"]
    notice = (f'<p class="alert error">Conflicts with {len(conflicts)} existing '
              'explicit rules or inherited denials (marked Conflict below); '
              'resolve them locally before retrying. No '
              'changes have been made.</p>' if conflicts else
              '<p class="subtle">No changes yet. The import will add only '
              'missing decisions and retain identical rules. A changed '
              'policy or expired rule invalidates this preview.</p>')
    submit = (f'<form method="post" action="/rules/apply">'
              f'<input type="hidden" name="csrf" value="{esc(CSRF)}">'
              f'<input type="hidden" name="state" value="{esc(plan["state"])}">'
              f'<input type="hidden" name="ticket" value="{esc(plan["ticket"])}">'
              f'<textarea name="rule_doc" hidden>{esc(plan["canonical"])}</textarea>'
              '<label>Type IMPORT to apply '
              '<input name="confirm" required pattern="IMPORT" maxlength="6"></label>'
              '<button name="action" value="apply" class="allow">Import rules</button>'
              '</form>' if not conflicts and plan["add"] else
              '<p>No changes can be applied from this preview.</p>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="referrer" content="no-referrer"><title>Rule import preview</title>'
            f'<link rel="stylesheet" href="/dashboard.css"></head><body><main><h1>Domain rule import preview</h1>'
            f'<p>New: {len(plan["add"])} · Unchanged: {len(plan["unchanged"])}'
            f' · Conflicts: {len(conflicts)}</p>{notice}'
            '<div class="scroll"><table><thead><tr><th>Domain</th><th>Preview</th>'
            '<th>Decision</th><th>Scope</th><th>Expiration (UTC)</th></tr></thead><tbody>' +
            rows + '</tbody></table></div>' + submit +
            '<p><a href="/">Return without applying</a></p></main></body></html>')


def render(query='', message='', kind='success', page=1,
           pending_sort='recent'):
    c=policy.counts()
    # Pending rows have independent pages; explicit decisions are never hidden
    # by a large pending queue.
    pending_sort = _pending_sort(pending_sort)
    pending_total = policy.pending_match_count(query)
    page = min(page, _last_pending_page(pending_total))
    pending=''.join(row_html(r, query=query, page=page,
                              pending_sort=pending_sort) for r in policy.entries(
        query, limit=PENDING_PAGE_SIZE, status='pending',
        offset=(page - 1) * PENDING_PAGE_SIZE, pending_sort=pending_sort))
    other=''.join(row_html(r, query=query, page=page,
                            pending_sort=pending_sort) for r in policy.entries(
        query, limit=2000, status='decided'))
    alert=f'<p class="alert {"error" if kind=="error" else "success"}">{esc(message)}</p>' if message else ''
    table_head='<div class="scroll"><table><thead><tr><th>Domain</th><th>Decision / scope / expiry</th><th>Requests / latest</th><th>Manage</th></tr></thead>'
    query_escaped=esc(query)
    audit_html = decision_audit_html(query)
    once_html = once_grants_html(query, page=page,
                                pending_sort=pending_sort)
    once_log = once_audit_html(query)
    # Reuse the existing privacy-preserving history index. The overview
    # never opts into stored query or requested-page path disclosure.
    activity_html = history_items_html(show_queries=False)
    overview = _overview_history_metrics()
    activity_summary = overview_summary_html(overview)
    operations_html = operations_overview_html(overview)
    service_rows = service_overview_rows()
    return render_template(
        "policy",
        alert=alert, pending_count=c["pending"], allowed_count=c["allowed"],
        denied_count=c["denied"], dashboard_port=PORT, csrf=esc(CSRF),
        once_html=once_html, once_log=once_log,
        activity_html=activity_html, activity_summary=activity_summary,
        operations_html=operations_html, service_rows=service_rows,
        max_pending_total=policy.MAX_PENDING_TOTAL,
        max_pending_global_hour=policy.MAX_NEW_PENDING_GLOBAL_PER_HOUR,
        max_pending_per_site=policy.MAX_PENDING_PER_SITE,
        max_pending_site_hour=policy.MAX_NEW_PENDING_PER_SITE_PER_HOUR,
        table_head=table_head,
        pending_rows=pending or '<tr><td colspan="4">No pending requests.</td></tr>',
        max_bulk_decisions=policy.MAX_BULK_DECISIONS,
        query_escaped=query_escaped, pending_page=page,
        pending_sort=pending_sort,
        pending_sort_options=pending_sort_options(pending_sort),
        pending_pages_html=pending_pagination_html(
            query, page, pending_total, pending_sort),
        refresh_href=esc('/?q=' + quote(query[:100], safe='') +
                         '&page=' + str(page) +
                         ('' if pending_sort == 'recent' else
                          '&sort=' + pending_sort)),
        other_rows=other or '<tr><td colspan="4">No matching domains.</td></tr>',
        audit_html=audit_html,
        database_path=esc(policy.database_path()),
    )


def history_settings_page(message="", kind="success", submitted=None):
    """Owner-only local settings page; fields are allowlisted, never secrets."""
    info = safe_settings.dashboard_settings()
    vals = info["values"]
    # Keep a rejected form intact, including its stale version: never silently
    # upgrade the CAS token and risk overwriting a concurrent settings change.
    displayed = ({**vals, "state": info["state"]}
                 if submitted is None else submitted)
    locked = not info["editable"]
    note = ('An environment override is present. Saved values below are '
            'inactive for overridden fields; remove the override and restart '
            'before editing settings in this dashboard.'
            if locked else
            'Changes apply to the next history operation in the running '
            'process, without restarting. Older records already deleted '
            'by retention cannot be recovered.')
    alerts = ('<p class="alert ' + ("error" if kind == "error" else "success") +
              '">' + esc(message) + '</p>') if message else ''
    override = ''.join('<li>' + esc(key) + ': ' +
                       ("environment override" if info["environment_override"][key]
                        else "saved or default") + '</li>' for key in vals)
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="referrer" content="no-referrer">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Local Search MCP — History settings</title><link rel="stylesheet" href="/dashboard.css">'
            '</head><body><main><h1>Nonsecret history settings</h1>' +
            alerts + '<p>' + esc(note) + '</p><ul>' + override + '</ul>'
            '<form method="post" action="/settings">'
            '<input type="hidden" name="csrf" value="' + esc(CSRF) + '">'
            '<input type="hidden" name="state" value="' + esc(displayed["state"]) + '">'
            '<label>Indexed history retention (1–365 days) '
            '<input type="number" name="retention_days" min="1" max="365" '
            'required value="' + esc(displayed["retention_days"]) + '"></label>'
            '<label>JSONL rotation size (64,000–100,000,000 bytes) '
            '<input type="number" name="max_bytes" min="64000" '
            'max="100000000" required value="' + esc(displayed["max_bytes"]) + '"></label>'
            '<label>Type APPLY <input name="confirm" required pattern="APPLY" '
            'maxlength="5"></label><button type="submit"' +
            (' disabled' if locked else '') + '>Apply history settings</button>'
            '</form><p class="subtle">The values are stored in a separate '
            'owner-only file; this panel never displays or modifies API keys, '
            'tunnel credentials, private configuration, domain rules or paths. '
            'Changing retention controls future pruning of the derived index; '
            'changing size affects the next JSONL append/rotation.</p>'
            '<p><a href="/">Return to domain approvals</a></p></main></body></html>')


def upstream_settings_page(message="", kind="success", submitted=None):
    """Strictly nonsecret, operator-only timeout configuration panel."""
    info = upstream_settings.dashboard_settings()
    vals = info["values"]
    shown = ({**vals, "state": info["state"]}
             if submitted is None else submitted)
    locked = not info["editable"]
    alert = ('<p class="alert ' + ('error' if kind == 'error' else 'success') +
             '">' + esc(message) + '</p>') if message else ''
    sources = ''.join('<li>' + esc(key) + ': ' +
                      ('environment override' if info["environment_override"][key]
                       else 'saved or default') + '</li>' for key in vals)
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="referrer" content="no-referrer">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Local Search MCP — Upstream timeouts</title><link rel="stylesheet" href="/dashboard.css">'
            '</head><body><main><h1>Upstream request timeouts</h1>' +
            alert + '<p>Timeouts apply to the next outgoing upstream request '
            'without restarting the bridge. Requests already running retain '
            'their original timeout. The Crawl4AI /health safety check keeps '
            'its own fixed short timeout.</p><ul>' + sources + '</ul>' +
            ('<p class="alert error">An environment override is present. '
             'Remove it from the service environment and restart before '
             'editing these saved settings.</p>' if locked else '') +
            '<form method="post" action="/upstream-settings">'
            '<input type="hidden" name="csrf" value="' + esc(CSRF) + '">'
            '<input type="hidden" name="state" value="' + esc(shown["state"]) + '">'
            '<label>SearXNG search timeout (5–120 seconds) '
            '<input type="number" name="searxng_seconds" min="5" max="120" '
            'required value="' + esc(shown["searxng_seconds"]) + '"></label>'
            '<label>Crawl4AI crawl timeout (15–300 seconds) '
            '<input type="number" name="crawl_seconds" min="15" max="300" '
            'required value="' + esc(shown["crawl_seconds"]) + '"></label>'
            '<label>Type APPLY <input name="confirm" required pattern="APPLY" '
            'maxlength="5"></label><button type="submit"' +
            (' disabled' if locked else '') + '>Apply upstream timeouts</button>'
            '</form><p class="subtle">This separate owner-only file stores '
            'only two validated integers. It never reads or changes credentials, '
            'domain permissions, history data, endpoint URLs or service ports. '
            'Existing cached responses may still be served until their TTL expires.</p>'
            '<p><a href="/settings">History settings</a> · '
            '<a href="/">Return to domain approvals</a></p></main></body></html>')


def cache_settings_page(message="", kind="success", submitted=None):
    """Strictly nonsecret, operator-only TTL configuration panel."""
    info = cache_settings.dashboard_settings()
    vals = info["values"]
    shown = ({**vals, "state": info["state"]}
             if submitted is None else submitted)
    locked = not info["editable"]
    alert = ('<p class="alert ' + ('error' if kind == 'error' else 'success') +
             '">' + esc(message) + '</p>') if message else ''
    sources = ''.join('<li>' + esc(key) + ': ' +
                      ('environment override' if info["environment_override"][key]
                       else 'saved or default') + '</li>' for key in vals)
    locked_notice = ('<p class="alert error">An environment override is present. '
                     'Remove it from the service environment and restart before '
                     'editing these saved settings.</p>' if locked else '')
    return render_template(
        "cache-settings", alert=alert, sources=sources,
        locked_notice=locked_notice, csrf=esc(CSRF),
        state=esc(shown["state"]),
        search_seconds=esc(shown["search_seconds"]),
        fetch_seconds=esc(shown["fetch_seconds"]),
        disabled=" disabled" if locked else "",
    )


def portable_settings_page(message="", kind="success"):
    alert = ('<p class="alert ' + ("error" if kind == "error" else "success") +
             '">' + esc(message) + '</p>') if message else ''
    return render_template("portable-settings", alert=alert, csrf=esc(CSRF))


def portable_settings_preview(plan: dict, document: str) -> str:
    """Bounded, HTML-escaped preview; submit the same text under CAS."""
    rows = ''.join(
        '<tr><td>' + esc(key) + '</td><td>' + esc(values["old"]) +
        '</td><td>' + esc(values["new"]) + '</td></tr>'
        for key, values in plan["changes"].items()
    )
    if not rows:
        rows = '<tr><td colspan="3">No changes</td></tr>'
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="referrer" content="no-referrer">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Local Search MCP — Settings import preview</title>'
        '<link rel="stylesheet" href="/dashboard.css"></head><body><main>'
        '<h1>Nonsecret settings import preview</h1><p>Profile: ' +
        esc(plan["profile"]) + ' (one atomic profile only)</p>'
        '<table><thead><tr><th>Setting</th><th>Current</th><th>New</th>'
        '</tr></thead><tbody>' + rows + '</tbody></table>'
        '<form method="post" action="/portable-settings/apply">'
        '<input type="hidden" name="csrf" value="' + esc(CSRF) + '">'
        '<input type="hidden" name="state" value="' + esc(plan["state"]) + '">'
        '<textarea name="settings_doc" hidden readonly>' + esc(document) + '</textarea>'
        '<label>Type IMPORT to confirm <input name="confirm" required '
        'pattern="IMPORT" maxlength="6"></label>'
        '<button type="submit">Apply validated settings</button></form>'
        '<p>Stale or conflicting values and environment overrides refuse the '
        'write; no secrets or service restart actions are included.</p>'
        '<p><a href="/portable-settings">Cancel import</a></p></main></body></html>'
    )


def status_page():
    """Read-only local TCP diagnostics; no mutation or privileged probes."""
    rows = ''.join(
        '<tr><td>' + esc(x['service']) + '</td><td>' +
        esc(x['status']) + '</td><td>' + esc(x['source']) +
        '</td></tr>' for x in service_status.probe_services())
    ports = ''.join(
        '<tr><td>' + esc(item['service']) + '</td><td>' +
        esc(item['port'] if item['valid'] else 'Invalid (check local configuration)') +
        '</td><td>' + esc(item['source']) + '</td><td>' +
        esc(item['apply']) + '</td></tr>'
        for item in service_status.configured_ports())
    ports += (
        '<tr><td>Dashboard</td><td>' + esc(PORT) +
        '</td><td>active listener</td><td>Bridge restart and health-monitor '
        'coordination required for a port change</td></tr>'
        '<tr><td>MCP</td><td>' + esc(MCP_PORT) + '</td><td>fixed loopback listener</td>'
        '<td>Not configurable through the dashboard</td></tr>'
    )
    limits = ''.join(
        '<tr><td>' + esc(item['name']) + '</td><td>' +
        esc(item['value']) + '</td><td>' + esc(item['apply']) +
        '</td></tr>' for item in service_status.fixed_safety_limits())
    return render_template("status", rows=rows, ports=ports, limits=limits,
                           mcp_port=esc(MCP_PORT))


def history_page(kind="", status="", domain="", hours=0, show_queries=False,
                 filter_error=""):
    filters = {"kind": kind, "status": status, "domain": domain, "hours": hours}
    try:
        retention_label = str(history_index._retention_days())
        metrics = (history_index.summarize_history(**filters)
                   if not filter_error else [])
    except (OSError, ValueError, sqlite3.Error):
        retention_label = "configured"
        metrics = []
    warning = (f'<p class="alert error" role="alert">{esc(filter_error)}</p>'
               if filter_error else '')
    invalid_kind_option = (
        f'<option value="{esc(kind)}" selected disabled>'
        f'Unsupported operation: {esc(kind)}</option>'
        if filter_error and kind not in ("", "search", "fetch", "page") else '')
    rows = (recent_rows(**filters, show_queries=show_queries) if not filter_error
            else '<p class="subtle">Correct the filters above to view history.</p>')
    options = ''.join(
        f'<option value="{n}"{" selected" if hours == n else ""}>{label}</option>'
        for n, label in ((0, "All retained"), (1, "Last hour"),
                         (24, "Last 24 hours"), (168, "Last 7 days")))
    return render_template(
        "history",
        invalid_kind_option=invalid_kind_option,
        search_selected=" selected" if kind == "search" else "",
        fetch_selected=" selected" if kind == "fetch" else "",
        page_selected=" selected" if kind == "page" else "",
        status=esc(status), domain=esc(domain), options=options,
        query_checked=' checked' if show_queries else '',
        warning=warning, rows=rows,
        summary_html=history_summary_html(metrics),
        retention_label=esc(retention_label),
    )


class Handler(BaseHTTPRequestHandler):
    server_version='LocalSearchAdmin/1.0'
    def log_message(self, format, *args):
        pass
    def _host_valid(self):
        return self.headers.get('Host','') in (f'127.0.0.1:{PORT}',f'localhost:{PORT}')
    def _respond(self, status, content, content_type='text/html; charset=utf-8',
                 download_name=None):
        data=content.encode('utf-8') if isinstance(content,str) else content
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        if download_name is not None:
            # Reject arbitrary filenames: fixed, in-repository names only.
            if download_name not in ('domain-rules-v1.json',
                                     'nonsecret-settings-v1.json'):
                raise ValueError('Unrecognized download filename')
            self.send_header('Content-Disposition',
                             'attachment; filename="' + download_name + '"')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers();self.wfile.write(data)
    def _read_origin_valid(self):
        """Refuse cross-site script/API GETs even on the loopback listener."""
        fetch_site = self.headers.get('Sec-Fetch-Site', '').strip().lower()
        if fetch_site and fetch_site not in ('same-origin', 'none'):
            return False
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        if origin is not None:
            try:
                parsed = urlsplit(origin)
                if (parsed.scheme != 'http' or parsed.netloc != host
                        or parsed.username or parsed.password
                        or parsed.path or parsed.query or parsed.fragment):
                    return False
            except ValueError:
                return False
        referer = self.headers.get('Referer')
        if referer:
            try:
                parsed = urlsplit(referer)
                if (parsed.scheme != 'http' or parsed.netloc != host
                        or parsed.username or parsed.password):
                    return False
            except ValueError:
                return False
        return True

    def do_GET(self):
        if not self._host_valid():return self._respond(403,'Forbidden')
        path=urlsplit(self.path)
        if path.path=='/healthz':return self._respond(200,json.dumps({'status':'ok'}),'application/json')
        if path.path=='/dashboard.css':
            if not self._read_origin_valid():return self._respond(403,'Forbidden')
            # Fixed package-owned stylesheet; never serve a requested file path.
            return self._respond(200, STYLESHEET, 'text/css; charset=utf-8')
        if path.path=='/dashboard-live.js':
            if not self._read_origin_valid():return self._respond(403,'Forbidden')
            # Fixed in-repository program literal, never a path read from
            # request input. Keep the existing restricted CSP and MIME type.
            return self._respond(200, LIVE_SCRIPT, 'text/javascript; charset=utf-8')
        if path.path=='/live':
            if not self._read_origin_valid():return self._respond(403,'Forbidden')
            query=parse_qs(path.query)
            view=query.get('view',['policy'])[0]
            if view not in ('policy','history'):return self._respond(400,'Invalid view')
            q=query.get('q',[''])[0][:100]
            try:
                page = _pending_page(query.get('page', ['1'])[0])
                sort_values = query.get('sort', ['recent'])
                if len(sort_values) != 1:
                    raise ValueError("Duplicate pending sort")
                pending_sort = _pending_sort(sort_values[0])
            except ValueError:
                return self._respond(400, 'Invalid pending review page or sort')
            overview_flag = query.get('overview', ['0'])
            if (len(overview_flag) != 1
                    or overview_flag[0] not in ('0', '1')
                    or (view == 'history' and overview_flag[0] != '0')):
                return self._respond(400, 'Invalid operational overview option')
            try:
                filters = _history_filters(query) if view == 'history' else None
            except ValueError:
                return self._respond(400, 'Invalid history filter')
            return self._respond(200,json.dumps(live_snapshot(
                q, view, filters, include_overview=overview_flag[0] == '1',
                pending_page=page, pending_sort=pending_sort)),
                                 'application/json; charset=utf-8')
        if path.path=='/settings':
            if not self._read_origin_valid():return self._respond(403,'Forbidden')
            try:
                return self._respond(200,history_settings_page())
            except (ValueError, OSError):
                return self._respond(400,'Nonsecret settings unavailable; inspect permissions and file contents')
        if path.path=='/upstream-settings':
            if not self._read_origin_valid():return self._respond(403,'Forbidden')
            try:
                return self._respond(200, upstream_settings_page())
            except (ValueError, OSError):
                return self._respond(400,
                    'Upstream settings unavailable; inspect permissions and file contents')
        if path.path=='/cache-settings':
            if not self._read_origin_valid():return self._respond(403,'Forbidden')
            try:
                return self._respond(200, cache_settings_page())
            except (ValueError, OSError):
                return self._respond(400,
                    'Cache settings unavailable; inspect permissions and file contents')
        if path.path=='/on-demand-state':
            if not self._read_origin_valid():return self._respond(403,'Forbidden')
            from scripts import on_demand_supervisor
            return self._respond(200, json.dumps(on_demand_supervisor.local_status()),
                                 'application/json; charset=utf-8')
        if path.path=='/status':
            if not self._read_origin_valid():return self._respond(403,'Forbidden')
            return self._respond(200,status_page())
        if path.path=='/portable-settings':
            if not self._read_origin_valid():return self._respond(403,'Forbidden')
            return self._respond(200, portable_settings_page())
        if path.path=='/portable-settings/export':
            if not self._read_origin_valid():return self._respond(403,'Forbidden')
            query=parse_qs(path.query, max_num_fields=2)
            if set(query) != {'profile'} or len(query['profile']) != 1:
                return self._respond(400, 'Invalid settings export selection')
            try:
                document = json.dumps(portable_settings.export_profile(
                    query['profile'][0]), indent=2, ensure_ascii=True)
            except (ValueError, OSError):
                return self._respond(400, 'Nonsecret settings cannot be exported')
            return self._respond(200, document, 'application/json; charset=utf-8',
                                 download_name='nonsecret-settings-v1.json')
        if path.path=='/rules/export':
            if not self._read_origin_valid():return self._respond(403,'Forbidden')
            try:
                document = json.dumps(policy_transfer.export_rules(),
                                      indent=2, ensure_ascii=True)
                if len(document.encode('utf-8')) > policy_transfer.MAX_JSON_BYTES:
                    raise ValueError('Rule export exceeds 48 KB limit')
            except ValueError as exc:
                return self._respond(400, render(message=str(exc),kind='error'))
            return self._respond(200, document, 'application/json; charset=utf-8',
                                 download_name='domain-rules-v1.json')
        if path.path=='/history':
            query=parse_qs(path.query)
            try:
                filters = _history_filters(query)
            except ValueError:
                try:
                    # Keep the form and navigation accessible for invalid
                    # kind/status/domain text; invalid windows still hard-fail.
                    filters = _history_filters(query, validate_text=False)
                except ValueError:
                    return self._respond(400, 'Invalid history time window')
                return self._respond(400, history_page(
                    **filters, filter_error="Invalid history filter. Use a "
                    "hostname (not a URL), and a supported operation/status."))
            return self._respond(200,history_page(**filters))
        if path.path!='/':return self._respond(404,'Not found')
        query = parse_qs(path.query)
        q=query.get('q',[''])[0][:100]
        try:
            page = _pending_page(query.get('page', ['1'])[0])
            sorts = query.get('sort', ['recent'])
            if len(sorts) != 1:
                raise ValueError("Duplicate pending sort")
            pending_sort = _pending_sort(sorts[0])
        except ValueError:
            return self._respond(400, 'Invalid pending review page or sort')
        return self._respond(200,render(q, page=page,
                                        pending_sort=pending_sort))
    def _post_origin_valid(self):
        """Accept same-origin forms even when Firefox omits Origin.

        The previous check required Origin on *every* POST. Some browsers and
        privacy settings omit it on normal forms, so legitimate approvals failed.
        Keep the exact local Host check, Fetch Metadata checks, and CSRF token.
        """
        host = self.headers.get('Host', '')
        expected = f'http://{host}'
        origin = self.headers.get('Origin')
        fetch_site = self.headers.get('Sec-Fetch-Site', '').strip().lower()
        if fetch_site and fetch_site not in ('same-origin', 'none'):
            return False
        if origin is not None and origin != expected:
            # Opaque origins can be generated by sandboxed documents. Never
            # allow them without browser-confirmed same-origin Fetch Metadata.
            if not (origin == 'null' and fetch_site == 'same-origin'):
                return False
        referer = self.headers.get('Referer')
        if referer:
            try:
                parsed = urlsplit(referer)
                if parsed.scheme != 'http' or parsed.netloc != host or parsed.username or parsed.password:
                    return False
            except ValueError:
                return False
        return True

    def do_POST(self):
        if not self._host_valid():return self._respond(403,'Forbidden')
        if not self._post_origin_valid():
            return self._respond(403,'Invalid origin')
        if self.path not in ('/domain', '/pending/clear', '/bulk', '/once',
                             '/rules/preview', '/rules/apply', '/settings',
                             '/upstream-settings', '/cache-settings',
                             '/portable-settings/preview', '/portable-settings/apply'):
            return self._respond(404,'Not found')
        try:
            length=int(self.headers.get('Content-Length','-1'))
        except ValueError:return self._respond(400,'Invalid size')
        # Form encoding can triple each UTF-8 byte (%HH) before the JSON
        # parser checks its 48 KB *decoded* limit. Include bounded headroom
        # for CSRF, preview ticket/state and form field names.
        limit = (3 * policy_transfer.MAX_JSON_BYTES + 4096
                 if self.path in ('/rules/preview', '/rules/apply')
                 else (3 * portable_settings.MAX_JSON_BYTES + 4096)
                 if self.path in ('/portable-settings/preview',
                                  '/portable-settings/apply')
                 else 8192 if self.path == '/bulk' else 4096)
        if length < 0 or length > limit:
            return self._respond(413,'Request too large')
        try:
            vals=parse_qs(self.rfile.read(length).decode('utf-8'),
                          keep_blank_values=True, max_num_fields=40)
        except (ValueError,UnicodeError):
            return self._respond(400,'Invalid form data')
        def val(key):return vals.get(key,[''])[0]
        if not secrets.compare_digest(val('csrf'),CSRF):return self._respond(403,'Invalid CSRF token')
        # Browser-submitted view state is presentation-only, not authority.
        # Limit its size and reject controls to keep HTML and database
        # search behavior consistent with the bounded GET filter.
        try:
            review_page = _pending_page(val('review_page') or '1')
        except ValueError:
            review_page = 1
        try:
            review_sort = _pending_sort(val('review_sort') or 'recent')
        except ValueError:
            review_sort = 'recent'
        review_raw = val('review_query')
        review_query = (review_raw if len(review_raw) <= 100 and
                        all(ch.isprintable() for ch in review_raw) else '')
        if self.path == '/pending/clear':
            if val('confirm') != 'CLEAR':
                return self._respond(400, render(query=review_query, page=review_page, pending_sort=review_sort, message='Type CLEAR to confirm removal.', kind='error'))
            removed = policy.clear_pending_requests()
            return self._respond(200,render(query=review_query, page=review_page, pending_sort=review_sort, message=f"Cleared {removed} pending requests."))
        if self.path in ('/portable-settings/preview', '/portable-settings/apply'):
            try:
                if self.path == '/portable-settings/preview':
                    plan = portable_settings.preview(val('settings_doc'))
                    return self._respond(200, portable_settings_preview(
                        plan, val('settings_doc')))
                if val('confirm') != 'IMPORT':
                    raise ValueError('Type IMPORT to confirm the settings import')
                profile = portable_settings.apply(val('settings_doc'), val('state'))
                return self._respond(200, portable_settings_page(
                    message='Imported ' + profile + ' settings; applied to future operations.'))
            except (ValueError, OSError) as exc:
                # Do not expose operating-system paths, or redirect the
                # operator to the unrelated domain-policy page on failure.
                message = ('Cannot safely read or save nonsecret settings'
                           if isinstance(exc, OSError) else str(exc))
                return self._respond(400, portable_settings_page(
                    message=message, kind='error'))
        try:
            if self.path == '/cache-settings':
                submitted = {
                    'search_seconds': val('search_seconds'),
                    'fetch_seconds': val('fetch_seconds'),
                    'state': val('state'),
                }
                try:
                    if val('confirm') != 'APPLY':
                        raise ValueError('Type APPLY to confirm the cache lifetime update')
                    cache_settings.update_settings({
                        'search_seconds': submitted['search_seconds'],
                        'fetch_seconds': submitted['fetch_seconds'],
                    }, submitted['state'])
                except (ValueError, OSError) as exc:
                    message = ('Cannot safely save cache settings'
                               if isinstance(exc, OSError) else str(exc))
                    try:
                        return self._respond(400, cache_settings_page(
                            message=message, kind='error', submitted=submitted))
                    except (ValueError, OSError):
                        return self._respond(400,
                            'Cache settings unavailable; inspect permissions and file contents')
                return self._respond(200, cache_settings_page(
                    message='Cache lifetimes saved; applied to subsequent cache entries.'))
            if self.path == '/upstream-settings':
                submitted = {
                    'searxng_seconds': val('searxng_seconds'),
                    'crawl_seconds': val('crawl_seconds'),
                    'state': val('state'),
                }
                try:
                    if val('confirm') != 'APPLY':
                        raise ValueError('Type APPLY to confirm the timeout update')
                    upstream_settings.update_settings({
                        'searxng_seconds': submitted['searxng_seconds'],
                        'crawl_seconds': submitted['crawl_seconds'],
                    }, submitted['state'])
                except (ValueError, OSError) as exc:
                    message = ('Cannot safely save upstream settings'
                               if isinstance(exc, OSError) else str(exc))
                    try:
                        return self._respond(400, upstream_settings_page(
                            message=message, kind='error', submitted=submitted))
                    except (ValueError, OSError):
                        return self._respond(400,
                            'Upstream settings unavailable; inspect permissions and file contents')
                return self._respond(200, upstream_settings_page(
                    message='Upstream timeouts saved; applied to subsequent requests.'))
            if self.path == '/settings':
                submitted = {
                    'retention_days': val('retention_days'),
                    'max_bytes': val('max_bytes'),
                    'state': val('state'),
                }
                try:
                    if val('confirm') != 'APPLY':
                        raise ValueError('Type APPLY to confirm the settings update')
                    safe_settings.update_settings({
                        'retention_days': submitted['retention_days'],
                        'max_bytes': submitted['max_bytes'],
                    }, submitted['state'])
                except (ValueError, OSError) as exc:
                    # Keep a rejected edit on the settings page, not the domain
                    # approvals page. Never expose filesystem paths in errors.
                    message = ('Cannot safely save nonsecret settings'
                               if isinstance(exc, OSError) else str(exc))
                    try:
                        return self._respond(400, history_settings_page(
                            message=message, kind='error', submitted=submitted))
                    except (ValueError, OSError):
                        # The saved document itself may be malformed/unsafe.
                        return self._respond(400,
                            'Nonsecret settings unavailable; inspect permissions and file contents')
                return self._respond(200, history_settings_page(
                    message='History settings saved; effective for subsequent history operations.'))
            if self.path == '/rules/preview':
                plan = policy_transfer.preview_import(val('rule_doc'))
                return self._respond(200, import_preview_html(plan))
            if self.path == '/rules/apply':
                if val('confirm') != 'IMPORT':
                    raise ValueError('Type IMPORT to confirm')
                amount = policy_transfer.apply_import(
                    val('rule_doc'), val('state'), val('ticket'))
                return self._respond(200, render(
                    message=f'Imported {amount} explicit domain rules.'))
            if self.path == '/once':
                action = val('action')
                domain = val('domain')
                reason = val('reason')
                if action == 'stage':
                    duration = int(val('expires_minutes'))
                    if duration not in (60, 1440, 10080):
                        raise ValueError('Choose a bounded one-use expiry')
                    policy._stage_once_grant(
                        domain, scope=val('scope'), expires_minutes=duration,
                        reason=reason,
                        expected_request_id=val('request_id') or None)
                    return self._respond(200, render(
                        query=review_query, page=review_page, pending_sort=review_sort, message=f'One-time grant staged for {domain[:253]}.'))
                if action == 'revoke':
                    policy._revoke_once_grant(domain, reason=reason)
                    return self._respond(200, render(
                        query=review_query, page=review_page, pending_sort=review_sort, message=f'One-time grant revoked for {domain[:253]}.'))
                raise ValueError('Unrecognized one-use action')
            if self.path == '/bulk':
                amount = policy.bulk_decide(vals.get('selected', []), val('action'),
                    scope=val('scope'), expires_minutes=int(val('expires_minutes')),
                    reason=val('reason'))
                return self._respond(200, render(
                    query=review_query, page=review_page, pending_sort=review_sort, message=f"Updated {amount} pending requests."))
            # The new-domain and existing-rule forms require an explicit
            # duration. A missing/blank field must not silently turn a
            # temporary approval into a permanent grant.
            action = val('action')
            duration = val('expires_minutes')
            if action in ('allow', 'deny') and duration not in (
                    '0', '60', '1440', '10080'):
                raise ValueError('Choose an explicit permission duration')
            policy.decide(val('domain'), action, val('new_domain'),
                          scope=val('scope') or 'subdomains',
                          expires_minutes=int(duration) if action in ('allow', 'deny') else 0,
                          reason=val('reason'),
                          expected_request_id=val('request_id') or None)
            msg=f"Decision updated for {val('domain')[:253]}"
            return self._respond(200,render(query=review_query, page=review_page, pending_sort=review_sort, message=msg))
        except (ValueError,sqlite3.IntegrityError) as exc:
            return self._respond(400,render(query=review_query, page=review_page, pending_sort=review_sort, message=str(exc),kind='error'))


def create_dashboard_server():
    """Initialize and bind before exposing the MCP endpoint; fail closed."""
    policy.initialize()
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    server.daemon_threads=True
    return server


def run_dashboard():
    """Standalone dashboard entry point for local development."""
    server=create_dashboard_server()
    print(f"Local dashboard: http://{HOST}:{PORT}/", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
