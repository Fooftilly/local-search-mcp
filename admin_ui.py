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
from urllib.parse import parse_qs, urlsplit

import domain_policy as policy
from dashboard_live_script import LIVE_SCRIPT

HOST='127.0.0.1'
PORT=int(os.environ.get('LOCAL_SEARCH_MCP_DASHBOARD_PORT','8791'))
CSRF=secrets.token_urlsafe(32)
_LIVE_SNAPSHOT_LOCK = threading.Lock()
_LAST_LIVE_SNAPSHOT_US = 0
# Ephemeral ID distinguishes dashboard process restarts even if system time
# moves backward. The browser rejects delayed responses from retired runs.
_LIVE_SNAPSHOT_RUN = secrets.token_hex(16)

CSS="""
:root{color-scheme:dark;font-family:Inter,system-ui,-apple-system,sans-serif;background:#101318;color:#e9edf2;font-size:14px}
*{box-sizing:border-box}body{margin:0}a{color:#9bc8ff}header{border-bottom:1px solid #343b44;padding:17px max(22px,calc((100vw - 1150px)/2));display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.brand{font-weight:700;letter-spacing:.02em;font-size:16px}main{max-width:1150px;margin:auto;padding:26px 22px 70px}h1{font-size:26px;margin:0 0 8px}h2{font-size:18px;margin:24px 0 10px}p{line-height:1.45;color:#b5bfcd}.subtle{color:#99a6b6;font-size:12px}nav a{margin-left:16px;text-decoration:none}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin:23px 0}.stat{background:#1a2028;border:1px solid #343b44;padding:14px 18px;min-width:152px;flex:1}.stat b{font-size:25px;display:block;margin-top:7px}.stat span{font-size:12px;color:#b2bdcb}
.panel{border:1px solid #343b44;background:#151a21;padding:18px 20px;margin:18px 0}input{padding:9px 10px;color:#e9edf2;background:#0f141a;border:1px solid #45515f;border-radius:3px;min-width:0}input:focus{outline:2px solid #80aef0}button{padding:8px 11px;background:#253245;border:1px solid #506076;color:#eff5fd;cursor:pointer;border-radius:3px;white-space:nowrap}button:hover{background:#34475e}button.allow{background:#214b38;border-color:#3c8064}button.deny{background:#613531;border-color:#aa6157}button.delete{background:transparent}form{display:inline-flex;gap:6px;flex-wrap:wrap;align-items:center}.wide{width:min(500px,80vw)}.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}table{width:100%;border-collapse:collapse}th,td{text-align:left;vertical-align:middle;border-bottom:1px solid #323b47;padding:10px 10px}th{font-size:12px;font-weight:600;color:#aab5c3}td{font-size:13px}td.domain{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}tbody tr:hover{background:#1b232d}.pill{display:inline-block;border-radius:3px;border:1px solid #586274;padding:4px 6px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.03em}.pending{color:#ffca82;border-color:#805b2f}.allowed{color:#97e4b8;border-color:#427957}.denied{color:#f8a3a3;border-color:#884444}.actions{min-width:240px}.scroll{overflow-x:auto}.alert{padding:12px;border:1px solid #7c6640;background:#3a3020}.error{border-color:#954945;background:#442626}.success{border-color:#3b7c5d;background:#21362c}code{color:#cee1fa} .small{font-size:12px} .history{font-family:ui-monospace,monospace;font-size:12px}
"""


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


def history_items_html():
    items = recent_history()
    out = []
    for e in items:
        term = e.get('query', e.get('url', ''))
        out.append(f'<tr><td class="small">{esc(e.get("time_utc",""))}</td><td>{esc(e.get("kind",""))}</td><td>{esc(e.get("status",""))}</td><td class="domain">{esc(str(term)[:180])}</td></tr>')
    return ''.join(out) or '<tr><td colspan="4">No MCP history yet.</td></tr>'


def recent_rows():
    return ('<div class="scroll"><table><thead><tr><th>UTC time</th><th>Operation</th><th>Status</th><th>Query or URL</th></tr></thead>'
            '<tbody id="live-history">' + history_items_html() + '</tbody></table></div>')


