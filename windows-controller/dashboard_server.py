from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


STAGE_LABELS = {
    "cloud-prepare": "云端生成 RAW10000",
    "local-select": "本地网络优选",
    "cloud-publish": "发布订阅结果",
    "local-cleanup": "清理本地缓存",
    "replenish-cloud-pool": "自动补充候选池",
}

LOCAL_RULE_DEFAULTS = {
    "tcp_max_ms": 200.0,
    "tls_max_ms": 200.0,
    "http_ttfb_max_ms": 200.0,
    "average_max_ms": 200.0,
    "jitter_max_ms": 200.0,
    "loss_max_percent": 20.0,
    "speed_min_mbps": 3.0,
}

LOCAL_OPTION_DEFAULTS = {
    "continuous_three_rounds": True,
}


def utc_timestamp() -> float:
    return time.time()


def open_dashboard_url(url: str) -> bool:
    try:
        if os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            webbrowser.open(url, new=2)
        return True
    except OSError:
        try:
            return bool(webbrowser.open(url, new=2))
        except webbrowser.Error:
            return False


def dashboard_is_healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}api/state", timeout=1.5) as response:
            return response.status == HTTPStatus.OK
    except (OSError, urllib.error.URLError):
        return False


class DashboardState:
    def __init__(self, root: Path, repository: str, branch: str) -> None:
        self.root = root
        self.repository = repository
        self.branch = branch
        self.dashboard_dir = root / "dashboard"
        self.log_path = root / "logs" / "manual-last.log"
        self.cache_dir = root / "dashboard-cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.nodes_cache = self.cache_dir / "nodes.json"
        self.health_cache = self.cache_dir / "health.json"
        self.manual_script = root / "manual-start.ps1"
        self.rules_path = root / "app" / "data" / "local-rules.json"
        self.options_path = root / "local-options.json"
        self.continue_queue_path = root / "app" / "data" / "handoff" / "dashboard-continue-request.json"
        self.stop_after_current_path = root / "app" / "data" / "handoff" / "stop-after-current.json"
        # Reopening the dashboard is a new manual session. A queued Continue
        # from an older process must never launch work by itself.
        self.continue_queue_path.unlink(missing_ok=True)
        self.gh = shutil.which("gh")
        self.process: subprocess.Popen[str] | None = None
        self.process_started_at: float | None = None
        self.process_ended_at: float | None = None
        self.exit_code: int | None = None
        self.stop_requested = False
        self.lock = threading.RLock()
        self.run_id: int | None = None
        self.run_url = ""
        self.gh_state: dict[str, object] = {}
        self.gh_checked_at = 0.0
        self.run_list_checked_at = 0.0
        self.observed_run_ids: list[int] = []
        self.nodes_checked_at = 0.0
        self.last_error = ""
        self.shutdown_at: float | None = None
        self.dispatch_pending_until = 0.0
        self.queue_retry_after = 0.0
        self.cycle_started = False

    def _run_command(self, args: list[str], timeout: int = 25) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=str(self.root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def start_controller(self) -> bool:
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return False
            if not self.manual_script.is_file():
                self.last_error = f"找不到控制脚本：{self.manual_script}"
                return False
            powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            if not powershell.is_file():
                self.last_error = "找不到 Windows PowerShell 5.1。"
                return False
            self.exit_code = None
            self.last_error = ""
            self.stop_requested = False
            self.stop_after_current_path.unlink(missing_ok=True)
            self.process_started_at = utc_timestamp()
            self.process_ended_at = None
            self.run_id = None
            self.run_url = ""
            self.gh_state = {}
            self.observed_run_ids = []
            self.shutdown_at = None
            self._clear_cycle_state()
            self.log_path.unlink(missing_ok=True)
            self.process = subprocess.Popen(
                [
                    str(powershell),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(self.manual_script),
                    "-Repository",
                    self.repository,
                    "-Branch",
                    self.branch,
                    "-LocalRoot",
                    str(self.root),
                ],
                cwd=str(self.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.cycle_started = True
            threading.Thread(target=self._drain_controller, daemon=True).start()
            return True

    def _clear_cycle_state(self) -> None:
        handoff = self.root / "app" / "data" / "handoff"
        for name in (
            "local-live-results.json.gz",
            "local-qualified.json.gz",
            "local-attempted-ips.txt.gz",
        ):
            (handoff / name).unlink(missing_ok=True)

    def _drain_controller(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.stdout is not None:
                for _ in process.stdout:
                    pass
            code = process.wait()
        except Exception as exc:  # pragma: no cover - defensive Windows process handling
            code = 1
            with self.lock:
                self.last_error = str(exc)
        with self.lock:
            self.exit_code = code
            self.process_ended_at = utc_timestamp()
            self.shutdown_at = utc_timestamp() + (300 if code == 0 else 1800)
        self.refresh_remote_outputs(force=True)
        self.refresh_github(force=True)

    def stop_monitor(self) -> None:
        with self.lock:
            process = self.process
            self.stop_requested = True
        if process is not None and process.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        with self.lock:
            self.shutdown_at = utc_timestamp() + 3

    def _local_selection_pids(self) -> list[int]:
        if os.name != "nt":
            return []
        powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if not powershell.is_file():
            return []
        runtime = str((self.root / "runtime" / "python" / "python.exe").resolve()).replace("'", "''")
        script = (
            f"$runtime = '{runtime}'; "
            "$items = @(Get-CimInstance Win32_Process | Where-Object { "
            "$_.Name -ieq 'python.exe' -and $_.ExecutablePath -ieq $runtime -and "
            "$_.CommandLine -match '(?i)(^|\\s)main\\.py\\s+local-select(\\s|$)' }); "
            "@($items | ForEach-Object { [int]$_.ProcessId }) | ConvertTo-Json -Compress"
        )
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        result = self._run_command(
            [str(powershell), "-NoProfile", "-EncodedCommand", encoded],
            timeout=12,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        try:
            values = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        if isinstance(values, int):
            return [values]
        return [int(value) for value in values if isinstance(value, int)] if isinstance(values, list) else []

    def stop_selection(self) -> dict[str, object]:
        """Stop local probes now and suppress automatic replenishment."""
        self.refresh_github(force=True)
        workflow_active = self._workflow_active()
        self.continue_queue_path.unlink(missing_ok=True)
        (self.root / "app" / "data" / "handoff" / "force-rerank.json").unlink(missing_ok=True)
        self.stop_after_current_path.parent.mkdir(parents=True, exist_ok=True)
        if workflow_active:
            self.stop_after_current_path.write_text(
                json.dumps(
                    {
                        "requested_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "mode": "stop-local-and-finish-current-cloud-run",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        else:
            self.stop_after_current_path.unlink(missing_ok=True)

        terminated: list[int] = []
        for pid in self._local_selection_pids():
            result = self._run_command(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=15)
            if result.returncode == 0:
                terminated.append(pid)
        with self.lock:
            self.stop_requested = True
            self.dispatch_pending_until = 0.0
            self.last_error = ""
            self.shutdown_at = None
        self._append_dashboard_log(
            "已请求停止优选：本地探测立即结束，当前 GitHub 工作流完成后不再自动补池。"
        )
        return {
            "stopped": True,
            "workflow_active": workflow_active,
            "local_processes_terminated": terminated,
            "finish_current_cloud_run": workflow_active,
        }

    def _append_dashboard_log(self, message: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def local_rules(self) -> dict[str, float]:
        payload = self.load_json_file(self.rules_path, {})
        values = payload.get("ordinary", payload) if isinstance(payload, dict) else {}
        rules = dict(LOCAL_RULE_DEFAULTS)
        if isinstance(values, dict):
            for name, default in LOCAL_RULE_DEFAULTS.items():
                raw = values.get(name, default)
                if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    rules[name] = float(raw)
        return rules

    def save_local_rules(self, payload: object) -> dict[str, float]:
        values = payload.get("ordinary", payload) if isinstance(payload, dict) else None
        if not isinstance(values, dict):
            raise ValueError("规则内容必须是对象")
        rules: dict[str, float] = {}
        for name, default in LOCAL_RULE_DEFAULTS.items():
            raw = values.get(name, default)
            if not isinstance(raw, (int, float)) or isinstance(raw, bool):
                raise ValueError(f"{name} 必须是数字")
            rules[name] = float(raw)
        for name in ("tcp_max_ms", "tls_max_ms", "http_ttfb_max_ms", "average_max_ms"):
            if rules[name] <= 0:
                raise ValueError(f"{name} 必须大于 0")
        if rules["jitter_max_ms"] < 0:
            raise ValueError("jitter_max_ms 不能小于 0")
        if not 0 <= rules["loss_max_percent"] <= 100:
            raise ValueError("loss_max_percent 必须在 0 到 100 之间")
        if rules["speed_min_mbps"] < 0:
            raise ValueError("speed_min_mbps 不能小于 0")
        self.rules_path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "schema": 1,
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "ordinary": rules,
            "jp_exempt": True,
        }
        temporary = self.rules_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.rules_path)
        self._append_dashboard_log("普通节点自定义规则已保存；日本节点继续使用独立通道。")
        return rules

    def local_options(self) -> dict[str, bool]:
        payload = self.load_json_file(self.options_path, {})
        values = payload.get("selection", payload) if isinstance(payload, dict) else {}
        options = dict(LOCAL_OPTION_DEFAULTS)
        if isinstance(values, dict):
            for name, default in LOCAL_OPTION_DEFAULTS.items():
                raw = values.get(name, default)
                if isinstance(raw, bool):
                    options[name] = raw
        return options

    def save_local_options(self, payload: object) -> dict[str, bool]:
        values = payload.get("selection", payload) if isinstance(payload, dict) else None
        if not isinstance(values, dict):
            raise ValueError("运行选项必须是对象")
        raw = values.get("continuous_three_rounds", True)
        if not isinstance(raw, bool):
            raise ValueError("continuous_three_rounds 必须是布尔值")
        options = {"continuous_three_rounds": raw}
        document = {
            "schema": 1,
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "selection": options,
        }
        temporary = self.options_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.options_path)
        mode = "开启" if raw else "关闭"
        self._append_dashboard_log(f"三轮连续筛选已{mode}。")
        return options

    def _workflow_active(self) -> bool:
        with self.lock:
            return str(self.gh_state.get("status") or "") in {
                "queued", "in_progress", "waiting", "pending"
            } or utc_timestamp() < self.dispatch_pending_until

    def request_continue(self) -> dict[str, object]:
        with self.lock:
            if not self.cycle_started:
                return {
                    "started": False,
                    "queued": False,
                    "reason": "请先点击“开始新一轮”；续选只能接在本次面板会话之后。",
                }
        self.continue_queue_path.parent.mkdir(parents=True, exist_ok=True)
        request = {
            "requested_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "mode": "always-fetch-new-raw10000-and-rerank",
        }
        self.continue_queue_path.write_text(
            json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.refresh_github(force=True)
        if self._workflow_active():
            self._append_dashboard_log("已排队继续优选；当前工作流结束后获取新的10000个官方候选。")
            return {"started": False, "queued": True, "reason": "已加入续选队列，当前流程结束后自动运行。"}
        return self.process_continue_queue(force=True)

    def process_continue_queue(self, force: bool = False) -> dict[str, object]:
        if not self.continue_queue_path.is_file():
            return {"started": False, "queued": False}
        if not force and utc_timestamp() < self.queue_retry_after:
            return {"started": False, "queued": True}
        if self._workflow_active():
            return {"started": False, "queued": True}
        result = self.force_continue()
        if result.get("started"):
            self.continue_queue_path.unlink(missing_ok=True)
            return {**result, "queued": False}
        self.queue_retry_after = utc_timestamp() + 30
        with self.lock:
            self.last_error = str(result.get("reason") or "排队续选暂时无法启动")
        return {**result, "queued": True}

    def force_continue(self) -> dict[str, object]:
        with self.lock:
            if not self.cycle_started:
                return {
                    "started": False,
                    "reason": "请先点击“开始新一轮”；续选不能跳过首轮全量链接。",
                }
        self.stop_after_current_path.unlink(missing_ok=True)
        with self.lock:
            self.stop_requested = False
        self.refresh_github(force=True)
        with self.lock:
            workflow_status = str(self.gh_state.get("status") or "")
            if workflow_status in {"queued", "in_progress", "waiting", "pending"}:
                return {"started": False, "reason": "当前已有工作流正在运行，请等待本轮结束。"}
        if not self.gh:
            return {"started": False, "reason": "找不到 GitHub CLI。"}
        self.refresh_remote_outputs(force=True)
        nodes = self.nodes()
        previous_top100_ips = {
            str(item.get("ip") or "")
            for item in nodes[:100]
            if str(item.get("ip") or "")
        }

        handoff_dir = self.root / "app" / "data" / "handoff"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        accumulator = handoff_dir / "local-qualified.json.gz"
        marker = handoff_dir / "force-rerank.json"
        generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        # Manual continuation belongs to the same open dashboard session. Keep
        # all live qualified nodes and seed the next local pass with them; a
        # brand-new Start action is the only operation that clears this panel.
        merged_nodes, _source, _path = self.live_nodes_with_meta()
        payload = {
            "schema": 1,
            "generated_at": generated_at,
            "report": {
                "cycle_round": 0,
                "forced_rerank": True,
                "existing_count": len(merged_nodes),
                "previous_top100_reserved_for_retest": 0,
            },
            "nodes": merged_nodes,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        temporary = accumulator.with_suffix(accumulator.suffix + ".tmp")
        temporary.write_bytes(gzip.compress(encoded, compresslevel=9, mtime=0))
        temporary.replace(accumulator)
        attempted = handoff_dir / "local-attempted-ips.txt.gz"
        attempted_values = {
            str(item.get("ip") or "")
            for item in merged_nodes
            if str(item.get("ip") or "")
        } | previous_top100_ips
        attempted_text = "\n".join(sorted(attempted_values))
        if attempted_text:
            attempted_text += "\n"
        attempted.write_bytes(gzip.compress(attempted_text.encode("utf-8"), compresslevel=9, mtime=0))
        marker.write_text(
            json.dumps(
                {
                    "mode": "force-rerank",
                    "created_at": generated_at,
                    "existing_count": len(merged_nodes),
                    "previous_top100_reserved_for_retest": 0,
                    "ranking": "latency-speed-loss-jitter",
                    "general_target": 300,
                    "jp_target": 10,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        result = self._run_command(
            [
                self.gh,
                "workflow",
                "run",
                "update.yml",
                "--repo",
                self.repository,
                "--ref",
                self.branch,
                "-f",
                "continuation=true",
            ]
        )
        if result.returncode != 0:
            reason = (result.stderr or result.stdout or "GitHub 工作流触发失败").strip()
            return {"started": False, "reason": reason}
        with self.lock:
            self.process_started_at = utc_timestamp()
            self.process_ended_at = None
            self.exit_code = None
            self.last_error = ""
            self.run_id = None
            self.run_url = ""
            self.gh_state = {}
            self.observed_run_ids = []
            self.dispatch_pending_until = utc_timestamp() + 90
            self.shutdown_at = None
            self.run_list_checked_at = 0.0
            self.gh_checked_at = 0.0
        self._append_dashboard_log(
            f"可视化面板已强制续选：保存历史 {len(merged_nodes)} 条，"
            "本次会话中的上一轮节点不重复测试；"
            "正在请求新的10000个官方候选，完成后按本地规则重排300条普通节点并附加日本节点。"
        )
        return {
            "started": True,
            "existing_count": len(merged_nodes),
            "previous_top100_retest_count": 0,
        }

    def read_log(self) -> str:
        if not self.log_path.is_file():
            return "等待控制器写入日志……"
        try:
            text = self.log_path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            return f"读取日志失败：{exc}"
        lines = text.splitlines()
        return "\n".join(lines[-350:])

    def _discover_run(self, log_text: str) -> None:
        matches = re.findall(r"(?:运行编号\s*#|actions/runs/)(\d+)", log_text)
        if matches:
            candidate_id = int(matches[-1])
            # The manual log remains on disk after a run. Do not let that old
            # number overwrite a newer active run discovered from GitHub.
            if self.run_id is None or candidate_id >= self.run_id:
                self.run_id = candidate_id
                self.run_url = f"https://github.com/{self.repository}/actions/runs/{self.run_id}"
            if candidate_id not in self.observed_run_ids:
                self.observed_run_ids.append(candidate_id)

    def refresh_github(self, force: bool = False) -> None:
        now = utc_timestamp()
        with self.lock:
            log_text = self.read_log()
            if self.cycle_started:
                self._discover_run(log_text)
            if not force and now - self.gh_checked_at < 4:
                return
            self.gh_checked_at = now
        if not self.gh:
            return
        try:
            if force or now - self.run_list_checked_at >= 6:
                list_result = self._run_command(
                    [
                        self.gh,
                        "run",
                        "list",
                        "--repo",
                        self.repository,
                        "--workflow",
                        "update.yml",
                        "--limit",
                        "10",
                        "--json",
                        "databaseId,status,conclusion,url,createdAt,event",
                    ]
                )
                self.run_list_checked_at = now
                if list_result.returncode == 0 and list_result.stdout.strip():
                    runs = json.loads(list_result.stdout)
                    active = next(
                        (
                            item
                            for item in runs
                            if str(item.get("status")) in {"queued", "in_progress", "waiting", "pending"}
                        ),
                        None,
                    )
                    if active:
                        candidate_id = int(active["databaseId"])
                        with self.lock:
                            self.cycle_started = True
                            self.run_id = candidate_id
                            self.run_url = str(active.get("url") or self.run_url)
                            if candidate_id not in self.observed_run_ids:
                                self.observed_run_ids.append(candidate_id)
            with self.lock:
                run_id = self.run_id
            if not run_id:
                return
            result = self._run_command(
                [
                    self.gh,
                    "run",
                    "view",
                    str(run_id),
                    "--repo",
                    self.repository,
                    "--json",
                    "status,conclusion,url,jobs",
                ]
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                with self.lock:
                    self.gh_state = data
                    self.run_url = str(data.get("url") or self.run_url)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            with self.lock:
                self.last_error = f"GitHub 状态读取失败：{exc}"

    def _download_repo_json(self, repo_path: str) -> object | None:
        if not self.gh:
            return None
        endpoint = f"repos/{self.repository}/contents/{repo_path}?ref={self.branch}"
        result = self._run_command(
            [self.gh, "api", "-H", "Accept: application/vnd.github.raw+json", endpoint]
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return json.loads(result.stdout)

    def refresh_remote_outputs(self, force: bool = False) -> None:
        now = utc_timestamp()
        with self.lock:
            if not force and now - self.nodes_checked_at < 15:
                return
            self.nodes_checked_at = now
        try:
            nodes = self._download_repo_json("output/nodes.json")
            if isinstance(nodes, list) and nodes:
                self.nodes_cache.write_text(json.dumps(nodes, ensure_ascii=False), encoding="utf-8")
            health = self._download_repo_json("output/health.json")
            if isinstance(health, dict):
                self.health_cache.write_text(json.dumps(health, ensure_ascii=False), encoding="utf-8")
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            with self.lock:
                if not self.last_error:
                    self.last_error = f"结果同步失败：{exc}"

    def load_json_file(self, path: Path, default: object) -> object:
        if not path.is_file():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return default

    def _nodes_from_handoff(self, path: Path) -> list[dict[str, object]]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        except (OSError, gzip.BadGzipFile, UnicodeDecodeError, json.JSONDecodeError):
            return []
        records = payload.get("nodes", []) if isinstance(payload, dict) else []
        public: list[dict[str, object]] = []
        for item in records if isinstance(records, list) else []:
            if not isinstance(item, dict) or not item.get("ip"):
                continue
            ip = str(item["ip"])
            port = int(item.get("port", 443))
            display = f"[{ip}]" if ":" in ip else ip
            public.append({
                "ip": ip,
                "port": port,
                "ip_port": f"{display}:{port}",
                "country": str(item.get("country") or item.get("country_hint") or "").upper(),
                "colo_country": str(item.get("colo_country") or "").upper(),
                "region": item.get("region") or "",
                "city": item.get("city") or "",
                "colo": item.get("colo") or "",
                "tcp_latency_ms": item.get("tcp_latency_ms"),
                "tls_latency_ms": item.get("tls_latency_ms"),
                "http_latency_ms": item.get("http_latency_ms"),
                "average_latency_ms": item.get("average_latency_ms"),
                "jitter_ms": item.get("overall_jitter_ms", item.get("tcp_jitter_ms")),
                "loss_rate": item.get("tcp_loss_rate"),
                "speed_mbps": item.get("speed_mbps"),
                "score": item.get("score", 0),
            })
        return public

    def live_nodes_with_meta(self) -> tuple[list[dict[str, object]], str, Path | None]:
        path = self.root / "app" / "data" / "handoff" / "local-live-results.json.gz"
        return self._nodes_from_handoff(path), "live-current-cycle", (
            path if path.is_file() else None
        )

    def nodes_with_meta(self) -> tuple[list[dict[str, object]], str, Path | None]:
        local_nodes = self.root / "app" / "output" / "nodes.json"
        if local_nodes.is_file():
            data = self.load_json_file(local_nodes, [])
            if isinstance(data, list) and data:
                return data, "local-ready", local_nodes
        data = self.load_json_file(self.nodes_cache, [])
        return (data if isinstance(data, list) else []), "published-cache", (
            self.nodes_cache if self.nodes_cache.is_file() else None
        )

    def nodes(self) -> list[dict[str, object]]:
        return self.nodes_with_meta()[0]

    def health(self) -> dict[str, object]:
        local_health = self.root / "app" / "output" / "health.json"
        source = local_health if local_health.is_file() else self.health_cache
        data = self.load_json_file(source, {})
        return data if isinstance(data, dict) else {}

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            process = self.process
            running = process is not None and process.poll() is None
            gh_status = str(self.gh_state.get("status") or "")
            gh_conclusion = str(self.gh_state.get("conclusion") or "")
            if running:
                status = "running"
            elif self.exit_code == 0:
                status = "success"
            elif self.exit_code is not None:
                status = "stopped" if self.stop_requested else "failure"
            else:
                status = "idle"
            jobs: dict[str, dict[str, object]] = {}
            for job in self.gh_state.get("jobs", []) if isinstance(self.gh_state.get("jobs"), list) else []:
                if not isinstance(job, dict):
                    continue
                name = str(job.get("name") or "")
                jobs[name] = {
                    "name": name,
                    "label": STAGE_LABELS.get(name, name),
                    "status": job.get("status") or "pending",
                    "conclusion": job.get("conclusion") or "",
                    "startedAt": job.get("startedAt") or "",
                    "completedAt": job.get("completedAt") or "",
                    "url": job.get("url") or "",
                    "steps": [
                        {
                            "name": step.get("name") or "",
                            "status": step.get("status") or "pending",
                            "conclusion": step.get("conclusion") or "",
                            "number": step.get("number"),
                        }
                        for step in job.get("steps", [])
                        if isinstance(step, dict)
                    ],
                }
            stages = []
            for name, label in STAGE_LABELS.items():
                stages.append(jobs.get(name, {"name": name, "label": label, "status": "pending", "conclusion": "", "steps": []}))
            all_steps = [step for stage in stages for step in stage.get("steps", [])]
            completed_steps = sum(1 for step in all_steps if step.get("status") == "completed")
            current_stage = next((stage for stage in stages if stage.get("status") == "in_progress"), None)
            if current_stage is None:
                current_stage = next(
                    (
                        stage
                        for stage in stages
                        if stage.get("name") in jobs
                        and stage.get("status") in {"queued", "waiting", "pending"}
                    ),
                    None,
                )
            current_step = None
            if current_stage:
                current_step = next(
                    (step for step in current_stage.get("steps", []) if step.get("status") == "in_progress"),
                    None,
                )
            started = self.process_started_at
            ended = self.process_ended_at
            elapsed = int(max(0, (ended or utc_timestamp()) - started)) if started else 0
            shutdown_in = max(0, int(self.shutdown_at - utc_timestamp())) if self.shutdown_at else None
            workflow_active = gh_status in {"queued", "in_progress", "waiting", "pending"}
            if not self.cycle_started and not workflow_active:
                status = "idle"
            elif self.stop_requested and workflow_active:
                status = "stopping"
            elif self.stop_requested:
                status = "stopped"
            elif running:
                status = "running"
            elif workflow_active:
                status = "running"
            elif utc_timestamp() < self.dispatch_pending_until:
                status = "running"
            elif gh_status == "completed" and gh_conclusion == "success":
                status = "success"
            elif gh_status == "completed" and gh_conclusion in {"failure", "cancelled", "timed_out"}:
                status = "failure"
            _nodes, result_source, result_path = self.nodes_with_meta()
            return {
                "status": status,
                "running": status in {"running", "stopping"},
                "pid": process.pid if running and process else None,
                "exit_code": self.exit_code,
                "run_id": self.run_id,
                "run_url": self.run_url,
                "gh_status": gh_status,
                "gh_conclusion": gh_conclusion,
                "elapsed_seconds": elapsed,
                "started_at": datetime.fromtimestamp(started).isoformat(timespec="seconds") if started else "",
                "stages": stages,
                "workflow_progress": {
                    "completed_steps": completed_steps,
                    "total_steps": len(all_steps),
                    "current_stage": current_stage.get("label") if current_stage else "",
                    "current_step": current_step.get("name") if current_step else "",
                    "round": len(self.observed_run_ids),
                },
                "health": self.health(),
                "log": self.read_log(),
                "last_error": self.last_error,
                "continuation_queued": self.continue_queue_path.is_file(),
                "cycle_started": self.cycle_started,
                "stop_requested": self.stop_requested,
                "local_rules": self.local_rules(),
                "local_options": self.local_options(),
                "shutdown_in": shutdown_in,
                "result_source": result_source,
                "result_updated_at": (
                    datetime.fromtimestamp(result_path.stat().st_mtime).isoformat(timespec="seconds")
                    if result_path is not None and result_path.is_file()
                    else ""
                ),
            }


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def make_handler(state: DashboardState, server_ref: dict[str, ThreadingHTTPServer]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NoodeCGDashboard/1.0"

        def _send(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, value: object, status: int = HTTPStatus.OK) -> None:
            self._send(status, json_bytes(value), "application/json; charset=utf-8")

        def _read_json(self) -> object:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0 or length > 65536:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_GET(self) -> None:
            route = urlsplit(self.path).path
            if route == "/api/state":
                self._json(state.snapshot())
                return
            if route == "/api/nodes":
                nodes, source, path = state.nodes_with_meta()
                updated_at = (
                    datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
                    if path is not None and path.is_file()
                    else ""
                )
                self._json({"nodes": nodes, "source": source, "updated_at": updated_at})
                return
            if route == "/api/live-nodes":
                nodes, source, path = state.live_nodes_with_meta()
                updated_at = (
                    datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
                    if path is not None and path.is_file()
                    else ""
                )
                self._json({"nodes": nodes, "source": source, "updated_at": updated_at})
                return
            if route == "/api/rules":
                self._json({"ordinary": state.local_rules(), "jp_exempt": True})
                return
            if route == "/api/options":
                self._json({"selection": state.local_options()})
                return
            if route == "/api/healthz":
                self._json({"status": "ok"})
                return
            static_map = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/index.html": ("index.html", "text/html; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            }
            item = static_map.get(route)
            if not item:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            path = state.dashboard_dir / item[0]
            if not path.is_file():
                self._json({"error": f"missing {item[0]}"}, HTTPStatus.NOT_FOUND)
                return
            self._send(HTTPStatus.OK, path.read_bytes(), item[1])

        def do_POST(self) -> None:
            route = urlsplit(self.path).path
            if route == "/api/start":
                started = state.start_controller()
                self._json({"started": started, "state": state.snapshot()})
                return
            if route == "/api/continue":
                result = state.request_continue()
                self._json(result)
                return
            if route == "/api/stop-selection":
                self._json(state.stop_selection())
                return
            if route == "/api/rules":
                try:
                    rules = state.save_local_rules(self._read_json())
                    self._json({"saved": True, "ordinary": rules, "jp_exempt": True})
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._json({"saved": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if route == "/api/options":
                try:
                    options = state.save_local_options(self._read_json())
                    self._json({"saved": True, "selection": options})
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._json({"saved": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if route == "/api/stop-monitor":
                state.stop_monitor()
                self._json({"stopped": True})
                return
            if route == "/api/shutdown":
                self._json({"closing": True})
                server = server_ref.get("server")
                if server:
                    threading.Thread(target=server.shutdown, daemon=True).start()
                return
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


def serve(
    root: Path,
    host: str,
    port: int,
    repository: str,
    branch: str,
    auto_start: bool,
    open_browser: bool,
) -> int:
    url = f"http://{host}:{port}/"
    state = DashboardState(root, repository, branch)
    server_ref: dict[str, ThreadingHTTPServer] = {}
    try:
        server = ThreadingHTTPServer((host, port), make_handler(state, server_ref))
    except OSError as exc:
        if dashboard_is_healthy(url):
            if open_browser:
                open_dashboard_url(url)
            print(f"Noode-CG 可视化面板已经运行：{url}")
            return 0
        print(f"Noode-CG 可视化面板无法监听 {host}:{port}：{exc}", file=sys.stderr)
        return 1
    server.daemon_threads = True
    server_ref["server"] = server

    def open_dashboard() -> None:
        time.sleep(0.8)
        open_dashboard_url(url)

    def watchdog() -> None:
        while True:
            time.sleep(1)
            if state.shutdown_at:
                state.refresh_github()
                workflow_status = str(state.gh_state.get("status") or "")
                if (
                    workflow_status in {"queued", "in_progress", "waiting", "pending"}
                    or state.continue_queue_path.is_file()
                ):
                    state.shutdown_at = utc_timestamp() + 300
                    continue
            if state.shutdown_at and utc_timestamp() >= state.shutdown_at:
                server.shutdown()
                return

    def background_refresh() -> None:
        while True:
            try:
                state.refresh_github()
                state.refresh_remote_outputs()
                state.process_continue_queue()
            except Exception as exc:  # pragma: no cover - keep the local UI alive on transient network failures
                with state.lock:
                    state.last_error = f"后台状态刷新失败：{exc}"
            time.sleep(3)

    if open_browser:
        threading.Thread(target=open_dashboard, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=background_refresh, daemon=True).start()
    if auto_start:
        state.start_controller()
    print("Noode-CG 本地可视化面板")
    print(f"浏览器地址：{url}")
    print("关闭此窗口只会停止本地监控，不会取消已经提交到 GitHub Actions 的任务。")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        process = state.process
        if process is not None and process.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError:
                pass
        server.server_close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Noode-CG local browser dashboard")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=13336)
    parser.add_argument("--repository", default="jachjkl/Noode-CG")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    return serve(
        Path(args.root).resolve(),
        args.host,
        args.port,
        args.repository,
        args.branch,
        not args.no_start,
        not args.no_browser,
    )


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    raise SystemExit(main())
