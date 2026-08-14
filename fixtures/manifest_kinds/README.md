# Manifest-kind fixture

This synthetic regression exercises xlog manifest classification without
opening FSDB contents or recursively searching the fixture tree.

- `both_1`: valid xvp and xdebug manifests; xdebug is preferred.
- `legacy_only_1`: only a valid legacy xvp manifest.
- `xdebug_missing_1`: xvp explicitly references a missing xdebug manifest.
- `schema_mismatch_1`: the xdebug document declares an unsupported schema.
- `ambiguous_1`: two explicit xdebug manifest paths have equal priority.
