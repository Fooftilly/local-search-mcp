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
import provider_observations
import engine_dashboard
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

# Native radios remain ordinary HTML form fields without JavaScript.
# The permission handler still validates every submitted value.
SCOPE_CHOICES = (("exact", "Exact host"), ("subdomains", "Host + subdomains"))
DURATION_CHOICES = (("60", "1 hour"), ("1440", "24 hours"),
                    ("10080", "7 days"), ("0", "Permanent"))
ONCE_DURATION_CHOICES = DURATION_CHOICES[:-1]


def choice_buttons(name, caption, choices, selected=None, *, required=True):
    """Accessible radio group; HTML lives exclusively in bundled templates."""
    if selected is not None and selected not in {key for key, _ in choices}:
        raise ValueError("Unknown radio choice")
    options = [
        render_template("choice-radio", field_name=esc(name), value=esc(key),
                        label=esc(label),
                        checked=" checked" if selected == key else "",
                        required=" required" if required else "")
        for key, label in choices
    ]
    return render_template("choice-group", caption=esc(caption),
                           options="".join(options))


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

HISTORY_PAGE_SIZE = 25
MAX_HISTORY_PAGE = history_index.MAX_HISTORY_ROWS // HISTORY_PAGE_SIZE


def _history_page_number(raw):
    if (type(raw) is not str or not raw.isascii() or
            not raw.isdecimal() or len(raw) > 4):
        raise ValueError("Invalid history page")
    value = int(raw)
    if not 1 <= value <= MAX_HISTORY_PAGE:
        raise ValueError("Invalid history page")
    return value


def _history_last_page(total):
    return min(MAX_HISTORY_PAGE, max(
        1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE))


def history_pagination_html(filters, page, total):
    """Read-only, escaped navigation retaining existing filters."""
    final = _history_last_page(total)
    first = (page - 1) * HISTORY_PAGE_SIZE + 1 if total and page <= final else 0
    last = min(page * HISTORY_PAGE_SIZE, total) if first else 0

    def href(number):
        args = [(key, str(filters.get(key, ''))) for key in
                ('kind', 'status', 'domain')]
        args += [('hours', str(filters.get('hours', 0))),
                 ('page', str(number))]
        if filters.get('show_queries'):
            args.append(('show_queries', '1'))
        return esc('/history?' + '&'.join(
            quote(k, safe='') + '=' + quote(v, safe='')
            for k, v in args))

    links = []
    if page > 1:
        links.append(render_template("history-prev", href=href(page - 1)))
    if page < final:
        links.append(render_template("history-next", href=href(page + 1)))
    return render_template("history-nav", first=first, last=last,
                           total=total, page=page, final=final,
                           links="".join(links))


def history_items_html(kind="", status="", domain="", hours=0,
                       show_queries=False, page=1, items=None,
                       show_engines=False):
    if items is None:
        try:
            items = history_index.search_history(kind=kind, status=status,
                                                  domain=domain, hours=hours,
                                                  reveal_queries=show_queries,
                                                  include_engines=show_engines,
                                                  offset=(page - 1) * HISTORY_PAGE_SIZE)
        except (OSError, ValueError, sqlite3.Error):
            return render_template(
                "history-unavailable", columns=6 if show_engines else 5)
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
        engine_cell = (
            render_template(
                "history-engine-cell",
                engine=esc(event.get("engine_attribution", "—")
                           if event["kind"] == "search" else "—"))
            if show_engines else "")
        out.append(render_template(
            "history-row", stamp=esc(stamp), kind=esc(event["kind"]),
            status=esc(event["status"]), display=esc(display),
            css_class="domain history-query" if show_queries else "domain",
            engine_cell=engine_cell, elapsed=esc(elapsed)))
    return "".join(out) or render_template(
        "history-empty", columns=6 if show_engines else 5)


def recent_rows(kind="", status="", domain="", hours=0, show_queries=False,
                page=1, items=None, unavailable=False):
    column = "Search query / page URL / domain" if show_queries else "Domain"
    rows = (render_template("history-unavailable", columns=6)
            if unavailable else
            history_items_html(kind, status, domain, hours, show_queries,
                               page=page, items=items, show_engines=True))
    return render_template("history-table", column=esc(column), rows=rows)


