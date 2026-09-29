"""Run configured comparisons and assemble a static GitHub Pages artifact."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlparse

from .html_report import text
from .report import CATEGORIES, validate_selection
from .client import DEFAULT_HISTORY_HOURS

SELECTION_FIELDS = ("giturl", "branch", "origin", "base", "head")
LIMITS = {
    "history_hours": (DEFAULT_HISTORY_HOURS, 720),
    "max_issue_lookups": (20, 1000),
    "max_evidence": (3, 100),
    "log_bytes": (8192, 1_048_576),
}
STATUSES = {
    0: "NO_REVIEW_SIGNALS_IN_OBSERVED_RESULTS",
    1: "REVIEW_REQUIRED",
    2: "EVIDENCE_INCOMPLETE",
}


def load_comparisons(path):
    """Validate the entire manifest before making requests or writing files."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_comparisons(document)


def validate_comparisons(document):
    if not isinstance(document, dict) or set(document) != {"comparisons"}:
        raise ValueError("Configuration must contain only a comparisons array")
    rows = document["comparisons"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("Configure at least one comparison")
    allowed = set(SELECTION_FIELDS) | set(LIMITS) | {
        "id", "title", "dashboard_api", "include_issues",
    }
    comparisons, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - allowed:
            raise ValueError("Comparison must be an object with supported fields only")
        entry = {"origin": "maestro", "include_issues": True, **row}
        slug = entry.get("id")
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise ValueError("Comparison id must contain lowercase letters, digits and single hyphens")
        if len(slug) > 64 or slug in seen:
            raise ValueError("Comparison ids must be unique and at most 64 characters")
        seen.add(slug)
        entry.setdefault("title", slug)
        if not isinstance(entry["title"], str) or not entry["title"].strip():
            raise ValueError(f"{slug}: title must be a nonempty string")
        if any(not isinstance(entry.get(key), str) for key in SELECTION_FIELDS):
            raise ValueError(f"{slug}: selection fields must be strings")
        validate_selection(entry)
        for key in ("base", "head"):
            entry[key] = entry[key].lower()
        if type(entry["include_issues"]) is not bool:
            raise ValueError(f"{slug}: include_issues must be true or false")
        for key, (default, maximum) in LIMITS.items():
            value = entry.setdefault(key, default)
            minimum = 1 if key == "history_hours" else 0
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{slug}: {key} must be an integer from {minimum} to {maximum}")
        if "dashboard_api" in entry:
            endpoint = entry["dashboard_api"]
            if not isinstance(endpoint, str):
                raise ValueError(f"{slug}: dashboard_api must be an HTTP(S) URL")
            url = urlparse(endpoint)
            if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password:
                raise ValueError(f"{slug}: dashboard_api must be an HTTP(S) URL without credentials")
        comparisons.append(entry)
    return comparisons


def run_comparison(entry, destination, *, timeout=900):
    """Keep exit codes 0/1/2 only when matching, complete report files exist."""
    command = [sys.executable, "-m", "kci_release_review", "compare"]
    for key in SELECTION_FIELDS:
        command.append(f"--{key}={entry[key]}")
    for key in LIMITS:
        command.append(f"--{key.replace('_', '-')}={entry.get(key, LIMITS[key][0])}")
    if "dashboard_api" in entry:
        command.append(f"--dashboard-api={entry['dashboard_api']}")
    if not entry["include_issues"]:
        command.append("--no-issues")
    command.append(f"--out={destination}")
    # No shell interpolation, and no --force: stale reports must never be reused.
    completed = subprocess.run(command, check=False, timeout=timeout)
    if completed.returncode not in STATUSES:
        raise RuntimeError(f"{entry['id']}: comparison exited with {completed.returncode}")
    return load_result(entry, destination, completed.returncode)


def load_result(entry, destination, exit_code):
    """Validate newly generated or restored report files for this selection."""
    html_path, json_path = destination / "report.html", destination / "report.json"
    if not html_path.is_file() or not html_path.stat().st_size or not json_path.is_file():
        raise RuntimeError(f"{entry['id']}: comparison did not write both report files")
    report = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        raise ValueError(f"{entry['id']}: unsupported report schema")
    assessment = report.get("assessment")
    if (not isinstance(assessment, dict)
            or type(assessment.get("exit_code")) is not int
            or assessment["exit_code"] != exit_code
            or assessment.get("status") != STATUSES[exit_code]):
        raise ValueError(f"{entry['id']}: report assessment does not match the process exit code")
    selection = {key: entry[key] for key in SELECTION_FIELDS}
    if report.get("selection") != selection:
        raise ValueError(f"{entry['id']}: report describes a different comparison")
    comparison = report.get("comparison")
    if comparison is None:
        if exit_code != 2:
            raise ValueError(f"{entry['id']}: missing comparison without an incomplete assessment")
        counts = None
    else:
        counts = comparison.get("counts") if isinstance(comparison, dict) else None
        if (not isinstance(counts, dict) or set(counts) != set(CATEGORIES)
                or any(type(value) is not int or value < 0 for value in counts.values())):
            raise ValueError(f"{entry['id']}: invalid comparison counts")
    diagnostics = {key: report.get(key, []) for key in ("incomplete_reasons", "errors")}
    if any(not isinstance(values, list) or any(not isinstance(value, str) for value in values)
           for values in diagnostics.values()):
        raise ValueError(f"{entry['id']}: invalid report diagnostics")
    return {
        "id": entry["id"], "title": entry["title"], "selection": selection,
        "status": assessment["status"], "exit_code": exit_code,
        "counts": counts, "finished_at": report.get("finished_at"),
        "report_html": f"{entry['id']}/report.html",
        "report_json": f"{entry['id']}/report.json",
        **diagnostics,
    }


def render_index(summary):
    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        'style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
        '<title>KernelCI release comparisons</title><style>',
        'body{font:16px/1.55 system-ui,sans-serif;color:#172b3a;background:#f1f5f8;margin:0}',
        'main{max-width:1100px;margin:auto;padding:32px 20px}h1{line-height:1.2}',
        'section{background:white;border:1px solid #d9e1e8;border-radius:12px;padding:22px;margin:24px 0}',
        'h2{margin-top:0}a{color:#075b80;text-underline-offset:3px}',
        'code{overflow-wrap:anywhere;font-size:13px}.muted{color:#526a7c}',
        '.status{border-left:4px solid #ba841d;padding:10px;background:#fff6dc}',
        'dl{display:grid;grid-template-columns:90px 1fr;gap:8px}dd{margin:0;overflow-wrap:anywhere}',
        '.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%}',
        'th,td{text-align:left;padding:10px;border-bottom:1px solid #d9e1e8}',
        '@media(max-width:600px){main{padding:18px 12px}section{padding:16px}}',
        '</style></head><body><main><h1>KernelCI release comparisons</h1>',
        f'<p class="muted">Updated {text(summary["generated_at"])} (UTC).</p>',
        '<p>These reports describe observed test executions. Required coverage, pending jobs '
        'and release policy are not assessed. A successful publication does not approve a release.</p>',
        '<p><a href="summary.json">Download the comparison index as JSON</a></p>',
    ]
    if summary.get("release_watch"):
        parts.append('<p><a href="release-state.json">Release discovery and retry state</a></p>')
        for message in summary.get("source_errors", []):
            parts.append(f'<p class="status"><strong>Discovery error:</strong> {text(message)}</p>')
        for message in summary.get("notices", []):
            parts.append(f'<p class="muted">{text(message)}</p>')
    for entry in summary["comparisons"]:
        parts.extend([
            f'<section><h2>{text(entry["title"])}</h2>',
            f'<p class="status"><strong>{text(entry["status"].replace("_", " "))}</strong>'
            f' (comparison exit code {entry["exit_code"]})</p>',
        ])
        if entry.get("watch_status"):
            parts.append(f'<p><strong>{text(entry["watch_status"].replace("_", " "))}</strong>: '
                         f'{text(entry.get("watch_note", ""))}</p>')
        reasons = entry.get("errors", []) + entry.get("incomplete_reasons", [])
        if reasons:
            parts.append('<p><strong>Why this comparison is incomplete:</strong></p><ul>')
            parts.extend(f'<li>{text(reason)}</li>' for reason in reasons)
            parts.append('</ul>')
        parts.append('<dl>')
        for key in SELECTION_FIELDS:
            parts.append(f'<dt>{text(key)}</dt><dd><code>{text(entry["selection"][key])}</code></dd>')
        parts.append('</dl><div class="scroll"><table><thead><tr>')
        for category in CATEGORIES:
            parts.append(f'<th scope="col">{text(category.replace("_", " ").title())}</th>')
        parts.append('</tr></thead><tbody><tr>')
        for category in CATEGORIES:
            count = (entry["counts"] or {}).get(category, "unavailable")
            parts.append(f'<td>{text(count)}</td>')
        parts.append('</tr></tbody></table></div>')
        if entry.get("report_html"):
            parts.append(f'<p><a href="{text(entry["report_html"])}">Read the comparison report</a> · '
                         f'<a href="{text(entry["report_json"])}">Download full JSON</a></p>')
        else:
            parts.append('<p>No report yet. This comparison remains queued.</p>')
        parts.append('</section>')
    parts.append('</main></body></html>')
    return "\n".join(parts) + "\n"


