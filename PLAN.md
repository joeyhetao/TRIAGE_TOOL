# xlog Architecture Plan

## Positioning

`xlog` is the regression-log provider for `xregress`. It owns recursive log
discovery, case status detection, error extraction, first-error deduplication,
representative-case selection, xdebug recommendation, and the versioned result
bundle. It does not provide a web UI, LLM analysis, knowledge-base lookup,
database storage, or artifact debugging.

## Public Interface

The stable machine interface is the `xlog.v1` JSON action envelope. Version 1
publishes `actions`, `schema`, and `scan`. The `bin/xlog` command exposes the
same actions through `xlog --json <request>` and command-line shortcuts.

`scan` requires a regression root and an absolute bundle output path. Stdout
contains one JSON action response only; diagnostics use stderr. The complete
scan result is atomically written as `xlog_bundle.v1` so xregress can import it
without reimplementing scanner, clustering, or xdebug recommendation rules.

## Data Ownership

- A discovered `.log` file is one case. `case_id` is its POSIX path relative to
  the regression root, so equal file names in separate directories stay distinct.
- Each case keeps the first five non-warning errors in appearance order.
- Each case derives `test_id` and `seed` from the log filename. The default rule
  treats a trailing `_<digits>` suffix in the file stem as the seed; unmatched
  names keep the stem as `test_id` and record `seed_parse_status: fallback`.
- Only the first non-warning error of a failed case contributes to a failure
  cluster. Its preferred identity is `level + error_id + location`; normalized
  description is used only when the ID or location is absent.
- A cluster publishes a deterministic SHA-256 ID, its first sorted member as the
  representative case, member case IDs, the representative real error, and a
  deterministic recommendation record for downstream xdebug selection.
- Per-log read failures remain visible as `status: error` and never stop the
  batch. A missing PASS marker without a parseable error is a failed,
  unclustered case.

## xdebug Recommendation

`xlog` recommends a bounded set of failed cases for later `xdebug` analysis so
xregress does not need to run expensive debug on every deduplicated error. The
selection is deterministic and auditable; LLMs are not part of the initial
recommendation path.

- `scan` accepts `limits.debug_budget` and CLI `--debug-budget`; the default is
  `20`. If fewer clusters are eligible, xlog emits the actual count.
- The bundle publishes `debug_recommendation` with policy version, budget,
  eligible count, selected count, `recommended_debug_cases`, and
  `deferred_cluster_ids`.
- Each failure cluster publishes `recommendation` with rank, selected flag,
  recommended case, alternate case IDs, score components, and reasons.
- Cluster priority is severity first (`FATAL` before `ERROR`), then distinct
  test count, same-test seed coverage, occurrence count, signature completeness,
  and stable cluster ID tie-break.
- Candidate selection is independent inside each failure cluster. It first
  prefers a case with a known total simulation time, then the shortest total
  simulation time, then complete local evidence, stronger seed coverage for the
  test, smaller numeric seed, and stable `case_id`. A repeated `test_id` in a
  previously selected cluster must not override the shortest-time choice.
- `simulation_time` records raw value, unit, normalized femtoseconds, and
  source. Parser priority is an explicit VCS simulation-end/report time, then
  the largest observed simulation timestamp in the log, then `unavailable`.
  CPU, wall-clock, elapsed, and real runtime fields are never simulation time.
- `xregress` should consume the recommended primary case first and may use
  alternates if downstream artifacts are missing. Unclustered failures are not
  automatically selected for xdebug.

## Configuration

Built-in parser defaults preserve the legacy extra-error and PASS markers. An
optional JSON config supplies `extra_patterns` and `pass_patterns`. Request
`args.parser` fields override matching config-file fields; omitted fields keep
their lower-priority values. The bundle records the final effective configuration.

## Package Layout

```text
bin/xlog                 executable wrapper
src/xlog/                CLI, actions, scanner, parser, dedup, recommendation and bundle code
schemas/                 public request and bundle schemas
config/default_parser.json
tests/                   parser, dedup, recommendation, scan, CLI and schema tests
```

The package uses only the Python standard library. New scan formats, artifact
resolvers, or project-specific discovery conventions must be explicit,
versioned extensions rather than hidden xregress logic.

## Release Rules

The release archive is `XLOG-linux-<version>.tar.gz`. It includes only the
CLI, source, schemas, default configuration and documentation. Test artifacts,
logs, old triage UI files, databases, secrets and third-party wheels are not
distributed.