def history_summary_html(metrics):
    """Accessible, CSP-compatible bars using template-backed progress rows."""
    peak = max((int(x["count"]) for x in metrics), default=1)
    rows = [
        render_template(
            "history-summary-row", kind=esc(row["kind"]),
            status=esc(row["status"]), count=int(row["count"]), peak=peak,
            duration=esc(row["avg_duration_ms"] if
                         row["avg_duration_ms"] is not None else "—"))
        for row in metrics
    ]
    return "".join(rows) or render_template("history-summary-empty")


def _history_filters(query, *, validate_text=True):
    """Parse bounded GET filters; preserve form input on text-validation errors."""
    values = {key: query.get(key, [''])[0][:253 if key == 'domain' else 48]
              for key in ('kind', 'status', 'domain')}
    raw = query.get('hours', ['0'])[0]
    if raw not in ('0', '1', '24', '168'):
        raise ValueError("Invalid history time window")
    values['hours'] = int(raw)
    # Personal console: show audit queries by default. The form pairs a
    # hidden "0" with the checkbox's "1", so unchecked sends only "0".
    # Preserve old direct links with a single explicit "1".
    visible = query.get('show_queries', ['1'])
    if visible not in (['0'], ['1'], ['0', '1']):
        raise ValueError("Invalid history query visibility")
    values['show_queries'] = visible[-1] == '1'
    pages = query.get('page', ['1'])
    if len(pages) != 1:
        raise ValueError("Invalid history page")
    values['page'] = _history_page_number(pages[0])
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


def pending_sort_buttons(selected):
    return choice_buttons(
        "sort", "Review order",
        (("recent", "Recently updated"), ("oldest", "Oldest waiting"),
         ("frequent", "Most requests")), selected)