def live_snapshot(query='', view='policy'):
    """Order loopback snapshots across threads, including out-of-order delivery.

    Each policy snapshot has a strictly increasing, safely representable
    microsecond sequence number; browser tabs discard stale responses rather
    than regressing a newer shared pending-queue baseline.
    """
    if view == 'history':
        return {'history_html': history_items_html()}
    global _LAST_LIVE_SNAPSHOT_US
    with _LIVE_SNAPSHOT_LOCK:
        pending_rows = policy.entries(query, limit=200, status='pending')
        decisions = policy.entries(query, limit=2000, status='decided')
        domains = [row['domain'] for row in policy.entries(
            limit=policy.MAX_PENDING_TOTAL, status='pending')]
        counts = policy.counts()
        _LAST_LIVE_SNAPSHOT_US = max(
            time.time_ns() // 1000, _LAST_LIVE_SNAPSHOT_US + 1)
        return {
            'snapshot_run': _LIVE_SNAPSHOT_RUN,
            'snapshot_us': _LAST_LIVE_SNAPSHOT_US,
            'counts': counts,
            'pending_html': ''.join(row_html(r) for r in pending_rows)
                or '<tr><td colspan="4">No pending requests.</td></tr>',
            'decided_html': ''.join(row_html(r) for r in decisions)
                or '<tr><td colspan="4">No matching domains.</td></tr>',
            'audit_html': decision_audit_rows(query),
            'pending_domains': domains,
        }


def row_html(r):
    dom = esc(r['domain'])
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
        f'<div class="subtle">{esc(policy.safe_display_url(r["last_requested_url"]) if r.get("last_requested_url") else "—")}</div>')
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
                f'<input type="hidden" name="csrf" value="{token}">'
                f'<input type="hidden" name="domain" value="{dom}">'
                + controls +
                f'<button class="{cls}" type="submit" name="action" value="{name}">'
                f'{label}</button></form>')
    actions = (action('allow', 'Allow', 'allow') +
               action('deny', 'Deny', 'deny') + action('delete', 'Remove', 'delete'))
    edit = (f'<form method="post" action="/domain">'
            f'<input type="hidden" name="csrf" value="{token}">'
            f'<input type="hidden" name="domain" value="{dom}">'
            f'<label class="small">Reason (optional) '
            f'<input name="reason" maxlength="160" placeholder="Short local note"></label>'
            f'<input aria-label="Rename {dom}" name="new_domain" value="{dom}"'
            f' style="width:195px"><button name="action" value="rename">'
            f'Rename</button></form>')
    return (f'<tr><td class="domain">{dom}</td>'
            f'<td><span class="pill {stat}">{stat}</span>{limits}</td>'
            f'<td>{int(r["request_count"])}{request_meta}</td>'
            f'<td class="actions">{actions}'
            f'<div style="margin-top:7px">{edit}</div></td></tr>')


def decision_audit_rows(search=""):
    """Render escaped, filtered decision rows for initial and live views."""
    rows = policy.recent_decisions(search=search)
    cells = ''.join(
        '<tr><td class="small">' + esc(r['at']) + '</td>'
        '<td class="domain">' + esc(r['domain']) + '</td>'
        '<td>' + esc(r['action']) + '</td>'
        '<td class="small">' + esc(r['request_id'] or '—') + '</td>'
        '<td>' + esc(r['reason'] or '—') + '</td>'
        '<td>' + esc(policy.safe_display_url(r['requested_url']) if r['requested_url'] else '—') + '</td>'
        '<td>' + esc(r['first_requested_at'] or '—') + '</td>'
        '<td>' + esc(r['last_requested_at'] or '—') + '</td>'
        '<td>' + str(int(r['request_count'])) + '</td></tr>'
        for r in rows)
    return cells or '<tr><td colspan="9">No decisions recorded.</td></tr>'


def decision_audit_html(search=""):
    return ('<div class="scroll"><table><thead><tr><th>UTC time</th>'
            '<th>Domain</th><th>Action</th><th>Request ID</th>'
            '<th>Operator reason</th><th>Last requested URL</th>'
            '<th>First request</th><th>Last request</th><th>Count</th>'
            '</tr></thead><tbody id="live-audit">' +
            decision_audit_rows(search) + '</tbody></table></div>')


