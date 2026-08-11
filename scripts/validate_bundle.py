#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xlog.schema_validation import SchemaValidationError, validate_instance


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate an xlog bundle against its published schema")
    parser.add_argument("bundle", help="path to xlog_bundle.json")
    parser.add_argument(
        "--schema",
        default=str(ROOT / "schemas" / "xlog_bundle.v1.schema.json"),
        help="path to the bundle schema",
    )
    args = parser.parse_args(argv)

    try:
        bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
        schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
        validate_instance(bundle, schema)
    except (OSError, ValueError, SchemaValidationError) as exc:
        print("invalid: %s" % exc, file=sys.stderr)
        return 1

    if bundle.get("api_version") != "xlog_bundle.v1" or bundle.get("schema_revision") != "1.2":
        print("invalid: expected xlog_bundle.v1 schema_revision 1.2", file=sys.stderr)
        return 1
    print("valid: %s (xlog_bundle.v1 revision 1.2)" % Path(args.bundle))
    return 0


if __name__ == "__main__":
    sys.exit(main())
