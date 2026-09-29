"""History age and checkout identity must not hide or alter release evidence."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest

from kcidev import KciDevError

from kci_release_review.__main__ import main
from kci_release_review.client import ReviewClient
from kci_release_review.report import collect_report
from test_review import HEAD, SELECTION, result, service, snapshot


HISTORY = {"origin": SELECTION["origin"], "git_url": SELECTION["giturl"],
           "git_branch": SELECTION["branch"], "commit_hash": HEAD,
           "unstable_tests": {"board-a": {"defconfig": {"arm64/gcc": {"suite.test": [{}]}}}}}


class HistoryTests(unittest.TestCase):
    def test_default_recovers_older_history_without_replacing_selected_commits(self):
        with service(snapshot([result("old", "PASS")]), snapshot([result("new", "FAIL")])) as (client, calls):
            fetch = calls["history"].side_effect
            def older_checkout(self, *args, **kwargs):
                if kwargs["max_age_in_hours"] <= 24:
                    raise KciDevError("Tree not found in the given interval")
                return fetch(self, *args, **kwargs)
            calls["history"].side_effect = older_checkout
            recovered = collect_report(client, SELECTION, include_issues=False, max_evidence=0)
            client.history_hours = 24
            incomplete = collect_report(client, SELECTION, include_issues=False, max_evidence=0)
        self.assertEqual(recovered["assessment"]["exit_code"], 1)
        self.assertEqual(recovered["history_lookup"]["max_age_in_hours"], 720)
        self.assertEqual(recovered["history_lookup"]["commit_hash"], HEAD)
        self.assertEqual(recovered["selection"], SELECTION)
        self.assertEqual(incomplete["assessment"]["exit_code"], 2)
        self.assertEqual(recovered["comparison"]["counts"], incomplete["comparison"]["counts"])
        self.assertTrue(any("last 24 hours" in reason for reason in incomplete["incomplete_reasons"]))
        self.assertIsNone(client._history_selection)

    def test_mismatched_history_never_reclassifies_the_exact_commit_transition(self):
        for field in ("origin", "git_url", "git_branch", "commit_hash"):
            with self.subTest(field=field):
                history = {**HISTORY, field: "wrong-checkout"}
                with service(snapshot([result("old", "PASS")]), snapshot([result("new", "FAIL")])) as (client, calls):
                    calls["history"].side_effect = None
                    calls["history"].return_value = history
                    report = collect_report(client, SELECTION, include_issues=False, max_evidence=0)
                self.assertEqual(report["assessment"]["exit_code"], 2)
                self.assertEqual(report["comparison"]["counts"]["regression"], 1)
                self.assertEqual(report["comparison"]["counts"]["unstable"], 0)
                self.assertIn(field, report["history_lookup"]["error"])

    def test_matching_history_preserves_kci_dev_instability_classification(self):
        with service(snapshot([result("old", "PASS")]), snapshot([result("new", "FAIL")])) as (client, calls):
            calls["history"].side_effect = None
            calls["history"].return_value = deepcopy(HISTORY)
            report = collect_report(client, SELECTION, include_issues=False, max_evidence=0)
        self.assertEqual(report["assessment"]["exit_code"], 1)
        self.assertEqual(report["comparison"]["counts"]["unstable"], 1)
        self.assertEqual(report["comparison"]["counts"]["regression"], 0)

    def test_missing_history_identity_remains_incomplete(self):
        for response in ({}, [], {"error": "unavailable"}):
            with self.subTest(response=response):
                with service(snapshot([result("old", "PASS")]), snapshot([result("new", "PASS")])) as (client, calls):
                    calls["history"].side_effect = None
                    calls["history"].return_value = response
                    report = collect_report(client, SELECTION, include_issues=False, max_evidence=0)
                self.assertEqual(report["assessment"]["exit_code"], 2)
                self.assertTrue(report["comparison"]["incomplete"])

    def test_explicit_positional_history_interval_is_preserved_and_recorded(self):
        with service(snapshot(), snapshot()) as (client, calls):
            client.get_tree_report("maestro", "main", SELECTION["giturl"], None, 3, 168, 12)
        self.assertEqual(client.history_lookup["max_age_in_hours"], 168)
        self.assertEqual(client.history_lookup["min_age_in_hours"], 12)
        self.assertEqual(client.history_lookup["history_size"], 3)
        self.assertEqual(calls["history"].call_args.kwargs["max_age_in_hours"], 168)

    def test_invalid_history_window_fails_before_api_requests(self):
        for hours in (0, -1, 721, True, 1.5, "24"):
            with self.subTest(hours=hours), self.assertRaises(ValueError):
                ReviewClient(history_hours=hours)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["compare", *[f"--{key}={value}" for key, value in SELECTION.items()], "--history-hours=721"])
        self.assertEqual(error.exception.code, 2)

    def test_cli_outputs_specific_reasons_and_requested_window(self):
        with tempfile.TemporaryDirectory() as directory:
            stdout = io.StringIO()
            with service(snapshot([result("old", "PASS")]), snapshot([result("new", "FAIL")]), history_error=True):
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    code = main(["compare", *[f"--{key}={value}" for key, value in SELECTION.items()],
                                 "--no-issues", "--max-evidence=0", "--history-hours=168", f"--out={directory}"])
            result_json = json.loads(stdout.getvalue())
            report = json.loads((Path(directory) / "report.json").read_text())
        self.assertEqual(code, 2)
        self.assertEqual(result_json["incomplete_reasons"], report["incomplete_reasons"])
        self.assertIn("history unavailable", " ".join(result_json["incomplete_reasons"]))
        self.assertEqual(result_json["history_lookup"]["max_age_in_hours"], 168)
        self.assertEqual(result_json["counts"]["regression"], 1)


if __name__ == "__main__":
    unittest.main()
