"""Discover released kernels, retain a retry queue, and publish KernelCI reviews."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlparse

from . import pages
from .releases import adjacent_releases, discover_sources, fetch_bytes

SCHEMA = "kci-release-watch-1"
FILES = ("report.html", "report.json")
WATCH_STATUSES = {"QUEUED", "WAITING_FOR_KERNELCI", "EVIDENCE_INCOMPLETE", "RETRY_ERROR", "COMPARED"}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    allowed = {"origin", "dashboard_api", "include_issues", *pages.LIMITS,
               "max_comparisons_per_run", "comparison_timeout_seconds",
               "time_budget_seconds", "overrides"}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError("Invalid release-watch configuration fields")
    config = {"origin": "maestro", "include_issues": False, "max_issue_lookups": 0,
              "max_evidence": 0, "log_bytes": 0,
              "history_hours": pages.LIMITS["history_hours"][0], "max_comparisons_per_run": 24,
              "comparison_timeout_seconds": 180, "time_budget_seconds": 2400,
              "overrides": {}, **config}
    for key, low, high in (("max_comparisons_per_run", 1, 100),
                           ("comparison_timeout_seconds", 30, 900),
                           ("time_budget_seconds", 30, 10800)):
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}")
    if not isinstance(config["overrides"], dict):
        raise ValueError("overrides must map stream IDs to KernelCI query settings")
    for stream_id, override in config["overrides"].items():
        if not re.fullmatch(r"[a-z0-9-]+", stream_id) or not isinstance(override, dict):
            raise ValueError("Invalid stream override")
        if set(override) - {"origin", "giturl", "branch", "dashboard_api"}:
            raise ValueError("Overrides only support origin, giturl, branch and dashboard_api")
    # Reuse CLI selection/budget validation, including dashboard URL checks.
    dummy = {"id": "check", "giturl": "https://example.test/linux.git", "branch": "main",
             "base": "a" * 40, "head": "b" * 40}
    options = query_options(config)
    pages.validate_comparisons({"comparisons": [{**dummy, **options}]})
    for override in config["overrides"].values():
        pages.validate_comparisons({"comparisons": [{**dummy, **options, **override}]})
    return config


def query_options(config):
    return {key: config[key] for key in ("origin", "dashboard_api", "include_issues", *pages.LIMITS)
            if key in config}


class PreviousSite:
    """Read a deployed snapshot. Only a missing state file bootstraps a new queue."""
    def __init__(self, directory=None, url=None):
        self.directory = Path(directory) if directory is not None else None
        self.url = url.rstrip("/") if url else None
        if self.url:
            parsed = urlparse(self.url)
            if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
                    or parsed.query or parsed.fragment):
                raise ValueError("Previous site URL must be HTTPS without credentials, query or fragment")

    def read(self, path, limit):
        if self.directory is not None:
            target = self.directory / path
            if target.is_symlink():
                raise ValueError("Previous site must not contain symlinked files")
            data = target.read_bytes()
            if len(data) > limit:
                raise ValueError(f"Previous file is too large: {path}")
            return data
        if self.url:
            return fetch_bytes(self.url + "/" + path, limit=limit)
        raise FileNotFoundError(path)

    def state(self):
        try:
            data = self.read("release-state.json", 16_000_000)
        except FileNotFoundError:
            return {"schema": SCHEMA, "initialized_at": timestamp(), "streams": {}, "releases": {}}
        except HTTPError as exc:
            if exc.code != 404:
                raise
            return {"schema": SCHEMA, "initialized_at": timestamp(), "streams": {}, "releases": {}}
        state = json.loads(data)
        if (not isinstance(state, dict) or state.get("schema") != SCHEMA
                or not isinstance(state.get("streams"), dict) or not isinstance(state.get("releases"), dict)):
            raise ValueError("Previous release state is malformed; refusing to reset it")
        for stream_id, cursor in state["streams"].items():
            if (not re.fullmatch(r"[a-z0-9-]+", stream_id) or not isinstance(cursor, dict)
                    or not isinstance(cursor.get("latest"), str)
                    or not re.fullmatch(r"[0-9a-f]{40}", cursor.get("commit", ""))):
                raise ValueError("Invalid saved stream cursor")
        for key, job in state["releases"].items():
            if not isinstance(job, dict) or not isinstance(job.get("config"), dict):
                raise ValueError("Invalid saved release")
            validated = pages.validate_comparisons({"comparisons": [job["config"]]})[0]
            if validated["id"] != key or job.get("stream") not in state["streams"]:
                raise ValueError("Saved release ID or stream does not match its configuration")
            release_selection = job.get("release_selection")
            if (not isinstance(release_selection, dict) or set(release_selection) != {"giturl", "branch"}
                    or any(not isinstance(value, str) or not value for value in release_selection.values())):
                raise ValueError("Invalid authoritative release selection")
            if not isinstance(job.get("head_tag"), str) or not isinstance(job.get("base_tag"), str):
                raise ValueError("Saved release is missing tag names")
            if job.get("watch_status") not in WATCH_STATUSES or not isinstance(job.get("discovered_at"), str):
                raise ValueError("Invalid saved tracker status")
            if type(job.get("attempts")) is not int or job["attempts"] < 0:
                raise ValueError("Invalid saved attempt count")
            if job.get("last_attempt") is not None and not isinstance(job["last_attempt"], str):
                raise ValueError("Invalid saved attempt timestamp")
            if "files" in job:
                if (not isinstance(job["files"], dict) or set(job["files"]) != set(FILES)
                        or any(not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                               for digest in job["files"].values())
                        or not isinstance(job.get("summary"), dict)
                        or type(job["summary"].get("exit_code")) is not int
                        or job["summary"].get("exit_code") not in pages.STATUSES):
                    raise ValueError("Invalid saved report metadata")
            elif job.get("summary"):
                raise ValueError("Saved summary has no matching report files")
            elif job["watch_status"] not in {"QUEUED", "RETRY_ERROR"}:
                raise ValueError("Saved assessment has no report files")
        return state

    def restore(self, state, destination):
        for key, job in state["releases"].items():
            if "files" not in job:
                continue
            target = destination / key
            target.mkdir()
            for name, digest in job["files"].items():
                data = self.read(f"{key}/{name}", 128_000_000)
                if hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError(f"Previous report checksum mismatch: {key}/{name}")
                (target / name).write_bytes(data)
            job["summary"] = pages.load_result(job["config"], target, job["summary"]["exit_code"])


def queue_releases(state, streams, catalogs, config, now):
    """Advance discovery independently of CI availability; retain every new pair."""
    errors = []
    for stream in streams:
        stream_id = stream["id"]
        tags = catalogs[stream["giturl"]]
        cursor = state["streams"].get(stream_id)
        try:
            if cursor and tags.get(cursor["latest"]) != cursor["commit"]:
                raise ValueError(f"Previously observed tag moved or disappeared: {cursor['latest']}")
            pairs = adjacent_releases(stream, tags, cursor["latest"] if cursor else None)
            jobs = {}
            for base_tag, head_tag in pairs:
                key = "release-" + stream_id + "-" + head_tag.replace(".", "-")
                entry = {"id": key, "title": f"{stream['branch']}: {base_tag} to {head_tag}",
                         "giturl": stream["giturl"], "branch": stream["branch"],
                         "base": tags[base_tag], "head": tags[head_tag], **query_options(config),
                         **config["overrides"].get(stream_id, {})}
                entry = pages.validate_comparisons({"comparisons": [entry]})[0]
                jobs[key] = {"stream": stream_id, "source": stream["source"], "base_tag": base_tag,
                             "release_selection": {"giturl": stream["giturl"], "branch": stream["branch"]},
                             "head_tag": head_tag, "config": entry, "discovered_at": now,
                             "attempts": 0, "last_attempt": None, "watch_status": "QUEUED",
                             "watch_note": "Awaiting a KernelCI comparison."}
            # Do not partially advance a stream if its validation failed.
            for key, job in jobs.items():
                state["releases"].setdefault(key, job)
            state["streams"][stream_id] = {"latest": stream["latest"], "commit": tags[stream["latest"]]}
        except ValueError as exc:
            errors.append(f"{stream_id}: {exc}")
    # Apply explicit query overrides to pending/older releases as well.
    for job in state["releases"].values():
        defaults = {**job["config"], **job["release_selection"]}
        defaults.pop("dashboard_api", None)
        settings = {**defaults, **query_options(config), **config["overrides"].get(job["stream"], {})}
        settings = pages.validate_comparisons({"comparisons": [settings]})[0]
        if settings != job["config"]:
            # Keep the old report under its original selection until replacement succeeds.
            job["next_config"] = settings
        else:
            job.pop("next_config", None)
    return errors


def retry_candidates(state):
    jobs = []
    for key, job in state["releases"].items():
        latest = state["streams"][job["stream"]]["latest"] == job["head_tag"]
        if latest or job["watch_status"] != "COMPARED" or "next_config" in job:
            jobs.append((key, job))
    # Never-attempted releases first, then least recently attempted retries.
    return sorted(jobs, key=lambda pair: (pair[1]["last_attempt"] or "", pair[1]["discovered_at"], pair[0]))


def describe_evidence(report):
    if report.get("errors"):
        return "RETRY_ERROR", "KernelCI request or comparison failed; retry remains queued."
    for side in ("base", "head"):
        sections = report["observations"][side].values()
        completed = sum(section["statuses"].get(status, 0)
                        for section in sections for status in ("PASS", "FAIL", "ERROR"))
        if not completed:
            return "WAITING_FOR_KERNELCI", f"No completed outcomes for the exact {side} release commit; retry remains queued."
    if report["assessment"]["exit_code"] == 2:
        if report.get("history_lookup", {}).get("state") == "error":
            return "EVIDENCE_INCOMPLETE", "Exact-commit results available, but tree history is unavailable or mismatched; retry remains queued."
        return "EVIDENCE_INCOMPLETE", "Comparison available, but evidence is incomplete; retry remains queued."
    return "COMPARED", "Observed results compared. Required coverage and release approval are not assessed."


def process_queue(state, destination, config, deadline):
    attempted = 0
    for key, job in retry_candidates(state):
        if attempted >= config["max_comparisons_per_run"]:
            break
        remaining = deadline - time.monotonic()
        if remaining < config["comparison_timeout_seconds"] + 15:
            break
        attempted += 1
        job["attempts"] += 1
        job["last_attempt"] = timestamp()
        print(f"Comparing {job['base_tag']} -> {job['head_tag']} ({job['stream']})", flush=True)
        entry = job.get("next_config", job["config"])
        try:
            with tempfile.TemporaryDirectory(prefix="release-review-") as temporary:
                path = Path(temporary)
                result = pages.run_comparison(entry, path, timeout=config["comparison_timeout_seconds"])
                report = json.loads((path / "report.json").read_text(encoding="utf-8"))
                status, note = describe_evidence(report)
                if report.get("errors") and (job.get("summary") or {}).get("counts") is not None:
                    job.update(watch_status=status, watch_note=note + " The earlier report is retained below.")
                    continue
                target = destination / key
                target.mkdir(exist_ok=True)
                for name in FILES:
                    shutil.copyfile(path / name, target / name)
                job.update(config=entry, summary=result, watch_status=status, watch_note=note,
                           files={name: hashlib.sha256((target / name).read_bytes()).hexdigest() for name in FILES})
                job.pop("next_config", None)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            job.update(watch_status="RETRY_ERROR", watch_note=f"{type(exc).__name__}: {exc}. Retry remains queued.")
    return attempted


def site_summary(state, manual):
    entries = list(manual)
    for key, job in reversed(list(state["releases"].items())):
        entry = job.get("summary") or {
            "id": key, "title": job["config"]["title"], "status": "NOT_COMPARED",
            "exit_code": "not run", "counts": None,
            "selection": {field: job["config"][field] for field in pages.SELECTION_FIELDS},
        }
        note = job["watch_note"]
        if job.get("summary"):
            note += " Report collected: " + str(job["summary"].get("finished_at")) + "."
        entries.append({**entry, "watch_status": job["watch_status"], "watch_note": note,
                        "attempts": job["attempts"], "last_attempt": job["last_attempt"]})
    return {"schema_version": 1, "generated_at": timestamp(), "release_watch": True,
            "comparisons": entries, "source_errors": state["source_errors"], "notices": state["notices"]}


def build_daily(config, destination, previous, manual=()):
    start = time.monotonic()
    state = deepcopy(previous.state())
    streams, catalogs, notices, errors = discover_sources()
    errors.extend(queue_releases(state, streams, catalogs, config, timestamp()))
    state.update(source_errors=errors, notices=notices)
    destination = Path(destination).resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("Output directory must be new or empty")
    if any(entry["id"] in state["releases"] for entry in manual):
        raise ValueError("Manual comparison ID collides with a discovered release")
    destination.mkdir(parents=True, exist_ok=True)
    previous.restore(state, destination)
    manual_results = [pages.run_comparison(entry, destination / entry["id"],
                                          timeout=config["comparison_timeout_seconds"]) for entry in manual]
    attempted = process_queue(state, destination, config, start + config["time_budget_seconds"])
    state["updated_at"] = timestamp()
    summary = site_summary(state, manual_results)
    summary["notices"] = [*summary["notices"],
                          f"Attempted {attempted} automatic comparisons this run. Queued, missing and incomplete evidence is retried on later runs.",
                          "The newest release in each stream is refreshed on every daily run, subject to the request/time budget."]
    for name, value in (("release-state.json", state), ("summary.json", summary)):
        (destination / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (destination / "index.html").write_text(pages.render_index(summary), encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("release-watch.json"))
    parser.add_argument("--out", type=Path, default=Path("_site"))
    parser.add_argument("--manual-config", type=Path)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--previous-site", type=Path)
    source.add_argument("--site-url")
    parser.add_argument("--check", action="store_true", help="Validate configuration offline")
    parser.add_argument("--discover-only", action="store_true", help="Inspect release tags without querying KernelCI or saving state")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        manual = pages.load_comparisons(args.manual_config) if args.manual_config else []
        previous = PreviousSite(args.previous_site, args.site_url)
        if args.check:
            print("Release-watch configuration is valid")
            return 0
        if args.discover_only:
            streams, catalogs, notices, errors = discover_sources()
            state = deepcopy(previous.state())
            errors.extend(queue_releases(state, streams, catalogs, config, timestamp()))
            print(json.dumps({"streams": streams, "releases": state["releases"],
                              "notices": notices, "errors": errors}, indent=2))
            return 1 if errors else 0
        summary = build_daily(config, args.out, previous, manual)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            pages.write_actions_summary(summary, os.environ["GITHUB_STEP_SUMMARY"])
        for error in summary["source_errors"]:
            message = "Discovery error: " + error
            if os.environ.get("GITHUB_ACTIONS") == "true":
                message = "::warning::" + message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(message, file=sys.stderr)
        print(f"Site written to {args.out}; {len(summary['comparisons'])} comparison records")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Daily release build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