def _last_pending_page(total):
    return min(MAX_PENDING_PAGE, max(
        1, (total + PENDING_PAGE_SIZE - 1) // PENDING_PAGE_SIZE))


def pending_pagination_html(query, page, total, pending_sort='recent'):
    """Accessible bounded navigation; URLs escaped before rendering."""
    pages = _last_pending_page(total)
    first = (page - 1) * PENDING_PAGE_SIZE + 1 if total and page <= pages else 0
    last = min(page * PENDING_PAGE_SIZE, total) if first else 0

    def href(number):
        return esc('/?q=' + quote(query, safe='') + '&page=' + str(number) +
                   ('' if pending_sort == 'recent' else
                    '&sort=' + pending_sort))

    links = []
    if page > 1:
        links.append(render_template("pending-prev", href=href(page - 1)))
    if page < pages:
        links.append(render_template("pending-next", href=href(page + 1)))
    return render_template("pending-nav", first=first, last=last,
                           total=total, page=page, pages=pages,
                           links="".join(links))


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
            snapshot = history_index.dashboard_history_snapshot(
                **{k: filters[k] for k in ('kind', 'status', 'domain', 'hours')
                   if k in filters},
                page=filters.get('page', 1),
                reveal_queries=filters.get('show_queries', False),
                include_engines=True,
                page_size=HISTORY_PAGE_SIZE)
        except (OSError, ValueError, sqlite3.Error):
            # A corrupt/unavailable index must not masquerade as zero events.
            return {
                'history_html': render_template("history-unavailable", columns=6),
                'history_summary_html': render_template("history-summary-unavailable"),
                'history_pages_html': render_template("history-pages-unavailable"),
                'history_page': filters.get('page', 1)}
        return {'history_html': history_items_html(
                    items=snapshot['rows'],
                    show_queries=filters.get('show_queries', False),
                    show_engines=True),
                'history_summary_html': history_summary_html(snapshot['metrics']),
                'history_pages_html': history_pagination_html(
                    filters, snapshot['page'], snapshot['total']),
                'history_page': snapshot['page']}
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
                or render_template("domain-empty-pending"),
            'decided_html': ''.join(row_html(
                r, query=query, page=pending_page, pending_sort=pending_sort)
                for r in decisions)
                or render_template("domain-empty-decided"),
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
        return render_template("overview-unavailable")
    return history_summary_html(metrics)


def operations_overview_html(metrics):
    """Aggregate 24h counts only; no URLs, domains or query text."""
    if metrics is None:
        return render_template("operations-unavailable")
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
        render_template("operations-stat", label=esc(label), value=count)
        for label, count in totals.items())


def service_overview_rows():
    """Bounded local TCP-only view, not proof of application health."""
    return ''.join(
        render_template("service-row", service=esc(item['service']),
                        status=esc(item['status']), source=esc(item['source']))
        for item in service_status.probe_services())


def row_html(r, query='', page=1, pending_sort='recent'):
    """Permission row data and decisions; HTML stays in fixed web templates."""
    dom = esc(r['domain'])
    request_id = esc(r.get("request_id") or "")
    review_context = render_template(
        "domain-review-context", review_query=esc(query[:100]),
        review_page=page, review_sort=esc(pending_sort))
    stat = esc('expired' if r.get('expired') else r['status'])
    token = esc(CSRF)
    scope = r.get('match_scope', 'subdomains')
    scope_label = 'Exact host' if scope == 'exact' else 'Host + subdomains'
    expires = r.get('expires_at') or 'Never'
    limits = ("" if r['status'] == 'pending' else
              render_template("domain-limits",
                              scope_label=esc(scope_label), expires=esc(expires)))
    request_meta = render_template(
        "domain-request-meta", request_id=esc(r.get("request_id") or "—"),
        first_requested_at=esc(r.get("first_requested_at") or "—"),
        last_requested_at=esc(r.get("last_requested_at") or "—"),
        request_origin=esc(policy.safe_request_origin(r["last_requested_url"])
                           if r.get("last_requested_url") else "—"))
    opts = (choice_buttons("scope", "Scope", SCOPE_CHOICES,
                           "exact" if scope == "exact" else "subdomains") +
            choice_buttons("expires_minutes", "Duration", DURATION_CHOICES))

    def action(action_name, label, cls=''):
        controls = ((opts if action_name in ('allow', 'deny') else '') +
                    render_template("domain-reason"))
        return render_template(
            "domain-action-form", csrf=token, review_context=review_context,
            domain=dom, controls=controls, button_class=esc(cls),
            action_name=esc(action_name), button_label=esc(label))

    # A pending decision defaults to a one-hour exact-host rule, never a
    # permanent/subdomain-wide grant. The existing server checks still apply.
    quick = (render_template(
        "domain-quick-form", csrf=token, review_context=review_context,
        domain=dom, request_id=request_id,
        scope_buttons=choice_buttons("scope", "Scope",
                                     SCOPE_CHOICES, "exact"),
        duration_buttons=choice_buttons(
            "expires_minutes", "Duration", DURATION_CHOICES, "60"))
        if r['status'] == 'pending' and r.get('request_id') else "")
    once = (render_template(
        "domain-once-form", csrf=token, review_context=review_context,
        domain=dom, request_id=request_id,
        scope_buttons=choice_buttons("scope", "Scope", SCOPE_CHOICES, "exact"),
        duration_buttons=choice_buttons(
            "expires_minutes", "Valid for", ONCE_DURATION_CHOICES, "60"))
        if r['status'] == 'pending' and not r.get('explicit_rule')
        and r.get('request_id') else "")
    edit = render_template(
        "domain-edit-form", csrf=token, review_context=review_context,
        domain=dom, reason=render_template("domain-reason"))
    select = (render_template(
        "domain-bulk-select", domain=dom, request_id=request_id)
        if (r['status'] == 'pending' and not r.get('explicit_rule')
            and r.get('request_id')) else "")
    maintenance = render_template(
        "domain-maintenance",
        actions=(action('allow', 'Allow', 'allow') +
                 action('deny', 'Deny', 'deny')
                 if r['status'] != 'pending' else ""),
        remove=action('delete', 'Remove', 'delete'), edit=edit)
    return render_template(
        "domain-table-row", select=select, domain=dom, status=stat,
        limits=limits, request_count=int(r["request_count"]),
        request_meta=request_meta, actions=quick + once + maintenance)


def decision_audit_rows(search=""):
    """Escaped decision evidence; markup owned by immutable templates."""
    rows = policy.recent_decisions(search=search)
    cells = ''.join(
        render_template(
            "decision-row", at=esc(r['at']), domain=esc(r['domain']),
            action=esc(r['action']), request_id=esc(r['request_id'] or '—'),
            reason=esc(r['reason'] or '—'),
            requested_url=esc(policy.safe_request_origin(r['requested_url'])
                              if r['requested_url'] else '—'),
            first_requested_at=esc(r['first_requested_at'] or '—'),
            last_requested_at=esc(r['last_requested_at'] or '—'),
            request_count=int(r['request_count']))
        for r in rows)
    return cells or render_template("decision-empty")


def decision_audit_html(search=""):
    return render_template("decision-table", rows=decision_audit_rows(search))


def once_grants_html(search="", page=1, pending_sort='recent'):
    """Bounded local grant status; never expose bearer grant identifiers."""
    rows = policy.once_grant_entries(search=search)
    cells = ''.join(
        render_template(
            "once-grant-row", domain=esc(r['domain']), state=esc(r['state']),
            scope=esc('Exact host' if r['match_scope'] == 'exact'
                      else 'Host + subdomains'),
            expires_at=esc(r['expires_at'] or 'Never'),
            csrf=esc(CSRF), review_query=esc(search[:100]),
            review_page=page, review_sort=esc(pending_sort))
        for r in rows)
    return render_template("once-grant-table",
                           rows=cells or render_template("once-grant-empty"))


def once_audit_html(search=""):
    """Separate action log avoids changing existing decisions CHECK schema."""
    rows = policy.recent_once_events(search=search)
    cells = ''.join(
        render_template(
            "once-audit-row", at=esc(r['at']), domain=esc(r['domain']),
            action=esc(r['action']), scope=esc(r['scope'] or '—'),
            expires_at=esc(r['expires_at'] or 'Never'),
            request_id=esc(r['request_id'] or '—'),
            reason=esc(r['reason'] or '—'),
            requested_url=esc(r['requested_url'] or '—'))
        for r in rows)
    return render_template("once-audit-table",
                           rows=cells or render_template("once-audit-empty"))


def import_preview_html(plan: dict) -> str:
    """Read-only rule import preview with stale-state-checked POST."""
    new = set(plan["add"])
    unchanged = set(plan["unchanged"])
    rows = "".join(
        render_template(
            "import-rule-row", domain=esc(x["domain"]),
            classification=esc("New" if x["domain"] in new else
                               "Unchanged" if x["domain"] in unchanged
                               else "Conflict"),
            status=esc(x["status"]), scope=esc(x["scope"]),
            expires_at=esc(x["expires_at"] or "Permanent"))
        for x in plan["rules"])
    conflicts = plan["conflicts"]
    notice = (render_template("import-rule-conflict", count=len(conflicts))
              if conflicts else render_template("import-rule-safe"))
    submit = (
        render_template("import-rule-form", csrf=esc(CSRF),
                        state=esc(plan["state"]), ticket=esc(plan["ticket"]),
                        canonical=esc(plan["canonical"]))
        if not conflicts and plan["add"] else
        render_template("import-rule-no-change"))
    return render_template(
        "import-rule-page", new_count=len(plan["add"]),
        unchanged_count=len(plan["unchanged"]),
        conflict_count=len(conflicts), notice=notice,
        rows=rows, submit=submit)


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
    alert=(render_template("domain-alert",
                           kind="error" if kind == "error" else "success",
                           message=esc(message)) if message else "")
    table_head=render_template("domain-table-head")
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
        pending_rows=pending or render_template("domain-empty-pending"),
        max_bulk_decisions=policy.MAX_BULK_DECISIONS,
        query_escaped=query_escaped, pending_page=page,
        pending_sort=pending_sort,
        pending_sort_buttons=pending_sort_buttons(pending_sort),
        bulk_scope_buttons=choice_buttons("scope", "Scope", SCOPE_CHOICES, "exact"),
        bulk_duration_buttons=choice_buttons("expires_minutes", "Duration",
                                             DURATION_CHOICES, "60"),
        manual_scope_buttons=choice_buttons("scope", "Scope", SCOPE_CHOICES, "exact"),
        manual_duration_buttons=choice_buttons("expires_minutes", "Duration",
                                               DURATION_CHOICES),
        stage_scope_buttons=choice_buttons("scope", "Scope", SCOPE_CHOICES, "exact"),
        stage_duration_buttons=choice_buttons("expires_minutes", "Valid for",
                                              ONCE_DURATION_CHOICES, "60"),
        pending_pages_html=pending_pagination_html(
            query, page, pending_total, pending_sort),
        refresh_href=esc('/?q=' + quote(query[:100], safe='') +
                         '&page=' + str(page) +
                         ('' if pending_sort == 'recent' else
                          '&sort=' + pending_sort)),
        other_rows=other or render_template("domain-empty-decided"),
        audit_html=audit_html,
        database_path=esc(policy.database_path()),
    )


