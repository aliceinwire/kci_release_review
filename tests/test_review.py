"""Exercise the real kci-dev comparison API with controlled service responses."""

from contextlib import ExitStack, contextmanager, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kcidev import KciDevError, KernelCIClient

from kci_release_review.__main__ import main, write_report
from kci_release_review.client import ReviewClient, make_client
from kci_release_review.html_report import render_html
from kci_release_review.report import collect_report

BASE, HEAD = "a" * 40, "b" * 40
SELECTION = dict(origin="maestro", giturl="https://example.test/linux.git",
                 branch="main", base=BASE, head=HEAD)


def result(result_id, status, path="suite.test", **extra):
    return dict(id=result_id, status=status, origin="maestro", platform="board-a",
                architecture="arm64", compiler="gcc", config_name="defconfig",
                path=path, **extra)


def snapshot(tests=(), builds=(), boots=()):
    return dict(tests=list(tests), builds=list(builds), boots=list(boots))


@contextmanager
def service(base, head, *, history_error=False, log_error=False, max_issues=20):
    data = {BASE: base, HEAD: head}
    with ExitStack() as stack:
        calls = {}
        for section in ("builds", "boots", "tests"):
            def fetch(self, origin, giturl, branch, commit, _section=section):
                return {_section: data[commit][_section]}
            calls[section] = stack.enter_context(patch.object(KernelCIClient, "get_" + section,
                                                             autospec=True, side_effect=fetch))
        calls["history"] = stack.enter_context(patch.object(
            KernelCIClient, "get_tree_report", autospec=True,
            side_effect=KciDevError("history unavailable") if history_error else None,
            return_value={}))
        for method in ("get_build_issues", "get_boot_issues"):
            calls[method] = stack.enter_context(patch.object(
                KernelCIClient, method, autospec=True, return_value=[{"id": "known:issue-1"}]))
        for method in ("get_build", "get_test"):
            calls[method] = stack.enter_context(patch.object(
                KernelCIClient, method, autospec=True,
                side_effect=lambda self, result_id: {"id": result_id, "status": "FAIL",
                                                    "log_url": "https://logs.example.test/run.log"}))
        calls["log"] = stack.enter_context(patch.object(
            KernelCIClient, "get_log", autospec=True,
            side_effect=KciDevError("log unavailable") if log_error else None,
            return_value={"text": "kernel panic", "returned_bytes": 12, "total_bytes": 4096,
                          "truncated": True, "scan_limited": False,
                          "deadline_exceeded": False, "source": "dashboard"}))
        yield ReviewClient(max_issue_lookups=max_issues), calls


