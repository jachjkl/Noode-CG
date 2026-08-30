from __future__ import annotations

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
                    "current_selection": 3,
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
                    "tls_three_attempt_average_under_300ms": len(prepared),
                    "https_ttfb_three_attempt_average_under_300ms": len(prepared),
                }

            def speed(records: list[NodeResult], **_kwargs):
                prepared = [ready(node) for node in records]
                return prepared, {
                    "speed_tested": len(prepared),
                    "speed_at_least_16mbps": len(prepared),
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
            self.assertEqual(len(retest_scan), 4)
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
                    "current_selection": 3,
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
                    "tls_three_attempt_average_under_300ms": len(prepared),
                    "https_ttfb_three_attempt_average_under_300ms": len(prepared),
                }

            speed_call = 0

            def speed(records: list[NodeResult], **_kwargs):
                nonlocal speed_call
                speed_call += 1
                prepared = [ready(node) for node in records]
                keep = 1 if speed_call == 1 else 2 if speed_call == 2 else len(prepared)
                qualified = prepared[:keep]
                return qualified, {
                    "speed_tested": len(prepared),
                    "speed_at_least_16mbps": len(qualified),
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


if __name__ == "__main__":
    unittest.main()
