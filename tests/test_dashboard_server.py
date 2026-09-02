from __future__ import annotations

import gzip
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_ROOT = ROOT / "windows-controller"
sys.path.insert(0, str(CONTROLLER_ROOT))

from dashboard_server import DashboardState  # noqa: E402


class DashboardServerTests(unittest.TestCase):
    def test_old_controller_log_cannot_replace_a_newer_active_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = DashboardState(Path(temporary), "owner/repo", "main")
            state.run_id = 200
            state.run_url = "https://github.com/owner/repo/actions/runs/200"

            state._discover_run("云端工作流已成功触发，运行编号 #100。")

            self.assertEqual(state.run_id, 200)
            self.assertTrue(state.run_url.endswith("/200"))

    def test_completed_historical_run_is_not_adopted_before_manual_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = DashboardState(Path(temporary), "owner/repo", "main")
            state.gh = "gh.exe"
            completed = subprocess.CompletedProcess(
                [],
                0,
                json.dumps([{
                    "databaseId": 100,
                    "status": "completed",
                    "conclusion": "failure",
                    "url": "https://github.com/owner/repo/actions/runs/100",
                    "createdAt": "2026-09-02T00:00:00Z",
                    "event": "workflow_dispatch",
                }]),
                "",
            )

            with patch.object(state, "_run_command", return_value=completed):
                state.refresh_github(force=True)

            snapshot = state.snapshot()
            self.assertEqual(snapshot["status"], "idle")
            self.assertFalse(snapshot["cycle_started"])
            self.assertIsNone(snapshot["run_id"])

    def test_dashboard_table_exposes_sort_event_target(self) -> None:
        html = (CONTROLLER_ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
        script = (CONTROLLER_ROOT / "dashboard" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="nodeTable"', html)
        self.assertIn('querySelector("#nodeTable thead")', script)

    def test_dashboard_has_separate_live_and_published_result_panels(self) -> None:
        html = (CONTROLLER_ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
        script = (CONTROLLER_ROOT / "dashboard" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="liveNodeRows"', html)
        self.assertIn("本轮实时优选 IP 结果", html)
        self.assertIn("云端已发布 IP 结果", html)
        self.assertIn('fetch("/api/live-nodes"', script)

    def test_published_results_do_not_get_replaced_by_unpublished_accumulator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handoff = root / "app" / "data" / "handoff"
            output = root / "app" / "output"
            cache = root / "dashboard-cache"
            handoff.mkdir(parents=True)
            output.mkdir(parents=True)
            cache.mkdir(parents=True)
            (output / "health.json").write_text(
                json.dumps({"published": False}), encoding="utf-8"
            )
            (cache / "nodes.json").write_text(
                json.dumps([{"ip": "192.0.2.1", "country": "HK"}]), encoding="utf-8"
            )
            payload = {
                "schema": 1,
                "nodes": [{
                    "ip": "198.51.100.20",
                    "port": 443,
                    "country_hint": "US",
                    "tcp_latency_ms": 80.0,
                    "tls_latency_ms": 100.0,
                    "http_latency_ms": 120.0,
                    "average_latency_ms": 100.0,
                    "overall_jitter_ms": 12.0,
                    "tcp_loss_rate": 0.1,
                    "speed_mbps": 8.0,
                }],
            }
            (handoff / "local-qualified.json.gz").write_bytes(
                gzip.compress(json.dumps(payload).encode("utf-8"), mtime=0)
            )

            state = DashboardState(root, "owner/repo", "main")
            nodes, source, path = state.nodes_with_meta()

            self.assertEqual(source, "published-cache")
            self.assertEqual(path, cache / "nodes.json")
            self.assertEqual(nodes[0]["ip"], "192.0.2.1")

    def test_live_results_are_exposed_separately_from_published_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handoff = root / "app" / "data" / "handoff"
            output = root / "app" / "output"
            handoff.mkdir(parents=True)
            output.mkdir(parents=True)
            (output / "health.json").write_text(
                json.dumps({"published": True}), encoding="utf-8"
            )
            (output / "nodes.json").write_text(
                json.dumps([{"ip": "192.0.2.1", "country": "HK"}]), encoding="utf-8"
            )
            payload = {
                "schema": 1,
                "report": {"status": "running", "live_preview": True},
                "nodes": [{"ip": "198.51.100.90", "port": 443, "country_hint": "US"}],
            }
            live = handoff / "local-live-results.json.gz"
            live.write_bytes(
                gzip.compress(json.dumps(payload).encode("utf-8"), mtime=0)
            )
            live.touch()

            state = DashboardState(root, "owner/repo", "main")
            published, source, path = state.nodes_with_meta()
            nodes, live_source, live_path = state.live_nodes_with_meta()

            self.assertEqual(source, "local-ready")
            self.assertEqual(path, output / "nodes.json")
            self.assertEqual(published[0]["ip"], "192.0.2.1")
            self.assertEqual(live_source, "live-current-cycle")
            self.assertEqual(live_path, live)
            self.assertEqual(nodes[0]["ip"], "198.51.100.90")

    def test_starting_a_new_cycle_clears_previous_live_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            live = root / "app" / "data" / "handoff" / "local-live-results.json.gz"
            live.parent.mkdir(parents=True)
            live.write_bytes(b"old-live-results")
            script = root / "manual-start.ps1"
            script.write_text("exit 0", encoding="utf-8")
            state = DashboardState(root, "owner/repo", "main")
            state.manual_script = script
            fake_process = unittest.mock.MagicMock()
            fake_process.poll.return_value = None
            fake_process.stdout = []

            with (
                patch("dashboard_server.Path.is_file", return_value=True),
                patch("dashboard_server.subprocess.Popen", return_value=fake_process),
                patch("dashboard_server.threading.Thread"),
            ):
                started = state.start_controller()

            self.assertTrue(started)
            self.assertFalse(live.exists())

    def test_default_local_rules_match_the_saved_200ms_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = DashboardState(Path(temporary), "owner/repo", "main")
            rules = state.local_rules()
            self.assertEqual(rules["tcp_max_ms"], 200)
            self.assertEqual(rules["tls_max_ms"], 200)
            self.assertEqual(rules["http_ttfb_max_ms"], 200)
            self.assertEqual(rules["average_max_ms"], 200)
            self.assertEqual(rules["jitter_max_ms"], 200)
            self.assertEqual(rules["loss_max_percent"], 20)
            self.assertEqual(rules["speed_min_mbps"], 3)

    def test_stop_selection_kills_only_detected_local_probe_and_clears_requeue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = DashboardState(root, "owner/repo", "main")
            state.gh_state = {"status": "in_progress"}
            state.continue_queue_path.parent.mkdir(parents=True)
            state.continue_queue_path.write_text("{}", encoding="utf-8")
            force = root / "app" / "data" / "handoff" / "force-rerank.json"
            force.write_text("{}", encoding="utf-8")
            completed = subprocess.CompletedProcess([], 0, "", "")

            with (
                patch.object(state, "refresh_github"),
                patch.object(state, "_local_selection_pids", return_value=[4321]),
                patch.object(state, "_run_command", return_value=completed) as command,
            ):
                result = state.stop_selection()

            self.assertTrue(result["workflow_active"])
            self.assertEqual(result["local_processes_terminated"], [4321])
            self.assertTrue(state.stop_after_current_path.is_file())
            self.assertFalse(state.continue_queue_path.exists())
            self.assertFalse(force.exists())
            command.assert_called_once_with(
                ["taskkill", "/PID", "4321", "/T", "/F"], timeout=15
            )

    def test_force_continue_saves_current_nodes_and_dispatches_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "app" / "output"
            output.mkdir(parents=True)
            nodes = [
                {"ip": f"198.51.100.{index}", "port": 443, "country": "US"}
                for index in range(1, 103)
            ]
            (output / "nodes.json").write_text(
                json.dumps(nodes), encoding="utf-8"
            )
            state = DashboardState(root, "owner/repo", "main")
            state.gh = "gh.exe"
            state.cycle_started = True
            state.gh_state = {"status": "completed"}
            completed = subprocess.CompletedProcess([], 0, "", "")

            with (
                patch.object(state, "refresh_github"),
                patch.object(state, "refresh_remote_outputs"),
                patch.object(state, "_run_command", return_value=completed) as command,
            ):
                result = state.force_continue()

            self.assertTrue(result["started"])
            self.assertEqual(result["existing_count"], 0)
            self.assertEqual(result["previous_top100_retest_count"], 0)
            command.assert_called_once()
            args = command.call_args.args[0]
            self.assertEqual(args[-2:], ["-f", "continuation=true"])
            handoff = root / "app" / "data" / "handoff"
            marker = json.loads((handoff / "force-rerank.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["general_target"], 300)
            self.assertEqual(marker["jp_target"], 10)
            self.assertEqual(marker["previous_top100_reserved_for_retest"], 0)
            payload = json.loads(gzip.decompress(
                (handoff / "local-qualified.json.gz").read_bytes()
            ))
            self.assertEqual(payload["nodes"], [])

    def test_force_continue_refuses_to_overlap_active_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = DashboardState(Path(temporary), "owner/repo", "main")
            state.gh = "gh.exe"
            state.cycle_started = True
            state.gh_state = {"status": "in_progress"}
            with patch.object(state, "refresh_github"):
                result = state.force_continue()
            self.assertFalse(result["started"])
            self.assertIn("正在运行", result["reason"])

    def test_continue_button_queues_while_workflow_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = DashboardState(root, "owner/repo", "main")
            state.gh = "gh.exe"
            state.cycle_started = True
            state.gh_state = {"status": "in_progress"}
            with patch.object(state, "refresh_github"):
                result = state.request_continue()

            self.assertFalse(result["started"])
            self.assertTrue(result["queued"])
            self.assertTrue(state.continue_queue_path.is_file())
            request = json.loads(state.continue_queue_path.read_text(encoding="utf-8"))
            self.assertEqual(request["mode"], "always-fetch-new-raw10000-and-rerank")

    def test_continuous_three_round_option_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = DashboardState(Path(temporary), "owner/repo", "main")
            self.assertTrue(state.local_options()["continuous_three_rounds"])

            saved = state.save_local_options({
                "selection": {"continuous_three_rounds": False}
            })

            self.assertFalse(saved["continuous_three_rounds"])
            self.assertFalse(state.local_options()["continuous_three_rounds"])

    def test_launcher_opens_dashboard_without_automatic_selection(self) -> None:
        launcher = (CONTROLLER_ROOT / "launch-dashboard.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('"--no-start"', launcher)

    def test_queued_continue_dispatches_after_workflow_becomes_idle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = DashboardState(root, "owner/repo", "main")
            state.gh = "gh.exe"
            state.cycle_started = True
            state.gh_state = {"status": "in_progress"}
            with patch.object(state, "refresh_github"):
                queued = state.request_continue()
            self.assertTrue(queued["queued"])

            state.gh_state = {"status": "completed", "conclusion": "success"}
            with patch.object(
                state,
                "force_continue",
                return_value={"started": True, "existing_count": 17},
            ) as dispatch:
                result = state.process_continue_queue(force=True)

            self.assertTrue(result["started"])
            dispatch.assert_called_once()
            self.assertFalse(state.continue_queue_path.exists())

    def test_continue_cannot_skip_the_manual_first_round(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = DashboardState(Path(temporary), "owner/repo", "main")
            state.gh = "gh.exe"

            result = state.request_continue()

            self.assertFalse(result["started"])
            self.assertFalse(result["queued"])
            self.assertIn("开始新一轮", result["reason"])
            self.assertFalse(state.continue_queue_path.exists())

    def test_local_rules_are_validated_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = DashboardState(Path(temporary), "owner/repo", "main")
            rules = state.save_local_rules({
                "ordinary": {
                    "tcp_max_ms": 220,
                    "tls_max_ms": 240,
                    "http_ttfb_max_ms": 260,
                    "average_max_ms": 230,
                    "jitter_max_ms": 80,
                    "loss_max_percent": 15,
                    "speed_min_mbps": 6,
                }
            })

            self.assertEqual(rules["loss_max_percent"], 15)
            self.assertEqual(state.local_rules()["speed_min_mbps"], 6)
            document = json.loads(state.rules_path.read_text(encoding="utf-8"))
            self.assertTrue(document["jp_exempt"])


if __name__ == "__main__":
    unittest.main()