def _settings_alert(message, kind="success"):
    return (render_template("settings-alert",
                            kind="error" if kind == "error" else "success",
                            message=esc(message)) if message else "")


def _settings_sources(info, values):
    return "".join(
        render_template(
            "settings-source", key=esc(key),
            source=("environment override"
                    if info["environment_override"][key] else "saved or default"))
        for key in values)


def history_settings_page(message="", kind="success", submitted=None):
    """Owner-only history settings; presentation is in bundled HTML."""
    info = safe_settings.dashboard_settings()
    vals = info["values"]
    shown = ({**vals, "state": info["state"]}
             if submitted is None else submitted)
    locked = not info["editable"]
    note = ('An environment override is present. Saved values below are '
            'inactive for overridden fields; remove the override and restart '
            'before editing settings in this dashboard.'
            if locked else
            'Changes apply to the next history operation in the running '
            'process, without restarting. Older records already deleted '
            'by retention cannot be recovered.')
    return render_template(
        "history-settings", alert=_settings_alert(message, kind),
        note=esc(note), sources=_settings_sources(info, vals),
        csrf=esc(CSRF), state=esc(shown["state"]),
        retention_days=esc(shown["retention_days"]),
        max_bytes=esc(shown["max_bytes"]),
        disabled=" disabled" if locked else "")


