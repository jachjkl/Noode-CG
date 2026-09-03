from __future__ import annotations

import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.handoff import (
    _rank_with_colo_diversity,
    prepare_cloud_handoff,
    run_local_selection,
)
from core.models import NodeResult


def qualified(node: NodeResult, *, country: str = "US") -> NodeResult:
    node.tcp_ok = node.tls_ok = node.http_ok = True
    node.tcp_latency_ms = 50.0
    node.tls_latency_ms = 60.0
    node.http_latency_ms = 70.0
    node.tcp_jitter_ms = node.tls_jitter_ms = node.http_jitter_ms = 2.0
    node.average_latency_ms = 60.0
    node.overall_jitter_ms = 2.0
    node.tcp_loss_rate = 0.0
    node.speed_mbps = 20.0
    node.country = country
    node.country_hint = country
    node.colo_country = country
    return node


class HandoffPipelineTests(unittest.TestCase):
    def test_cloud_passes_all_links_and_official_candidates_without_tcp_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jp = NodeResult(ip="198.18.0.10", country_hint="JP")
            jp.add_source("link")
            us = NodeResult(ip="198.51.100.10", country_hint="US")
            us.add_source("link")
            official = NodeResult(ip="203.0.113.10")
            config = {
                "_base_dir": str(root),
                "paths": {"output": "output"},
                "rolling": {"snapshot_path": "previous.json", "previous_limit": 100},
                "sources": {"cloudflare_ranges": {"official_batch_size": 1}},
                "pipeline": {
                    "source_priority": ["link"],
                    "prefilter_tcp": {},
                    "jp_source_requirement": {"country": "JP", "count": 1},
                },
                "handoff": {
                    "pool_path": "pool.json.gz",
                    "health_path": "health.json",
                    "target": 1,
                    "max_official_rounds": 1,
                },
            }
            scanned: set[str] = set()

            async def scan(records, _options):
                scanned.update(node.ip for node in records)
                return [qualified(node) for node in records]

            with (
                patch("core.handoff.load_previous_top", return_value=([], [])),
                patch("core.handoff.collect_source_candidates", return_value=([jp, us], [])),
                patch("core.handoff.collect_official_batch", return_value=([official], [])),
                patch("core.handoff.scan_tcp", new=AsyncMock(side_effect=scan)),
            ):
                report = prepare_cloud_handoff(config)

            nodes = json.loads(gzip.decompress((root / "pool.json.gz").read_bytes()))["nodes"]
            self.assertTrue(report["cloud_prefilter_skipped"])
            self.assertEqual(scanned, set())
            self.assertEqual({jp.ip, us.ip, official.ip}, {node["ip"] for node in nodes})

    def test_continuation_excludes_attempted_and_prior_official_ips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempted = root / "attempted.txt.gz"
            attempted.write_bytes(gzip.compress(b"198.51.100.1\n", mtime=0))
            prior = root / "official.txt.gz"
            prior.write_bytes(gzip.compress(b"198.51.100.2\n", mtime=0))
            fresh = NodeResult(ip="198.51.100.3")
            config = {
                "_base_dir": str(root),
                "paths": {"output": "output"},
                "rolling": {
                    "snapshot_path": "previous.json",
                    "previous_limit": 100,
                    "official_snapshot_path": "official.txt.gz",
                },
                "sources": {"cloudflare_ranges": {"official_batch_size": 1}},
                "pipeline": {
                    "source_priority": [],
                    "prefilter_tcp": {},
                    "jp_source_requirement": {"country": "JP", "count": 1},
                },
                "handoff": {
                    "pool_path": "pool.json.gz",
                    "health_path": "health.json",
                    "attempted_path": "attempted.txt.gz",
                    "target": 1,
                    "max_official_rounds": 1,
                },
            }
            captured_exclusions: list[set[str]] = []

            def official_batch(_config, *, exclude_ips, round_index):
                captured_exclusions.append(set(exclude_ips))
                return [fresh], []

            async def scan(records, _options):
                return [qualified(node) for node in records]

            with (
                patch.dict(os.environ, {"NOODE_CONTINUATION": "true"}),
                patch("core.handoff.load_previous_top", return_value=([], [])),
                patch("core.handoff.collect_source_candidates", return_value=([], [])) as sources_call,
                patch("core.handoff.collect_official_batch", side_effect=official_batch),
                patch("core.handoff.scan_tcp", new=AsyncMock(side_effect=scan)),
            ):
                prepare_cloud_handoff(config)

            sources_call.assert_not_called()
            self.assertEqual(captured_exclusions, [{"198.51.100.1", "198.51.100.2"}])
            snapshot = set(gzip.decompress(prior.read_bytes()).decode().splitlines())
            self.assertEqual(snapshot, {"198.51.100.2", fresh.ip})

    def test_new_cycle_ignores_stale_attempted_and_prior_official_ips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempted = root / "attempted.txt.gz"
            attempted.write_bytes(gzip.compress(b"198.51.100.1\n", mtime=0))
            prior = root / "official.txt.gz"
            prior.write_bytes(gzip.compress(b"198.51.100.2\n", mtime=0))
            fresh = NodeResult(ip="198.51.100.3")
            config = {
                "_base_dir": str(root),
                "paths": {"output": "output"},
                "rolling": {
                    "snapshot_path": "previous.json",
                    "previous_limit": 100,
                    "official_snapshot_path": "official.txt.gz",
                },
                "sources": {"cloudflare_ranges": {"official_batch_size": 1}},
                "pipeline": {
                    "source_priority": [],
                    "prefilter_tcp": {},
                    "jp_source_requirement": {"country": "JP", "count": 1},
                },
                "handoff": {
                    "pool_path": "pool.json.gz",
                    "health_path": "health.json",
                    "attempted_path": "attempted.txt.gz",
                    "target": 1,
                    "max_official_rounds": 1,
                },
            }
            captured_exclusions: list[set[str]] = []

            def official_batch(_config, *, exclude_ips, round_index):
                captured_exclusions.append(set(exclude_ips))
                return [fresh], []

            async def scan(records, _options):
                return [qualified(node) for node in records]

            with (
                patch.dict(os.environ, {"NOODE_CONTINUATION": "false"}),
                patch("core.handoff.load_previous_top", return_value=([], [])),
                patch("core.handoff.collect_source_candidates", return_value=([], [])),
                patch("core.handoff.collect_official_batch", side_effect=official_batch),
                patch("core.handoff.scan_tcp", new=AsyncMock(side_effect=scan)),
            ):
                report = prepare_cloud_handoff(config)

            self.assertEqual(captured_exclusions, [set()])
            self.assertFalse(report["continuation"])
            self.assertEqual(report["attempted_excluded"], 0)
            self.assertEqual(report["prior_official_excluded"], 0)
            snapshot = set(gzip.decompress(prior.read_bytes()).decode().splitlines())
            self.assertEqual(snapshot, {fresh.ip})

    def test_new_cycle_clears_stale_accumulator_before_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handoff_dir = root / "data" / "handoff"
            handoff_dir.mkdir(parents=True)
            stale = qualified(NodeResult(ip="198.51.100.70"))
            accumulator = handoff_dir / "local-qualified.json.gz"
            accumulator.write_bytes(gzip.compress(json.dumps({
                "schema": 1,
                "nodes": [stale.to_dict()],
            }).encode(), mtime=0))
            attempted = handoff_dir / "local-attempted-ips.txt.gz"
            attempted.write_bytes(gzip.compress(f"{stale.ip}\n".encode(), mtime=0))
            fresh = NodeResult(ip="198.51.100.71")
            config = {
                "_base_dir": str(root),
                "paths": {"output": "output"},
                "rolling": {
                    "snapshot_path": "previous.json",
                    "previous_limit": 100,
                    "official_snapshot_path": "previous-official.txt.gz",
                },
                "sources": {"cloudflare_ranges": {"official_batch_size": 1}},
                "pipeline": {"source_priority": []},
                "handoff": {
                    "pool_path": "data/handoff/cloud-raw10000.json.gz",
                    "health_path": "data/handoff/cloud-health.json",
                    "accumulator_path": "data/handoff/local-qualified.json.gz",
                    "attempted_path": "data/handoff/local-attempted-ips.txt.gz",
                    "target": 1,
                },
            }

            with (
                patch.dict(os.environ, {"NOODE_CONTINUATION": "false"}),
                patch("core.handoff.load_previous_top", return_value=([], [])),
                patch("core.handoff.collect_source_candidates", return_value=([], [])),
                patch("core.handoff.collect_official_batch", return_value=([fresh], [])),
            ):
                prepare_cloud_handoff(config)

            payload = json.loads(gzip.decompress(
                (handoff_dir / "cloud-raw10000.json.gz").read_bytes()
            ))
            self.assertEqual(payload["state"]["accumulated"], [])
            self.assertFalse(accumulator.exists())
            self.assertFalse(attempted.exists())

    def test_cloud_passes_link_ipv6_for_local_testing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            linked_ipv6 = NodeResult(
                ip="2606:4700:3023:fa4e:70b5:5319:8d88:be3",
                country_hint="US",
            )
            linked_ipv6.add_source("link")
            official = NodeResult(ip="203.0.113.20")
            config = {
                "_base_dir": str(root),
                "paths": {"output": "output"},
                "rolling": {"snapshot_path": "previous.json", "previous_limit": 100},
                "sources": {"cloudflare_ranges": {"official_batch_size": 1}},
                "pipeline": {
                    "source_priority": ["link"],
                    "prefilter_tcp": {},
                    "jp_source_requirement": {"country": "JP", "count": 0},
                },
                "handoff": {
                    "pool_path": "pool.json.gz",
                    "health_path": "health.json",
                    "target": 1,
                    "max_official_rounds": 1,
                },
            }
            scanned: set[str] = set()

            async def scan(records, _options):
                scanned.update(node.ip for node in records)
                return [qualified(node) for node in records]

            with (
                patch("core.handoff.load_previous_top", return_value=([], [])),
                patch(
                    "core.handoff.collect_source_candidates",
                    return_value=([linked_ipv6], []),
                ),
                patch("core.handoff.collect_official_batch", return_value=([official], [])),
                patch("core.handoff.scan_tcp", new=AsyncMock(side_effect=scan)),
            ):
                prepare_cloud_handoff(config)

            nodes = json.loads(gzip.decompress((root / "pool.json.gz").read_bytes()))["nodes"]
            self.assertEqual(scanned, set())
            self.assertIn(linked_ipv6.ip, {node["ip"] for node in nodes})

    def test_cloud_prepare_requests_exactly_one_official_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {
                "_base_dir": str(root),
                "paths": {"output": "output"},
                "rolling": {"snapshot_path": "previous.json", "previous_limit": 100},
                "sources": {"cloudflare_ranges": {"official_batch_size": 10000}},
                "pipeline": {
                    "source_priority": [],
                    "prefilter_tcp": {},
                    "jp_source_requirement": {"country": "JP", "count": 0},
                },
                "handoff": {
                    "pool_path": "pool.json.gz",
                    "health_path": "health.json",
                    "target": 10000,
                    "max_official_rounds": 1,
                },
            }

            with (
                patch("core.handoff.load_previous_top", return_value=([], [])),
                patch("core.handoff.collect_source_candidates", return_value=([], [])),
                patch("core.handoff.collect_official_batch", return_value=([], [])) as collect,
                patch("core.handoff.scan_tcp", new=AsyncMock()) as scan,
            ):
                report = prepare_cloud_handoff(config)

            self.assertEqual(collect.call_count, 1)
            scan.assert_not_awaited()
            self.assertEqual(len(report["rounds"]), 1)
            self.assertEqual(report["rounds"][0]["official_unique"], 0)

    def test_colo_cap_is_soft_and_spreads_the_first_choices(self) -> None:
        records = []
        for index, colo in enumerate(("LAX", "LAX", "NRT", "FRA"), start=1):
            node = qualified(NodeResult(ip=f"198.51.100.{index}"))
            node.colo = colo
            node.tcp_loss_rate = 0.0
            node.speed_mbps = 100.0 - index
            records.append(node)
        selected = _rank_with_colo_diversity(records, count=4, max_per_colo=1)
        self.assertEqual({node.colo for node in selected[:3]}, {"LAX", "NRT", "FRA"})
        self.assertEqual(len(selected), 4)

    def test_cloud_prepare_writes_new_pool_and_excludes_previous_top(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            previous = NodeResult(ip="192.0.2.1", country_hint="US")
            linked = NodeResult(ip="192.0.2.2", country_hint="US")
            linked.add_source("link")
            official = [
                NodeResult(ip="192.0.2.3", country_hint="US"),
                NodeResult(ip="192.0.2.4", country_hint="US"),
            ]
            config = {
                "_base_dir": str(root),
                "paths": {"output": "output"},
                "rolling": {"snapshot_path": "previous.json", "previous_limit": 1},
                "sources": {"cloudflare_ranges": {"official_batch_size": 2}},
                "pipeline": {
                    "source_priority": ["link"],
                    "prefilter_tcp": {"stage": "cloud-prefilter"},
                },
                "handoff": {
                    "pool_path": "data/handoff/cloud-raw10000.json.gz",
                    "health_path": "data/handoff/cloud-health.json",
                    "target": 2,
                    "max_official_rounds": 1,
                },
            }

            async def scan(records, _options):
                for index, node in enumerate(records):
                    node.tcp_ok = True
                    node.tcp_latency_ms = 100.0 + index
                    node.tcp_jitter_ms = 1.0
                    node.tcp_loss_rate = 0.0
                return records

            with (
                patch("core.handoff.load_previous_top", return_value=([previous], [])),
                patch("core.handoff.collect_source_candidates", return_value=([previous, linked], [])),
                patch("core.handoff.collect_official_batch", return_value=(official, [])),
                patch("core.handoff.scan_tcp", new=AsyncMock(side_effect=scan)),
            ):
                report = prepare_cloud_handoff(config)

            path = root / "data" / "handoff" / "cloud-raw10000.json.gz"
            payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["selected"], 4)
            self.assertIn(previous.ip, {item["ip"] for item in payload["nodes"]})
            self.assertEqual(len({item["ip"] for item in payload["nodes"]}), 4)

    def test_local_selection_retests_handoff_plus_previous_and_publishes_total_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "locations.json").write_text("{}", encoding="utf-8")
            handoff_nodes = [
                NodeResult(ip="198.51.100.1", country_hint="US"),
                NodeResult(ip="198.51.100.2", country_hint="US"),
                NodeResult(ip="198.18.0.10", country_hint="JP"),
            ]
            pool_path = root / "data" / "handoff" / "cloud-raw10000.json.gz"
            pool_path.parent.mkdir(parents=True)
            pool_path.write_bytes(
                gzip.compress(
                    json.dumps({"schema": 1, "nodes": [node.to_dict() for node in handoff_nodes]}).encode(),
                    mtime=0,
                )
            )
            previous = NodeResult(ip="203.0.113.1", country_hint="US")
            config = {
                "_base_dir": str(root),
                "project": {"target_domain": "worker.example.com", "user_agent": "test"},
                "paths": {"locations": "locations.json", "output": "output"},
                "rolling": {"snapshot_path": "previous.json", "previous_limit": 1},
                "pipeline": {
                    "quality_tcp": {"stage": "local-quality"},
                    "speed": {"minimum_mbps": 3},
                    "location_filter": {
                        "excluded_countries": ["CN"],
                        "require_known_endpoint_country": True,
                        "require_known_colo_country": True,
                    },
                    "jp_source_requirement": {"country": "JP", "count": 1, "tcp_attempts": 3},
                    "country_minimums": {"JP": 1},
                    "speed_batch_size": 2,
                },
                "handoff": {"pool_path": "data/handoff/cloud-raw10000.json.gz"},
                "output": {
                    "top_nodes": 3,
                    "minimum_publish": 3,
                    "preserve_last_good": True,
                    "write_compatibility_zip": False,
                },
                "vantage": {"probe_files": []},
            }
            scanned_ips: set[str] = set()

            async def scan(records, _options):
                scanned_ips.update(node.ip for node in records)
                return [qualified(node) for node in records]

            def metrics(records: list[NodeResult], **_kwargs):
                prepared = [qualified(node) for node in records]
                return prepared, {"foreign_combined_latency_qualified": len(prepared)}

            def speed(records: list[NodeResult], **_kwargs):
                prepared = [qualified(node) for node in records]
                return prepared, {"speed_at_least_minimum": len(prepared)}

            jp = qualified(handoff_nodes[2], country="JP")
            with (
                patch("core.handoff.load_previous_top", return_value=([previous], [])),
                patch("core.handoff.scan_tcp", new=AsyncMock(side_effect=scan)),
                patch("core.handoff._three_metric_checks", side_effect=metrics),
                patch("core.handoff._speed_checks", side_effect=speed),
                patch(
                    "core.handoff._source_country_tcp_speed_checks",
                    return_value=([jp], {"selected_unique_ips": 1}),
                ),
                patch("core.handoff.load_locations", return_value={}),
            ):
                report = run_local_selection(config)

            published = json.loads((root / "output" / "nodes.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "ok")
            self.assertEqual(len(published), 4)
            self.assertEqual(sum(item["country"] == "JP" for item in published), 1)
            self.assertIn(previous.ip, scanned_ips)
            self.assertEqual(report["counts"]["combined_unique"], 4)

    def test_insufficient_local_results_are_accumulated_for_next_cloud_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "locations.json").write_text("{}", encoding="utf-8")
            us = NodeResult(ip="198.51.100.20", country_hint="US")
            jp = NodeResult(ip="198.18.0.20", country_hint="JP")
            pool_path = root / "data" / "handoff" / "cloud-raw10000.json.gz"
            pool_path.parent.mkdir(parents=True)
            pool_path.write_bytes(gzip.compress(json.dumps({
                "schema": 1,
                "nodes": [us.to_dict(), jp.to_dict()],
            }).encode(), mtime=0))
            config = self._local_config(root, target=3, jp_count=1)

            async def scan(records, _options):
                return [qualified(node) for node in records]

            with (
                patch("core.handoff.load_previous_top", return_value=([], [])),
                patch("core.handoff.scan_tcp", new=AsyncMock(side_effect=scan)),
                patch(
                    "core.handoff._three_metric_checks",
                    side_effect=lambda records, **_kwargs: (
                        [qualified(node) for node in records],
                        {"foreign_combined_latency_qualified": len(records)},
                    ),
                ),
                patch(
                    "core.handoff._speed_checks",
                    side_effect=lambda records, **_kwargs: (
                        [qualified(node) for node in records],
                        {"speed_at_least_minimum": len(records)},
                    ),
                ),
                patch(
                    "core.handoff._source_country_tcp_speed_checks",
                    return_value=([qualified(jp, country="JP")], {"selected_unique_ips": 1}),
                ),
                patch("core.handoff.load_locations", return_value={}),
            ):
                report = run_local_selection(config)

            self.assertFalse(report["published"])
            self.assertTrue(report["needs_more"])
            self.assertTrue((root / "data" / "handoff" / "local-qualified.json.gz").is_file())
            attempted = gzip.decompress(
                (root / "data" / "handoff" / "local-attempted-ips.txt.gz").read_bytes()
            ).decode().splitlines()
            self.assertEqual(set(attempted), {us.ip, jp.ip})

    def test_new_local_cycle_ignores_stale_restored_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "locations.json").write_text("{}", encoding="utf-8")
            stale = qualified(NodeResult(ip="198.51.100.40"))
            fresh = NodeResult(ip="198.51.100.41", country_hint="US")
            handoff_dir = root / "data" / "handoff"
            handoff_dir.mkdir(parents=True)
            (handoff_dir / "cloud-raw10000.json.gz").write_bytes(gzip.compress(json.dumps({
                "schema": 1,
                "report": {"continuation": False},
                "nodes": [fresh.to_dict()],
                "state": {
                    "accumulated": [stale.to_dict()],
                    "attempted_ips": [stale.ip],
                },
            }).encode(), mtime=0))
            (handoff_dir / "local-qualified.json.gz").write_bytes(gzip.compress(json.dumps({
                "schema": 1,
                "nodes": [stale.to_dict()],
            }).encode(), mtime=0))
            (handoff_dir / "local-attempted-ips.txt.gz").write_bytes(
                gzip.compress(f"{stale.ip}\n".encode(), mtime=0)
            )
            config = self._local_config(root, target=2, jp_count=0)
            scanned: set[str] = set()

            async def scan(records, _options):
                scanned.update(node.ip for node in records)
                return [qualified(node) for node in records]

            with (
                patch("core.handoff.load_previous_top", return_value=([], [])),
                patch("core.handoff.scan_tcp", new=AsyncMock(side_effect=scan)),
                patch(
                    "core.handoff._three_metric_checks",
                    side_effect=lambda records, **_kwargs: (
                        [qualified(node) for node in records],
                        {"foreign_combined_latency_qualified": len(records)},
                    ),
                ),
                patch(
                    "core.handoff._speed_checks",
                    side_effect=lambda records, **_kwargs: (
                        [qualified(node) for node in records],
                        {"speed_at_least_minimum": len(records)},
                    ),
                ),
                patch("core.handoff._source_country_tcp_speed_checks", return_value=([], {})),
                patch("core.handoff.load_locations", return_value={}),
            ):
                report = run_local_selection(config)

            self.assertEqual(scanned, {fresh.ip})
            self.assertEqual(report["counts"]["accumulated_loaded"], 0)
            attempted = gzip.decompress(
                (handoff_dir / "local-attempted-ips.txt.gz").read_bytes()
            ).decode().splitlines()
            self.assertEqual(attempted, [fresh.ip])

    def test_next_local_pass_uses_accumulator_without_retesting_old_successes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "locations.json").write_text("{}", encoding="utf-8")
            old_us = qualified(NodeResult(ip="198.51.100.30"))
            old_jp = qualified(NodeResult(ip="198.18.0.30"), country="JP")
            new_us = NodeResult(ip="198.51.100.31", country_hint="US")
            handoff_dir = root / "data" / "handoff"
            handoff_dir.mkdir(parents=True)
            (handoff_dir / "cloud-raw10000.json.gz").write_bytes(gzip.compress(json.dumps({
                "schema": 1,
                "report": {"continuation": True},
                "nodes": [new_us.to_dict()],
            }).encode(), mtime=0))
            (handoff_dir / "local-qualified.json.gz").write_bytes(gzip.compress(json.dumps({
                "schema": 1,
                "nodes": [old_us.to_dict(), old_jp.to_dict()],
            }).encode(), mtime=0))
            (handoff_dir / "local-attempted-ips.txt.gz").write_bytes(
                gzip.compress(f"{old_us.ip}\n{old_jp.ip}\n".encode(), mtime=0)
            )
            config = self._local_config(root, target=3, jp_count=1)
            config["_local_options"] = {"continuous_three_rounds": False}
            scanned: set[str] = set()

            async def scan(records, _options):
                scanned.update(node.ip for node in records)
                return [qualified(node) for node in records]

            with (
                patch("core.handoff.load_previous_top", return_value=([], [])),
                patch("core.handoff.scan_tcp", new=AsyncMock(side_effect=scan)),
                patch(
                    "core.handoff._three_metric_checks",
                    side_effect=lambda records, **_kwargs: (
                        [qualified(node) for node in records],
                        {"foreign_combined_latency_qualified": len(records)},
                    ),
                ),
                patch(
                    "core.handoff._speed_checks",
                    side_effect=lambda records, **_kwargs: (
                        [qualified(node) for node in records],
                        {"speed_at_least_minimum": len(records)},
                    ),
                ),
                patch("core.handoff._source_country_tcp_speed_checks", return_value=([], {})),
                patch("core.handoff.load_locations", return_value={}),
            ):
                report = run_local_selection(config)

            self.assertTrue(report["published"])
            self.assertFalse(report["needs_more"])
            self.assertEqual(scanned, {new_us.ip})
            self.assertFalse((handoff_dir / "local-qualified.json.gz").exists())
            self.assertFalse((handoff_dir / "local-attempted-ips.txt.gz").exists())

    def test_continuous_mode_publishes_accumulated_results_after_third_round(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "locations.json").write_text("{}", encoding="utf-8")
            handoff_dir = root / "data" / "handoff"
            handoff_dir.mkdir(parents=True)
            config = self._local_config(root, target=5, jp_count=1)
            japan = NodeResult(ip="198.18.0.80", country_hint="JP")

            async def scan(records, _options):
                return [qualified(node) for node in records]

            def jp_lane(records, **_kwargs):
                selected = [
                    qualified(node, country="JP")
                    for node in records
                    if (node.country_hint or node.country).upper() == "JP"
                ]
                return selected, {"selected_unique_ips": len(selected)}

            reports = []
            with (
                patch("core.handoff.load_previous_top", return_value=([], [])),
                patch("core.handoff.scan_tcp", new=AsyncMock(side_effect=scan)),
                patch(
                    "core.handoff._three_metric_checks",
                    side_effect=lambda records, **_kwargs: (
                        [qualified(node) for node in records],
                        {"foreign_combined_latency_qualified": len(records)},
                    ),
                ),
                patch(
                    "core.handoff._speed_checks",
                    side_effect=lambda records, **_kwargs: (
                        [qualified(node) for node in records],
                        {"speed_at_least_minimum": len(records)},
                    ),
                ),
                patch("core.handoff._source_country_tcp_speed_checks", side_effect=jp_lane),
                patch("core.handoff.load_locations", return_value={}),
            ):
                for round_index in range(3):
                    us = NodeResult(
                        ip=f"198.51.100.{81 + round_index}",
                        country_hint="US",
                    )
                    nodes = [us, japan] if round_index == 0 else [us]
                    (handoff_dir / "cloud-raw10000.json.gz").write_bytes(
                        gzip.compress(json.dumps({
                            "schema": 1,
                            "report": {"continuation": round_index > 0},
                            "nodes": [node.to_dict() for node in nodes],
                        }).encode(), mtime=0)
                    )
                    reports.append(run_local_selection(config))

            self.assertFalse(reports[0]["published"])
            self.assertTrue(reports[0]["needs_more"])
            self.assertFalse(reports[1]["published"])
            self.assertTrue(reports[1]["needs_more"])
            self.assertTrue(reports[2]["published"])
            self.assertFalse(reports[2]["needs_more"])
            self.assertEqual(reports[2]["cycle_round"], 3)
            self.assertEqual(reports[2]["counts"]["ordinary_replacements"], 3)
            self.assertEqual(reports[2]["counts"]["final_selected"], 4)

    def test_local_pass_restores_embedded_cloud_state_without_git_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "locations.json").write_text("{}", encoding="utf-8")
            old_us = qualified(NodeResult(ip="198.51.100.40"))
            old_jp = qualified(NodeResult(ip="198.18.0.40"), country="JP")
            previous = NodeResult(ip="203.0.113.40", country_hint="US")
            new_us = NodeResult(ip="198.51.100.41", country_hint="US")
            handoff_dir = root / "data" / "handoff"
            handoff_dir.mkdir(parents=True)
            (handoff_dir / "cloud-raw10000.json.gz").write_bytes(gzip.compress(json.dumps({
                "schema": 1,
                "report": {"continuation": True},
                "nodes": [new_us.to_dict()],
                "state": {
                    "previous_top100": [previous.to_dict()],
                    "accumulated": [old_us.to_dict(), old_jp.to_dict()],
                    "attempted_ips": [old_us.ip, old_jp.ip],
                },
            }).encode(), mtime=0))
            config = self._local_config(root, target=3, jp_count=1)
            scanned: set[str] = set()

            async def scan(records, _options):
                scanned.update(node.ip for node in records)
                return [qualified(node) for node in records]

            with (
                patch("core.handoff.load_previous_top", return_value=([], [])),
                patch("core.handoff.scan_tcp", new=AsyncMock(side_effect=scan)),
                patch(
                    "core.handoff._three_metric_checks",
                    side_effect=lambda records, **_kwargs: (
                        [qualified(node) for node in records],
                        {"foreign_combined_latency_qualified": len(records)},
                    ),
                ),
                patch(
                    "core.handoff._speed_checks",
                    side_effect=lambda records, **_kwargs: (
                        [qualified(node) for node in records],
                        {"speed_at_least_minimum": len(records)},
                    ),
                ),
                patch("core.handoff._source_country_tcp_speed_checks", return_value=([], {})),
                patch("core.handoff.load_locations", return_value={}),
            ):
                report = run_local_selection(config)

            self.assertTrue(report["published"])
            self.assertEqual(report["counts"]["previous_loaded"], 1)
            self.assertEqual(report["counts"]["accumulated_loaded"], 2)
            self.assertEqual(scanned, {new_us.ip, previous.ip})

    @staticmethod
    def _local_config(root: Path, *, target: int, jp_count: int) -> dict:
        return {
            "_base_dir": str(root),
            "project": {"target_domain": "worker.example.com", "user_agent": "test"},
            "paths": {"locations": "locations.json", "output": "output"},
            "rolling": {"snapshot_path": "previous.json", "previous_limit": 100},
            "pipeline": {
                "prefilter_tcp": {},
                "quality_tcp": {"stage": "local-quality"},
                "speed": {"minimum_mbps": 3},
                "location_filter": {
                    "excluded_countries": ["CN"],
                    "require_known_endpoint_country": True,
                    "require_known_colo_country": True,
                },
                "jp_source_requirement": {"country": "JP", "count": jp_count, "tcp_attempts": 3},
                "country_minimums": {"JP": jp_count},
                "speed_batch_size": 2,
            },
            "handoff": {
                "pool_path": "data/handoff/cloud-raw10000.json.gz",
                "accumulator_path": "data/handoff/local-qualified.json.gz",
                "attempted_path": "data/handoff/local-attempted-ips.txt.gz",
            },
            "output": {
                "top_nodes": target,
                "minimum_publish": target,
                "preserve_last_good": True,
                "write_compatibility_zip": False,
            },
            "_local_options": {"continuous_three_rounds": True},
            "vantage": {"probe_files": []},
        }


if __name__ == "__main__":
    unittest.main()
