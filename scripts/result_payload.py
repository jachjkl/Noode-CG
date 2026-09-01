from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path, PurePosixPath


PAYLOAD_FILES = (
    "output/nodes.txt",
    "output/nodes.json",
    "output/nodes.csv",
    "output/api.json",
    "output/health.json",
    "output/ip.zip",
    "data/previous-top100.json",
    "data/handoff/local-qualified.json.gz",
    "data/handoff/local-attempted-ips.txt.gz",
)
_ALLOWED = frozenset(PAYLOAD_FILES)


def pack_payload(root: Path, archive: Path) -> list[str]:
    root = root.resolve()
    archive = archive.resolve()
    archive.parent.mkdir(parents=True, exist_ok=True)
    included: list[str] = []
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as handle:
        for relative in PAYLOAD_FILES:
            source = root / relative
            if not source.is_file():
                continue
            handle.write(source, arcname=relative)
            included.append(relative)
    if "output/health.json" not in included:
        archive.unlink(missing_ok=True)
        raise ValueError("output/health.json is required in a local result payload")
    return included


def _normalise_member(name: str) -> str:
    normalised = name.replace("\\", "/")
    path = PurePosixPath(normalised)
    if path.is_absolute() or ".." in path.parts or normalised not in _ALLOWED:
        raise ValueError(f"unexpected result payload member: {name}")
    return normalised


def unpack_payload(archive: Path, destination: Path) -> list[str]:
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    unpacked: list[str] = []
    with zipfile.ZipFile(archive, "r") as handle:
        for member in handle.infolist():
            relative = _normalise_member(member.filename)
            if member.is_dir():
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(handle.read(member))
            unpacked.append(relative)
    if "output/health.json" not in unpacked:
        raise ValueError("result payload does not contain output/health.json")
    return unpacked


def main() -> int:
    parser = argparse.ArgumentParser(description="Pack or unpack the bounded local-result payload")
    subparsers = parser.add_subparsers(dest="command", required=True)
    pack = subparsers.add_parser("pack")
    pack.add_argument("--root", type=Path, required=True)
    pack.add_argument("--archive", type=Path, required=True)
    unpack = subparsers.add_parser("unpack")
    unpack.add_argument("--archive", type=Path, required=True)
    unpack.add_argument("--destination", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.command == "pack":
        files = pack_payload(arguments.root, arguments.archive)
    else:
        files = unpack_payload(arguments.archive, arguments.destination)
    print(json.dumps({"command": arguments.command, "files": files}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