def upstream_settings_page(message="", kind="success", submitted=None):
    """Operator-only upstream timeout form with file-backed markup."""
    info = upstream_settings.dashboard_settings()
    vals = info["values"]
    shown = ({**vals, "state": info["state"]}
             if submitted is None else submitted)
    locked = not info["editable"]
    return render_template(
        "upstream-settings", alert=_settings_alert(message, kind),
        sources=_settings_sources(info, vals),
        locked_notice=render_template("settings-locked") if locked else "",
        csrf=esc(CSRF), state=esc(shown["state"]),
        searxng_seconds=esc(shown["searxng_seconds"]),
        crawl_seconds=esc(shown["crawl_seconds"]),
        disabled=" disabled" if locked else "")


def cache_settings_page(message="", kind="success", submitted=None):
    """Strictly nonsecret, operator-only TTL configuration panel."""
    info = cache_settings.dashboard_settings()
    vals = info["values"]
    shown = ({**vals, "state": info["state"]}
             if submitted is None else submitted)
    locked = not info["editable"]
    alert = _settings_alert(message, kind)
    sources = _settings_sources(info, vals)
    locked_notice = render_template("settings-locked") if locked else ""
    return render_template(
        "cache-settings", alert=alert, sources=sources,
        locked_notice=locked_notice, csrf=esc(CSRF),
        state=esc(shown["state"]),
        search_seconds=esc(shown["search_seconds"]),
        fetch_seconds=esc(shown["fetch_seconds"]),
        disabled=" disabled" if locked else "",
    )


