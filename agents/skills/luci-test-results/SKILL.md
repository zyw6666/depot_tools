---
name: luci-test-results
description: >
  Monitor, triage, and analyze LUCI build and CQ trybot results (including
  tests and compile). Use this to monitor Gerrit CL trybots or watch CQ status
  (preferring subagents for polling), find failed builds for a CL, group test
  failures by Swarming task, check specific test runs, and query cross-build
  test history. For high-level builder step logs or compile errors, use the
  'buildbucket' skill.
---

# LUCI Triage Cheat Sheet

This skill provides modular scripts for deep-diving into **Test-level**
failures and test history across LUCI shards and tasks.

> [!TIP]
> **CLI Help & Flags**: Run any script with `-h` or `--help` (e.g.
> `vpython3 scripts/list_failures.py -h` or
> `vpython3 scripts/test_history.py -h`) to view all supported arguments,
> defaults, and flags without reading the script source files.

## 1. Resolve Build ID & Inspect Builds

Resolve a builder name + build number to a canonical `<BUILD_ID>`, or fetch
detailed build properties:

```bash
# Resolve builder + build number to Buildbucket ID:
vpython3 scripts/luci_client.py resolve-build-id \
  --builder "<BUILDER>" \
  --build-number <NUMBER> \
  [--project chromium] \
  [--bucket ci]

# Get detailed build metadata and step summaries:
vpython3 scripts/luci_client.py get-build \
  --build-id <BUILD_ID>
```

For example, for the URL
`https://ci.chromium.org/ui/p/chromium/builders/try/linux-chromeos-rel/2769679/overview`:
```bash
vpython3 scripts/luci_client.py resolve-build-id \
  --builder "linux-chromeos-rel" \
  --build-number 2769679 \
  --project chromium \
  --bucket try
```

## 2. Find Builds for Gerrit CL

Find builds for a specific CL and patchset (defaults to non-successful builds):

```bash
vpython3 scripts/find_cl_builds.py \
  [--cl <CL_NUMBER_OR_URL>] \
  [--patchset <PATCHSET>] \
  [--all] \
  [--all-attempts] \
  [--merge-reused] \
  [--host <HOST>]
```

> [!NOTE]
> - If `--cl` is omitted, the script auto-detects the active branch's CL via
>   `git cl issue`. You can pass either a numeric CL ID (`8260911`) or a full
>   Gerrit/crrev URL.
> - By default, this command deduplicates retry attempts per builder (keeping
>   the newest build ID) and returns only builds that did not succeed
>   (e.g., `FAILURE`, `INFRA_FAILURE`). Pass `--all` to include `SUCCESS` and
>   `STARTED` builds, or `--all-attempts` to include superseded retry builds.
> - If `--patchset` is omitted, the script auto-detects the latest patchset via
>   the Gerrit REST API (with automatic retries and caching). If the latest
>   patchset has 0 builds, it retries up to 3 preceding patchsets and reports
>   which one it used in the `patchset` field of each returned build (plus a
>   `stderr` notice). Pass `--merge-reused` to also merge reused CQ builds from
>   recent patchsets when the latest patchset has `<= 5` builds (such as after
>   a trivial rebase or description edit).
> - **Gerrit Auth Issue**: Auto-detecting patchset for internal CLs
>   (on `chromium-review.git.corp.google.com`) might fail with auth errors.
>   Workaround: Provide `--patchset` explicitly.

## 2b. Monitor CL Try Jobs (`monitor.py`)

Check or poll the aggregate CQ trybot status across one or more Gerrit CLs.

### Manual Check (Immediate Status)

```bash
# Check the active branch's CL (auto-detected via `git cl issue`):
vpython3 scripts/monitor.py

# Check one or more specific Gerrit CL numbers or URLs:
vpython3 scripts/monitor.py <CL_1> [<CL_2> ...] [--patchset <PATCHSET>] [--host <HOST>] [--json]
```