def render(query='', message='', kind='success'):
    c=policy.counts()
    # Separate independent limits: pending floods cannot hide stored decisions.
    pending=''.join(row_html(r) for r in policy.entries(query,limit=200,status='pending'))
    other=''.join(row_html(r) for r in policy.entries(query,limit=2000,status='decided'))
    alert=f'<p class="alert {"error" if kind=="error" else "success"}">{esc(message)}</p>' if message else ''
    table_head='<div class="scroll"><table><thead><tr><th>Domain</th><th>Decision / scope / expiry</th><th>Requests / latest</th><th>Manage</th></tr></thead>'
    query_escaped=esc(query)
    audit_html = decision_audit_html(query)
    return f'''<!doctype html><html lang="en" data-live-view="policy"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="referrer" content="no-referrer"><title>Local Search MCP — Domain approvals</title><style>{CSS}</style><script defer src="/dashboard-live.js"></script></head><body><header><div class="brand">LOCAL SEARCH MCP</div><nav><a href="/">Domain policy</a><a href="/history">Request history</a><a href="http://127.0.0.1:8080/ui">Tunnel diagnostics</a></nav></header>
<main><h1>Domain approvals</h1><p>Approve or deny domains requested by agents. Changes apply to the next fetch without restarting MCP. The dashboard is accessible only from this computer.</p>{alert}
<section class="stats"><div class="stat"><span>Awaiting approval</span><b id="live-count-pending">{c['pending']}</b></div><div class="stat"><span>Allowed</span><b id="live-count-allowed">{c['allowed']}</b></div><div class="stat"><span>Denied</span><b id="live-count-denied">{c['denied']}</b></div><div class="stat"><span>Dashboard</span><b style="font-size:14px">127.0.0.1:{PORT}</b></div></section>
<div class="panel"><h2 style="margin-top:0">Add domain</h2><form method="post" action="/domain"><input type="hidden" name="csrf" value="{esc(CSRF)}"><input name="domain" class="wide" placeholder="example.org" required maxlength="253" aria-label="New domain"><label>Scope <select name="scope"><option value="subdomains">Host + subdomains</option><option value="exact">Exact host only</option></select></label><label>Duration <select name="expires_minutes"><option value="0">Permanent</option><option value="60">1 hour</option><option value="1440">24 hours</option><option value="10080">7 days</option></select></label><label>Reason (optional) <input name="reason" maxlength="160" placeholder="Short local note"></label><button name="action" value="allow" class="allow">Add allowed</button><button name="action" value="deny" class="deny">Add denied</button></form><p class="subtle">Domains only (no URLs or IPs). Select exact host to exclude subdomains, and optionally set a 1-hour, 24-hour or 7-day expiry. Older rules remain permanent with subdomains. Never approve private-network addresses.</p></div>
<div class="row" style="justify-content:space-between"><h2>Requests awaiting review</h2><div><button type="button" id="notification-toggle">Enable desktop notifications</button> <a href="/">Refresh requests</a></div></div>
<p class="subtle" role="status" id="live-status">Live updates starting · manual refresh works without JavaScript.</p>
<p class="subtle">Showing at most 200 pending entries (filter to find specific hosts). Agent-created queue entries are limited to {policy.MAX_PENDING_TOTAL} total, {policy.MAX_NEW_PENDING_GLOBAL_PER_HOUR} globally per hour, {policy.MAX_PENDING_PER_SITE} per registrable domain and {policy.MAX_NEW_PENDING_PER_SITE_PER_HOUR} new hosts per site per hour. Manual allow/deny remains available even when the queue is full. Existing entries are never silently removed.</p>
{table_head}<tbody id="live-pending">{pending or '<tr><td colspan="4">No pending requests.</td></tr>'}</tbody></table></div>
<div class="panel"><h2 style="margin-top:0">Clear pending requests</h2>
<p>Removes <strong>all pending requests</strong>, including rows not currently shown or matching the filter. Existing allow/deny rules and the rolling-hour admission limits are not affected. Agents may need to retry after the rate limit expires.</p>
<form method="post" action="/pending/clear"><input type="hidden" name="csrf" value="{esc(CSRF)}"><label for="clear-confirm">Type CLEAR to confirm</label><input id="clear-confirm" name="confirm" placeholder="CLEAR" pattern="CLEAR" maxlength="5" required><button type="submit" class="delete">Clear all pending</button></form>
</div>
<div class="row" style="justify-content:space-between"><h2>Allowed and denied domains</h2><form method="get" action="/"><input name="q" value="{query_escaped}" placeholder="Filter by domain"><button type="submit">Filter</button></form></div>
{table_head}<tbody id="live-decided">{other or '<tr><td colspan="4">No matching domains.</td></tr>'}</tbody></table></div>
<h2>Recent local decisions</h2>
<p class="subtle">Only the most recent 25 actions; no page bodies or URL query strings.</p>
{audit_html}
<div class="panel"><h2 style="margin-top:0">Configuration and safety</h2><p>Search endpoint: <code>127.0.0.1:8888</code> · Crawl endpoint: <code>127.0.0.1:11235</code> · MCP: <code>127.0.0.1:8790/mcp</code> · Dashboard: <code>127.0.0.1:{PORT}</code>.</p><p>Domains are stored in <code>{esc(policy.database_path())}</code>. Your existing credentials stay in the launcher’s private configuration and are not displayed here.</p><p class="small">Unknown domains generate a pending request rather than a Python traceback. Only the user can approve them here; agents cannot invoke a tool to grant access. A domain's DNS is also checked for public addresses before an allowed fetch. Crawl4AI should still be isolated from your local network to guard against redirects and DNS rebinding.</p></div></main></body></html>'''


