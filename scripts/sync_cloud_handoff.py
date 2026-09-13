"""Bounded public handoff downloads. Never send credentials to mirrors."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime


def sources(repository: str, ref: str) -> list[str]:
    raw = f"https://raw.githubusercontent.com/{repository}/{ref}/data/handoff/cloud-raw10000.json.gz"
    return [f"https://ghfast.top/{raw}", f"https://gh.ddlc.top/{raw}",
            f"https://cors.isteed.cc/{raw}",
            f"https://cdn.jsdelivr.net/gh/{repository}@{ref}/data/handoff/cloud-raw10000.json.gz", raw,
            f"https://github.com/{repository}/raw/{ref}/data/handoff/cloud-raw10000.json.gz"]


def download(destination: Path, expected: str, urls: list[str], *, timeout: float = 30,
             emit=print) -> None:
    expected = expected.lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError("无效的可信 SHA-256")
    def valid(path):
        return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected
    if valid(destination):
        emit("本地候选文件校验成功，无需下载。")
        return
    curl = shutil.which("curl.exe" if os.name == "nt" else "curl")
    if not curl:
        raise RuntimeError("未找到 curl，无法执行有总时限的候选下载。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    for index, url in enumerate(urls, 1):
        emit(f"下载云端候选 {index}/{len(urls)}：{url}（最多 {timeout:g} 秒）")
        fd, name = tempfile.mkstemp(prefix=".cloud-handoff-", suffix=".tmp", dir=destination.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            result = subprocess.run(
                [curl, "--disable", "--fail", "--silent", "--show-error", "--location",
                 "--max-redirs", "4", "--connect-timeout", str(min(10, timeout)),
                 "--max-time", str(timeout), "--output", name, url],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode:
                raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[:300])
            if not valid(temporary):
                raise ValueError("SHA-256 不匹配，拒绝使用过期或被修改的候选文件")
            os.replace(temporary, destination)
            emit("云端候选下载并校验成功，即将开始本地网络检查与测速。")
            return
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            reason = "达到总时限" if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
            emit(f"当前下载地址失败：{reason}；自动切换下一个地址。")
        finally:
            temporary.unlink(missing_ok=True)
    raise RuntimeError("全部下载地址失败；保留原文件，不使用未校验的候选，不启动测速。")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--repository", default="jachjkl/Noode-CG")
    parser.add_argument("--ref", default="main")
    parser.add_argument("--destination", default="data/handoff/cloud-raw10000.json.gz")
    args = parser.parse_args()
    root = Path(os.environ.get("NOODE_LOCAL_ROOT", Path.cwd().parent))
    logfile = root / "logs" / f"local-flow-download-{datetime.now():%Y%m%d-%H%M%S}.log"
    def emit(message):
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
        print(line, flush=True)
        try:
            logfile.parent.mkdir(parents=True, exist_ok=True)
            with logfile.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass  # Console remains authoritative if the disk is unavailable.
    try:
        download(Path(args.destination), args.sha256, sources(args.repository, args.ref), emit=emit)
    except Exception as exc:
        emit(f"候选下载失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