def portable_settings_page(message="", kind="success"):
    return render_template("portable-settings",
                           alert=_settings_alert(message, kind),
                           csrf=esc(CSRF))


def portable_settings_preview(plan: dict, document: str) -> str:
    """Bounded, escaped preview; submit unchanged operator input under CAS."""
    rows = "".join(
        render_template("portable-preview-row", key=esc(key),
                        old=esc(values["old"]), new=esc(values["new"]))
        for key, values in plan["changes"].items())
    return render_template(
        "portable-preview-page", profile=esc(plan["profile"]),
        rows=rows or render_template("portable-preview-empty"),
        csrf=esc(CSRF), state=esc(plan["state"]), document=esc(document))


def status_page():
    """Read-only local TCP diagnostics; template rendering only."""
    rows = service_overview_rows()
    ports = "".join(
        render_template("status-port-row",
                        service=esc(item['service']),
                        port=esc(item['port'] if item['valid'] else
                                 'Invalid (check local configuration)'),
                        source=esc(item['source']),
                        apply=esc(item['apply']))
        for item in service_status.configured_ports())
    ports += render_template("status-dashboard-port", port=esc(PORT))
    ports += render_template("status-mcp-port", port=esc(MCP_PORT))
    limits = "".join(
        render_template("status-limit-row",
                        display_name=esc(item['name']), value=esc(item['value']),
                        apply=esc(item['apply']))
        for item in service_status.fixed_safety_limits())
    return render_template("status", rows=rows, ports=ports, limits=limits,
                           mcp_port=esc(MCP_PORT))


def history_page(kind="", status="", domain="", hours=0, show_queries=True,
                 filter_error="", page=1):
    filters = {"kind": kind, "status": status, "domain": domain, "hours": hours}
    snapshot = None
    history_unavailable = False
    try:
        retention_label = str(history_index._retention_days())
        if not filter_error:
            snapshot = history_index.dashboard_history_snapshot(
                **filters, page=page, reveal_queries=show_queries,
                include_engines=True, page_size=HISTORY_PAGE_SIZE)
    except (OSError, ValueError, sqlite3.Error):
        retention_label = "configured"
        history_unavailable = not bool(filter_error)
    metrics = snapshot['metrics'] if snapshot is not None else []
    warning = (render_template("history-filter-warning",
                               message=esc(filter_error))
               if filter_error else '')
    invalid_kind = bool(
        filter_error and kind not in ("", "search", "fetch", "page"))
    total = snapshot['total'] if snapshot is not None else 0
    page = snapshot['page'] if snapshot is not None else page
    full_filters = {**filters, 'show_queries': show_queries}
    pages_html = (history_pagination_html(full_filters, page, total)
                  if snapshot is not None else
                  render_template("history-pages-unavailable")
                  if history_unavailable else '')
    rows = (recent_rows(**filters, show_queries=show_queries, page=page,
                        items=snapshot['rows'])
            if snapshot is not None else
            recent_rows(**filters, show_queries=show_queries, page=page,
                        items=[], unavailable=True)
            if history_unavailable else
            render_template("history-filter-correction"))
    valid_kind = kind if kind in ("", "search", "fetch", "page") else ""
    return render_template(
        "history",
        history_kind_buttons=choice_buttons("kind", "Operation",
            (("", "All"), ("search", "Search"), ("fetch", "Fetch"),
             ("page", "Continuation")), valid_kind),
        history_time_buttons=choice_buttons("hours", "Time",
            (("0", "All retained"), ("1", "Last hour"),
             ("24", "Last 24 hours"), ("168", "Last 7 days")), str(hours)),
        invalid_kind_notice=(
            render_template("history-filter-notice", kind=esc(kind))
            if invalid_kind else ''),
        status=esc(status), domain=esc(domain),
        query_checked=' checked' if show_queries else '',
        warning=warning, rows=rows,
        page=page, pages_html=pages_html,
        summary_html=(render_template("history-summary-unavailable")
                      if history_unavailable else history_summary_html(metrics)),
        retention_label=esc(retention_label),
    )


