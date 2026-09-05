"""One-time migration of the last six committed official snapshots.

Run from the repository root. Never overwrites an existing batch history.
Legacy snapshots may be cumulative; retaining them conservatively excludes
more addresses during migration, rather than reissuing a previous address.
"""
from __future__ import annotations

import gzip
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.io_utils import atomic_write_json  # noqa: E402


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    target = root / "data/official-batch-history.json"
    if target.exists():
        print("Official history already exists; preserved.")
        return
    source = "data/previous-official-ips.txt.gz"
    commits = subprocess.check_output(
        ["git", "log", "-6", "--format=%H", "--", source], cwd=root, text=True,
    ).splitlines()
    batches = []
    for commit in reversed(commits):
        blob = subprocess.check_output(["git", "show", f"{commit}:{source}"], cwd=root)
        batches.append(sorted(set(gzip.decompress(blob).decode("utf-8").splitlines())))
    if batches:
        atomic_write_json(target, batches)
    print(f"Migrated {len(batches)} historical official snapshots.")


if __name__ == "__main__":
    main()
