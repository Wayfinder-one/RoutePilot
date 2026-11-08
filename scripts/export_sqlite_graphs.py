#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from router.control.graph_migration import export_sqlite_graphs  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Export SQLite routing trees to shared v3 graph files")
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    print(json.dumps(export_sqlite_graphs(args.db, args.out), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
