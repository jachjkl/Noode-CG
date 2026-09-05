from __future__ import annotations

import argparse
import base64
import gzip
import json
import math
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
from urllib.parse import parse_qs, urlsplit

STAGE_LABELS = {
    "cloud-prepare": "云端生成 RAW10000",
    "local-select": "本地网络优选",
    "pre-publish-test": "发布前竞赛复测",
    "cloud-publish": "推送到 GitHub",
    "local-cleanup": "清理本地缓存",
    "replenish-cloud-pool": "自动补充候选池",
}

LOCAL_RULE_DEFAULTS = {
    "latency_probe": "tcp",
    "tcp_enabled": True,
    "tls_enabled": False,
    "http_enabled": False,
    "latency_max_ms": 200.0,
    "tcp_max_ms": 200.0,
    "tls_max_ms": 200.0,
    "http_ttfb_max_ms": 200.0,
    "average_max_ms": 200.0,
    "jitter_max_ms": 200.0,
    "loss_max_percent": 30.0,
    "speed_min_mbps": 3.0,
}

LOCAL_OPTION_DEFAULTS = {
    "continuous_three_rounds": False,
}

MANAGED_BROWSER: subprocess.Popen | None = None


def utc_timestamp() -> float:
    return time.time()


def iso_timestamp(value: object) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def open_dashboard_url(url: str) -> bool:
    global MANAGED_BROWSER
    try:
        if os.name == "nt":
            edge = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
            if edge.is_file():
                profile = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Noode-CG" / "dashboard-browser"
                MANAGED_BROWSER = subprocess.Popen([
                    str(edge), f"--app={url}", f"--user-data-dir={profile}",
                    "--no-first-run", "--no-default-browser-check", "--disable-background-mode",
                    "--disable-extensions", "--disable-features=Translate",
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
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
        self.browser_clients: dict[str, float | None] = {}
        self.close_when_idle = False
        self.repository = repository
        self.branch = branch
        self.dashboard_dir = root / "dashboard"
        self.session_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log_path = root / "logs" / f"run-{self.session_stamp}.log"
        self.latest_log_path = root / "logs" / "manual-last.log"
        self.cache_dir = root / "dashboard-cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.nodes_cache = self.cache_dir / "nodes.json"
        self.health_cache = self.cache_dir / "health.json"
        self.manual_script = root / "manual-start.ps1"
        self.rules_path = root / "app" / "data" / "local-rules.json"
        self.live_tests_path = root / "app" / "data" / "handoff" / "local-live-tests.json.gz"
        self.competition_results_path = root / "app" / "data" / "handoff" / "publish-competition-results.json.gz"
        self.options_path = root / "local-options.json"
        self.continue_queue_path = root / "app" / "data" / "handoff" / "dashboard-continue-request.json"
        self.publish_queue_path = root / "app" / "data" / "handoff" / "dashboard-publish-request.json"
        self.stop_after_current_path = root / "app" / "data" / "handoff" / "stop-after-current.json"
        # Reopening the dashboard is a new manual session. A queued Continue
        # from an older process must never launch work by itself.
        self.continue_queue_path.unlink(missing_ok=True)
        self.publish_queue_path.unlink(missing_ok=True)
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
        self.cloud_round_count = 0
        self.round_status_cleared = False
        self.nodes_checked_at = 0.0
        self.last_error = ""
        self.shutdown_at: float | None = None
        self.dispatch_pending_until = 0.0
        self.queue_retry_after = 0.0
        self.cycle_started = False
        self.cloud_connection_status = "checking" if self.gh else "cli_missing"
        self.cloud_connection_detail = "正在验证 GitHub 连接" if self.gh else "未找到 GitHub CLI"
        self.cloud_connection_checked_at = 0.0
        # A newly opened controller is always a fresh local UI session. Clear
        # stale workflow/test/result latches left by an older CMD process now,
        # while preserving the saved rules and verified published-node cache.
        self.clear_session_state()

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
            if self.close_when_idle:
                self.last_error = "面板正在完成关闭收尾，不能开始新任务。"
                return False
            if self.cycle_started:
                self.last_error = "本次面板会话已经开始；需要增加候选时请点击“继续优选并重排”。"
                return False
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
            self.cloud_round_count = 1
            self.round_status_cleared = False
            self.shutdown_at = None
            # A new dashboard session clears the preceding session exactly
            # once. Later continuation rounds append to the same live result.
            self._clear_cycle_state()
            self._reset_round_status()
            self._append_dashboard_log("开始本会话优选，后续日志持续追加。")
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
                    "-LogPath",
                    str(self.log_path),
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
            "local-live-tests.json.gz",
            "publish-competition-results.json.gz",
            "local-qualified.json.gz",
            "local-attempted-ips.txt.gz",
        ):
            (handoff / name).unlink(missing_ok=True)

    def _reset_round_status(self) -> None:
        """Remove stale stage telemetry but keep this session's winners."""
        for path in (
            self.live_tests_path,
            self.competition_results_path,
            self.root / "app" / "output" / "health.json",
            self.health_cache,
        ):
            path.unlink(missing_ok=True)

    def clear_session_state(self) -> None:
        """Clear transient run/UI state only when the panel really closes."""
        self._clear_cycle_state()
        handoff = self.root / "app" / "data" / "handoff"
        for path in (
            self.continue_queue_path,
            self.publish_queue_path,
            self.stop_after_current_path,
            handoff / "force-rerank.json",
            handoff / "cloud-raw10000.json.gz",
            self.root / "app" / "output" / "health.json",
            self.health_cache,
        ):
            path.unlink(missing_ok=True)
        with self.lock:
            self.cycle_started = False
            self.stop_requested = False
            self.run_id = None
            self.run_url = ""
            self.gh_state = {}
            self.observed_run_ids = []
            self.cloud_round_count = 0
            self.round_status_cleared = False
            self.exit_code = None
            self.process_started_at = None
            self.process_ended_at = None
            self.dispatch_pending_until = 0.0
            self.shutdown_at = None

    def _rotate_successful_logs(self, pattern: str, *, keep: int) -> None:
        try:
            logs = sorted(
                (path for path in (self.root / "logs").glob(pattern) if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for path in logs[max(1, keep):]:
                path.unlink(missing_ok=True)
        except OSError:
            pass

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
            # A completed workflow must not close the dashboard or release the
            # session Start lock. Only the explicit Close button ends it.
            self.shutdown_at = None
        if code == 0:
            self._rotate_successful_logs("run-*.log", keep=2)
        self.refresh_remote_outputs(force=True)
        self.refresh_github(force=True)

    def browser_presence(self, client: str, closed: bool = False) -> None:
        if not client or len(client) > 80:
            raise ValueError("invalid browser client")
        with self.lock:
            self.browser_clients[client] = utc_timestamp() + 8 if closed else None

    def browser_has_closed(self) -> bool:
        with self.lock:
            return bool(self.browser_clients) and all(
                deadline is not None and deadline <= utc_timestamp()
                for deadline in self.browser_clients.values()
            )

    def request_close(self) -> None:
        with self.lock:
            if self.close_when_idle:
                return
            self.close_when_idle = True
        self._append_dashboard_log("面板已请求关闭：停止新增优选，等待竞赛复测与发布收尾后退出后台。")
        try:
            self.stop_selection()
        except Exception:
            with self.lock:
                self.close_when_idle = False
            raise

    def ready_to_close(self) -> bool:
        if not self.close_when_idle:
            return False
        self.refresh_github(force=True)
        with self.lock:
            controller_active = self.process is not None and self.process.poll() is None
        return not (controller_active or self._workflow_active() or self._local_selection_pids())

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
        """Stop at a safe boundary and preserve qualified nodes for publishing."""
        self.refresh_github(force=True)
        workflow_active = self._workflow_active()
        local_active = bool(self._local_selection_pids())
        round_active = workflow_active or local_active
        jobs = self.gh_state.get("jobs", []) if isinstance(self.gh_state.get("jobs"), list) else []
        local_job = next(
            (job for job in jobs if isinstance(job, dict) and job.get("name") == "local-select"),
            {},
        )
        publish_job = next(
            (job for job in jobs if isinstance(job, dict) and job.get("name") == "cloud-publish"),
            {},
        )
        local_started = str(local_job.get("status") or "") == "in_progress" or (
            str(local_job.get("status") or "") == "completed"
            and str(local_job.get("conclusion") or "") == "success"
        )
        publish_started = str(publish_job.get("status") or "") in {"in_progress", "completed"}
        cloud_cancelled = False
        self.continue_queue_path.unlink(missing_ok=True)
        self.publish_queue_path.unlink(missing_ok=True)
        (self.root / "app" / "data" / "handoff" / "force-rerank.json").unlink(missing_ok=True)
        self.stop_after_current_path.parent.mkdir(parents=True, exist_ok=True)
        if (
            workflow_active
            and not local_active
            and not local_started
            and not publish_started
            and self.run_id is not None
            and self.gh
        ):
            cancel = self._run_command([
                self.gh,
                "run",
                "cancel",
                str(self.run_id),
                "--repo",
                self.repository,
            ])
            cloud_cancelled = cancel.returncode == 0
        if round_active and not cloud_cancelled:
            self.stop_after_current_path.write_text(
                json.dumps(
                    {
                        "requested_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "mode": "finish-current-100-batch-retest-publish-then-stop",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        else:
            self.stop_after_current_path.unlink(missing_ok=True)

        with self.lock:
            self.stop_requested = round_active
            self.dispatch_pending_until = 0.0
            self.last_error = ""
            self.shutdown_at = None
        if cloud_cancelled:
            self._append_dashboard_log("已停止优选：本地测速尚未开始，云端工作流已取消。")
        elif round_active:
            self._append_dashboard_log(
                "已请求停止优选：当前100-IP批次结束后停止扫描；随后完成发布前竞赛复测并推送TOP300。"
            )
        return {
            "stopped": round_active,
            "workflow_active": workflow_active,
            "local_active": local_active,
            "local_processes_terminated": [],
            "finish_current_cloud_run": workflow_active and not cloud_cancelled,
            "cloud_cancelled": cloud_cancelled,
        }

    def check_cloud_connection(self, force: bool = False) -> dict[str, object]:
        now = utc_timestamp()
        with self.lock:
            if not force and now - self.cloud_connection_checked_at < 15:
                return self.cloud_connection()
            self.cloud_connection_checked_at = now
        if not self.gh:
            with self.lock:
                self.cloud_connection_status = "cli_missing"
                self.cloud_connection_detail = "未找到 GitHub CLI"
            return self.cloud_connection()
        try:
            auth = self._run_command([self.gh, "auth", "status"], timeout=20)
            if auth.returncode != 0:
                with self.lock:
                    self.cloud_connection_status = "unauthenticated"
                    self.cloud_connection_detail = "GitHub CLI 尚未登录"
                return self.cloud_connection()
            remote = self._run_command(
                [self.gh, "api", f"repos/{self.repository}", "--jq", ".full_name"],
                timeout=20,
            )
            if remote.returncode != 0:
                detail = (remote.stderr or remote.stdout or "无法访问 GitHub 仓库").strip()
                with self.lock:
                    self.cloud_connection_status = "offline"
                    self.cloud_connection_detail = detail[:180]
                return self.cloud_connection()
            with self.lock:
                self.cloud_connection_status = "connected"
                self.cloud_connection_detail = f"已连接 {self.repository}"
        except (OSError, subprocess.SubprocessError) as exc:
            with self.lock:
                self.cloud_connection_status = "offline"
                self.cloud_connection_detail = f"云端连接失败：{exc}"
        return self.cloud_connection()

    def cloud_connection(self) -> dict[str, object]:
        with self.lock:
            return {
                "status": self.cloud_connection_status,
                "detail": self.cloud_connection_detail,
                "checked_at": self.cloud_connection_checked_at,
            }

    def _append_dashboard_log(self, message: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        with self.latest_log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    @staticmethod
    def _selected_latency_probe(values: dict[str, object], *, saving: bool = False) -> str:
        explicit = values.get("latency_probe")
        if explicit is not None:
            if not isinstance(explicit, str) or explicit.lower() not in {"tcp", "tls", "https"}:
                raise ValueError("延迟测试方式必须是 TCP、TLS 或 HTTPS TTFB")
            return explicit.lower()
        legacy: list[str] = []
        for key, probe in (
            ("tcp_enabled", "tcp"),
            ("tls_enabled", "tls"),
            ("http_enabled", "https"),
        ):
            raw = values.get(key, LOCAL_RULE_DEFAULTS[key])
            if not isinstance(raw, bool):
                raise ValueError(f"{key} 必须是布尔值")
            if raw:
                legacy.append(probe)
        if saving and not legacy:
            raise ValueError("请选择一种延迟测试方式")
        return "tcp" if "tcp" in legacy or not legacy else legacy[0]

    def local_rules(self) -> dict[str, float | bool | str]:
        payload = self.load_json_file(self.rules_path, {})
        values = payload.get("ordinary", payload) if isinstance(payload, dict) else {}
        rules = dict(LOCAL_RULE_DEFAULTS)
        if isinstance(values, dict):
            for name, default in LOCAL_RULE_DEFAULTS.items():
                raw = values.get(name, default)
                if name != "latency_probe" and not name.endswith("_enabled") and isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    rules[name] = float(raw)
            try:
                selected_probe = self._selected_latency_probe(values)
            except ValueError:
                selected_probe = "tcp"
            rules["latency_probe"] = selected_probe
            rules["tcp_enabled"] = selected_probe == "tcp"
            rules["tls_enabled"] = selected_probe == "tls"
            rules["http_enabled"] = selected_probe == "https"
            legacy_limit_key = {
                "tcp": "tcp_max_ms",
                "tls": "tls_max_ms",
                "https": "http_ttfb_max_ms",
            }[selected_probe]
            raw_limit = values.get(
                "latency_max_ms",
                values.get(legacy_limit_key, LOCAL_RULE_DEFAULTS["latency_max_ms"]),
            )
            if isinstance(raw_limit, (int, float)) and not isinstance(raw_limit, bool):
                rules["latency_max_ms"] = float(raw_limit)
            for key in ("tcp_max_ms", "tls_max_ms", "http_ttfb_max_ms", "average_max_ms"):
                rules[key] = rules["latency_max_ms"]
        return rules

    def save_local_rules(self, payload: object) -> dict[str, float | bool | str]:
        values = payload.get("ordinary", payload) if isinstance(payload, dict) else None
        if not isinstance(values, dict):
            raise ValueError("规则内容必须是对象")
        selected_probe = self._selected_latency_probe(values, saving=True)
        rules: dict[str, float | bool | str] = {
            "latency_probe": selected_probe,
            "tcp_enabled": selected_probe == "tcp",
            "tls_enabled": selected_probe == "tls",
            "http_enabled": selected_probe == "https",
        }
        legacy_limit_key = {
            "tcp": "tcp_max_ms",
            "tls": "tls_max_ms",
            "https": "http_ttfb_max_ms",
        }[selected_probe]
        raw_limit = values.get(
            "latency_max_ms",
            values.get(legacy_limit_key, LOCAL_RULE_DEFAULTS["latency_max_ms"]),
        )
        if not isinstance(raw_limit, (int, float)) or isinstance(raw_limit, bool):
            raise ValueError("latency_max_ms 必须是数字")
        latency_limit = float(raw_limit)
        if not math.isfinite(latency_limit) or latency_limit <= 0:
            raise ValueError("latency_max_ms 必须是大于 0 的有限数字")
        if not latency_limit.is_integer():
            raise ValueError("延迟上限必须是整数")
        rules["latency_max_ms"] = latency_limit
        for name in ("jitter_max_ms", "loss_max_percent", "speed_min_mbps"):
            default = LOCAL_RULE_DEFAULTS[name]
            raw = values.get(name, default)
            if not isinstance(raw, (int, float)) or isinstance(raw, bool):
                raise ValueError(f"{name} 必须是数字")
            rules[name] = float(raw)
            if not math.isfinite(rules[name]):
                raise ValueError(f"{name} 必须是有限数字")
            if not rules[name].is_integer():
                raise ValueError(f"{name} 必须是整数")
        for name in ("tcp_max_ms", "tls_max_ms", "http_ttfb_max_ms", "average_max_ms"):
            rules[name] = latency_limit
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
        try:
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.rules_path)
            stored = self.load_json_file(self.rules_path, None)
            stored_values = stored.get("ordinary") if isinstance(stored, dict) else None
            if stored_values != rules:
                raise OSError("规则文件写入后校验不一致")
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self._append_dashboard_log("普通节点自定义规则已保存；日本节点继续使用独立通道。")
        return rules

    def local_options(self) -> dict[str, bool]:
        return dict(LOCAL_OPTION_DEFAULTS)

    def save_local_options(self, payload: object) -> dict[str, bool]:
        values = payload.get("selection", payload) if isinstance(payload, dict) else None
        if not isinstance(values, dict):
            raise ValueError("运行选项必须是对象")
        options = {"continuous_three_rounds": False}
        document = {
            "schema": 1,
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "selection": options,
        }
        temporary = self.options_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.options_path)
            stored = self.load_json_file(self.options_path, None)
            stored_values = stored.get("selection") if isinstance(stored, dict) else None
            if stored_values != options:
                raise OSError("运行选项文件写入后校验不一致")
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self._append_dashboard_log("自动三轮连续筛选已移除；需要新候选时请手动点击继续优选。")
        return options

    def _workflow_active(self) -> bool:
        with self.lock:
            return str(self.gh_state.get("status") or "") in {
                "queued", "in_progress", "waiting", "pending"
            } or utc_timestamp() < self.dispatch_pending_until

    def request_continue(self) -> dict[str, object]:
        with self.lock:
            if self.close_when_idle:
                return {"started": False, "queued": False, "reason": "正在关闭收尾，不能继续优选。"}
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

    def request_publish(self) -> dict[str, object]:
        with self.lock:
            if self.close_when_idle:
                return {"started": False, "queued": False, "reason": "正在关闭收尾，不能重复推送。"}
            if not self.cycle_started:
                return {
                    "started": False,
                    "queued": False,
                    "reason": "请先完成至少一轮本地优选，再手动推送。",
                }
        self.publish_queue_path.parent.mkdir(parents=True, exist_ok=True)
        self.publish_queue_path.write_text(
            json.dumps({
                "requested_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "mode": "retest-and-publish-current-session",
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.refresh_github(force=True)
        if self._workflow_active():
            self._append_dashboard_log("已排队手动推送；当前整轮结束后复测云端与本地合格节点并发布。")
            return {"started": False, "queued": True, "reason": "手动推送已排队。"}
        return self.process_publish_queue(force=True)

    def process_publish_queue(self, force: bool = False) -> dict[str, object]:
        if not self.publish_queue_path.is_file():
            return {"started": False, "queued": False}
        if self._workflow_active():
            return {"started": False, "queued": True}
        handoff_dir = self.root / "app" / "data" / "handoff"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        existing_count = len(
            self._nodes_from_handoff(handoff_dir / "local-qualified.json.gz")
        )
        self._write_rerank_marker(
            handoff_dir,
            existing_count=existing_count,
            mode="manual-publish-merge-cloud-dedupe-retest-top300",
        )
        result = self._dispatch_workflow(publish_only=True)
        if result.get("started"):
            self.publish_queue_path.unlink(missing_ok=True)
            self._append_dashboard_log(
                "手动推送已启动：本会话合格IP与云端现有IP合并去重，"
                "按当前本地规则复测后仅发布TOP300普通节点，并附加JP。"
            )
            return {**result, "queued": False, "existing_count": existing_count}
        (handoff_dir / "force-rerank.json").unlink(missing_ok=True)
        return {**result, "queued": True}

    @staticmethod
    def _write_rerank_marker(
        handoff_dir: Path,
        *,
        existing_count: int,
        mode: str,
    ) -> None:
        marker = handoff_dir / "force-rerank.json"
        marker.write_text(
            json.dumps(
                {
                    "mode": mode,
                    "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "existing_count": existing_count,
                    "previous_top100_reserved_for_retest": 0,
                    "ranking": "local-rules-latency-speed-loss-jitter",
                    "general_target": 300,
                    "jp_target": 10,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _dispatch_workflow(self, *, publish_only: bool) -> dict[str, object]:
        if not self.gh:
            return {"started": False, "reason": "找不到 GitHub CLI。"}
        with self.lock:
            if (
                str(self.gh_state.get("status") or "")
                in {"queued", "in_progress", "waiting", "pending"}
                or utc_timestamp() < self.dispatch_pending_until
            ):
                return {"started": False, "reason": "当前已有任务正在触发或运行。"}
            # Claim the dispatch slot before invoking gh so two rapid clicks
            # cannot create duplicate cloud rounds.
            self.dispatch_pending_until = utc_timestamp() + 90
        self._reset_round_status()
        result = self._run_command([
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
            "-f",
            f"publish_only={'true' if publish_only else 'false'}",
        ])
        if result.returncode != 0:
            reason = (result.stderr or result.stdout or "GitHub 工作流触发失败").strip()
            with self.lock:
                self.dispatch_pending_until = 0.0
            return {"started": False, "reason": reason}
        with self.lock:
            self.process_started_at = utc_timestamp()
            self.process_ended_at = None
            self.exit_code = None
            self.last_error = ""
            self.run_id = None
            self.run_url = ""
            self.gh_state = {}
            self.dispatch_pending_until = utc_timestamp() + 90
            if not publish_only:
                self.cloud_round_count += 1
            self.round_status_cleared = False
            self.shutdown_at = None
            self.run_list_checked_at = 0.0
            self.gh_checked_at = 0.0
        mode = "手动推送复测" if publish_only else "续选"
        self._append_dashboard_log(f"{mode}工作流已触发。")
        return {"started": True, "publish_only": publish_only}

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
        handoff_dir = self.root / "app" / "data" / "handoff"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        accumulator = handoff_dir / "local-qualified.json.gz"
        # local-qualified is the active selection accumulator. The live-result
        # file is append-only UI history and must never be copied back into the
        # candidate pool because it may contain nodes eliminated by a later
        # fresh retest.
        existing_count = len(self._nodes_from_handoff(accumulator))
        self._write_rerank_marker(
            handoff_dir,
            existing_count=existing_count,
            mode="continue-fetch-new-raw10000-merge-cloud-dedupe-retest-top300",
        )
        dispatch = self._dispatch_workflow(publish_only=False)
        if not dispatch.get("started"):
            return dispatch
        self._append_dashboard_log(
            f"可视化面板已强制续选：保留当前合格池 {existing_count} 条，"
            "本次会话中的上一轮节点不重复测试；"
            "正在请求新的10000个官方候选，完成后按本地规则重排300条普通节点并附加日本节点。"
        )
        return {
            "started": True,
            "existing_count": existing_count,
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
                    if str(data.get("status") or "") == "completed":
                        if self.process_ended_at is None:
                            self.process_ended_at = utc_timestamp()
                        if (
                            self.stop_requested
                            and str(data.get("conclusion") or "") == "success"
                        ):
                            # Keep every completed card latched on screen. The
                            # next explicit Start/Continue action resets it.
                            self.stop_after_current_path.unlink(missing_ok=True)
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
                with self.lock:
                    self.cloud_connection_status = "connected"
                    self.cloud_connection_detail = f"已连接 {self.repository}"
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

    def competition_nodes_with_meta(
        self,
    ) -> tuple[list[dict[str, object]], dict[str, object], str, Path | None]:
        path = self.competition_results_path
        report: dict[str, object] = {}
        if path.is_file():
            try:
                payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
                report_value = payload.get("report") if isinstance(payload, dict) else None
                if isinstance(report_value, dict):
                    report = dict(report_value)
            except (OSError, gzip.BadGzipFile, UnicodeDecodeError, json.JSONDecodeError):
                report = {"status": "degraded", "stage": "竞赛复测结果读取失败"}
        return self._nodes_from_handoff(path), report, "publish-competition", (
            path if path.is_file() else None
        )

    def live_tests_with_meta(
        self, limit: int = 200, offset: int = 0
    ) -> tuple[dict[str, object], list[dict[str, object]], str, Path | None]:
        """Read the per-candidate local test stream without replacing results."""
        path = self.live_tests_path
        empty_report: dict[str, object] = {
            "status": "idle",
            "stage": "",
            "total": 0,
            "processed": 0,
            "queued": 0,
            "testing": 0,
            "passed": 0,
            "eliminated": 0,
            "retained": 0,
        }
        if not path.is_file():
            return empty_report, [], "live-test-stream", None
        try:
            payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        except (OSError, gzip.BadGzipFile, UnicodeDecodeError, json.JSONDecodeError):
            return empty_report, [], "live-test-stream", path
        if not isinstance(payload, dict):
            return empty_report, [], "live-test-stream", path
        report_value = payload.get("report")
        report = dict(report_value) if isinstance(report_value, dict) else empty_report
        tests_value = payload.get("tests")
        tests = [item for item in tests_value if isinstance(item, dict)] if isinstance(tests_value, list) else []
        report.setdefault("total", len(tests))
        report.setdefault("processed", 0)
        report["records_total"] = len(tests)
        try:
            capped = min(1000, max(1, int(limit)))
        except (TypeError, ValueError):
            capped = 200
        try:
            start = max(0, int(offset))
        except (TypeError, ValueError):
            start = 0
        tests.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return report, tests[start:start + capped], "live-test-stream", path

    def nodes_with_meta(self) -> tuple[list[dict[str, object]], str, Path | None]:
        # The published panel represents GitHub, so a successfully verified
        # remote cache takes precedence over transient local output.
        data = self.load_json_file(self.nodes_cache, [])
        if isinstance(data, list) and data:
            return data, "published-cloud", self.nodes_cache
        local_nodes = self.root / "app" / "output" / "nodes.json"
        if local_nodes.is_file():
            data = self.load_json_file(local_nodes, [])
            if isinstance(data, list) and data:
                return data, "local-ready", local_nodes
        return [], "published-unavailable", None

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
            health_data = self.health()
            live_report, _tests, _live_source, _live_path = self.live_tests_with_meta(
                limit=1,
                offset=0,
            )
            live_stage = str(live_report.get("stage") or "")
            publish_retest = health_data.get("publish_retest", {})
            telemetry_started = self.process_started_at or 0.0
            health_is_current = (
                self.cycle_started
                and not self.round_status_cleared
                and iso_timestamp(health_data.get("generated_at")) >= telemetry_started - 5
            )
            live_is_current = (
                self.cycle_started
                and not self.round_status_cleared
                and max(
                    iso_timestamp(live_report.get("started_at")),
                    iso_timestamp(live_report.get("generated_at")),
                ) >= telemetry_started - 5
            )
            if not live_is_current:
                live_stage = ""
            publish_retest_done = (
                health_is_current
                and
                isinstance(publish_retest, dict)
                and str(publish_retest.get("status") or "") == "completed"
            )
            if publish_retest_done or "发布前竞赛复测完成" in live_stage:
                pretest_status, pretest_conclusion = "completed", "success"
                pretest_detail = "云端与本轮合格IP已合并去重并完成规则复测"
            elif live_stage.startswith("发布前竞赛复测"):
                pretest_status, pretest_conclusion = "in_progress", ""
                pretest_detail = live_stage
            else:
                pretest_status, pretest_conclusion = "pending", ""
                pretest_detail = "等待本地初筛完成"
            jobs["pre-publish-test"] = {
                "name": "pre-publish-test",
                "label": STAGE_LABELS["pre-publish-test"],
                "status": pretest_status,
                "conclusion": pretest_conclusion,
                "startedAt": "",
                "completedAt": "",
                "url": "",
                "steps": [{
                    "name": pretest_detail,
                    "status": pretest_status,
                    "conclusion": pretest_conclusion,
                    "number": 0,
                }],
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
            publish_job = jobs.get("cloud-publish", {})
            publish_success = (
                str(publish_job.get("status") or "") == "completed"
                and str(publish_job.get("conclusion") or "") == "success"
            )
            if gh_status == "completed" and gh_conclusion == "success" and publish_success:
                round_completion = {
                    "status": "completed",
                    "label": "本轮复测与推送已完成",
                    "detail": f"第 {self.cloud_round_count or 1} 轮结果已成功发布到云端",
                }
            elif str(publish_job.get("status") or "") == "in_progress":
                round_completion = {
                    "status": "publishing",
                    "label": "正在完成本轮推送",
                    "detail": "TOP300 普通节点与 JP 豁免节点正在写入云端",
                }
            elif pretest_status == "completed":
                round_completion = {
                    "status": "publishing",
                    "label": "竞赛复测已完成",
                    "detail": "已选出本轮结果，等待 GitHub 推送完成",
                }
            elif pretest_status == "in_progress":
                round_completion = {
                    "status": "testing",
                    "label": "正在进行发布前竞赛复测",
                    "detail": "本轮合格IP与云端IP正在合并、去重和完整测速",
                }
            elif workflow_active:
                round_completion = {
                    "status": "running",
                    "label": "本轮工作流进行中",
                    "detail": "完成竞赛复测和云端推送后将在这里确认",
                }
            elif gh_status == "completed" and gh_conclusion in {"failure", "cancelled", "timed_out"}:
                round_completion = {
                    "status": "failure",
                    "label": "本轮推送未完成",
                    "detail": "请查看工作流卡片与实时日志",
                }
            else:
                round_completion = {
                    "status": "idle",
                    "label": "等待本轮开始",
                    "detail": "完成复测与推送后会保持显示直到下一轮",
                }
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
                    "round": self.cloud_round_count,
                },
                "health": health_data,
                "publish_status": {
                    "status": str(jobs.get("cloud-publish", {}).get("status") or "pending"),
                    "conclusion": str(jobs.get("cloud-publish", {}).get("conclusion") or ""),
                    "pretest": pretest_status,
                    "detail": pretest_detail,
                },
                "round_completion": round_completion,
                "log": self.read_log(),
                "last_error": self.last_error,
                "continuation_queued": self.continue_queue_path.is_file(),
                "publish_queued": self.publish_queue_path.is_file(),
                "cloud_connection": self.cloud_connection(),
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
            request = urlsplit(self.path)
            route = request.path
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
            if route == "/api/competition-nodes":
                nodes, report, source, path = state.competition_nodes_with_meta()
                updated_at = (
                    datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
                    if path is not None and path.is_file()
                    else ""
                )
                self._json({
                    "nodes": nodes,
                    "report": report,
                    "source": source,
                    "updated_at": updated_at,
                })
                return
            if route == "/api/live-tests":
                query = parse_qs(request.query)
                limit = query.get("limit", ["200"])[0]
                offset = query.get("offset", ["0"])[0]
                report, tests, source, path = state.live_tests_with_meta(
                    limit=limit,
                    offset=offset,
                )
                updated_at = (
                    datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
                    if path is not None and path.is_file()
                    else ""
                )
                self._json({
                    "report": report,
                    "tests": tests,
                    "total": report.get("total", len(tests)),
                    "records_total": report.get("records_total", len(tests)),
                    "offset": max(0, int(offset)) if str(offset).isdigit() else 0,
                    "limit": min(1000, max(1, int(limit))) if str(limit).isdigit() else 200,
                    "source": source,
                    "updated_at": updated_at,
                })
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
            if route == "/api/browser-presence":
                try:
                    payload = self._read_json()
                    state.browser_presence(str(payload.get("client", "")), payload.get("closed") is True)
                    self._json({"ok": True})
                except (ValueError, TypeError, AttributeError):
                    self._json({"error": "invalid presence"}, HTTPStatus.BAD_REQUEST)
                return
            if route == "/api/publish":
                self._json(state.request_publish())
                return
            if route == "/api/stop-selection":
                self._json(state.stop_selection())
                return
            if route == "/api/rules":
                try:
                    rules = state.save_local_rules(self._read_json())
                    self._json({"saved": True, "ordinary": rules, "jp_exempt": True})
                except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._json({"saved": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                except OSError as exc:
                    self._json({"saved": False, "error": f"规则文件写入失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            if route == "/api/options":
                try:
                    options = state.save_local_options(self._read_json())
                    self._json({"saved": True, "selection": options})
                except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._json({"saved": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                except OSError as exc:
                    self._json({"saved": False, "error": f"运行选项文件写入失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            if route == "/api/stop-monitor":
                state.stop_monitor()
                self._json({"stopped": True})
                return
            if route == "/api/shutdown":
                state.request_close()
                self._json({"closing": True, "finishing": True})
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
            print(f"Noode-CG 可视化面板已经运行：{url}")
            return 0
        print(f"Noode-CG 可视化面板无法监听 {host}:{port}：{exc}", file=sys.stderr)
        return 1
    server.daemon_threads = True
    server_ref["server"] = server
    state._append_dashboard_log("本地面板已启动；等待手动开始。关闭页面后自动停止并完成收尾。")

    def open_dashboard() -> None:
        time.sleep(0.8)
        open_dashboard_url(url)

    def watchdog() -> None:
        while True:
            time.sleep(1)
            try:
                if state.browser_has_closed() and not state.close_when_idle:
                    state.request_close()
                if state.ready_to_close():
                    state._append_dashboard_log("本轮收尾已结束，关闭本地面板后台。")
                    server.shutdown()
                    return
            except Exception as exc:
                state._append_dashboard_log(f"关闭收尾检查失败，将继续等待：{exc}")
            if state.shutdown_at:
                state.refresh_github()
                workflow_status = str(state.gh_state.get("status") or "")
                if (
                    workflow_status in {"queued", "in_progress", "waiting", "pending"}
                    or state.continue_queue_path.is_file()
                    or state.publish_queue_path.is_file()
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
                state.check_cloud_connection()
                state.refresh_remote_outputs()
                state.process_publish_queue()
                if not state.publish_queue_path.is_file():
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
        if MANAGED_BROWSER is not None and MANAGED_BROWSER.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(MANAGED_BROWSER.pid), "/T", "/F"],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
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
        # Closing the CMD window is also an explicit end of the local UI
        # session. Reset buttons, workflow-card latches and transient probe
        # files, while keeping local-rules.json and the published node cache.
        state.clear_session_state()
        server.server_close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Noode-CG local browser dashboard")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=13336)
    parser.add_argument("--repository", default="jachjkl/Noode-CG")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--no-start", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    return serve(
        Path(args.root).resolve(),
        args.host,
        args.port,
        args.repository,
        args.branch,
        bool(args.start and not args.no_start),
        not args.no_browser,
    )


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    raise SystemExit(main())
