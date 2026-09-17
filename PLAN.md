# xlog Architecture Plan

## Positioning

`xlog` is the deterministic regression-log provider consumed through
`xregress` MCP Lite by a single external Codex/Claude Agent. It owns recursive
log discovery, case status detection, error extraction, first-error
deduplication, representative-case selection, optional xdebug recommendation,
and the versioned result bundle. It does not provide a web UI, LLM analysis,
knowledge-base lookup, database storage, or artifact debugging.

The external Codex/Claude Agent is the only reasoning and orchestration actor.
`xlog` publishes deterministic facts and optional recommendations; it never
chooses whether xverif MCP is invoked or fixes the investigation order.

## Public Interface

The stable machine interface is the `xlog.v1` JSON action envelope. Version 1
publishes `actions`, `schema`, and `scan`. The `bin/xlog` command exposes the
same actions through `xlog --json <request>` and command-line shortcuts.

`scan` requires a regression root and an absolute bundle output path. Stdout
contains one JSON action response only; diagnostics use stderr. The complete
scan result is atomically written as `xlog_bundle.v1` so xregress can import it
without reimplementing scanner, clustering, or xdebug recommendation rules.

### Bundle schema evolution

`xlog_bundle.v1` keeps its envelope major version while carrying a compatible
`schema_revision`. A missing revision means legacy `1.0`. Minor revisions may add
structured fields, but consumers must preserve and accept older valid bundles.
`description_template` and `description_template_status` are introduced in revision
`1.1`: a new producer must emit either a normalized template with `present`, or
`unavailable` with a reason. A 1.0 bundle may omit both; its consumer must represent
the template as unknown rather than reconstructing it from log text. Making a
non-empty template universally mandatory would require a future major bundle version.

Revision `1.2` adds non-authoritative `scope_hint`, a path-independent
`portable_signature`, explicit recommended/alternate case snapshots, and the RTL
injection import fixture. These fields are deterministic candidate facts only.
`scope_hint.final_routing` is always `undetermined`; xlog never emits a Wiki target
or a root-cause decision.

Revision `1.3` adds an explicit `artifacts.manifests` snapshot without changing
the revision 1.2 meaning of `resources.run_manifest`. The legacy resource remains
the xvp-owned `xvp_case_manifest.v1`; the new snapshot records separate
`xvp.case_manifest` and `xdebug.run_manifest` descriptors with expected and
declared schema versions, path candidates, resolution status, parse status and
selection status. A valid published `xdebug.run-manifest.v1` is preferred. A
valid xvp manifest is only a labeled `legacy_fallback` fact and is never passed
to xdebug as `target.run_manifest`. Revision 1.2 bundles that omit the new
snapshot remain valid inputs.

## Data Ownership

- A discovered `.log` file is one case. `case_id` is its POSIX path relative to
  the regression root, so equal file names in separate directories stay distinct.
- Rerun backup logs whose filename ends in `_bk.log` (case-insensitive) are
  excluded during discovery, before ordering and log-count limits. They never
  enter parsing, case statistics, first-error clustering, or recommendations.
- Recursive discovery applies the filename, `.log` suffix and backup filters
  before filesystem type validation. Non-log artifacts therefore do not incur
  per-path `is_file` or `stat` work in the xlog candidate filter.
- On the normal scan path, `parse_log` streams each log once and collects
  parser facts plus artifact references during that same pass. The artifact
  snapshot consumes the supplied reference set, including an explicitly empty
  set, without reopening the log. A missing reference set is reserved for
  compatibility callers and parse-error recovery.
- Each case keeps the first five non-warning errors in appearance order.
- Each case derives `test_id` and `seed` from the log filename. The default rule
  treats a trailing `_<digits>` suffix in the file stem as the seed; unmatched
  names keep the stem as `test_id` and record `seed_parse_status: fallback`.
- Only the first non-warning error of a failed case contributes to a failure
  cluster. Environment and unknown-origin identities are
  `level + error_id + location + description_template`. Tool/framework candidate
  identities use `level + error_id + producer + portable description_template`,
  excluding source path, line number and dynamic values. Static message changes
  still create a new cluster.
- Each case publishes a structured primary-error report ID, error type, source
  location and event time. Revision 1.1 additionally publishes normalized
  `description_template` plus `description_template_status`; each failure cluster
  publishes the same template/status and a stable SHA-256 fingerprint for xregress
  and xmanager references.
- `description_template` is a versioned structured fact, never a root-cause
  inference. `scope_hint` distinguishes only a deterministic shared-tool/framework,
  environment, or unknown candidate based on the parser producer; its status is
  always non-authoritative and final routing remains undetermined.
  Its absence in a valid legacy bundle remains unknown. xlog never labels an error
  as final private/public knowledge, chooses a Wiki target, or names a public
  ErrorDomain; those are LLM knowledge choices.
- Each case publishes deterministic artifact facts for log, FSDB, daidir, KDB,
  the legacy xvp case manifest and the optional xdebug run manifest. Discovery
  uses explicit log references,
  same-directory conventions and configured templates only; it never performs a
  recursive artifact search across the regression.
- Manifest parsing is intentionally shallow and factual: xlog reads the JSON
  object, declared schema and published state, but does not validate FSDB/KDB
  contents, recompute resource digests or infer debugging conclusions. The xvp
  manifest's versioned `external_manifests[]` path is an explicit discovery
  source, not permission for directory traversal or peer searches.
- A cluster publishes a deterministic SHA-256 ID, its first sorted member as the
  representative case, member case IDs, the representative real error, and a
  deterministic recommendation record for downstream xdebug selection.
