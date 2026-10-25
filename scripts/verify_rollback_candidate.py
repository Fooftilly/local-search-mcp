#!/usr/bin/env python3
"""Verify a private offline rollback candidate; never activate or execute it."""
from __future__ import annotations

import argparse
from pathlib import Path
import json
import os
import subprocess
import sys

if __package__:
    from . import rollback_candidate
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import rollback_candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path,
                        help="Private directory created by scripts/rollback_candidate.py")
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        parser.error("Do not run rollback inspections as root")
    try:
        result = rollback_candidate.inspect(args.candidate)
    except (ValueError, OSError, subprocess.SubprocessError):
        # Do not print filenames, filesystem errors, or private archive paths.
        print("Rollback candidate integrity check failed; inspect your private "
              "staging directory and permissions locally", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
