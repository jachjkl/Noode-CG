from __future__ import annotations

import unittest

from core.models import NodeResult
from core.scorer import score_nodes, select_diverse

OPTIONS = {
    "weights": {
        "latency": 0.20,
        "p95": 0.15,
        "jitter": 0.10,
        "loss": 0.15,
        "speed": 0.15,
        "completion": 0.10,
        "history": 0.10,
        "protocol": 0.05,
    },
    "caps": {"latency_ms": 500, "p95_latency_ms": 800, "jitter_ms": 150, "speed_mbps": 80},
    "region_priority": {"JP": 1.0, "US": 0.6},
    "default_region_score": 0.5,
}


def node(ip: str, *, latency: float, speed: float, country: str = "JP") -> NodeResult:
    value = NodeResult(ip=ip, country=country)
    value.tcp_ok = value.tls_ok = value.http_ok = True
    value.tcp_latency_ms = latency
    value.http_latency_ms = latency
    value.tcp_p95_latency_ms = latency
    value.tcp_jitter_ms = 1
    value.tcp_loss_rate = 0
    value.speed_mbps = speed
    value.speed_completion_rate = 1
    value.speed_ok = True
    value.history_score = 0.5
    return value


class ScorerTests(unittest.TestCase):
    def test_fast_node_ranks_first(self) -> None:
        ranked = score_nodes(
            [node("104.16.1.1", latency=200, speed=5), node("104.17.1.1", latency=20, speed=80)],
            OPTIONS,
        )
        self.assertEqual(ranked[0].ip, "104.17.1.1")
        self.assertGreater(ranked[0].score, ranked[1].score)

    def test_stable_node_beats_fast_but_lossy_node(self) -> None:
        flaky = node("104.16.1.1", latency=15, speed=80)
        flaky.tcp_p95_latency_ms = 700
        flaky.tcp_jitter_ms = 120
        flaky.tcp_loss_rate = 0.4
        flaky.speed_completion_rate = 0.6
        flaky.history_score = 0.2
        stable = node("104.17.1.1", latency=60, speed=30)
        stable.tcp_p95_latency_ms = 85
        stable.tcp_jitter_ms = 8
        stable.tcp_loss_rate = 0
        stable.history_score = 0.9

        ranked = score_nodes([flaky, stable], OPTIONS)

        self.assertEqual(ranked[0].ip, stable.ip)

    def test_country_and_colo_caps_are_not_relaxed_to_fill(self) -> None:
        ranked = score_nodes(
            [node(f"104.16.1.{index}", latency=index, speed=50) for index in range(1, 8)],
            OPTIONS,
        )
        selected = select_diverse(
            ranked,
            {
                "top_nodes": 5,
                "max_per_country": 2,
                "max_per_ipv4_24": 1,
                "max_per_ipv6_48": 1,
            },
        )
        self.assertEqual(len(selected), 2)

    def test_colo_and_region_caps_prevent_concentration(self) -> None:
        values = [node(f"104.16.{index // 250}.{index % 250 + 1}", latency=index, speed=50) for index in range(12)]
        for index, value in enumerate(values):
            value.colo = "LAX" if index < 8 else "NRT"
            value.region = "North America" if index < 8 else "Asia Pacific"
        ranked = score_nodes(values, OPTIONS)
        selected = select_diverse(
            ranked,
            {
                "top_nodes": 4,
                "max_per_country": 6,
                "max_per_region": 3,
                "max_per_colo": 2,
                "max_per_ipv4_24": 6,
                "max_per_ipv6_48": 6,
            },
        )
        self.assertLessEqual(max(sum(item.colo == colo for item in selected) for colo in {"LAX", "NRT"}), 2)

    def test_asia_reservations_and_official_cap_survive_us_ranking(self) -> None:
        official = [node(f"104.16.10.{index}", latency=index, speed=80, country="US") for index in range(1, 11)]
        for value in official:
            value.colo = "LAX"
            value.colo_country = "US"
            value.region = "North America"
            value.sources = ["cloudflare-official-ipv4"]
        asia = [node(f"198.51.100.{index}", latency=100 + index, speed=20) for index in range(1, 11)]
        for index, value in enumerate(asia):
            value.colo = "NRT" if index < 5 else "ICN"
            value.colo_country = "JP" if index < 5 else "KR"
            value.region = "Asia Pacific"
            value.sources = ["required-source"]

        selected = select_diverse(
            score_nodes(official + asia, OPTIONS),
            {
                "top_nodes": 12,
                "minimum_per_colo": {"NRT": 3, "ICN": 3},
                "minimum_per_country": {"JP": 3, "KR": 3},
                "max_official_generated": 2,
                "max_per_country": 12,
                "max_per_region": 12,
                "max_per_colo": 12,
                "max_per_ipv4_24": 12,
                "max_per_ipv6_48": 12,
            },
        )

        self.assertGreaterEqual(sum(value.colo == "NRT" for value in selected), 3)
        self.assertGreaterEqual(sum(value.colo == "ICN" for value in selected), 3)
        self.assertLessEqual(sum(value.official_only for value in selected), 2)


if __name__ == "__main__":
    unittest.main()
