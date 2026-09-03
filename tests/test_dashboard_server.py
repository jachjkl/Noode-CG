from __future__ import annotations

import gzip
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_ROOT = ROOT / "windows-controller"
sys.path.insert(0, str(CONTROLLER_ROOT))

from dashboard_server import DashboardState, main  # noqa: E402


class DashboardServerTests(unittest.TestCase):
    def test_dashboard_cli_defaults_to_manual_start(self) -> None:
        with (
            patch.object(sys, "argv", ["dashboard_server.py", "--no-browser"]),
            patch("dashboard_server.serve", return_value=0) as serve,
        ):
            self.assertEqual(main(), 0)

        self.assertFalse(serve.call_args.args[5])

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

    def test_dashboard_has_live_test_stream_panel(self) -> None:
        html = (CONTROLLER_ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
        script = (CONTROLLER_ROOT / "dashboard" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="liveTestRows"', html)
        self.assertIn('id="liveTestPageSize"', html)
        self.assertIn('id="liveTestNext"', html)
        self.assertIn("本地实时测速与自动淘汰", html)
        self.assertIn('fetch(`/api/live-tests?', script)
        self.assertIn('<option value="200" selected>200</option>', html)
        self.assertIn('<option value="600">600</option>', html)
        self.assertIn('<option value="1000">1000</option>', html)
        self.assertNotIn('<option value="100">100</option>', html)
        self.assertNotIn('<option value="500">500</option>', html)

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

            self.assertEqual(source, "published-cloud")
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

    def test_live_test_stream_returns_latest_records_with_total_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = DashboardState(root, "owner/repo", "main")
            state.live_tests_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema": 1,
                "report": {
                    "status": "running",
                    "stage": "TLS",
                    "total": 2,
                    "processed": 1,
                    "eliminated": 1,
                },
                "tests": [
                    {"key": "old", "ip": "192.0.2.1", "updated_at": "2026-09-02T01:00:00+00:00"},
                    {"key": "new", "ip": "192.0.2.2", "updated_at": "2026-09-02T02:00:00+00:00"},
                ],
            }
            state.live_tests_path.write_bytes(
                gzip.compress(json.dumps(payload).encode("utf-8"), mtime=0)
            )

            report, tests, source, path = state.live_tests_with_meta(limit=1)

            self.assertEqual(source, "live-test-stream")
            self.assertEqual(path, state.live_tests_path)
            self.assertEqual(report["total"], 2)
            self.assertEqual(report["records_total"], 2)
            self.assertEqual(tests[0]["ip"], "192.0.2.2")

            report, tests, _, _ = state.live_tests_with_meta(limit=1, offset=1)
            self.assertEqual(report["records_total"], 2)
            self.assertEqual(tests[0]["ip"], "192.0.2.1")

    def test_new_cycle_clears_live_test_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = DashboardState(root, "owner/repo", "main")
            state.live_tests_path.parent.mkdir(parents=True, exist_ok=True)
            state.live_tests_path.write_bytes(b"old-live-tests")
            script = root / "manual-start.ps1"
            script.write_text("exit 0", encoding="utf-8")
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
            self.assertFalse(state.live_tests_path.exists())

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
            self.assertEqual(rules["loss_max_percent"], 30)
            self.assertEqual(rules["speed_min_mbps"], 3)
            self.assertEqual(rules["latency_probe"], "tcp")
            self.assertTrue(rules["tcp_enabled"])
            self.assertFalse(rules["tls_enabled"])
            self.assertFalse(rules["http_enabled"])

    def test_stop_selection_finishes_current_round_without_killing_local_probe(self) -> None:
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
            self.assertEqual(result["local_processes_terminated"], [])
            self.assertTrue(state.stop_after_current_path.is_file())
            self.assertFalse(state.continue_queue_path.exists())
            self.assertFalse(force.exists())
            command.assert_not_called()
            marker = json.loads(state.stop_after_current_path.read_text(encoding="utf-8"))
            self.assertIn("100-batch", marker["mode"])

    def test_stop_selection_cancels_cloud_before_local_testing_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = DashboardState(Path(temporary), "owner/repo", "main")
            state.gh = "gh.exe"
            state.run_id = 123
            state.gh_state = {
                "status": "in_progress",
                "jobs": [{"name": "cloud-prepare", "status": "in_progress"}],
            }
            completed = subprocess.CompletedProcess([], 0, "", "")
            with (
                patch.object(state, "refresh_github"),
                patch.object(state, "_local_selection_pids", return_value=[]),
                patch.object(state, "_run_command", return_value=completed) as command,
            ):
                result = state.stop_selection()
            self.assertTrue(result["cloud_cancelled"])
            self.assertFalse(state.stop_after_current_path.exists())
            self.assertIn("cancel", command.call_args.args[0])

    def test_close_session_clears_transient_state_but_keeps_published_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = DashboardState(root, "owner/repo", "main")
            handoff = root / "app" / "data" / "handoff"
            output = root / "app" / "output"
            handoff.mkdir(parents=True, exist_ok=True)
            output.mkdir(parents=True, exist_ok=True)
            for path in (
                state.live_tests_path,
                handoff / "local-live-results.json.gz",
                handoff / "cloud-raw10000.json.gz",
                output / "health.json",
            ):
                path.write_bytes(b"state")
            state.nodes_cache.write_text("[]", encoding="utf-8")
            state.cycle_started = True
            state.run_id = 999

            state.clear_session_state()

            self.assertFalse(state.cycle_started)
            self.assertIsNone(state.run_id)
            self.assertFalse(state.live_tests_path.exists())
            self.assertFalse((handoff / "cloud-raw10000.json.gz").exists())
            self.assertFalse((output / "health.json").exists())
            self.assertTrue(state.nodes_cache.exists())

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
            self.assertIn("continuation=true", args)
            self.assertIn("publish_only=false", args)
            handoff = root / "app" / "data" / "handoff"
            marker = json.loads((handoff / "force-rerank.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["general_target"], 300)
            self.assertEqual(marker["jp_target"], 10)
            self.assertEqual(marker["previous_top100_reserved_for_retest"], 0)
            self.assertFalse((handoff / "local-qualified.json.gz").exists())

    def test_manual_publish_queues_while_active_and_uses_publish_only_when_idle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = DashboardState(root, "owner/repo", "main")
            state.gh = "gh.exe"
            state.cycle_started = True
            state.gh_state = {"status": "in_progress"}
            with patch.object(state, "refresh_github"):
                queued = state.request_publish()
            self.assertTrue(queued["queued"])
            self.assertTrue(state.publish_queue_path.is_file())

            state.gh_state = {"status": "completed", "conclusion": "success"}
            completed = subprocess.CompletedProcess([], 0, "", "")
            with patch.object(state, "_run_command", return_value=completed) as command:
                started = state.process_publish_queue(force=True)
            self.assertTrue(started["started"])
            self.assertFalse(state.publish_queue_path.exists())
            args = command.call_args.args[0]
            self.assertIn("publish_only=true", args)
            marker = json.loads((
                root / "app" / "data" / "handoff" / "force-rerank.json"
            ).read_text(encoding="utf-8"))
            self.assertEqual(
                marker["mode"],
                "manual-publish-merge-cloud-dedupe-retest-top300",
            )
            self.assertEqual(state.cloud_round_count, 0)

    def test_cloud_connection_is_reported_separately_from_run_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = DashboardState(Path(temporary), "owner/repo", "main")
            state.gh = "gh.exe"
            completed = subprocess.CompletedProcess([], 0, "owner/repo\n", "")
            with patch.object(state, "_run_command", return_value=completed):
                connection = state.check_cloud_connection(force=True)
            self.assertEqual(connection["status"], "connected")
            self.assertEqual(state.snapshot()["cloud_connection"]["status"], "connected")

    def test_dashboard_exposes_cloud_badge_and_manual_publish_button(self) -> None:
        html = (CONTROLLER_ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
        script = (CONTROLLER_ROOT / "dashboard" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="cloudConnectionBadge"', html)
        self.assertIn('id="publishButton"', html)
        self.assertIn('post("/api/publish")', script)
        self.assertIn("state.cloud_connection", script)
        self.assertIn('id="ruleLatencyMax"', html)
        self.assertNotIn('id="ruleTcp"', html)
        self.assertNotIn('id="ruleTls"', html)
        self.assertNotIn('id="ruleHttp"', html)
        self.assertNotIn('id="continuousRounds"', html)

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

    def test_continuous_three_round_option_is_permanently_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = DashboardState(Path(temporary), "owner/repo", "main")
            self.assertFalse(state.local_options()["continuous_three_rounds"])

            saved = state.save_local_options({
                "selection": {"continuous_three_rounds": True}
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
                    "latency_probe": "tls",
                    "latency_max_ms": 240,
                    "jitter_max_ms": 80,
                    "loss_max_percent": 15,
                    "speed_min_mbps": 6,
                }
            })

            self.assertEqual(rules["loss_max_percent"], 15)
            self.assertEqual(state.local_rules()["speed_min_mbps"], 6)
            self.assertFalse(state.local_rules()["tcp_enabled"])
            self.assertTrue(state.local_rules()["tls_enabled"])
            self.assertEqual(state.local_rules()["latency_probe"], "tls")
            self.assertEqual(state.local_rules()["latency_max_ms"], 240)
            document = json.loads(state.rules_path.read_text(encoding="utf-8"))
            self.assertTrue(document["jp_exempt"])

    def test_local_rules_reject_nonfinite_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = DashboardState(Path(temporary), "owner/repo", "main")
            with self.assertRaises(ValueError):
                state.save_local_rules({"ordinary": {"tcp_max_ms": float("nan")}})

    def test_local_rules_reject_invalid_latency_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = DashboardState(Path(temporary), "owner/repo", "main")
            with self.assertRaisesRegex(ValueError, "必须是 TCP"):
                state.save_local_rules({"ordinary": {
                    "latency_probe": "icmp",
                }})

    def test_legacy_multi_metric_rule_is_normalized_to_tcp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = DashboardState(Path(temporary), "owner/repo", "main")
            state.rules_path.parent.mkdir(parents=True)
            state.rules_path.write_text(json.dumps({"ordinary": {
                "tcp_enabled": True,
                "tls_enabled": True,
                "http_enabled": True,
            }}), encoding="utf-8")

            rules = state.local_rules()
            self.assertEqual(rules["latency_probe"], "tcp")
            self.assertTrue(rules["tcp_enabled"])
            self.assertFalse(rules["tls_enabled"])
            self.assertFalse(rules["http_enabled"])


if __name__ == "__main__":
    unittest.main()
