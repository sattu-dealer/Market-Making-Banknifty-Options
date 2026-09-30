#!/usr/bin/env python3
"""Download or refresh the exchange instrument master.

The master is a public, no-auth CSV, so this works before any broker account
exists. It is ~36 MB and gitignored; every contract fact in the project is read
from it at runtime rather than hardcoded.

    python scripts/refresh_master.py [--force]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bnfmm.data import instruments as ins  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true", help="redownload even if the local copy is fresh"
    )
    parser.add_argument("--config", default=None, help="path to instruments.yaml")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = ins.load_config(args.config) if args.config else ins.load_config()
    path = ins.ensure_master(config, force=args.force)
    rows = len(ins.read_master(path))
    print(f"{path}  ({path.stat().st_size / 1e6:.1f} MB, {rows:,} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
