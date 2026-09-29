"""A portable HTML report. No JavaScript, external assets, or active content."""

from html import escape
import json
from urllib.parse import quote, urlparse

from .report import CATEGORIES, SECTIONS


def text(value):
    return escape(str(value if value is not None else "not observed"), quote=True)


def link(url, label):
    try:
        parsed = urlparse(str(url))
    except ValueError:
        return text(label)
    if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.username or parsed.password:
        return text(label)
    return f'<a href="{text(url)}" rel="noreferrer">{text(label)}</a>'


def source(report, kind, result_id):
    if not result_id:
        return "not observed"
    endpoint = "build" if kind == "build" else "test"
    url = report["dashboard_api"].rstrip("/") + f"/{endpoint}/{quote(str(result_id), safe='')}"
    return link(url, result_id)


def render_html(report, rows_per_category=50):
    selection = report["selection"]
    comparison = report.get("comparison") or {"items": [], "counts": {}}
    counts = comparison["counts"]
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
             '<title>KernelCI release comparison</title><style>',
             'body{font:16px/1.55 system-ui,sans-serif;color:#172b3a;background:#f1f5f8;margin:0}',
             'main{max-width:1200px;margin:auto;padding:36px 24px}h1{font-size:36px;line-height:1.2;margin:8px 0 18px}',
             'h2{margin:0 0 16px;font-size:23px}h3{font-size:18px}.eyebrow{color:#526a7c;letter-spacing:.1em;font-size:12px;font-weight:700}',
             '.panel{background:white;border:1px solid #d9e1e8;border-radius:12px;padding:22px;margin:20px 0}',
             '.status{background:#fff6dc;border:1px solid #dfb965;border-radius:10px;padding:18px;margin:22px 0}',
             '.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.card{background:white;border:1px solid #d9e1e8;border-radius:10px;padding:16px}',
             '.card strong{display:block;font-size:28px}.muted,small{color:#526a7c}a{color:#075b80;text-underline-offset:3px}',
             'code,pre{font-family:ui-monospace,monospace;font-size:12px}code{overflow-wrap:anywhere}',
             'pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f5f8;padding:14px;border-radius:6px;max-height:500px;overflow:auto}',
             '.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:12px 10px;text-align:left;vertical-align:top;border-bottom:1px solid #e3e9ef}',
             'th{background:#f6f8fa}td{overflow-wrap:anywhere;min-width:95px}.identity{min-width:220px}summary{cursor:pointer;font-weight:650}',
             'dl{display:grid;grid-template-columns:100px 1fr;gap:8px}dt{color:#526a7c}dd{margin:0;overflow-wrap:anywhere}li{margin:8px 0}',
             '@media(max-width:650px){main{padding:22px 14px}h1{font-size:28px}.cards{grid-template-columns:repeat(2,1fr)}.panel{padding:16px}}',
             '@media print{body{background:white}.panel,.card{break-inside:avoid}pre{max-height:none}.scroll{overflow:visible}a{color:inherit}}',
             '</style></head><body><main>',
             '<div class="eyebrow">KERNELCI / RELEASE REVIEW</div>',
             '<h1>What changed between these revisions?</h1>',
             f'<p class="muted">Collected {text(report["finished_at"])} · kci-dev {text(report["kci_dev_version"])}</p>',
             f'<div class="status"><strong>{text(report["assessment"]["status"].replace("_", " "))}</strong>',
             '<br>Required coverage and pending jobs have not been assessed. This report does not approve a release.</div>',
             '<section class="panel"><h2>Compared checkouts</h2><dl>']
    for name in ("giturl", "branch", "origin", "base", "head"):
        parts.append(f'<dt>{text(name)}</dt><dd><code>{text(selection[name])}</code></dd>')
    parts.extend(['</dl><p>All reported changes and raw selected evidence: <a href="report.json">report.json</a>.</p></section>',
                  '<div class="cards">'])
    for category in CATEGORIES:
        parts.append(f'<div class="card"><span>{text(category.replace("_", " ").title())}</span><strong>{text(counts.get(category, "unavailable"))}</strong></div>')
    parts.append('</div><section class="panel"><h2>Evidence and scope</h2><ul>')
    for note in report["errors"] + report["incomplete_reasons"] + report["notes"]:
        parts.append(f'<li>{text(note)}</li>')
    parts.append('</ul><div class="scroll"><table><thead><tr><th>Checkout</th><th>Builds</th><th>Boots</th><th>Tests</th></tr></thead><tbody>')
    for side in ("base", "head"):
        parts.append(f'<tr><th>{text(side.title())}</th>')
        for section in SECTIONS:
            observed = report["observations"][side][section]
            status_text = ", ".join(f"{key}: {value}" for key, value in sorted(observed["statuses"].items()))
            value = f'{observed["total"]} executions' if observed["fetched"] else "unavailable"
            parts.append(f'<td>{text(value)}<br><small>{text(status_text or "no outcomes")}</small></td>')
        parts.append('</tr>')
    parts.append('</tbody></table></div></section>')

    for category in CATEGORIES:
        items = [item for item in comparison["items"] if item["category"] == category]
        parts.append(f'<section class="panel"><h2>{text(category.replace("_", " ").title())} <small>({len(items)})</small></h2>')
        if not items:
            parts.append('<p class="muted">No entries returned in this category.</p></section>')
            continue
        if len(items) > rows_per_category:
            parts.append(f'<p>Showing {rows_per_category} of {len(items)} entries. All entries are preserved in report.json.</p>')
        parts.append('<div class="scroll"><table><thead><tr><th>Result identity</th><th>Base</th><th>Head</th><th>Known issues at head</th></tr></thead><tbody>')
        for item in items[:rows_per_category]:
            ident = item["identity"]
            kind = "build" if ident["kind"] == "build" else "test"
            issue = report["issues"]["lookups"].get(f'{kind}:{item.get("head_id")}')
            if issue and issue["state"] == "fetched":
                ids = item.get("known_issues", [])
                issue_text = ", ".join(map(str, ids)) if ids else "No associated issue returned"
            elif issue:
                issue_text = issue.get("error", issue["state"])
            else:
                issue_text = "Not checked for this entry"
            parts.append('<tr><td class="identity">')
            parts.append(f'<strong>{text(ident.get("path"))}</strong><br><small>')
            parts.append(text(" · ".join(str(ident.get(key, "unknown")) for key in ("kind", "origin", "platform", "architecture", "compiler", "config"))))
            parts.append(f'<br>Occurrence {text(item.get("occurrence", 0))}</small></td>')
            for side in ("base", "head"):
                parts.append(f'<td><strong>{text(item.get(side + "_status"))}</strong><br><code>{source(report, kind, item.get(side + "_id"))}</code></td>')
            parts.append(f'<td>{text(issue_text)}</td></tr>')
        parts.append('</tbody></table></div></section>')

    parts.append('<section class="panel"><h2>Selected failure evidence</h2>')
    if not report["evidence"]:
        parts.append('<p class="muted">No supplemental details or logs were fetched.</p>')
    for entry in report["evidence"].values():
        parts.append(f'<h3>{source(report, entry["kind"], entry["result_id"])}</h3>')
        for error in entry["errors"]:
            parts.append(f'<p>{text(error)}</p>')
        details = entry.get("details", {})
        if details.get("log_url"):
            parts.append(f'<p>{link(details["log_url"], "Original log")}</p>')
        if entry.get("log_note"):
            parts.append(f'<p class="muted">{text(entry["log_note"])}</p>')
        log = entry.get("log")
        if log:
            fields = ("source", "returned_bytes", "total_bytes", "truncated", "scan_limited", "deadline_exceeded")
            parts.append('<p class="muted">' + text(" · ".join(f'{key}: {log.get(key, "unknown")}' for key in fields)) + '</p>')
            parts.append(f'<pre>{text(log["text"])}</pre>')
        if details:
            parts.append(f'<details><summary>Result details</summary><pre>{text(json.dumps(details, indent=2, ensure_ascii=False))}</pre></details>')
    parts.append('</section><p class="muted">Source links open the selected dashboard API. Unchanged results contribute to the observed counts but do not appear in the change tables.</p></main></body></html>')
    return "\n".join(parts)