def provider_observations_page(hours: int = 24) -> str:
    """Aggregate only on explicit page request, never on live dashboard polls."""
    if type(hours) is not int or hours not in provider_observations.WINDOWS:
        raise ValueError("Invalid provider-observation window")
    try:
        result = provider_observations.observations(hours)
    except (OSError, ValueError):
        # Never echo private file paths, SQLite internals, query terms, or
        # provider responses on an unsafe or corrupt audit source.
        return render_template(
            "provider-observations",
            hour_buttons=choice_buttons("hours", "UTC time window",
                 (("1", "Last hour"), ("24", "Last 24 hours"),
                  ("168", "Last 7 days")), str(hours)),
            notice=render_template("provider-source-unavailable"),
            coverage_rows=render_template("provider-unavailable-two"),
            engine_rows=render_template("provider-unavailable-four"),
            category_rows=render_template("provider-unavailable-four"),
            reported_failure_rows=render_template("provider-unavailable-two"),
            isolated_rows=render_template("provider-isolated-unavailable"))
    def table_rows(items):
        return "".join(
            render_template(
                "provider-time-row", display_name=esc(row["name"]),
                observed=int(row["observed_searches"]),
                timed=int(row["timed_searches"]),
                mean=esc(row["mean_search_call_ms"]
                         if row["mean_search_call_ms"] is not None
                         else "Not recorded"))
            for row in items) or render_template("provider-time-empty")
    isolated_rows = "".join(
        render_template(
            "provider-isolated-row", engine_name=esc(row["name"]),
            operations=int(row["local_operations"]),
            results=int(row["reported_results"]),
            empty=int(row["reported_empty"]),
            reported_unresponsive=int(row["reported_unresponsive"]),
            other=int(row["other_outcomes"]),
            recoveries=int(row["recovery_operations"]),
            mean_ms=esc(row["mean_local_search_ms"]))
        for row in result["isolated_engine_operations"]) or (
        render_template("provider-isolated-empty"))
    reported_failure_rows = "".join(
        render_template("provider-failure-row", display_name=esc(row["name"]),
                        calls=int(row["reported_unresponsive_calls"]))
        for row in result["reported_unresponsive_engines"]) or (
        render_template("provider-failure-empty"))
    labels = [
        ("Audited search calls", result["sampled_search_calls"]),
        ("Non-success calls (not attributable to an engine)",
         result["non_success_search_calls"]),
        ("Calls with a retained result engine",
         result["search_calls_with_observed_sources"]),
        ("Calls without a retained result engine",
         result["search_calls_without_observed_sources"]),
        ("Files scanned", result["source_files_scanned"]),
        ("Records scanned", result["records_examined"]),
        ("Malformed or oversized records ignored", result["malformed_records_ignored"]),
        ("Scan or retention coverage limited", "Yes" if result["retention_or_scan_limited"] else "No"),
        ("Engine group display truncated", "Yes" if result["engine_groups_truncated"] else "No"),
        ("Category group display truncated", "Yes" if result["category_groups_truncated"] else "No"),
        ("Provider diagnostic calls explicitly retained",
         result["opted_in_diagnostic_calls"]),
        ("Opted-in calls with reported provider failures",
         result["reported_partial_provider_calls"]),
        ("Opted-in calls with unknown diagnostic coverage",
         result["reported_unknown_provider_calls"]),
        ("Opted-in calls with no failures reported",
         result["reported_no_failures_calls"]),
        ("Provider names rejected or unparsed (bounded)", result["provider_labels_suppressed"]),
        ("Failure label table truncated", "Yes" if result["provider_failure_groups_truncated"] else "No"),
        ("Per-engine failure coverage", "Only explicit opted-in upstream reports"),
        ("Remote upstream engine execution latency", "Not measured"),
        ("Individually selected local search observations",
         sum(row["local_operations"] for row in result["isolated_engine_operations"])),
        ("Isolated engine grouping truncated",
         "Yes" if result["isolated_engine_groups_truncated"] else "No"),
        ("Cache hits from historical calls", "Not recorded"),
    ]
    coverage = "".join(
        render_template("provider-coverage-row",
                        title=esc(title), value=esc(value))
        for title, value in labels)
    notice = (render_template("provider-no-files")
              if not result["available"] else
              render_template("provider-partial")
              if result["retention_or_scan_limited"] else
              render_template("provider-bounded"))
    return render_template(
        "provider-observations",
        hour_buttons=choice_buttons("hours", "UTC time window",
             (("1", "Last hour"), ("24", "Last 24 hours"),
              ("168", "Last 7 days")), str(hours)), notice=notice,
        coverage_rows=coverage,
        engine_rows=table_rows(result["observed_result_engines"]),
        isolated_rows=isolated_rows,
        category_rows=table_rows(result["requested_categories"]),
        reported_failure_rows=reported_failure_rows)


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
            query=parse_qs(path.query, keep_blank_values=True)
            view=query.get('view',['policy'])[0]
            if view not in ('policy','history'):return self._respond(400,'Invalid view')
            q=query.get('q',[''])[0][:100]
            # History uses its own 1–4000 page bound. Applying the policy
            # queue's 1–20 limit here would break live updates on older pages.
            page, pending_sort = 1, 'recent'
            if view == 'policy':
                try:
                    page_values = query.get('page', ['1'])
                    if len(page_values) != 1:
                        raise ValueError("Duplicate pending page")
                    page = _pending_page(page_values[0])
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
        if path.path=='/engine-settings':
            if not self._read_origin_valid():return self._respond(403,'Forbidden')
            return self._respond(200, engine_dashboard.page(CSRF))
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
        if path.path == '/provider-observations':
            if not self._read_origin_valid():return self._respond(403, 'Forbidden')
            try:
                query = parse_qs(path.query, keep_blank_values=True, max_num_fields=2)
                if set(query) - {'hours'} or len(query.get('hours', ['24'])) != 1:
                    raise ValueError("Unsupported report filter")
                raw_hours = query.get('hours', ['24'])[0]
                if raw_hours not in ('1', '24', '168'):
                    raise ValueError("Invalid hours")
                return self._respond(200, provider_observations_page(int(raw_hours)))
            except ValueError:
                return self._respond(400, 'Invalid provider-observation window')
        if path.path=='/history':
            query=parse_qs(path.query, keep_blank_values=True)
            try:
                filters = _history_filters(query)
            except ValueError:
                try:
                    # Keep the form and navigation accessible for invalid
                    # kind/status/domain text; invalid windows still hard-fail.
                    filters = _history_filters(query, validate_text=False)
                except ValueError:
                    return self._respond(400, 'Invalid history window or page')
                return self._respond(400, history_page(
                    **filters, filter_error="Invalid history filter. Use a "
                    "hostname (not a URL), and a supported operation/status."))
            return self._respond(200,history_page(**filters))
        if path.path!='/':return self._respond(404,'Not found')
        query = parse_qs(path.query, keep_blank_values=True)
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
                             '/engine-settings',
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
                 else 160 * 1024 if self.path == '/engine-settings'
                 else 8192 if self.path == '/bulk' else 4096)
        if length < 0 or length > limit:
            return self._respond(413,'Request too large')
        try:
            vals=parse_qs(self.rfile.read(length).decode('utf-8'),
                          keep_blank_values=True,
                          max_num_fields=(520 if self.path == '/engine-settings'
                                          else 40))
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
            if self.path == '/engine-settings':
                try:
                    result = engine_dashboard.apply_form(vals)
                    return self._respond(200, engine_dashboard.page(CSRF, message=result))
                except (ValueError, OSError, engine_dashboard.engines.on_demand_docker.LifecycleError) as exc:
                    message = (str(exc) if isinstance(exc, ValueError)
                               else 'Engine selection unavailable or verification failed')
                    return self._respond(400, engine_dashboard.page(CSRF, message=message, error=True))
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
