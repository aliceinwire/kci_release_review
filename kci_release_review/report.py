"""Collect evidence and describe its limits without approving a release."""

from collections import Counter
from datetime import datetime, timezone
from importlib.metadata import version
import re

from kcidev import KciDevError

CATEGORIES = ("regression", "fixed", "unstable", "persistent_fail", "new", "missing")
SECTIONS = ("builds", "boots", "tests")
FAILURES = {"FAIL", "ERROR"}
TESTED_REF = "ba6b7134f1296702182b6559b5a2320f3d6b40fa"


def validate_selection(selection):
    for field in ("origin", "giturl", "branch"):
        if not isinstance(selection.get(field), str) or not selection[field].strip():
            raise ValueError(f"{field} must be a nonempty string")
    for field in ("base", "head"):
        if not re.fullmatch(r"[0-9a-fA-F]{40}", selection.get(field, "")):
            raise ValueError(f"{field} must be a full 40-character commit hash")
    if selection["base"].lower() == selection["head"].lower():
        raise ValueError("Choose two different tested commits")


def _validate_comparison(comparison, selection):
    if not isinstance(comparison, dict) or not isinstance(comparison.get("items"), list):
        raise KciDevError("Unexpected comparison response")
    if comparison.get("base") != selection["base"] or comparison.get("head") != selection["head"]:
        raise KciDevError("Comparison returned different commits")
    if not isinstance(comparison.get("incomplete"), bool):
        raise KciDevError("Comparison is missing its completeness flag")
    counts = Counter()
    for item in comparison["items"]:
        if not isinstance(item, dict) or item.get("category") not in CATEGORIES:
            raise KciDevError("Comparison contains an unknown category")
        identity = item.get("identity")
        if not isinstance(identity, dict) or identity.get("kind") not in ("build", "boot", "test"):
            raise KciDevError("Comparison contains an invalid result identity")
        counts[item["category"]] += 1
    if comparison.get("counts") != {key: counts[key] for key in CATEGORIES}:
        raise KciDevError("Comparison counts do not match its entries")


def _observation(client, selection, commit):
    sections = {}
    for section in SECTIONS:
        key = (selection["origin"], selection["giturl"], selection["branch"], commit, section)
        rows = client.snapshots.get(key)
        if rows is None:
            sections[section] = {"fetched": False, "total": None, "statuses": {}}
        else:
            statuses = Counter(str(row.get("status") or "UNKNOWN").upper() for row in rows)
            sections[section] = {"fetched": True, "total": len(rows), "statuses": dict(statuses)}
    return sections


def _evidence(client, comparison, limit, log_bytes, progress):
    candidates = []
    seen = set()
    # Fetch regression candidates first, then persistent and newly observed failures.
    for category in ("regression", "persistent_fail", "new", "unstable"):
        for item in comparison["items"]:
            if item["category"] != category:
                continue
            if category != "regression" and str(item.get("head_status")).upper() not in FAILURES:
                continue
            kind = "build" if item["identity"]["kind"] == "build" else "test"
            result_id = item.get("head_id")
            if result_id and (kind, result_id) not in seen:
                seen.add((kind, result_id))
                candidates.append((kind, result_id))
    evidence = {}
    for kind, result_id in candidates[:limit]:
        progress(f"Fetching {kind} evidence: {result_id}")
        entry = {"kind": kind, "result_id": result_id, "errors": []}
        evidence[f"{kind}:{result_id}"] = entry
        try:
            data = client.get_build(result_id) if kind == "build" else client.get_test(result_id)
            if not isinstance(data, dict):
                raise KciDevError("Unexpected detail response")
            entry["details"] = data
        except Exception as exc:
            entry["errors"].append(f"Details: {type(exc).__name__}: {exc}")
        if kind == "test" and log_bytes:
            try:
                log = client.get_log(result_id, max_bytes=log_bytes, tail=True)
                if not isinstance(log, dict) or not isinstance(log.get("text"), str):
                    raise KciDevError("Unexpected log response")
                entry["log"] = log
            except Exception as exc:
                entry["errors"].append(f"Log: {type(exc).__name__}: {exc}")
        elif kind == "build":
            entry["log_note"] = "Build log URL/excerpt is in the details; get_log accepts test IDs."
    return evidence, len(candidates) - len(evidence)


