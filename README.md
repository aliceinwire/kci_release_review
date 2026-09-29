# KernelCI release review

A standalone Python application using kci-dev's public `KernelCIClient` API.
It compares two tested kernel revisions, preserves the library's classifications,
and writes a portable HTML report plus the complete JSON report. It can also
discover trees and their previously tested commits.

The application makes read-only queries. It does not trigger jobs, submit results,
or modify GitHub. The kci-dev command-line interface is not invoked.

## Install

Requires Python 3.10 or newer and Git. From this directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m kci_release_review --help
```

The dependency is pinned to the inspected kci-dev commit
`ba6b7134f1296702182b6559b5a2320f3d6b40fa`. Its package version is `0.1.11`,
so a version number alone does not establish that the installed package has the
same API. To work against your own kci-dev checkout instead, install that checkout
into the environment with `python -m pip install /absolute/path/to/kci-dev`.

## First comparison

The following pair was discovered from the public dashboard on 2026-09-28:
linux-6.12.y, v6.12.110 (base) and v6.12.111 (head). The included
`examples/live-stable-6.12/` directory contains the real captured result.

```sh
python -m kci_release_review compare \
  --giturl https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git \
  --branch linux-6.12.y \
  --base fa5b06866b643cce7072a4e7c6be7ee3f0f1b95c \
  --head e2acc2211022246c77740d5df08265cc27eedcc5 \
  --max-issue-lookups 20 \
  --max-evidence 3 \
  --log-bytes 8192 \
  --out reports/stable-6.12
```

Open `reports/stable-6.12/report.html` in your browser. Keep `report.json` beside
it so the full-data link works. No server is needed. Existing output files are
protected unless `--force` is supplied. Without `--out`, each run gets a new
timestamped directory.

The captured comparison contains 1 regression candidate, 36 fixes, 389 persistent
failures, 5,426 new entries, and 796 missing entries. These are the library's
classifications of executions, not independently confirmed kernel bugs or fixes.
The live result is incomplete: the default history window did not include this
tree, the issue budget left some IDs unchecked, and the execution environment
could not resolve the log host. Detail responses and these errors are preserved.

## Discover other tested revisions

```sh
python -m kci_release_review trees --origin maestro --days 7 > trees.json

python -m kci_release_review history \
  --giturl https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git \
  --branch linux-6.12.y \
  --head e2acc2211022246c77740d5df08265cc27eedcc5 > history.json
```

Use full commit hashes that occur in the dashboard for the same origin, repository
URL, and branch. Do not infer that consecutive entries in this testing history are
adjacent Git commits. Discovery commands return the library's data unchanged;
diagnostics go to stderr, keeping stdout valid JSON.

## What the report means

- `comparison` is the public API's comparison object, including all categories,
  result identities, duplicate occurrences, source IDs, known issues, and the
  `incomplete` flag. The application does not reimplement classification.
- `observations` counts the exact result lists used by that comparison. Unchanged
  passes are included here even when the comparison's change list is empty.
- `history_lookup` records the history request and any failure. The pinned library
  requests a recent tree report, by default the last 24 hours, even when explicitly
  comparing older hashes. This application exposes that limitation.
- `issues.lookups` distinguishes fetched, failed, and budget-limited lookups.
  Empty `known_issues` is only evidence of no associated issue when the lookup was
  actually fetched. The pinned API enriches regressions and persistent failures,
  not every newly observed failure. Its lookup order is retained.
- `evidence` contains selected failing result details and bounded test-log tails.
  Regression candidates are expanded first. Build logs remain URLs/excerpts in
  build details because the public `get_log()` method accepts test IDs.
- Log truncation, scan-limit, and deadline flags are preserved. A scan-limited log
  might not contain the file's true end. Errors do not discard successful detail
  retrieval or the underlying comparison.
- HTML shows up to 50 entries per category. JSON preserves every comparison entry.

| Exit code | Meaning |
|---|---|
| 0 | No review signals were found in observed results. This is not release approval. |
| 1 | Comparison available, with failures, missing results, instability, or other unresolved outcomes requiring review. |
| 2 | Core comparison evidence is incomplete, an issue budget was exhausted, a request failed, or input/configuration was invalid. |

Required jobs, architectures, expected coverage, pending jobs, and release policy
are not evaluated. `required_coverage` is always `not_assessed`, and
`release_approved` is always false. Intentionally omitted supplemental details and
unavailable logs are documented separately; those omissions do not erase the
comparison. For a production release gate, define a coverage policy first.

## Request limits

Defaults are 20 unique known-issue lookups, 5 expanded failing result IDs, and
16,384 bytes per test log. `--no-issues` disables known-issue enrichment;
`--max-evidence 0` disables supplemental details; `--log-bytes 0` disables log
downloads. Increasing the limits can make many additional requests. All selected
checkout listings are fetched regardless of these supplemental limits.

## Another dashboard or configured instance

```toml
# review.toml
[staging]
dashboard_api = "https://your-staging-dashboard.example/api/"
```

Add `--config review.toml --instance staging` to a subcommand, or pass
`--dashboard-api https://your-staging-dashboard.example/api/` directly.
These are placeholder URLs. Supply your actual endpoint.

An explicitly selected instance must have a dashboard URL, either in config or
through the override. A Maestro `api` URL alone will not silently select the
production dashboard. No token is needed for public dashboard queries.

## Tests

```sh
python -m unittest discover -s tests -v
```

Tests run the actual kci-dev comparison algorithm with controlled service
responses. They cover empty and partial data, duplicate executions, missing
results/history, issue limits, routing, log errors, output fidelity, and HTML
escaping. Unit tests do not contact external services.

## Code layout

- `client.py`: public API subclass that records observations and limits issue calls.
- `report.py`: evidence collection, completeness assessment, and JSON structure.
- `html_report.py`: escaped static HTML presentation with source links.
- `__main__.py`: command-line input, configuration, and file output.

Source references:

- https://github.com/kernelci/kci-dev/blob/ba6b7134f1296702182b6559b5a2320f3d6b40fa/kcidev/api.py
- https://github.com/kernelci/kci-dev/blob/ba6b7134f1296702182b6559b5a2320f3d6b40fa/kcidev/libs/regression.py