class ReviewTests(unittest.TestCase):
    def test_regression_retains_identity_sources_issues_and_log_bound(self):
        with service(snapshot([result("old", "PASS")]), snapshot([result("new", "FAIL")])) as (client, calls):
            report = collect_report(client, SELECTION, log_bytes=4096)
        self.assertEqual(report["assessment"]["exit_code"], 1)
        item = report["comparison"]["items"][0]
        self.assertEqual((item["base_id"], item["head_id"]), ("old", "new"))
        self.assertEqual(item["identity"]["compiler"], "gcc")
        self.assertEqual(item["known_issues"], ["known:issue-1"])
        self.assertTrue(report["evidence"]["test:new"]["log"]["truncated"])
        self.assertEqual(calls["log"].call_args.kwargs, dict(max_bytes=4096, tail=True))
        self.assertTrue(all(calls[name].call_count == 2 for name in ("builds", "boots", "tests")))

    def test_empty_results_are_incomplete_even_when_library_report_is_complete(self):
        with service(snapshot(), snapshot()) as (client, _):
            report = collect_report(client, SELECTION)
        self.assertFalse(report["comparison"]["incomplete"])
        self.assertEqual(report["assessment"]["status"], "EVIDENCE_INCOMPLETE")

    def test_unchanged_passes_are_counted_without_becoming_release_approval(self):
        with service(snapshot([result("old", "PASS")]), snapshot([result("new", "PASS")])) as (client, _):
            report = collect_report(client, SELECTION)
        self.assertEqual(report["comparison"]["items"], [])
        self.assertEqual(report["observations"]["head"]["tests"]["total"], 1)
        self.assertEqual(report["assessment"]["exit_code"], 0)
        self.assertFalse(report["assessment"]["release_approved"])
        self.assertEqual(report["assessment"]["required_coverage"], "not_assessed")

    def test_missing_result_is_review_signal(self):
        base = snapshot([result("old", "PASS"), result("missing", "PASS", path="suite.other")])
        head = snapshot([result("new", "PASS")])
        with service(base, head) as (client, _):
            report = collect_report(client, SELECTION)
        self.assertEqual(report["comparison"]["counts"]["missing"], 1)
        self.assertEqual(report["assessment"]["exit_code"], 1)

    def test_identical_unrecognized_outcome_is_visible_even_without_library_change(self):
        base = snapshot([result("p0", "PASS"), result("u0", "CUSTOM", path="suite.other")])
        head = snapshot([result("p1", "PASS"), result("u1", "CUSTOM", path="suite.other")])
        with service(base, head) as (client, _):
            report = collect_report(client, SELECTION)
        self.assertEqual(report["comparison"]["items"], [])
        self.assertEqual(report["assessment"]["exit_code"], 1)

    def test_history_failure_does_not_discard_the_comparison(self):
        with service(snapshot([result("old", "PASS")]), snapshot([result("new", "FAIL")]), history_error=True) as (client, _):
            report = collect_report(client, SELECTION)
        self.assertTrue(report["comparison"]["incomplete"])
        self.assertIn("history unavailable", report["history_lookup"]["error"])
        self.assertEqual(report["comparison"]["counts"]["regression"], 1)
        self.assertEqual(report["assessment"]["exit_code"], 2)

    def test_issue_budget_is_explicit_and_does_not_erase_regressions(self):
        base = snapshot([result("a0", "PASS", path="one"), result("b0", "PASS", path="two")])
        head = snapshot([result("a1", "FAIL", path="one"), result("b1", "FAIL", path="two")])
        with service(base, head, max_issues=1) as (client, calls):
            report = collect_report(client, SELECTION, max_evidence=0)
        self.assertEqual(calls["get_boot_issues"].call_count, 1)
        self.assertEqual(report["comparison"]["counts"]["regression"], 2)
        self.assertEqual(report["issues"]["states"]["limited"], 1)
        self.assertEqual(report["evidence_omitted"], 2)
        self.assertEqual(report["assessment"]["exit_code"], 2)

    def test_duplicate_executions_are_not_collapsed(self):
        with service(snapshot([result("a", "PASS"), result("b", "PASS")]),
                     snapshot([result("c", "FAIL"), result("d", "FAIL")])) as (client, _):
            report = collect_report(client, SELECTION)
        self.assertEqual(report["comparison"]["counts"]["regression"], 2)
        self.assertEqual({row["occurrence"] for row in report["comparison"]["items"]}, {0, 1})

    def test_build_evidence_never_calls_test_log_api(self):
        with service(snapshot(builds=[result("a", "PASS", path="build")]),
                     snapshot(builds=[result("b", "FAIL", path="build")])) as (client, calls):
            report = collect_report(client, SELECTION)
        self.assertEqual(calls["get_build"].call_count, 1)
        calls["get_test"].assert_not_called()
        calls["log"].assert_not_called()
        self.assertIn("log_note", report["evidence"]["build:b"])

    def test_unavailable_logs_do_not_erase_details_or_issue_evidence(self):
        with service(snapshot([result("old", "PASS")]), snapshot([result("new", "FAIL")]), log_error=True) as (client, _):
            report = collect_report(client, SELECTION)
        self.assertIn("details", report["evidence"]["test:new"])
        self.assertIn("log unavailable", report["evidence"]["test:new"]["errors"][0])
        self.assertEqual(report["comparison"]["items"][0]["known_issues"], ["known:issue-1"])

    def test_partial_request_failure_generates_diagnostic_report(self):
        with service(snapshot([result("a", "PASS")]), snapshot([result("b", "PASS")])) as (client, _):
            with patch.object(KernelCIClient, "get_tests", side_effect=KciDevError("service down")):
                report = collect_report(client, SELECTION)
        self.assertIsNone(report["comparison"])
        self.assertIn("service down", report["errors"][0])
        self.assertEqual(report["assessment"]["exit_code"], 2)

    def test_html_escapes_remote_content_and_rejects_active_urls(self):
        payload = '<script>alert("x")</script>'
        with service(snapshot([result("a", "PASS", path=payload)]),
                     snapshot([result("b", "FAIL", path=payload)])) as (client, _):
            report = collect_report(client, SELECTION)
        report["evidence"]["test:b"]["details"]["log_url"] = "javascript:alert(1)"
        report["evidence"]["test:b"]["log"]["text"] = payload
        rendered = render_html(report)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn('href="javascript:', rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_same_commit_rejected_before_any_requests(self):
        with service(snapshot(), snapshot()) as (client, calls):
            with self.assertRaisesRegex(ValueError, "different"):
                collect_report(client, {**SELECTION, "head": BASE})
        calls["builds"].assert_not_called()

    def test_configured_endpoint_is_used_and_no_tokens_enter_report(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text('[staging]\ndashboard_api="https://staging.example.test/api/"\ntoken="secret-value"\n')
            client = make_client(config, "staging")
            self.assertEqual(client.dashboard_api, "https://staging.example.test/api/")
            with service(snapshot([result("a", "PASS")]), snapshot([result("b", "PASS")])):
                report = collect_report(client, SELECTION)
            self.assertNotIn("secret-value", json.dumps(report))
            self.assertEqual(report["dashboard_api"], "https://staging.example.test/api/")
            config.write_text('[staging]\napi="https://maestro.example.test"\n')
            with self.assertRaisesRegex(ValueError, "dashboard_api"):
                make_client(config, "staging")
            with self.assertRaisesRegex(ValueError, "not defined"):
                make_client(config, "missing")

    def test_cli_json_is_not_contaminated_by_library_stdout(self):
        def noisy_query(self, origin, days):
            print("diagnostic from library")
            return [{"tree_name": "example"}]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(KernelCIClient, "get_tree_list", noisy_query), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["trees"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), [{"tree_name": "example"}])
        self.assertIn("diagnostic", stderr.getvalue())

    def test_report_files_preserve_full_comparison(self):
        with service(snapshot([result("a", "FAIL")]), snapshot([result("b", "PASS")])) as (client, _):
            report = collect_report(client, SELECTION)
        with tempfile.TemporaryDirectory() as directory:
            write_report(report, Path(directory))
            self.assertEqual(json.loads((Path(directory) / "report.json").read_text())["comparison"], report["comparison"])
            self.assertIn("report.json", (Path(directory) / "report.html").read_text())

    def test_missing_source_id_is_reported_as_incomplete_evidence(self):
        with service(snapshot([result("old", "PASS")]), snapshot([result(None, "FAIL")])) as (client, _):
            report = collect_report(client, SELECTION)
        self.assertEqual(report["assessment"]["exit_code"], 2)
        self.assertTrue(any("source result IDs" in reason for reason in report["incomplete_reasons"]))

    def test_reusing_client_does_not_reuse_stale_data_or_mutate_prior_report(self):
        with service(snapshot([result("a", "PASS")]), snapshot([result("b", "FAIL")])) as (client, _):
            first = collect_report(client, SELECTION)
            saved = json.dumps(first)
            with patch.object(KernelCIClient, "get_builds", side_effect=KciDevError("offline")):
                second = collect_report(client, SELECTION)
        self.assertEqual(json.dumps(first), saved)
        self.assertFalse(second["observations"]["base"]["builds"]["fetched"])
        self.assertEqual(second["issues"]["lookups"], {})


if __name__ == "__main__":
    unittest.main()
