"""Run with python -m kci_release_review."""

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

from .client import make_client
from .html_report import render_html
from .report import collect_report, validate_selection


def bounded(low, high):
    def convert(value):
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return number
    return convert


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, help="Explicit kci-dev TOML config")
    common.add_argument("--instance", help="Instance in --config")
    common.add_argument("--dashboard-api", help="Dashboard API base URL override")
    common.add_argument("--origin", default="maestro")
    root = argparse.ArgumentParser(description="Compare KernelCI evidence for two tested revisions.")
    sub = root.add_subparsers(dest="command", required=True)
    trees = sub.add_parser("trees", parents=[common], help="Discover available trees and tested hashes")
    trees.add_argument("--days", type=bounded(1, 30), default=7)
    history = sub.add_parser("history", parents=[common], help="List checkouts preceding a tested head")
    history.add_argument("--giturl", required=True)
    history.add_argument("--branch", required=True)
    history.add_argument("--head", required=True)
    compare = sub.add_parser("compare", parents=[common], help="Write report.json and report.html")
    for name in ("giturl", "branch", "base", "head"):
        compare.add_argument("--" + name, required=True)
    compare.add_argument("--out", type=Path, default=None, help="Output directory (default: timestamped reports directory)")
    compare.add_argument("--force", action="store_true", help="Replace existing report files in --out")
    compare.add_argument("--no-issues", action="store_true", help="Disable supplemental known-issue requests")
    compare.add_argument("--max-issue-lookups", type=bounded(0, 1000), default=20)
    compare.add_argument("--max-evidence", type=bounded(0, 100), default=5, help="Maximum failing result IDs expanded")
    compare.add_argument("--log-bytes", type=bounded(0, 1_048_576), default=16_384, help="Bytes per test log; 0 disables downloads")
    return root


def write_report(report, destination):
    destination.mkdir(parents=True, exist_ok=True)
    outputs = {
        "report.json": json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        "report.html": render_html(report),
    }
    for name, contents in outputs.items():
        fd, temporary = tempfile.mkstemp(prefix=".review-", dir=destination)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(contents)
            os.replace(temporary, destination / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        out = None
        selection = None
        if args.command == "compare":
            selection = {name: getattr(args, name) for name in ("giturl", "branch", "origin", "base", "head")}
            validate_selection(selection)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            out = (args.out or Path("reports") / stamp).expanduser().resolve()
            if not args.force and any((out / name).exists() for name in ("report.json", "report.html")):
                raise ValueError("Report files already exist; choose another --out or use --force")
        with redirect_stdout(sys.stderr):
            client = make_client(args.config, args.instance, args.dashboard_api,
                                 getattr(args, "max_issue_lookups", 20))
            if args.command == "trees":
                result = client.get_tree_list(origin=args.origin, days=args.days)
            elif args.command == "history":
                result = client.get_commits_history(origin=args.origin, giturl=args.giturl,
                                                    branch=args.branch, commit=args.head)
            else:
                result = collect_report(client, selection, include_issues=not args.no_issues,
                                        max_evidence=args.max_evidence, log_bytes=args.log_bytes,
                                        progress=lambda message: print(message, file=sys.stderr))
        if args.command == "compare":
            write_report(result, out)
            print(json.dumps({"status": result["assessment"]["status"],
                              "report_json": str(out / "report.json"),
                              "report_html": str(out / "report.html"),
                              "counts": (result.get("comparison") or {}).get("counts", {})}, indent=2))
            return result["assessment"]["exit_code"]
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
