"""Record the observations made by the public comparison method."""

from pathlib import Path
from urllib.parse import urlparse

from kcidev import KciDevError, KernelCIClient

DEFAULT_HISTORY_HOURS = 720  # Dashboard tree-report API maximum: 30 days.


class ReviewClient(KernelCIClient):
    """Keep the exact listings used by compare_results, without fetching twice.

    All HTTP requests and classifications are implemented by kci-dev.
    Public method overrides record data and bound supplemental issue requests.
    Collection resets observations at the start of each comparison.
    """

    def __init__(self, *args, max_issue_lookups=20,
                 history_hours=DEFAULT_HISTORY_HOURS, **kwargs):
        if not isinstance(max_issue_lookups, int) or not 0 <= max_issue_lookups <= 1000:
            raise ValueError("max_issue_lookups must be 0..1000")
        if type(history_hours) is not int or not 1 <= history_hours <= 720:
            raise ValueError("history_hours must be an integer from 1 to 720")
        super().__init__(*args, **kwargs)
        self.max_issue_lookups = max_issue_lookups
        self.history_hours = history_hours
        self.begin_comparison()

    def begin_comparison(self):
        """Start a fresh observation set without mutating earlier reports."""
        self.snapshots = {}
        self.issue_lookups = {}
        self.issue_requests = 0
        self.history_lookup = {"state": "not_requested"}
        self._history_selection = None

    def _record(self, section, args, kwargs, response):
        names = ("origin", "giturl", "branch", "commit")
        scope = tuple(args[i] if len(args) > i else kwargs[n]
                      for i, n in enumerate(names))
        if not isinstance(response, dict) or not isinstance(response.get(section), list):
            raise KciDevError(f"Unexpected {section} response: expected a result list")
        if not all(isinstance(row, dict) for row in response[section]):
            raise KciDevError(f"Unexpected {section} response: invalid result entries")
        self.snapshots[(*scope, section)] = response[section]
        return response

    def get_builds(self, *args, **kwargs):
        return self._record("builds", args, kwargs, super().get_builds(*args, **kwargs))

    def get_boots(self, *args, **kwargs):
        return self._record("boots", args, kwargs, super().get_boots(*args, **kwargs))

    def get_tests(self, *args, **kwargs):
        return self._record("tests", args, kwargs, super().get_tests(*args, **kwargs))

    def compare_results(self, base, head, giturl, branch, origin="maestro",
                        include_issues=False):
        # tree-report selects a branch checkout, not the requested commit.
        # Never let another checkout's history alter this comparison.
        self._history_selection = {"origin": origin, "git_url": giturl,
                                   "git_branch": branch, "commit_hash": head}
        try:
            return super().compare_results(base=base, head=head, giturl=giturl,
                                           branch=branch, origin=origin,
                                           include_issues=include_issues)
        finally:
            self._history_selection = None

    def get_tree_report(self, origin, git_branch, git_url, test_path=None,
                        history_size=10, max_age_in_hours=None, min_age_in_hours=0):
        if max_age_in_hours is None:
            max_age_in_hours = self.history_hours
        self.history_lookup = {"state": "requested",
                               "history_size": history_size,
                               "max_age_in_hours": max_age_in_hours,
                               "min_age_in_hours": min_age_in_hours}
        if self._history_selection:
            self.history_lookup["requested_head"] = self._history_selection["commit_hash"]
        try:
            response = super().get_tree_report(
                origin, git_branch, git_url, test_path=test_path,
                history_size=history_size, max_age_in_hours=max_age_in_hours,
                min_age_in_hours=min_age_in_hours)
            if not isinstance(response, dict):
                raise KciDevError("Unexpected tree history response")
            for key in ("commit_hash", "checkout_start_time", "origin", "git_url", "git_branch"):
                if key in response:
                    self.history_lookup[key] = response[key]
            if self._history_selection:
                mismatches = [key for key, value in self._history_selection.items()
                              if response.get(key) != value]
                if mismatches:
                    raise KciDevError(
                        "Tree history does not match the selected head checkout "
                        f"({', '.join(mismatches)}); selected head "
                        f"{self._history_selection['commit_hash']}, returned "
                        f"{response.get('commit_hash', 'unknown')}. "
                        "Only the exact-commit result comparison is available.")
        except Exception as exc:
            self.history_lookup.update(state="error", error=f"{type(exc).__name__}: {exc}")
            raise
        self.history_lookup["state"] = "fetched"
        return response

    def _issues(self, kind, result_id, fetch, *args, **kwargs):
        key = f"{kind}:{result_id}"
        entry = self.issue_lookups.get(key)
        if entry is None:
            entry = {"kind": kind, "result_id": result_id}
            self.issue_lookups[key] = entry
            if self.issue_requests >= self.max_issue_lookups:
                entry.update(state="limited", error="Issue lookup limit reached")
            else:
                self.issue_requests += 1
                try:
                    issues = fetch(result_id, *args, **kwargs)
                    if not isinstance(issues, list):
                        raise KciDevError("Unexpected known-issue response")
                    entry.update(state="fetched", issues=issues)
                except Exception as exc:
                    entry.update(state="error", error=f"{type(exc).__name__}: {exc}")
        if entry["state"] != "fetched":
            raise KciDevError(entry["error"])
        return entry["issues"]

    def get_build_issues(self, build_id, *args, **kwargs):
        return self._issues("build", build_id, super().get_build_issues, *args, **kwargs)

    def get_boot_issues(self, test_id, *args, **kwargs):
        return self._issues("test", test_id, super().get_boot_issues, *args, **kwargs)


def make_client(config=None, instance=None, dashboard_api=None, max_issue_lookups=20,
                history_hours=DEFAULT_HISTORY_HOURS):
    cfg = None
    if config:
        try:
            import tomllib
        except ModuleNotFoundError:  # Python 3.10, supplied by kci-dev.
            import tomli as tomllib
        with Path(config).expanduser().open("rb") as stream:
            cfg = tomllib.load(stream)
    selected = instance or (cfg or {}).get("default_instance")
    if selected:
        profile = (cfg or {}).get(selected)
        if not isinstance(profile, dict):
            raise ValueError(f"Instance {selected!r} is not defined in the config")
        if not (dashboard_api or profile.get("dashboard_api") or cfg.get("dashboard_api")):
            raise ValueError(
                "Set dashboard_api for the selected instance or pass --dashboard-api; "
                "the instance's Maestro URL does not select a dashboard"
            )
    client = ReviewClient(cfg=cfg, instance=selected, dashboard_api=dashboard_api,
                          max_issue_lookups=max_issue_lookups, history_hours=history_hours)
    url = urlparse(client.dashboard_api)
    if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password:
        raise ValueError("dashboard_api must be an HTTP(S) URL without embedded credentials")
    return client