def collect_report(client, selection, *, include_issues=True, max_evidence=5,
                   log_bytes=16_384, progress=lambda message: None):
    validate_selection(selection)
    selection = {**selection, "base": selection["base"].lower(), "head": selection["head"].lower()}
    if not 0 <= max_evidence <= 100 or not 0 <= log_bytes <= 1_048_576:
        raise ValueError("Evidence limit must be 0..100 and log bytes 0..1048576")
    client.begin_comparison()
    started = datetime.now(timezone.utc).isoformat()
    errors = []
    notes = [
        "Required test coverage, pending jobs, and release policy have not been assessed.",
        f"Classifications come from kci-dev; tree history is requested within the last {client.history_hours} hours "
        "and is used only when its checkout matches the selected head.",
        "Counts describe observed executions, including duplicates; they are not a coverage percentage.",
    ]
    progress("Comparing the two checkouts with kci-dev")
    try:
        comparison = client.compare_results(**selection, include_issues=include_issues)
        _validate_comparison(comparison, selection)
    except Exception as exc:
        comparison = None
        errors.append(f"Comparison: {type(exc).__name__}: {exc}")

    observations = {side: _observation(client, selection, selection[side]) for side in ("base", "head")}
    problems = []
    for side, sections in observations.items():
        if not all(section["fetched"] for section in sections.values()):
            problems.append(f"Some {side} result lists could not be retrieved.")
        if sum(section["total"] or 0 for section in sections.values()) == 0:
            problems.append(f"No result executions were retrieved for {side}.")
        completed = sum(section["statuses"].get(status, 0)
                        for section in sections.values() for status in ("PASS", "FAIL", "ERROR"))
        if not completed:
            problems.append(f"No PASS, FAIL, or ERROR outcomes were observed for {side}.")
    if comparison and comparison["incomplete"]:
        problems.append("kci-dev marked the comparison incomplete (history or issue evidence is missing).")
    if comparison:
        untraceable = 0
        for item in comparison["items"]:
            sides = ("head",) if item["category"] == "new" else (("base",) if item["category"] == "missing" else ("base", "head"))
            if any(not isinstance(item.get(side + "_id"), str) or not item[side + "_id"] for side in sides):
                untraceable += 1
        if untraceable:
            problems.append(f"{untraceable} comparison entries lack source result IDs needed for verification.")
    if client.history_lookup["state"] == "error":
        problems.append("History lookup: " + client.history_lookup["error"])
        if "Tree not found in the given interval" in client.history_lookup["error"]:
            problems.append(
                f"No tree history was returned within the last {client.history_lookup['max_age_in_hours']} hours. "
                "Exact-commit results below are retained. The dashboard supports at most 720 hours; "
                "older or absent history cannot be recovered by repeating this request.")
    issue_states = Counter(entry["state"] for entry in client.issue_lookups.values())
    if issue_states["limited"]:
        problems.append(f"Issue lookup limit left {issue_states['limited']} result IDs unchecked.")
    if issue_states["error"]:
        problems.append(f"Issue retrieval failed for {issue_states['error']} result IDs.")
    if not include_issues:
        notes.append("Known-issue lookup was disabled; empty issue lists do not mean no known issue exists.")

    evidence, omitted = {}, 0
    if comparison:
        evidence, omitted = _evidence(client, comparison, max_evidence, log_bytes, progress)
    if omitted:
        notes.append(f"Supplemental details/logs were limited: {omitted} failing result IDs were not expanded.")
    evidence_errors = sum(bool(entry["errors"]) for entry in evidence.values())
    if evidence_errors:
        notes.append(f"Supplemental evidence is unavailable for {evidence_errors} expanded result IDs; see their errors.")
    limited_logs = sum(bool(entry.get("log", {}).get("scan_limited") or
                            entry.get("log", {}).get("deadline_exceeded")) for entry in evidence.values())
    if limited_logs:
        notes.append(f"{limited_logs} log downloads stopped at the scan or time limit; excerpts may not contain the actual tail.")

    head_statuses = Counter()
    for section in observations["head"].values():
        head_statuses.update(section["statuses"])
    counts = comparison["counts"] if comparison else {}
    findings = any(counts.get(key, 0) for key in ("regression", "unstable", "persistent_fail", "missing"))
    findings = findings or any(count for status, count in head_statuses.items()
                               if status not in {"PASS", "SKIP"})
    if errors or problems:
        status, exit_code = "EVIDENCE_INCOMPLETE", 2
    elif findings:
        status, exit_code = "REVIEW_REQUIRED", 1
    else:
        status, exit_code = "NO_REVIEW_SIGNALS_IN_OBSERVED_RESULTS", 0
    return {
        "schema_version": 1,
        "application_version": "0.1.0",
        "kci_dev_version": version("kci-dev"),
        "tested_api_ref": TESTED_REF,
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "selection": selection,
        "dashboard_api": client.dashboard_api,
        "assessment": {"status": status, "exit_code": exit_code,
                       "release_approved": False, "required_coverage": "not_assessed"},
        "observations": observations,
        "comparison": comparison,
        "history_lookup": client.history_lookup,
        "issues": {"enabled": include_issues, "request_limit": client.max_issue_lookups,
                   "requests_made": client.issue_requests, "states": dict(issue_states),
                   "lookups": client.issue_lookups},
        "evidence": evidence,
        "evidence_omitted": omitted,
        "limits": {"max_evidence": max_evidence, "log_bytes": log_bytes},
        "incomplete_reasons": problems,
        "notes": notes,
        "errors": errors,
    }
