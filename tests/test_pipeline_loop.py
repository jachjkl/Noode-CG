from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.models import NodeResult
from core.pipeline import run_pipeline


def ready(node: NodeResult) -> NodeResult:
    node.tcp_ok = node.tls_ok = node.http_ok = True
    node.tcp_latency_ms = 50.0
    node.tls_latency_ms = 60.0
    node.http_latency_ms = 70.0
    node.average_latency_ms = 60.0
    node.tcp_loss_rate = 0.0
    node.speed_mbps = 20.0
    return node


class PipelineLoopTests(unittest.TestCase):
    def test_pipeline_waits_for_japanese_quota_before_final_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "locations.json").write_text("{}", encoding="utf-8")
            batches = [
                [NodeResult(ip=f"192.0.2.{index}", country_hint="US") for index in range(1, 3)],
                [NodeResult(ip=f"198.51.100.{index}", country_hint="JP") for index in range(1, 3)],
            ]
            config = {
                "_base_dir": str(root),
                "project": {"target_domain": "worker.example.com", "user_agent": "test"},
                "paths": {"locations": "locations.json", "checkpoints": "checkpoints", "output": "output"},
                "rolling": {
                    "previous_limit": 0,
                    "snapshot_path": "previous.json",
                    "official_snapshot_path": "previous-official.txt",
                },
                "sources": {"remote": [], "cloudflare_ranges": {"official_batch_size": 2}},
                "pipeline": {
                    "three_metric_shortlist": 2,
                    "speed_batch_size": 2,
                    "current_selection": 2,
                    "rolling_candidate_batch": 4,
                    "strict_tcp_candidates_per_round": 2,
                    "country_minimums": {"JP": 1},
                    "speed_country_reserve": {"JP": 1},
                    "max_runtime_seconds": 10000,
                    "minimum_round_budget_seconds": 1,
                    "postprocess_reserve_seconds": 1,
                    "tcp": {},
                    "rolling_retest": {},
                },
                "output": {
                    "top_nodes": 2,
                    "minimum_publish": 2,
                    "preserve_last_good": True,
                    "write_compatibility_zip": False,
                },
                "vantage": {"probe_files": []},
            }

            def metrics(records: list[NodeResult], **_kwargs):
                prepared = [ready(node) for node in records]
                for node in prepared:
                    node.country = node.country_hint
                return prepared, {
                    "tls_three_pass_success": len(prepared),
                    "https_ttfb_three_pass_success": len(prepared),
                }

            def speed(records: list[NodeResult], **_kwargs):
                prepared = [ready(node) for node in records]
                return prepared, {
                    "speed_tested_once": len(prepared),
                    "speed_at_least_minimum": len(prepared),
                }

            with (
                patch("core.pipeline.measure_network_baseline", return_value={"all_targets_passed": True}),
                patch("core.pipeline.load_previous_top", return_value=([], [])),
                patch("core.pipeline.collect_source_candidates", return_value=([], [])),
                patch(
                    "core.pipeline.collect_official_batch",
                    side_effect=[(batches[0], []), (batches[1], [])],
                ) as official_call,
                patch("core.pipeline.scan_tcp", new=AsyncMock(side_effect=lambda records, _options: list(records))),
                patch("core.pipeline._three_metric_checks", side_effect=metrics),
                patch("core.pipeline._speed_checks", side_effect=speed),
                patch("core.pipeline.load_locations", return_value={}),
            ):
                report = run_pipeline(config)

            published = json.loads((root / "output" / "nodes.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "ok")
            self.assertEqual(official_call.call_count, 2)
            self.assertEqual(len(published), 2)
            self.assertGreaterEqual(sum(node["country"] == "JP" for node in published), 1)

    def test_full_sources_plus_official_then_previous_top_retest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            checkpoints = root / "checkpoints"
            locations = root / "locations.json"
            locations.write_text("{}", encoding="utf-8")
            source_a = NodeResult(ip="1.1.1.1")
            source_a.add_source("source-a")
            source_b = NodeResult(ip="8.8.8.8")
            source_b.add_source("source-b")
            official = [NodeResult(ip=f"104.16.0.{index}") for index in range(1, 6)]
            previous = [NodeResult(ip="9.9.9.9")]
            config = {
                "_base_dir": str(root),
                "project": {"target_domain": "worker.example.com", "user_agent": "test"},
                "paths": {
                    "locations": "locations.json",
                    "checkpoints": "checkpoints",
                    "output": "output",
                },
                "rolling": {
                    "previous_limit": 1,
                    "snapshot_path": "previous.json",
                    "official_snapshot_path": "previous-official.txt",
                },
                "sources": {
                    "remote": [
                        {"name": "source-a", "min_records": 1},
                        {"name": "source-b", "min_records": 1},
                    ],
                    "cloudflare_ranges": {"official_batch_size": 5},
                },
                "pipeline": {
                    "three_metric_shortlist": 5,
                    "speed_batch_size": 5,
                    "current_selection": 3,
                    "strict_tcp_candidates_per_round": 5,
                    "max_runtime_seconds": 10000,
                    "minimum_round_budget_seconds": 1,
                    "postprocess_reserve_seconds": 1,
                    "tcp": {},
                    "rolling_retest": {},
                },
                "output": {
                    "top_nodes": 3,
                    "minimum_publish": 3,
                    "preserve_last_good": True,
                    "write_compatibility_zip": False,
                },
                "vantage": {"probe_files": []},
            }

            async_scan = AsyncMock(side_effect=lambda records, _options: list(records))

            def metrics(records: list[NodeResult], **_kwargs):
                prepared = [ready(node) for node in records]
                return prepared, {
                    "tls_three_pass_success": len(prepared),
                    "https_ttfb_three_pass_success": len(prepared),
                }

            def speed(records: list[NodeResult], **_kwargs):
                prepared = [ready(node) for node in records]
                return prepared, {
                    "speed_tested_once": len(prepared),
                    "speed_at_least_minimum": len(prepared),
                }

            with (
                patch(
                    "core.pipeline.measure_network_baseline",
                    return_value={"all_targets_passed": True},
                ),
                patch("core.pipeline.load_previous_top", return_value=(previous, [])),
                patch("core.pipeline.collect_source_candidates", return_value=([source_a, source_b], [])),
                patch("core.pipeline.collect_official_batch", return_value=(official, [])) as official_call,
                patch("core.pipeline.scan_tcp", new=async_scan),
                patch("core.pipeline._three_metric_checks", side_effect=metrics),
                patch("core.pipeline._speed_checks", side_effect=speed),
                patch("core.pipeline.load_locations", return_value={}),
            ):
                report = run_pipeline(config)

            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["selected"], 3)
            self.assertEqual(official_call.call_count, 1)
            first_scan = async_scan.await_args_list[0].args[0]
            self.assertEqual(len(first_scan), 7)
            retest_scan = async_scan.await_args_list[1].args[0]
            self.assertEqual(len(retest_scan), 6)
            self.assertEqual(async_scan.await_count, 2)
            self.assertIn("9.9.9.9", {node.ip for node in retest_scan})
            self.assertTrue((output / "nodes.txt").is_file())
            self.assertTrue(checkpoints.is_dir())

    def test_speed_qualified_results_accumulate_across_multiple_5000_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "locations.json").write_text("{}", encoding="utf-8")
            official_batches = [
                [NodeResult(ip=f"104.16.{batch}.{index}") for index in range(1, 6)]
                for batch in range(2)
            ]
            config = {
                "_base_dir": str(root),
                "project": {"target_domain": "worker.example.com", "user_agent": "test"},
                "paths": {"locations": "locations.json", "checkpoints": "checkpoints", "output": "output"},
                "rolling": {
                    "previous_limit": 1,
                    "snapshot_path": "previous.json",
                    "official_snapshot_path": "previous-official.txt",
                },
                "sources": {"remote": [], "cloudflare_ranges": {"official_batch_size": 5}},
                "pipeline": {
                    "three_metric_shortlist": 5,
                    "speed_batch_size": 5,
                    "current_selection": 3,
                    "strict_tcp_candidates_per_round": 5,
                    "max_runtime_seconds": 10000,
                    "minimum_round_budget_seconds": 1,
                    "postprocess_reserve_seconds": 1,
                    "tcp": {},
                    "rolling_retest": {},
                },
                "output": {
                    "top_nodes": 3,
                    "minimum_publish": 3,
                    "preserve_last_good": True,
                    "write_compatibility_zip": False,
                },
                "vantage": {"probe_files": []},
            }

            async_scan = AsyncMock(side_effect=lambda records, _options: list(records))

            def metrics(records: list[NodeResult], **_kwargs):
                prepared = [ready(node) for node in records]
                return prepared, {
                    "tls_three_pass_success": len(prepared),
                    "https_ttfb_three_pass_success": len(prepared),
                }

            speed_call = 0

            def speed(records: list[NodeResult], **_kwargs):
                nonlocal speed_call
                speed_call += 1
                prepared = [ready(node) for node in records]
                keep = 1 if speed_call == 1 else 2 if speed_call == 2 else len(prepared)
                qualified = prepared[:keep]
                return qualified, {
                    "speed_tested_once": len(prepared),
                    "speed_at_least_minimum": len(qualified),
                }

            with (
                patch("core.pipeline.measure_network_baseline", return_value={"all_targets_passed": True}),
                patch("core.pipeline.load_previous_top", return_value=([], [])),
                patch("core.pipeline.collect_source_candidates", return_value=([], [])),
                patch("core.pipeline.collect_official_batch", side_effect=[
                    (official_batches[0], []),
                    (official_batches[1], []),
                ]) as official_call,
                patch("core.pipeline.scan_tcp", new=async_scan),
                patch("core.pipeline._three_metric_checks", side_effect=metrics),
                patch("core.pipeline._speed_checks", side_effect=speed),
                patch("core.pipeline.load_locations", return_value={}),
            ):
                report = run_pipeline(config)

            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["selected"], 3)
            self.assertEqual(official_call.call_count, 2)
            self.assertEqual(len(report["speed_batches"]), 2)
            self.assertEqual(report["speed_batches"][0]["current_speed_qualified_total"], 1)
            self.assertEqual(report["speed_batches"][1]["current_speed_qualified_total"], 3)

    def test_final_verification_accumulates_successes_without_retesting_same_current_ip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "locations.json").write_text("{}", encoding="utf-8")
            batches = [
                [NodeResult(ip=f"104.17.{batch}.{index}") for index in range(1, 5)]
                for batch in range(2)
            ]
            config = {
                "_base_dir": str(root),
                "project": {"target_domain": "worker.example.com", "user_agent": "test"},
                "paths": {"locations": "locations.json", "checkpoints": "checkpoints", "output": "output"},
                "rolling": {
                    "previous_limit": 0,
                    "snapshot_path": "previous.json",
                    "official_snapshot_path": "previous-official.txt",
                },
                "sources": {"remote": [], "cloudflare_ranges": {"official_batch_size": 4}},
                "pipeline": {
                    "three_metric_shortlist": 4,
                    "speed_batch_size": 4,
                    "current_selection": 2,
                    "rolling_candidate_batch": 4,
                    "strict_tcp_candidates_per_round": 4,
                    "max_runtime_seconds": 10000,
                    "minimum_round_budget_seconds": 1,
                    "postprocess_reserve_seconds": 1,
                    "tcp": {},
                    "rolling_retest": {},
                },
                "output": {
                    "top_nodes": 2,
                    "minimum_publish": 2,
                    "preserve_last_good": True,
                    "write_compatibility_zip": False,
                },
                "vantage": {"probe_files": []},
            }
            metric_call = 0
            rolling_inputs: list[set[str]] = []

            def metrics(records: list[NodeResult], **_kwargs):
                nonlocal metric_call
                metric_call += 1
                prepared = [ready(node) for node in records]
                if metric_call % 2 == 0:
                    rolling_inputs.append({node.ip for node in prepared})
                    prepared = prepared[:1]
                return prepared, {
                    "tls_three_pass_success": len(prepared),
                    "https_ttfb_three_pass_success": len(prepared),
                }

            def speed(records: list[NodeResult], **_kwargs):
                prepared = [ready(node) for node in records]
                return prepared, {
                    "speed_tested_once": len(prepared),
                    "speed_at_least_minimum": len(prepared),
                }

            with (
                patch("core.pipeline.measure_network_baseline", return_value={"all_targets_passed": True}),
                patch("core.pipeline.load_previous_top", return_value=([], [])),
                patch("core.pipeline.collect_source_candidates", return_value=([], [])),
                patch(
                    "core.pipeline.collect_official_batch",
                    side_effect=[(batches[0], []), (batches[1], [])],
                ),
                patch("core.pipeline.scan_tcp", new=AsyncMock(side_effect=lambda records, _options: list(records))),
                patch("core.pipeline._three_metric_checks", side_effect=metrics),
                patch("core.pipeline._speed_checks", side_effect=speed),
                patch("core.pipeline.load_locations", return_value={}),
            ):
                report = run_pipeline(config)

            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["selected"], 2)
            self.assertEqual(len(rolling_inputs), 2)
            self.assertTrue(rolling_inputs[0].isdisjoint(rolling_inputs[1]))
            self.assertEqual(report["counts"]["rolling_verified_accumulated"], 2)


if __name__ == "__main__":
    unittest.main()
