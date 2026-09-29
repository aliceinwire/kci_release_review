"""Release feeds, numeric tag order, and exact predecessor selection."""

import json
import unittest
from unittest.mock import patch

from kci_release_review import releases


def catalog(*names):
    return {name: f"{index + 1:040x}" for index, name in enumerate(names)}


def stable_stream(version="6.12.111"):
    return releases.kernel_streams({"releases": [{"moniker": "longterm", "version": version}]})[0]


def cip_page(branch="linux-6.12.y-cip", version="v6.12.108-cip31"):
    return f"<pre>\n{branch}: interval 15 day\n  latest version: {version}\n  Status: On track\n</pre>"


class ReleaseTests(unittest.TestCase):
    def test_annotated_tags_use_peeled_commits_regardless_of_line_order(self):
        for rows in (
            ["a" * 40 + " refs/tags/v6.12.1", "b" * 40 + " refs/tags/v6.12.1^{}"],
            ["b" * 40 + " refs/tags/v6.12.1^{}", "a" * 40 + " refs/tags/v6.12.1"],
        ):
            tags = releases.parse_tags("\n".join(rows + ["c" * 40 + " refs/tags/v6.12.2"]))
            self.assertEqual(tags["v6.12.1"], "b" * 40)
            self.assertEqual(tags["v6.12.2"], "c" * 40)

    def test_new_stream_bootstraps_only_latest_pair(self):
        stream = stable_stream("6.12.11")
        tags = catalog("v6.12.8", "v6.12.9", "v6.12.10", "v6.12.11", "v6.12.12", "v6.1.12")
        self.assertEqual(releases.adjacent_releases(stream, tags), [("v6.12.10", "v6.12.11")])

    def test_numeric_order_and_all_intermediate_releases_after_missed_runs(self):
        stream = stable_stream("6.12.12")
        tags = catalog("v6.12.9", "v6.12.10", "v6.12.11", "v6.12.12", "v6.12.13-rc1")
        self.assertEqual(releases.adjacent_releases(stream, tags, "v6.12.9"),
                         [("v6.12.9", "v6.12.10"), ("v6.12.10", "v6.12.11"), ("v6.12.11", "v6.12.12")])
        self.assertEqual(releases.adjacent_releases(stream, tags, "v6.12.12"), [])

    def test_stable_first_point_release_compares_with_final_not_rc(self):
        stream = stable_stream("7.2.1")
        tags = catalog("v7.2-rc7", "v7.2", "v7.2.1", "v7.1.13")
        self.assertEqual(releases.adjacent_releases(stream, tags), [("v7.2", "v7.2.1")])

    def test_mainline_rc_final_and_next_cycle(self):
        stream = releases.kernel_streams({"releases": [{"moniker": "mainline", "version": "7.0-rc2"}]})[0]
        tags = catalog("v6.19-rc7", "v6.19", "v6.19.1", "v7.0-rc1", "v7.0-rc2")
        self.assertEqual(releases.adjacent_releases(stream, tags, "v6.19-rc7"),
                         [("v6.19-rc7", "v6.19"), ("v6.19", "v7.0-rc1"), ("v7.0-rc1", "v7.0-rc2")])

    def test_feed_final_release_and_rc_share_mainline_stream(self):
        streams = releases.kernel_streams({"releases": [
            {"moniker": "stable", "version": "7.2"},
            {"moniker": "mainline", "version": "7.3-rc1"},
            {"moniker": "linux-next", "version": "next-20260928"},
        ]})
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]["latest"], "v7.3-rc1")

    def test_cip_and_rt_never_mix_and_cip_sequence_is_numeric(self):
        normal = releases.cip_streams(cip_page(version="v6.12.111-cip32"))[0]
        rt = releases.cip_streams(cip_page("linux-6.12.y-cip-rt", "v6.12.111-cip32-rt10"))[0]
        tags = catalog("v6.12.108-cip31", "v6.12.111-cip32", "v6.12.105-cip30-rt9", "v6.12.111-cip32-rt10")
        self.assertEqual(releases.adjacent_releases(normal, tags), [("v6.12.108-cip31", "v6.12.111-cip32")])
        self.assertEqual(releases.adjacent_releases(rt, tags), [("v6.12.105-cip30-rt9", "v6.12.111-cip32-rt10")])

    def test_cip_page_lag_does_not_hide_published_tags(self):
        tags = catalog("v6.12.108-cip31", "v6.12.111-cip32")
        def fetch(url):
            if url == releases.CIP_FEED:
                return cip_page().encode()
            return json.dumps({"releases": [{"moniker": "stable", "version": "6.12.111"}]}).encode()
        with patch.object(releases, "fetch_bytes", side_effect=fetch):
            with patch.object(releases, "read_tags", side_effect=lambda url: tags if url == releases.CIP_GIT else catalog("v6.12.110", "v6.12.111")):
                streams, _, notices, errors = releases.discover_sources()
        self.assertFalse(errors)
        self.assertEqual(next(s["latest"] for s in streams if s["kind"] == "cip"), "v6.12.111-cip32")
        self.assertIn("official Git tags show v6.12.111-cip32", notices[0])

    def test_source_failure_is_explicit_and_other_source_continues(self):
        def fetch(url):
            if url == releases.KERNEL_FEED:
                raise OSError("feed unavailable")
            return cip_page().encode()
        with patch.object(releases, "fetch_bytes", side_effect=fetch):
            with patch.object(releases, "read_tags", return_value=catalog("v6.12.105-cip30", "v6.12.108-cip31")):
                streams, _, _, errors = releases.discover_sources()
        self.assertEqual(len(streams), 1)
        self.assertIn("kernel.org: feed unavailable", errors)

    def test_malformed_cip_page_is_not_an_empty_success(self):
        for html in ("maintenance", cip_page(version="v6.1.111-cip32"), cip_page().replace("latest version:", "changed field:")):
            with self.subTest(html=html), self.assertRaises(ValueError):
                releases.cip_streams(html)

    def test_missing_predecessor_or_disappeared_cursor_never_guesses(self):
        stream = stable_stream("6.12.2")
        with self.assertRaisesRegex(ValueError, "predecessor"):
            releases.adjacent_releases(stream, catalog("v6.12.2"))
        with self.assertRaisesRegex(ValueError, "disappeared"):
            releases.adjacent_releases(stream, catalog("v6.12.1", "v6.12.2"), "v6.12.0")
        with self.assertRaisesRegex(ValueError, "backwards"):
            releases.adjacent_releases(stream, catalog("v6.12.1", "v6.12.2", "v6.12.3"), "v6.12.3")


if __name__ == "__main__":
    unittest.main()
