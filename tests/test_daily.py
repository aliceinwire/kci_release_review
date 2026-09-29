"""Persistent discovery, delayed KernelCI data, and publication failure handling."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from kcidev import KciDevError, KernelCIClient
from kci_release_review import daily, pages, releases
from kci_release_review.__main__ import main as review_main
from test_review import BASE, HEAD, result, service, snapshot
from test_releases import stable_stream


class DailyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_file = self.root / "watch.json"
        self.config_file.write_text("{}")
        self.config = daily.load_config(self.config_file)
        self.stream = stable_stream("6.12.111")
        self.tags = {"v6.12.110": BASE, "v6.12.111": HEAD}
        self.discovery = ([self.stream], {releases.STABLE_GIT: self.tags}, [], [])

    def empty_state(self):
        return {"schema": daily.SCHEMA, "streams": {}, "releases": {}, "source_errors": [], "notices": []}

    def queued(self):
        state = self.empty_state()
        daily.queue_releases(state, [self.stream], {releases.STABLE_GIT: self.tags}, self.config, "2026-09-29T00:00:00+00:00")
        return state

    @staticmethod
    def run_cli(command, **kwargs):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = review_main(command[3:])
        return subprocess.CompletedProcess(command, code)

    def build(self, out, previous=None, discovery=None):
        with patch.object(daily, "discover_sources", return_value=discovery or self.discovery):
            with patch("kci_release_review.pages.subprocess.run", side_effect=self.run_cli):
                with redirect_stdout(io.StringIO()):
                    return daily.build_daily(self.config, out, daily.PreviousSite(previous))

    def test_bootstrap_duplicate_discovery_and_intervening_releases(self):
        state = self.queued()
        self.assertEqual(len(state["releases"]), 1)
        daily.queue_releases(state, [self.stream], {releases.STABLE_GIT: self.tags}, self.config, "later")
        self.assertEqual(len(state["releases"]), 1)
        tags = {**self.tags, "v6.12.112": "c" * 40, "v6.12.113": "d" * 40}
        daily.queue_releases(state, [stable_stream("6.12.113")], {releases.STABLE_GIT: tags}, self.config, "later")
        pairs = [(job["base_tag"], job["head_tag"]) for job in state["releases"].values()]
        self.assertEqual(pairs, [("v6.12.110", "v6.12.111"), ("v6.12.111", "v6.12.112"), ("v6.12.112", "v6.12.113")])

    def test_moved_tag_does_not_advance_cursor_or_rewrite_commits(self):
        state = self.queued()
        original = deepcopy(state)
        errors = daily.queue_releases(state, [self.stream], {releases.STABLE_GIT: {**self.tags, "v6.12.111": "c" * 40}}, self.config, "later")
        self.assertIn("moved or disappeared", errors[0])
        self.assertEqual(state, original)

    def test_missing_results_are_retained_and_retried_until_available(self):
        first, second = self.root / "first", self.root / "second"
        with service(snapshot(), snapshot()):
            summary = self.build(first)
        self.assertEqual(summary["comparisons"][0]["watch_status"], "WAITING_FOR_KERNELCI")
        with service(snapshot([result("old", "PASS")]), snapshot([result("new", "FAIL")])):
            summary = self.build(second, first)
        entry = summary["comparisons"][0]
        self.assertEqual(entry["watch_status"], "COMPARED")
        self.assertEqual(entry["exit_code"], 1)
        self.assertEqual(entry["attempts"], 2)
        self.assertEqual(entry["counts"]["regression"], 1)
        saved = json.loads((second / "release-state.json").read_text())
        self.assertEqual(len(saved["releases"]), 1)
        self.assertFalse(json.loads((second / entry["report_json"]).read_text())["assessment"]["release_approved"])

    def test_fatal_retry_keeps_previous_report_and_index_links(self):
        first, second = self.root / "first", self.root / "second"
        with service(snapshot([result("old", "PASS")]), snapshot([result("new", "FAIL")])):
            original = self.build(first)
        with patch.object(daily, "discover_sources", return_value=self.discovery):
            with patch.object(pages, "run_comparison", side_effect=subprocess.TimeoutExpired([], 180)):
                summary = daily.build_daily(self.config, second, daily.PreviousSite(first))
        entry = summary["comparisons"][0]
        self.assertEqual(entry["watch_status"], "RETRY_ERROR")
        self.assertEqual(entry["counts"], original["comparisons"][0]["counts"])
        self.assertEqual((second / entry["report_json"]).read_bytes(), (first / entry["report_json"]).read_bytes())
        self.assertIn(entry["report_html"], (second / "index.html").read_text())

    def test_api_error_diagnostic_does_not_replace_existing_comparison(self):
        first, second = self.root / "first", self.root / "second"
        with service(snapshot([result("old", "PASS")]), snapshot([result("new", "FAIL")])):
            original = self.build(first)
            with patch.object(KernelCIClient, "get_tests", side_effect=KciDevError("offline")):
                summary = self.build(second, first)
        entry = summary["comparisons"][0]
        self.assertEqual(entry["watch_status"], "RETRY_ERROR")
        self.assertEqual(entry["finished_at"], original["comparisons"][0]["finished_at"])
        self.assertIn("earlier report is retained", entry["watch_note"])

    def test_budget_defers_jobs_without_losing_them_and_next_run_is_fair(self):
        state = self.queued()
        tags = {**self.tags, "v6.12.112": "c" * 40}
        daily.queue_releases(state, [stable_stream("6.12.112")], {releases.STABLE_GIT: tags}, self.config, "later")
        config = {**self.config, "max_comparisons_per_run": 1}
        with patch.object(pages, "run_comparison", side_effect=RuntimeError("offline")) as run:
            count = daily.process_queue(state, self.root, config, time.monotonic() + 1000)
        self.assertEqual(count, 1)
        self.assertEqual(run.call_count, 1)
        jobs = list(state["releases"].values())
        self.assertEqual([job["attempts"] for job in jobs], [1, 0])
        self.assertEqual(daily.retry_candidates(state)[0][1]["head_tag"], "v6.12.112")
        with patch.object(pages, "run_comparison") as run:
            daily.process_queue(state, self.root, config, time.monotonic() - 1)
        run.assert_not_called()

    def test_older_completed_reports_are_not_requeried_but_incomplete_ones_are(self):
        state = self.queued()
        old = next(iter(state["releases"].values()))
        old["watch_status"] = "COMPARED"
        tags = {**self.tags, "v6.12.112": "c" * 40}
        daily.queue_releases(state, [stable_stream("6.12.112")], {releases.STABLE_GIT: tags}, self.config, "later")
        self.assertEqual(len(daily.retry_candidates(state)), 1)
        old["watch_status"] = "EVIDENCE_INCOMPLETE"
        self.assertEqual(len(daily.retry_candidates(state)), 2)

    def test_feed_outage_retains_cursor_and_pending_jobs_with_visible_error(self):
        first, second = self.root / "first", self.root / "second"
        with service(snapshot(), snapshot()):
            self.build(first)
            summary = self.build(second, first, ([], {}, [], ["CIP: unavailable"]))
        self.assertIn("CIP: unavailable", summary["source_errors"])
        self.assertIn("Discovery error:", (second / "index.html").read_text())
        before = json.loads((first / "release-state.json").read_text())
        after = json.loads((second / "release-state.json").read_text())
        self.assertEqual(before["streams"], after["streams"])
        self.assertEqual(len(after["releases"]), 1)

    def test_remote_404_bootstraps_but_errors_and_invalid_json_do_not_reset(self):
        previous = daily.PreviousSite(url="https://example.test/project/")
        with patch.object(daily, "fetch_bytes", side_effect=HTTPError("url", 404, "missing", {}, None)):
            self.assertEqual(previous.state()["releases"], {})
        for error in (HTTPError("url", 503, "unavailable", {}, None), OSError("timeout")):
            with patch.object(daily, "fetch_bytes", side_effect=error), self.assertRaises(OSError):
                previous.state()
        with patch.object(daily, "fetch_bytes", return_value=b"not JSON"), self.assertRaises(ValueError):
            previous.state()

    def test_corrupt_or_missing_archived_report_stops_publication(self):
        first = self.root / "first"
        with service(snapshot([result("old", "PASS")]), snapshot([result("new", "PASS")])):
            summary = self.build(first)
        path = first / summary["comparisons"][0]["report_json"]
        for bad in (b"corrupt", None):
            if bad is None:
                path.unlink()
            else:
                path.write_bytes(bad)
            out = self.root / ("missing" if bad is None else "corrupt")
            with self.assertRaises((ValueError, OSError)):
                self.build(out, first)
            self.assertFalse((out / "index.html").exists())

    def test_bad_saved_release_path_is_rejected(self):
        state = self.queued()
        key = next(iter(state["releases"]))
        state["releases"][key]["config"]["id"] = "../escape"
        previous = self.root / "previous"
        previous.mkdir()
        (previous / "release-state.json").write_text(json.dumps(state))
        with self.assertRaises(ValueError):
            daily.PreviousSite(previous).state()

    def test_changed_query_settings_retry_while_retaining_previous_selection(self):
        state = self.queued()
        job = next(iter(state["releases"].values()))
        original = deepcopy(job["config"])
        changed = {**self.config, "overrides": {self.stream["id"]: {"origin": "other"}}}
        daily.queue_releases(state, [self.stream], {releases.STABLE_GIT: self.tags}, changed, "later")
        self.assertEqual(job["config"], original)
        self.assertEqual(job["next_config"]["origin"], "other")
        daily.queue_releases(state, [self.stream], {releases.STABLE_GIT: self.tags}, self.config, "later")
        self.assertNotIn("next_config", job)

    def test_check_mode_is_offline_and_rejects_bad_budgets(self):
        with patch.object(daily, "discover_sources") as discover:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(daily.main(["--config", str(self.config_file), "--check"]), 0)
        discover.assert_not_called()
        for document in ({"max_comparisons_per_run": 0}, {"comparison_timeout_seconds": True},
                         {"overrides": {"stream": {"base": "a" * 40}}}, {"origin": 123}):
            self.config_file.write_text(json.dumps(document))
            with self.assertRaises(ValueError):
                daily.load_config(self.config_file)

    def test_removing_applied_override_restores_authoritative_query(self):
        state = self.queued()
        job = next(iter(state["releases"].values()))
        override = {"giturl": "https://mirror.example.test/linux.git", "branch": "alias",
                    "dashboard_api": "https://other.example.test/api/"}
        changed = {**self.config, "overrides": {self.stream["id"]: override}}
        daily.queue_releases(state, [self.stream], {releases.STABLE_GIT: self.tags}, changed, "later")
        job["config"] = job.pop("next_config")
        daily.queue_releases(state, [self.stream], {releases.STABLE_GIT: self.tags}, self.config, "later")
        self.assertEqual(job["next_config"]["giturl"], releases.STABLE_GIT)
        self.assertEqual(job["next_config"]["branch"], "linux-6.12.y")
        self.assertNotIn("dashboard_api", job["next_config"])

    def test_new_failed_job_has_no_broken_report_links(self):
        out = self.root / "out"
        with patch.object(daily, "discover_sources", return_value=self.discovery):
            with patch.object(pages, "run_comparison", side_effect=RuntimeError("no report")):
                summary = daily.build_daily(self.config, out, daily.PreviousSite())
        entry = summary["comparisons"][0]
        self.assertEqual(entry["status"], "NOT_COMPARED")
        html = (out / "index.html").read_text()
        self.assertNotIn('/report.html"', html)
        self.assertIn("No report yet", html)


if __name__ == "__main__":
    unittest.main()
