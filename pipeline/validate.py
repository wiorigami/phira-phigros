#!/usr/bin/env python3
"""Validate data/catalog.json, its chart packages, images and previews.

Thin CLI around build.validate_catalog(); writes data/validate-report.json.

Usage:
    python3 pipeline/validate.py            # per-entry table + summary
    python3 pipeline/validate.py --quiet    # only the summary line
    python3 pipeline/validate.py --fast     # skip ffprobe/decode checks

Exit code 0 = everything valid, 1 = at least one failure / missing catalog.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--quiet", action="store_true",
                        help="only print the final summary line")
    parser.add_argument("--fast", action="store_true",
                        help="skip deep checks (music/preview duration, decode)")
    parser.add_argument("--data-dir", default=None,
                        help="validate a relocated data tree instead of <repo root>/data")
    args = parser.parse_args(argv)

    if args.data_dir:
        build.configure_paths(args.data_dir)
    failures = build.validate_catalog(verbose=not args.quiet, write_report=True,
                                      deep=not args.fast)
    if args.quiet:
        report = build.load_json(build.REPORT_PATH, {})
        print(f"catalog entries={report.get('count', '?')} "
              f"valid={report.get('valid', '?')} failures={report.get('failures') and len(report['failures']) or 0} "
              f"zipsTotal={build.human_bytes(report.get('zipsTotalBytes', 0))}")
    if failures:
        print(f"VALIDATION FAILED: {failures} entr{'y' if failures == 1 else 'ies'} with errors",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
