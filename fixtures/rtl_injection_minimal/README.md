# Minimal RTL Injection Fixture

This fixture exercises the xlog-to-xregress fact contract without requiring a
real FSDB payload. The `.fsdb` files are path/state placeholders and must not be
opened by xdebug.

The input covers:

- one shared-tool VCS error repeated with different source paths, line numbers,
  and runtime values;
- one environment UVM error whose shortest-time recommended case has missing
  debug artifacts and whose alternate case has complete artifact paths;
- one case with two equal-priority FSDB references, reported as ambiguous.

Regenerate the canonical import bundle from the repository root:

```bash
PYTHONPATH=src python3 scripts/generate_fixture_bundle.py
```

For a live local-path bundle suitable for xdebug availability checks, scan the
fixture input directly and write outside the fixture directory:

```bash
bin/xlog scan \
  --root "$(pwd)/fixtures/rtl_injection_minimal/regression" \
  --output /tmp/xlog-rtl-injection-bundle.json
```
