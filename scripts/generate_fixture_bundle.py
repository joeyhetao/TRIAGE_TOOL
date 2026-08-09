#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from xlog.bundle import scan_regression  # noqa: E402
from xlog.config import load_effective_scan_config  # noqa: E402


FIXTURE_DIR = ROOT / "fixtures" / "rtl_injection_minimal"
REGRESSION_ROOT = FIXTURE_DIR / "regression"
DEFAULT_OUTPUT = FIXTURE_DIR / "xlog_bundle.fixture.json"
CANONICAL_ROOT = "/fixture/rtl_injection_minimal"


def _canonicalize(value, actual_root):
    if isinstance(value, dict):
        return dict((key, _canonicalize(item, actual_root)) for key, item in value.items())
    if isinstance(value, list):
        return [_canonicalize(item, actual_root) for item in value]
    if isinstance(value, str):
        return value.replace(actual_root, CANONICAL_ROOT)
    return value


def generate(output_path):
    config = load_effective_scan_config()
    bundle = scan_regression(
        str(REGRESSION_ROOT.resolve()),
        config["parser"],
        config["artifacts"],
        max_log_files=100,
        workers=1,
        debug_budget=20,
    )
    bundle = _canonicalize(bundle, str(REGRESSION_ROOT.resolve()))
    bundle["generated_at"] = "2000-01-01T00:00:00Z"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate the canonical xlog RTL-injection import fixture.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    output = generate(args.output.resolve())
    print(str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