- Per-log read failures remain visible as `status: error` and never stop the
  batch. A missing PASS marker without a parseable error is a failed,
  unclustered case.

## xdebug Recommendation

`xlog` can recommend a bounded set of failed cases as optional input for the
single external Codex/Claude Agent. The selection is deterministic and
auditable; LLMs are not part of the recommendation algorithm, and xlog does not
execute artifact debugging.

- `scan` accepts `limits.debug_budget` and CLI `--debug-budget`; the default is
  `20`. This value is only the maximum number of clusters included in xlog's
  recommendation output. It is not an Agent execution budget, a permission
  boundary, an MCP/tool-call limit, or a required investigation stage. If fewer
  clusters are eligible, xlog emits the actual count.
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
- Recommended debug cases include their artifact snapshot and directly usable
  xdebug target when FSDB and/or daidir were resolved. Missing or ambiguous
  artifacts remain structured facts and never change failure classification.
- The single external Codex/Claude Agent may consume a recommended primary case,
  choose an alternate, inspect a deferred or unclustered case, or decline
  artifact debugging. The Agent alone decides whether to call xverif MCP and in
  what order to investigate. Recommendations are optional deterministic facts,
  not permissions or a mandatory investigation order.

## Contract Fixture

`fixtures/rtl_injection_minimal` is the first-stage xregress import fixture. It
contains VCS-style shared-tool errors, UVM environment errors, a shortest-time
recommended case with missing artifacts, a complete alternate, and an ambiguous
FSDB case. `scripts/generate_fixture_bundle.py` regenerates the canonical
`xlog_bundle.fixture.json`; direct scanning of the fixture input produces a
machine-local bundle for availability checks.

`fixtures/manifest_kinds` is the revision 1.3 manifest-contract fixture. It
covers simultaneous xvp/xdebug manifests, legacy-only xvp input, an explicitly
referenced missing xdebug manifest, declared-schema mismatch and equal-priority
xdebug path ambiguity.

The recommendation is a deterministic default triage priority, not an investigation
permission limit. An external Agent may inspect deferred or unclustered cases without
changing xlog clustering, ranking, artifact facts, or this recommendation algorithm.

## Configuration

Built-in defaults preserve the legacy extra-error and PASS markers plus xvp
artifact naming conventions. An optional JSON config supplies parser fields and
an `artifacts` object with deterministic path templates. Request `args.parser`
and `args.artifacts` override matching config-file fields; omitted fields keep
their lower-priority values. `run_manifest_templates` names the legacy xvp case
manifest; `xdebug_run_manifest_templates` names the xdebug manifest and defaults
to the case-local `xdebug.run-manifest.v1.json`. The bundle records both effective
configurations.

## Primary Case Log Discovery

- Discovery remains limited to case-insensitive `.log` files and excludes
  `*_bk.log` before case construction.
- Log candidates are grouped by their immediate parent directory. A directory
  with one candidate keeps that log. A directory with multiple candidates keeps
  only one log when the parent basename either equals or ends with that log stem,
  compared case-insensitively, and that match is unique.
- Zero matches or multiple matches are intentionally unresolved. All candidates
  remain in deterministic relative-path order for backward compatibility; xlog
  does not guess which file is primary.
- The rule is structural rather than project-specific. No auxiliary filename,
  test prefix, company directory or physical Cache layout is hardcoded.
- Only selected logs enter parsing and bundle case construction. Their
  `case_id` remains the stable path relative to the requested regression root.
  This applies consistently to `xlog.v1 scan`, `scan_regression` and
  `scan_stream` callers through the shared discovery path.

## Large Regression I/O

- Each selected primary log is scanned line by line through EOF exactly once
  with bounded retained state. No head/tail sampling or whole-file buffering is
  used, so errors in the middle and PASS/simulation-time facts at the tail remain
  observable.
- Before expensive regex matching, the parser uses lowercase string sentinels
  for artifact references, simulation time, UVM, VCS, Xcelium, SVA and configured
  error families. Regex remains the final authority whenever its necessary
  sentinel is present.
- A successfully parsed log is opened for content exactly once. Artifact
  reference extraction no longer performs a second serial full-log pass.
- Parse failures remain structured. If parsing did not produce a reference set,
  artifact discovery may use the compatibility fallback to retain available
  artifact facts.
- FSDB, KDB and daidir content is never read or hashed. Existing bounded
  candidate checks inspect only path existence, type, readability and file
  size, using explicit references, same-directory conventions and configured
  templates.
- Parser concurrency remains bounded by the existing `workers` option. No
  scheduler, cache service, fixed Cache layout, company path or progress
  protocol is introduced.
- Discovery ordering, case ordering, cluster identities, recommendation
  ordering and serialized bundle output remain deterministic.

## Package Layout

```text
bin/xlog                 executable wrapper
src/xlog/                CLI, actions, scanner, parser, dedup, recommendation and bundle code
schemas/                 public request and bundle schemas
config/default_parser.json
tests/                   parser, dedup, recommendation, scan, CLI and schema tests
fixtures/                minimal RTL-injection inputs and canonical import bundle
```

The package uses only the Python standard library. New scan formats, artifact
resolvers, or project-specific discovery conventions must be explicit,
versioned extensions rather than hidden xregress logic.

## Release Rules

The release archive is `XLOG-linux-<version>.tar.gz`. It includes only the
CLI, source, schemas, default configuration and documentation. Test artifacts,
logs, old triage UI files, databases, secrets and third-party wheels are not
distributed.
