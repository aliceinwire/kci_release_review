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

## Publish comparisons with GitHub Actions and Pages

`.github/workflows/release-review-pages.yml` tests the application, runs every
entry in `comparisons.json`, and deploys the resulting static site. It runs on
relevant pushes to `main`, daily at 03:23 UTC, and through **Run workflow** in
the Actions tab. Scheduled runs can be delayed by GitHub. Pull requests run the
offline tests and validate the configuration, without querying KernelCI or
publishing. Manual runs on branches other than `main` also only validate.

### First deployment

1. In **Settings > Pages > Build and deployment**, select **GitHub Actions** as
   the source. This is required once, including for a repository with no existing
   Pages site. The normal workflow token cannot enable Pages by itself.
2. Review `comparisons.json`, apply the changes to `main`, and open
   **Actions > Release review Pages > Run workflow** if a run has not started.
3. Open the URL shown by the `github-pages` deployment. With the default project
   domain for this repository, it is
   <https://aliceinwire.github.io/kci_release_review/>.

No personal access token or additional secret is needed. The build job has only
`contents: read`; only the separate deployment job receives `pages: write` and
`id-token: write`. The workflow uses the `github-pages` environment and allows
deployment only from `main`. If that environment has protection rules, allow
`main` and satisfy any configured review requirement. Deployment uploads a Pages
artifact without committing generated files or creating a `gh-pages` branch.
If Pages previously served other content, the next deployment replaces it with
this comparison site. Existing custom-domain settings remain managed in Pages.

### Select the comparisons

The initial manifest uses the same real linux-6.12.y commit pair as the example
above. It deliberately uses explicit hashes: scheduled runs refresh evidence
for those pairs; they do **not** discover or advance to newer kernel releases.
Use the existing `trees` and `history` commands to find other tested hashes,
then update the manifest. Add more entries to publish several comparisons in
one site. Keep entries you still want linked from the index.

Each entry requires `id`, `giturl`, `branch`, `base`, and `head`. IDs are unique,
at most 64 characters, and use lowercase letters, digits, and single hyphens.
They become stable report directories, such as `stable-6-12/report.html`.
Base and head must be different full 40-character hashes. Optional fields are:

| Field | Default | Meaning |
|---|---|---|
| `title` | `id` | Label on the index |
| `origin` | `maestro` | KernelCI result origin |
| `dashboard_api` | kci-dev default | Public HTTP(S) dashboard endpoint |
| `include_issues` | `true` | Whether to fetch known issues |
| `max_issue_lookups` | `20` | Issue request budget, 0 to 1,000 |
| `max_evidence` | `3` | Expanded failing result IDs, 0 to 100 |
| `log_bytes` | `8192` | Bytes per test log, 0 to 1,048,576 |

Pages publishes the full JSON evidence and bounded log excerpts as well as HTML.
Use comparisons whose results are intended to be public. Unknown fields and
invalid selections fail validation before any comparison requests are made.

### Outcomes, updates, and retained reports

The site contains an `index.html`, a machine-readable `summary.json`, and the
unmodified `report.html` / `report.json` pair for each configured comparison.
All internal links are relative, including the full-data link in each report,
so both a project Pages URL and a custom domain work without a base-path option.

The site builder invokes `python -m kci_release_review compare` for each entry.
CLI exit codes **0, 1, and 2** are publishable only when both report files exist
and the JSON assessment matches the exit code and selected commits. Review
findings and incomplete evidence remain visible in the index, reports, workflow
warnings, and Actions job summary. A diagnostic report with no comparison is
published explicitly as incomplete, with counts shown as unavailable.

A crash, timeout, missing output, invalid JSON, or mismatched report fails the
build and prevents deployment, leaving the previously deployed site in place.
Each comparison has a 15-minute timeout; the build job has a 45-minute timeout.
Reduce request budgets or split large comparison lists if needed. Publication
success describes report generation and deployment, not kernel release approval.

Every successful deployment replaces the site with the current configured set;
it is not a growing archive of workflow runs. Each run's `github-pages` artifact
is retained for 14 days, subject to repository or organization retention limits.
The site itself remains available until another deployment replaces it.

### Build locally

After installing `requirements.txt`, validate and generate the same site:

```sh
python -m kci_release_review.pages --check
python -m kci_release_review.pages --config comparisons.json --out _site
```

Open `_site/index.html` in a browser. The output directory must be empty or new;
use a different `--out` path for subsequent local builds. Configuration checking
is offline; generating the site makes the application's read-only API requests.
The site builder adds no Python dependencies.

GitHub references:

- <https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages>
- <https://github.com/actions/configure-pages/blob/v5/action.yml>

## Code layout

- `client.py`: public API subclass that records observations and limits issue calls.
- `report.py`: evidence collection, completeness assessment, and JSON structure.
- `html_report.py`: escaped static HTML presentation with source links.
- `__main__.py`: command-line input, configuration, and file output.
- `pages.py`: manifest validation, comparison runner, static index, and Actions summary.
- `comparisons.json`: the tested commit pairs to refresh and publish.
- `.github/workflows/release-review-pages.yml`: tests, scheduled builds, and Pages deployment.

Source references:

- https://github.com/kernelci/kci-dev/blob/ba6b7134f1296702182b6559b5a2320f3d6b40fa/kcidev/api.py
- https://github.com/kernelci/kci-dev/blob/ba6b7134f1296702182b6559b5a2320f3d6b40fa/kcidev/libs/regression.py