def history_page():
    return f'<!doctype html><html lang="en" data-live-view="history"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>Local Search MCP — History</title><style>{CSS}</style><script defer src="/dashboard-live.js"></script></head><body><header><div class="brand">LOCAL SEARCH MCP</div><nav><a href="/">Domain policy</a><a href="/history">Request history</a></nav></header><main><h1>Recent MCP requests</h1><p>The last 25 entries from your existing JSONL history. No API secrets or full fetched page contents are stored here.</p>{recent_rows()}<p class="subtle" role="status" id="live-status">Live updates starting · manually reload if needed.</p><p><a href="/">Return to domain approvals</a></p></main></body></html>'


class Handler(BaseHTTPRequestHandler):
    server_version='LocalSearchAdmin/1.0'
    def log_message(self, format, *args):
        pass
    def _host_valid(self):
        return self.headers.get('Host','') in (f'127.0.0.1:{PORT}',f'localhost:{PORT}')
    def _respond(self, status, content, content_type='text/html; charset=utf-8'):
        data=content.encode('utf-8') if isinstance(content,str) else content
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
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
            return self._respond(200,json.dumps(live_snapshot(q,view)),
                                 'application/json; charset=utf-8')
        if path.path=='/history':return self._respond(200,history_page())
        if path.path!='/':return self._respond(404,'Not found')
        q=parse_qs(path.query).get('q',[''])[0][:100]
        return self._respond(200,render(q))
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
        if self.path not in ('/domain', '/pending/clear'):return self._respond(404,'Not found')
        try:
            length=int(self.headers.get('Content-Length','-1'))
        except ValueError:return self._respond(400,'Invalid size')
        if length < 0 or length>4096:return self._respond(413,'Request too large')
        vals=parse_qs(self.rfile.read(length).decode('utf-8'),keep_blank_values=True)
        def val(key):return vals.get(key,[''])[0]
        if not secrets.compare_digest(val('csrf'),CSRF):return self._respond(403,'Invalid CSRF token')
        if self.path == '/pending/clear':
            if val('confirm') != 'CLEAR':
                return self._respond(400, render(message='Type CLEAR to confirm removal.', kind='error'))
            removed = policy.clear_pending_requests()
            return self._respond(200,render(message=f"Cleared {removed} pending requests."))
        try:
            policy.decide(val('domain'),val('action'),val('new_domain'),
                          scope=val('scope') or 'subdomains',
                          expires_minutes=int(val('expires_minutes') or '0'),
                          reason=val('reason'))
            msg=f"Decision updated for {val('domain')[:253]}"
            return self._respond(200,render(message=msg))
        except (ValueError,sqlite3.IntegrityError) as exc:
            return self._respond(400,render(message=str(exc),kind='error'))


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