def build_site(comparisons, destination):
    destination = Path(destination).resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("Output directory must be empty; choose a new --out directory")
    destination.mkdir(parents=True, exist_ok=True)
    entries = []
    for entry in comparisons:
        print(f"Running comparison: {entry['id']}", flush=True)
        entries.append(run_comparison(entry, destination / entry["id"]))
    summary = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "comparisons": entries,
    }
    # Only publish an index once every configured comparison produced valid files.
    (destination / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (destination / "index.html").write_text(render_index(summary), encoding="utf-8")
    return summary


def write_actions_summary(summary, path):
    rows = ["## KernelCI release comparisons\n\n",
            "Publication success is not release approval. Reports are in the `github-pages` artifact.\n\n",
            "| Comparison | Assessment | CLI exit code |\n|---|---|---|\n"]
    for entry in summary["comparisons"]:
        # IDs and statuses have been validated, so no remote text enters Markdown.
        rows.append(f"| {entry['id']} | {entry['status']} | {entry['exit_code']} |\n")
    if summary.get("release_watch"):
        rows.append("\n### Release discovery and retries\n\n")
        for message in summary.get("source_errors", []):
            rows.append(f"<p><strong>Discovery error:</strong> {text(message)}</p>\n")
        for message in summary.get("notices", []):
            rows.append(f"<p>{text(message)}</p>\n")
        for entry in summary["comparisons"]:
            if entry.get("watch_status"):
                rows.append(f"<p><code>{text(entry['id'])}</code>: <strong>{text(entry['watch_status'])}</strong> "
                            f"{text(entry.get('watch_note', ''))}</p>\n")
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write("".join(rows))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("comparisons.json"))
    parser.add_argument("--out", type=Path, default=Path("_site"))
    parser.add_argument("--check", action="store_true", help="Validate config without requests or output files")
    args = parser.parse_args(argv)
    try:
        comparisons = load_comparisons(args.config)
        if args.check:
            print(f"Validated {len(comparisons)} comparison(s)")
            return 0
        summary = build_site(comparisons, args.out)
        for entry in summary["comparisons"]:
            message = f"{entry['id']}: {entry['status']} (CLI exit code {entry['exit_code']})"
            if os.environ.get("GITHUB_ACTIONS") == "true" and entry["exit_code"]:
                print(f"::warning::{message}")
            else:
                print(message)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            write_actions_summary(summary, os.environ["GITHUB_STEP_SUMMARY"])
        print(f"Site written to {args.out}")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Pages build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
