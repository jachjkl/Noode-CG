from __future__ import annotations

import unittest

from core.models import NodeResult
from core.ranking import calculate_average_latency, rank_final, rank_tcp


def measured(
    ip: str,
    *,
    tcp: float,
    tls: float,
    http: float,
    loss: float = 0.0,
    speed: float | None = 10.0,
) -> NodeResult:
    node = NodeResult(ip=ip, tcp_ok=True, tls_ok=True, http_ok=True)
    node.tcp_latency_ms = tcp
    node.tls_latency_ms = tls
    node.http_latency_ms = http
    node.tcp_loss_rate = loss
    node.speed_mbps = speed
    return node


class RankingTests(unittest.TestCase):
    def test_three_stage_average_is_calculated(self) -> None:
        node = measured("1.1.1.1", tcp=100, tls=200, http=300)
        self.assertEqual(calculate_average_latency(node), 200.0)

    def test_final_order_is_loss_then_speed_then_latency(self) -> None:
        slow = measured("1.1.1.1", tcp=30, tls=30, http=30, loss=0.0, speed=10)
        fast = measured("8.8.8.8", tcp=50, tls=50, http=50, loss=0.0, speed=100)
        lossy = measured("9.9.9.9", tcp=10, tls=10, http=10, loss=0.1, speed=1000)

        result = rank_final([lossy, slow, fast], count=500)

        self.assertEqual(result, [fast, slow, lossy])

    def test_configured_local_and_link_sources_outrank_runner_only_results(self) -> None:
        local = measured("192.0.2.10", tcp=250, tls=250, http=250, loss=0.1, speed=5)
        local.add_source("local-cfdata")
        linked = measured("192.0.2.11", tcp=200, tls=200, http=200, loss=0.0, speed=10)
        linked.add_source("zip.cm.edu.kg/all.txt")
        runner_only = measured("192.0.2.12", tcp=20, tls=20, http=20, loss=0.0, speed=100)
        runner_only.add_source("cloudflare-official-ipv4-round-1")

        result = rank_final(
            [runner_only, linked, local],
            count=3,
            source_priority=["local-cfdata", "zip.cm.edu.kg/all.txt"],
        )

        self.assertEqual(result, [local, linked, runner_only])

    def test_tcp_shortlist_keeps_preferred_vantage_candidates_ahead_of_runner_only(self) -> None:
        local = NodeResult(ip="192.0.2.20")
        local.tcp_latency_ms = 250
        local.tcp_jitter_ms = 10
        local.tcp_loss_rate = 0
        local.add_source("local-cfdata")
        runner_only = NodeResult(ip="192.0.2.21")
        runner_only.tcp_latency_ms = 20
        runner_only.tcp_jitter_ms = 1
        runner_only.tcp_loss_rate = 0
        runner_only.add_source("cloudflare-official-ipv4-round-1")

        result = rank_tcp(
            [runner_only, local],
            count=1,
            source_priority=["local-cfdata"],
        )

        self.assertEqual(result, [local])

    def test_final_count_uses_unique_ips_even_when_ports_differ(self) -> None:
        first = measured("1.1.1.1", tcp=10, tls=10, http=10, speed=100)
        duplicate = measured("1.1.1.1", tcp=20, tls=20, http=20, speed=90)
        duplicate.port = 8443
        other = measured("8.8.8.8", tcp=30, tls=30, http=30, speed=80)

        result = rank_final([duplicate, other, first], count=3)

        self.assertEqual(result, [first, other])

    def test_final_selection_reserves_ten_japanese_nodes(self) -> None:
        us_nodes = [
            measured(f"192.0.2.{index}", tcp=50, tls=60, http=70, speed=100 - index)
            for index in range(1, 21)
        ]
        jp_nodes = [
            measured(f"198.51.100.{index}", tcp=100, tls=110, http=120, speed=10)
            for index in range(1, 11)
        ]
        for node in us_nodes:
            node.country = "US"
        for node in jp_nodes:
            node.country = "JP"

        result = rank_final(
            [*us_nodes, *jp_nodes],
            count=20,
            minimum_by_country={"JP": 10},
        )

        self.assertEqual(len(result), 20)
        self.assertEqual(sum(node.country == "JP" for node in result), 10)

    def test_tcp_prefilter_keeps_fastest_nodes_and_country_reserve(self) -> None:
        fast_us = [NodeResult(ip=f"192.0.2.{index}", country_hint="US") for index in range(1, 6)]
        slower_jp = [NodeResult(ip=f"198.51.100.{index}", country_hint="JP") for index in range(1, 3)]
        for index, node in enumerate(fast_us, start=1):
            node.tcp_latency_ms = float(index)
            node.tcp_jitter_ms = 1.0
            node.tcp_loss_rate = 0.0
        for index, node in enumerate(slower_jp, start=100):
            node.tcp_latency_ms = float(index)
            node.tcp_jitter_ms = 1.0
            node.tcp_loss_rate = 0.0

        result = rank_tcp(
            [*fast_us, *slower_jp],
            count=5,
            minimum_by_country={"JP": 2},
        )

        self.assertEqual(len(result), 5)
        self.assertEqual(sum(node.country_hint == "JP" for node in result), 2)


if __name__ == "__main__":
    unittest.main()