**Deterministic Exit Codes:**
- `0`: All jobs passed (`ALL JOBS PASSED`).
- `1`: One or more jobs failed (`SOME JOBS FAILED`).
- `2`: Jobs are still running and none have failed yet (`SOME JOBS STILL RUNNING`).
- `3`: Error or no jobs found (`UNKNOWN STATE`).

### Subagent Delegation (Preferred for Watching CQ)

Trybot runs frequently take 30 to 90 minutes. Running recurring polling directly
in the primary agent floods the conversation history and ties up the primary
context. When asked to watch or monitor CQ/trybots until completion, **delegate
monitoring to a background subagent**:

- Spawn a subagent (`TypeName: "self"`, `Role: "Trybot Monitor"`) with
  `Model: "flash"` (polling CQ status is lightweight; reserve deeper reasoning
  models for the parent agent).
- Have the subagent schedule a recurring poll every 10 minutes (`*/10 * * * *`,
  `IsDaemon: false`) that runs:
  - `vpython3 <path_to>/scripts/monitor.py <CL_NUMBER>`
  - `git cl comments -i <CL_NUMBER>`
- When `monitor.py` exits with `0` (all passed) or `1` (some failed):
  - Cancel the scheduled cron task.
  - Check for any unaddressed reviewer comments via `git cl comments -i <CL_NUMBER>`.
  - If any builds failed (exit code `1`), triage the unexpected failures using
    `find_cl_builds.py` -> `list_failures.py` -> `fetch_log.py`.
  - Send a concise summary of the final status, any failure diagnostics, and any
    new reviewer comments back to the parent agent.
- After launching the subagent, inform the user that a background monitor was
  dispatched and do not poll in a loop in the parent agent.

### Checking Review Comments During CQ Monitoring

When checking or polling a CL, also fetch reviewer comments from Gerrit:

```bash
git cl comments -i <CL_NUMBER>
```

When reporting reviewer comments to the user:
- **Summarize & Link**: Summarize unaddressed reviewer comments and link each CL
  (`https://crrev.com/c/<CL_NUMBER>`).
- **Propose Improvements**: For comments requesting code or design changes, list
  a concrete proposed improvement (target file, line, and approach).
- **Draft Replies**: Provide a ready-to-copy reply written in the first-person
  voice of the CL owner so the user can paste it into Gerrit.
- **Strict Guardrails**:
  - **NEVER** automatically publish, post, or reply to comments on Gerrit.
  - **NEVER** implement code changes for review comments without explicit user
    approval first.

## 3. List Unexpected Failures

Get a clean list of tests that failed unexpectedly in a build, deduplicated and
grouped by Swarming task:

```bash
vpython3 scripts/list_failures.py \
  --build-id <BUILD_ID> \
  [--ignore-flaky] \
  [--include-exonerated] \
  [--limit <LIMIT>]

# Or resolve builder directly:
vpython3 scripts/list_failures.py \
  --builder "<BUILDER>" \
  --build-number <NUMBER> \
  [--project chromium] \
  [--bucket ci] \
  [--limit <LIMIT>]
```

- **Filtering:** By default, exonerated test variants (known flakes and baseline
  expectations) are excluded, and results are sorted with unexonerated
  `UNEXPECTED` regressions first.
- **Ignore Flakes:** Use `--ignore-flaky` to filter out flaky tests and return
  only unexonerated `UNEXPECTED` failures.
- **Include Exonerated:** Use `--include-exonerated` to include exonerated test
  variants in the output.
- **Triage Priority:** If multiple tests share a `task` ID, triage **one**
  result first (often indicating a shard crash or runner failure).

## 4. Fetch Log Snippet

Retrieve a filtered failure log snippet using the result name (`res`) from step
3:

```bash
vpython3 scripts/fetch_log.py \
  --res "<RES_NAME>" \
  [--raw]
```

## 5. Check Specific Test in a Single Build (`check_test.py`)

Query **ResultDB** for all runs (passing and failing) matching a regex inside **1
specific build**:

```bash
vpython3 scripts/check_test.py \
  --build-id <BUILD_ID> \
  --test-regex "<TEST_REGEX>"

# Or resolve builder directly:
vpython3 scripts/check_test.py \
  --builder "<BUILDER>" \
  --build-number <NUMBER> \
  [--project chromium] \
  [--bucket ci] \
  --test-regex "<TEST_REGEX>"
```

- **When to use (`check_test` vs. `list_failures` / `test_history`)**:
  - Unlike `list_failures.py` (which only returns unexpected failures), `check_test.py`
    fetches **all** results (`expectancy: ALL`, including `PASS`), returning each
    result's `res` resource name (usable directly with `fetch_log.py --res <RES>`)
    and `err` (`primaryErrorMessage`). Use it to verify that a test actually ran and
    passed in a specific build, or to discover the full ResultDB `testId` string from
    a partial class/method regex.
  - Unlike `test_history.py` (which looks across time/builders), `check_test.py`
    is strictly scoped to a single build.

## 6. Query Test History Across Builds & Builders (`test_history.py`)

Query **LUCI Analysis** for historical verdicts of **1 specific test ID** (or
substring) across time and CI/try builders:

```bash
# Formatted per-builder summary (default):
vpython3 scripts/test_history.py \
  (--test-id '<TEST_ID>' | --test-substring '<CLASS_OR_METHOD_SUBSTRING>') \
  [--project <chromium|chrome>] \
  [--builder <BUILDER>] \
  [--bucket <BUCKET>] \
  [--device-os <DEVICE_OS>] \
  [--device-type <DEVICE_TYPE>] \
  [--os <OS>] \
  [--test-suite <TEST_SUITE>] \
  [--limit <LIMIT>] \
  [--raw]
```

> [!IMPORTANT]
> **Single Quotes Required**: ResultDB Test IDs often contain exclamation marks
> (e.g. `!junit` or `!gtest`). Always enclose `--test-id` in **single quotes**
> (`'...'`) to prevent Bash from attempting history expansion
> (`bash: !...: event not found`).

- **When to use**: Once you have a test ID (from `list_failures.py` or `check_test.py`),
  or a `Class#method` substring (`--test-substring`, mutually exclusive with
  `--test-id`), use `test_history.py` to check if a failure is a fresh trunk
  regression, a known flake across multiple builders, or isolated to your CL.
- **Parallel Per-Builder Fair Sampling**: When `--builder` is omitted, discovers
  active builders via `QueryVariants` and fetches recent verdicts per builder in
  parallel (`--limit` defaults to `15` verdicts/builder across `ci` builders, or `100`
  verdicts when a single `--builder` is specified). This prevents high-frequency bots
  from crowding out slower platform/form-factor bots.
- **Per-Builder Structuring**: Failing and flaky builders are sorted first, while
  100% passing builders are condensed into a single summary line.
- **Timeline Glyphs**: Displays recent verdicts ordered newest →
  oldest (`P` = Pass, `F` = Fail, `R` = Flaky/Pass on Retry, `S` = Skip), along
  with daily breakdowns.
- **Raw Output**: Use `--raw` to print raw JSON verdicts instead of the summary.
- **Substring Resolution & Fuzzy Search Fallback**: Pass `--test-substring` (or a
  short `Class#method` to `--test-id`) to auto-resolve via `QueryTests`
  (`--test-id` and `--test-substring` are mutually exclusive). If no verdicts are
  found, matching candidate Test IDs are printed.

## Troubleshooting

- **"No Artifacts Found"**: This can happen if the build failed before producing
  ResultDB artifacts, if the logs were purged (old build), or if there is an
  auth issue (`bb auth-login`).
- **Fallback**: If artifacts are missing, use `bb log <build_id> <step_name>`
  (from the `buildbucket` skill) to see the raw step output.

## Implementation Notes

1. **Task-Based Triage:** A shard crash often manifests as
   `CascadingFailureException`. Triage the root failure in that shard first by
   checking the first failure in a task group.
2. **Log Filtering:** The `fetch_log.py` command automatically filters for
   `AssertionError`, `FATAL`, `Exception`, `FAIL`, and leak reports to keep the
   context window clean.
