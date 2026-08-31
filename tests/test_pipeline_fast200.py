from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.models import NodeResult
from core.pipeline import run_pipeline


def qualified(node: NodeResult) -> NodeResult:
    node.tcp_ok = node.tls_ok = node.http_ok = True
    node.tcp_latency_ms = 50.0
    node.tls_latency_ms = 60.0
    node.http_latency_ms = 70.0
    node.tcp_jitter_ms = node.tls_jitter_ms = node.http_jitter_ms = 5.0
    node.average_latency_ms = 60.0
    node.overall_jitter_ms = 5.0
    node.tcp_loss_rate = 0.0
    node.speed_mbps = 20.0
    node.country = node.country_hint
    return node


class FastTwoStagePipelineTests(unittest.TestCase):
    def test_jp_lane_uses_one_tcp_speed_ranking_and_keeps_unique_ips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "locations.json").write_text("{}", encoding="utf-8")
            jp = [
                NodeResult(ip="198.18.0.10", port=443, country_hint="JP"),
                NodeResult(ip="198.18.0.10", port=8443, country_hint="JP"),
                NodeResult(ip="198.18.0.11", port=443, country_hint="JP"),
            ]
            for node in jp:
                node.add_source("fixed-source")
            official = [
                NodeResult(ip="198.51.100.1", country_hint="US"),
                NodeResult(ip="198.51.100.2", country_hint="US"),
            ]
            config = {
                "_base_dir": str(root),
                "project": {"target_domain": "worker.example.com", "user_agent": "test"},
                "paths": {"locations": "locations.json", "checkpoints": "checkpoints", "output": "output"},
                "rolling": {
                    "previous_limit": 1,
                    "snapshot_path": "previous.json",
                    "official_snapshot_path": "previous-official.txt.gz",
                },
                "sources": {"remote": [], "cloudflare_ranges": {"official_batch_size": 2}},
                "pipeline": {
                    "prefilter_shortlist": 2,
                    "speed_batch_size": 2,
                    "current_selection": 3,
                    "maximum_combined_latency_ms": 300,
                    "maximum_component_latency_ms": 300,
                    "maximum_jitter_ms": 500,
                    "country_minimums": {"JP": 2},
                    "jp_source_requirement": {"country": "JP", "count": 2, "tcp_attempts": 3},
                    "prefilter_country_reserve": {"JP": 1},
                    "speed_country_reserve": {"JP": 1},
                    "max_official_rounds": 2,
                    "max_runtime_seconds": 10000,
                    "minimum_round_budget_seconds": 1,
                    "postprocess_reserve_seconds": 1,
                    "prefilter_tcp": {"stage": "prefilter", "maximum_average_latency_ms": 1000},
                    "quality_tcp": {"stage": "quality", "maximum_average_latency_ms": 300},
                    "tls": {"maximum_average_latency_ms": 300},
                    "http": {"maximum_average_ttfb_ms": 300},
                    "speed": {"minimum_mbps": 1},
                },
                "output": {
                    "top_nodes": 3,
                    "minimum_publish": 3,
                    "preserve_last_good": True,
                    "write_compatibility_zip": False,
                },
                "vantage": {"probe_files": []},
            }
            events: list[tuple[str, float]] = []
            async def scan(records, options):
                events.append((str(options["stage"]), float(options["maximum_average_latency_ms"])))
                return [qualified(node) for node in records]

            def metrics(records: list[NodeResult], **kwargs):
                events.append(("metrics", float(kwargs["pipeline"]["maximum_combined_latency_ms"])))
                prepared = [qualified(node) for node in records]
                return prepared, {
                    "tls_three_pass_success": len(prepared),
                    "https_ttfb_three_pass_success": len(prepared),
                }

            def speed(records: list[NodeResult], **kwargs):
                events.append(("speed", float(kwargs["pipeline"]["speed"]["minimum_mbps"])))
                prepared = [qualified(node) for node in records]
                return prepared, {
                    "speed_tested_once": len(prepared),
                    "speed_at_least_minimum": len(prepared),
                }

            with (
                patch("core.pipeline.measure_network_baseline", return_value={"all_targets_passed": True}),
                patch("core.pipeline.load_previous_top", return_value=([], [])),
                patch("core.pipeline.collect_source_candidates", return_value=(jp, [])),
                patch("core.pipeline.collect_official_batch", return_value=(official, [])) as official_call,
                patch(
                    "core.pipeline._source_country_tcp_speed_checks",
                    return_value=([qualified(jp[0]), qualified(jp[2])], {"selected_unique_ips": 2}),
                ) as jp_lane,
                patch("core.pipeline.scan_tcp", new=AsyncMock(side_effect=scan)),
                patch("core.pipeline._three_metric_checks", side_effect=metrics),
                patch("core.pipeline._speed_checks", side_effect=speed),
                patch("core.pipeline.load_locations", return_value={}),
            ):
                report = run_pipeline(config)

            self.assertEqual(report["status"], "ok")
            self.assertEqual(official_call.call_count, 1)
            self.assertEqual(jp_lane.call_count, 1)
            self.assertEqual(
                events[:4],
                [
                    ("prefilter", 1000.0),
                    ("quality", 300.0),
                    ("metrics", 300.0),
                    ("speed", 1.0),
                ],
            )
            self.assertEqual(report["counts"]["jp_source_qualified"], 2)
            self.assertEqual(report["counts"]["jp_source_test_attempts"], 1)
            compressed = root / "previous-official.txt.gz"
            self.assertTrue(compressed.is_file())
            self.assertEqual(
                gzip.decompress(compressed.read_bytes()).decode("utf-8").splitlines(),
                ["198.51.100.1", "198.51.100.2"],
            )

    def test_previous_is_retested_once_and_current_results_accumulate_without_retest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "locations.json").write_text("{}", encoding="utf-8")
            previous = [NodeResult(ip="192.0.2.1", country_hint="US")]
            official_batches = [
                [NodeResult(ip=f"198.51.100.{index}", country_hint="US") for index in range(1, 3)],
                [NodeResult(ip=f"203.0.113.{index}", country_hint="US") for index in range(1, 3)],
            ]
            source = NodeResult(ip="198.18.0.1", country_hint="JP")
            source.add_source("fixed-source")
            config = {
                "_base_dir": str(root),
                "project": {"target_domain": "worker.example.com", "user_agent": "test"},
                "paths": {"locations": "locations.json", "checkpoints": "checkpoints", "output": "output"},
                "rolling": {
                    "previous_limit": 1,
                    "snapshot_path": "previous.json",
                    "official_snapshot_path": "previous-official.txt",
                },
                "sources": {"remote": [], "cloudflare_ranges": {"official_batch_size": 2}},
                "pipeline": {
                    "prefilter_shortlist": 2,
                    "speed_batch_size": 2,
                    "current_selection": 4,
                    "country_minimums": {"JP": 1},
                    "jp_source_requirement": {"country": "JP", "count": 1},
                    "prefilter_country_reserve": {"JP": 1},
                    "speed_country_reserve": {"JP": 1},
                    "max_official_rounds": 2,
                    "max_runtime_seconds": 10000,
                    "minimum_round_budget_seconds": 1,
                    "postprocess_reserve_seconds": 1,
                    "prefilter_tcp": {"stage": "prefilter"},
                    "quality_tcp": {"stage": "quality"},
                },
                "output": {
                    "top_nodes": 4,
                    "minimum_publish": 4,
                    "preserve_last_good": True,
                    "write_compatibility_zip": False,
                },
                "vantage": {"probe_files": []},
            }
            scan_calls: list[tuple[str, set[str]]] = []

            async def scan(records, options):
                prepared = [qualified(node) for node in records]
                scan_calls.append((str(options["stage"]), {node.ip for node in prepared}))
                return prepared

            def metrics(records: list[NodeResult], **_kwargs):
                prepared = [qualified(node) for node in records]
                return prepared, {
                    "tls_three_pass_success": len(prepared),
                    "https_ttfb_three_pass_success": len(prepared),
                }

            def speed(records: list[NodeResult], **_kwargs):
                prepared = [qualified(node) for node in records]
                if any(node.ip.startswith("198.51.100.") for node in prepared):
                    prepared = prepared[:1]
                return prepared, {
                    "speed_tested_once": len(records),
                    "speed_at_least_minimum": len(prepared),
                }

            with (
                patch("core.pipeline.measure_network_baseline", return_value={"all_targets_passed": True}),
                patch("core.pipeline.load_previous_top", return_value=(previous, [])),
                patch("core.pipeline.collect_source_candidates", return_value=([source], [])) as sources_call,
                patch(
                    "core.pipeline._source_country_tcp_speed_checks",
                    return_value=([qualified(source)], {"selected_unique_ips": 1}),
                ),
                patch(
                    "core.pipeline.collect_official_batch",
                    side_effect=[(official_batches[0], []), (official_batches[1], [])],
                ) as official_call,
                patch("core.pipeline.scan_tcp", new=AsyncMock(side_effect=scan)),
                patch("core.pipeline._three_metric_checks", side_effect=metrics),
                patch("core.pipeline._speed_checks", side_effect=speed),
                patch("core.pipeline.load_locations", return_value={}),
            ):
                report = run_pipeline(config)

            published = json.loads((root / "output" / "nodes.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "ok")
            self.assertEqual(sources_call.call_count, 1)
            self.assertEqual(official_call.call_count, 2)
            self.assertEqual(
                [stage for stage, _ in scan_calls],
                ["quality", "prefilter", "quality", "prefilter", "quality"],
            )
            self.assertEqual(sum("192.0.2.1" in ips for _, ips in scan_calls), 1)
            prefilter_inputs = [ips for stage, ips in scan_calls if stage == "prefilter"]
            self.assertEqual([len(ips) for ips in prefilter_inputs], [2, 2])
            self.assertNotIn("198.18.0.1", prefilter_inputs[1])
            self.assertEqual(len(published), 4)
            self.assertEqual(
                [batch["current_speed_qualified_total"] for batch in report["speed_batches"]],
                [1, 3],
            )
            self.assertGreaterEqual(sum(node["country"] == "JP" for node in published), 1)
            snapshot = (root / "previous-official.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                snapshot,
                ["198.51.100.1", "198.51.100.2", "203.0.113.1", "203.0.113.2"],
            )

    def test_too_few_unique_link_jp_stops_before_official_pool_and_preserves_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "locations.json").write_text("{}", encoding="utf-8")
            output = root / "output"
            output.mkdir()
            (output / "nodes.txt").write_text("192.0.2.9:443#US\n", encoding="utf-8")
            (root / "previous-official.txt").write_text(
                "203.0.113.5\n203.0.113.4\n203.0.113.3\n", encoding="utf-8"
            )
            jp = NodeResult(ip="198.18.0.10", country_hint="JP")
            config = {
                "_base_dir": str(root),
                "project": {"target_domain": "worker.example.com", "user_agent": "test"},
                "paths": {"locations": "locations.json", "checkpoints": "checkpoints", "output": "output"},
                "rolling": {
                    "previous_limit": 1,
                    "snapshot_path": "previous.json",
                    "official_snapshot_path": "previous-official.txt",
                },
                "sources": {"remote": [], "cloudflare_ranges": {"official_batch_size": 2}},
                "pipeline": {
                    "prefilter_shortlist": 2,
                    "speed_batch_size": 2,
                    "current_selection": 2,
                    "country_minimums": {"JP": 2},
                    "jp_source_requirement": {"country": "JP", "count": 2, "tcp_attempts": 3},
                    "prefilter_country_reserve": {},
                    "speed_country_reserve": {},
                    "max_official_rounds": 2,
                    "max_runtime_seconds": 10000,
                    "minimum_round_budget_seconds": 1,
                    "postprocess_reserve_seconds": 1,
                    "prefilter_tcp": {"stage": "prefilter"},
                    "quality_tcp": {"stage": "quality"},
                },
                "output": {
                    "top_nodes": 2,
                    "minimum_publish": 2,
                    "preserve_last_good": True,
                    "write_compatibility_zip": False,
                },
                "vantage": {"probe_files": []},
            }

            async def scan(records, _options):
                return [qualified(node) for node in records]

            with (
                patch("core.pipeline.measure_network_baseline", return_value={"all_targets_passed": True}),
                patch("core.pipeline.load_previous_top", return_value=([], [])),
                patch("core.pipeline.collect_source_candidates", return_value=([jp], [])),
                patch(
                    "core.pipeline._source_country_tcp_speed_checks",
                    return_value=([qualified(jp)], {"selected_unique_ips": 1}),
                ),
                patch("core.pipeline.collect_official_batch") as official_call,
                patch("core.pipeline.scan_tcp", new=AsyncMock(side_effect=scan)),
                patch("core.pipeline._three_metric_checks", return_value=([], {})),
                patch("core.pipeline._speed_checks", return_value=([], {})),
                patch("core.pipeline.load_locations", return_value={}),
            ):
                report = run_pipeline(config)

            self.assertEqual(report["status"], "degraded")
            self.assertEqual(official_call.call_count, 0)
            self.assertFalse(report["published"])
            self.assertEqual((output / "nodes.txt").read_text(encoding="utf-8"), "192.0.2.9:443#US\n")
            self.assertEqual(
                (root / "previous-official.txt").read_text(encoding="utf-8").splitlines(),
                ["203.0.113.3", "203.0.113.4", "203.0.113.5"],
            )


if __name__ == "__main__":
    unittest.main()
