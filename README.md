# xlog

`xlog` is the regression-log provider for `xregress`. It scans a regression
directory, extracts simulation errors, keeps the first five non-warning errors
for every log case, deduplicates first failures, recommends bounded xdebug
cases, and writes an auditable `xlog_bundle.v1` result.

It is intentionally a CLI/JSON tool. There is no web UI, LLM integration,
knowledge database, Excel dependency, or single-log upload workflow.

## Quick start

```bash
/home/melo.liao/ai_tools/xlog/bin/xlog scan \
  --root /absolute/path/to/regression \
  --output /absolute/path/to/run/xlog_bundle.json \
  --debug-budget 20
```

The command prints one `xlog.v1` JSON response to stdout and atomically writes
the complete bundle to `--output`.

## JSON action

```json
{
  "api_version": "xlog.v1",
  "request_id": "regress-001",
  "action": "scan",
  "target": {
    "regression_root": "/absolute/path/to/regression"
  },
  "args": {
    "output_path": "/absolute/path/to/run/xlog_bundle.json"
  },
  "limits": {
    "max_log_files": 5000,
    "workers": 8,
    "debug_budget": 20
  }
}
```

Run it with `bin/xlog --json request.json` or `bin/xlog --json -`. Public
actions are `actions`, `schema`, and `scan`; use `bin/xlog actions` and
`bin/xlog schema --action scan --kind request` for machine-readable discovery.

## xdebug recommendation

The bundle contains `debug_recommendation`, a deterministic shortlist of failed
cases for downstream xdebug. The default budget is 20 failure clusters. Each
failure cluster also carries its own recommendation record with the selected
case, alternates, score components, and human-readable reasons.

Every case exposes `simulation_time` with the raw VCS time, unit, normalized
femtoseconds, and source. xlog uses an explicit simulation-end/report time when
available; otherwise it uses the largest observed simulation timestamp. CPU,
wall-clock, elapsed, and real runtime fields are excluded. Within one
deduplicated failure cluster, xlog selects the candidate with the shortest known
total simulation time. Cases with unavailable time follow known-time cases and
then use the stable evidence and seed tie-breakers.

Every case also exposes `artifacts`: resolved or unavailable log, FSDB, daidir,
KDB and optional run-manifest facts, candidate paths and a ready-to-use
`xdebug_target`. Discovery only uses paths written in the log, standard
same-directory names and explicit configuration templates, so xlog does not
accidentally assign another testcase's wave to the current case.

The first-pass recommendation is algorithmic and repeatable. LLMs may be used
later on xdebug evidence, but not for deciding the initial shortlist.

## Parser configuration

An optional JSON config can set parser patterns and artifact templates:

```json
{
  "extra_patterns": ["ERROR", "FATAL", "FAILED"],
  "pass_patterns": ["JVP TEST PASSED"],
  "artifacts": {
    "fsdb_templates": ["{log_dir}/{log_stem}.fsdb"],
    "daidir_templates": ["{log_dir}/simv.daidir"]
  }
}
```

Pass it with `--config /absolute/path/to/parser.json`. Request `args.parser`
overrides matching config-file fields. The effective result is always recorded
in the bundle.

## xregress integration

Configure `bin/xlog` as an `xlog_provider`. xregress consumes the generated
`xlog_bundle.v1`; it must not rescan logs, repeat xlog error clustering, or
re-rank the xdebug shortlist.

## Development and release

```bash
PYTHONPATH=src python3 -m pytest -q
bash scripts/build_linux_release.sh
PUBLISH_COMMIT_MSG=Describe-change bash scripts/publish_git.sh
```

The package requires only Python 3.6+ and the standard library. See `PLAN.md`
for architecture and extension rules.
