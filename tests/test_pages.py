"""Test publication outcomes using the real CLI with controlled API responses."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from kcidev import KciDevError, KernelCIClient

from kci_release_review.__main__ import main as review_main
from kci_release_review.pages import build_site, load_comparisons, main
from test_review import SELECTION, result, service, snapshot


class PagesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "comparisons.json"
        self.output = self.root / "site"
        self.row = {"id": "stable-test", "title": "Test comparison", **SELECTION}
        self.write_config([self.row])

    def write_config(self, rows):
        self.config.write_text(json.dumps({"comparisons": rows}), encoding="utf-8")

    def invoke(self, *extra):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return main(["--config", str(self.config), "--out", str(self.output), *extra])

    @staticmethod
    def run_cli(command, *, check, timeout):
        if check is not False or timeout != 900:
            raise AssertionError("Unexpected subprocess settings")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = review_main(command[3:])
        return subprocess.CompletedProcess(command, code)

    def test_all_assessments_publish_with_relative_links_and_full_json(self):
        for expected_code, head_status, history_error in ((0, "PASS", False),
                                                          (1, "FAIL", False),
                                                          (2, "FAIL", True)):
            with self.subTest(exit_code=expected_code):
                output = self.root / f"site-{expected_code}"
                with service(snapshot([result("old", "PASS")]),
                             snapshot([result("new", head_status)]), history_error=history_error):
                    with patch("kci_release_review.pages.subprocess.run", side_effect=self.run_cli):
                        summary = build_site(load_comparisons(self.config), output)
                entry = summary["comparisons"][0]
                self.assertEqual(entry["exit_code"], expected_code)
                report = json.loads((output / entry["report_json"]).read_text())
                self.assertEqual(entry["counts"], report["comparison"]["counts"])
                self.assertFalse(report["assessment"]["release_approved"])
                self.assertEqual(report["comparison"]["base"], SELECTION["base"])
                self.assertIn('href="stable-test/report.html"', (output / "index.html").read_text())
                self.assertIn('href="stable-test/report.json"', (output / "index.html").read_text())
                self.assertIn('href="report.json"', (output / entry["report_html"]).read_text())
                self.assertEqual(json.loads((output / "summary.json").read_text()), summary)

    def test_multiple_comparisons_and_actions_summary(self):
        self.write_config([self.row, {**self.row, "id": "second"}])
        summary_path = self.root / "job-summary.md"
        summary_path.write_text("Earlier step\n")
        with service(snapshot([result("old", "PASS")]), snapshot([result("new", "FAIL")])):
            with patch("kci_release_review.pages.subprocess.run", side_effect=self.run_cli) as run:
                with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_path), "GITHUB_ACTIONS": "true"}):
                    self.assertEqual(self.invoke(), 0)
        self.assertEqual(run.call_count, 2)
        index = (self.output / "index.html").read_text()
        self.assertIn('href="second/report.html"', index)
        summary = summary_path.read_text()
        self.assertTrue(summary.startswith("Earlier step\n"))
        self.assertIn("| stable-test | REVIEW_REQUIRED | 1 |", summary)
        self.assertIn("| second | REVIEW_REQUIRED | 1 |", summary)

    def test_api_failure_publishes_explicit_incomplete_diagnostic(self):
        with service(snapshot([result("old", "PASS")]), snapshot([result("new", "PASS")])):
            with patch.object(KernelCIClient, "get_tests", side_effect=KciDevError("offline")):
                with patch("kci_release_review.pages.subprocess.run", side_effect=self.run_cli):
                    self.assertEqual(self.invoke(), 0)
        summary = json.loads((self.output / "summary.json").read_text())
        self.assertIsNone(summary["comparisons"][0]["counts"])
        index = (self.output / "index.html").read_text()
        self.assertIn("EVIDENCE INCOMPLETE", index)
        self.assertIn("unavailable", index)

    def test_fatal_exit_missing_files_and_timeout_block_publication(self):
        cases = [subprocess.CompletedProcess([], code) for code in (0, 1, 2, 7)]
        cases.append(subprocess.TimeoutExpired(["python"], 900))
        for outcome in cases:
            with self.subTest(outcome=outcome):
                kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with patch("kci_release_review.pages.subprocess.run", **kwargs):
                    self.assertEqual(self.invoke(), 1)
                self.assertFalse((self.output / "index.html").exists())

    def test_invalid_report_never_gets_an_index(self):
        for mutation in ("json", "exit", "selection", "counts"):
            with self.subTest(mutation=mutation):
                def corrupt(command, **kwargs):
                    completed = self.run_cli(command, **kwargs)
                    destination = Path(next(arg[6:] for arg in command if arg.startswith("--out=")))
                    path = destination / "report.json"
                    report = json.loads(path.read_text())
                    if mutation == "exit":
                        report["assessment"]["exit_code"] = 2
                    elif mutation == "selection":
                        report["selection"]["head"] = "c" * 40
                    elif mutation == "counts":
                        report["comparison"]["counts"]["regression"] = "unknown"
                    path.write_text("not JSON" if mutation == "json" else json.dumps(report))
                    return completed
                with service(snapshot([result("old", "PASS")]), snapshot([result("new", "PASS")])):
                    with patch("kci_release_review.pages.subprocess.run", side_effect=corrupt):
                        with self.assertRaises((ValueError, RuntimeError)):
                            build_site(load_comparisons(self.config), self.root / mutation)
                self.assertFalse((self.root / mutation / "index.html").exists())

    def test_stale_output_is_not_reused_or_overwritten(self):
        self.output.mkdir()
        previous = self.output / "index.html"
        previous.write_text("previous site")
        with patch("kci_release_review.pages.subprocess.run") as run:
            self.assertEqual(self.invoke(), 1)
        run.assert_not_called()
        self.assertEqual(previous.read_text(), "previous site")

    def test_config_validation_happens_before_requests_or_output(self):
        invalid_rows = [
            [], [self.row, self.row], [{**self.row, "id": "../escape"}],
            [{**self.row, "head": self.row["base"]}], [{**self.row, "head": "short"}],
            [{**self.row, "base": 123}], [{**self.row, "max_evidence": 101}],
            [{**self.row, "log_bytes": True}], [{**self.row, "include_issues": "false"}],
            [{**self.row, "dashboard_api": "https://user:password@example.test/api/"}],
            [{**self.row, "max_issue_lookup": 3}],
        ]
        for rows in invalid_rows:
            with self.subTest(rows=rows):
                self.write_config(rows)
                with patch("kci_release_review.pages.subprocess.run") as run:
                    self.assertEqual(self.invoke(), 1)
                run.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_check_mode_makes_no_requests_or_files(self):
        with patch("kci_release_review.pages.subprocess.run") as run:
            self.assertEqual(self.invoke("--check"), 0)
        run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_configured_history_window_reaches_the_api_and_index_explains_errors(self):
        self.write_config([{**self.row, "history_hours": 168, "include_issues": False}])
        with service(snapshot([result("old", "PASS")]), snapshot([result("new", "FAIL")])) as (_, calls):
            calls["history"].side_effect = KciDevError("history <script>unavailable</script>")
            with patch("kci_release_review.pages.subprocess.run", side_effect=self.run_cli):
                summary = build_site(load_comparisons(self.config), self.output)
        self.assertEqual(calls["history"].call_args.kwargs["max_age_in_hours"], 168)
        self.assertTrue(summary["comparisons"][0]["incomplete_reasons"])
        index = (self.output / "index.html").read_text()
        self.assertIn("Why this comparison is incomplete", index)
        self.assertIn("history &lt;script&gt;unavailable&lt;/script&gt;", index)
        self.assertNotIn("<script>", index)

    def test_history_window_range_in_manifest(self):
        for value in (0, -1, 721, True, "720"):
            self.write_config([{**self.row, "history_hours": value}])
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_comparisons(self.config)

    def test_html_escaping_and_literal_cli_options(self):
        self.write_config([{
            **self.row, "title": '<script>alert("x")</script>',
            "branch": "--option $(touch not-a-command)",
            "include_issues": False, "max_evidence": 0, "log_bytes": 0,
            "dashboard_api": "https://dashboard.example.test/api/",
        }])
        with service(snapshot([result("old", "PASS")]), snapshot([result("new", "FAIL")])):
            with patch("kci_release_review.pages.subprocess.run", side_effect=self.run_cli) as run:
                self.assertEqual(self.invoke(), 0)
        command = run.call_args.args[0]
        self.assertIn("--branch=--option $(touch not-a-command)", command)
        self.assertIn("--no-issues", command)
        self.assertIn("--max-evidence=0", command)
        self.assertIn("--log-bytes=0", command)
        self.assertIn("--dashboard-api=https://dashboard.example.test/api/", command)
        self.assertNotIn("shell", run.call_args.kwargs)
        index = (self.output / "index.html").read_text()
        self.assertNotIn("<script>", index)
        self.assertIn("&lt;script&gt;", index)


if __name__ == "__main__":
    unittest.main()
