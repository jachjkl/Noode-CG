from __future__ import annotations

import unittest

from core.models import NodeResult
from core.scorer import score_nodes, select_diverse

OPTIONS = {
    "weights": {
        "latency": 0.35,
        "jitter": 0.10,
        "loss": 0.10,
        "speed": 0.25,
        "region": 0.10,
        "protocol": 0.10,
    },
    "caps": {"latency_ms": 500, "jitter_ms": 150, "speed_mbps": 100},
    "region_priority": {"JP": 1.0, "US": 0.6},
    "default_region_score": 0.5,
}


def node(ip: str, *, latency: float, speed: float, country: str = "JP") -> NodeResult:
    value = NodeResult(ip=ip, country=country)
    value.tcp_ok = value.tls_ok = value.http_ok = True
    value.tcp_latency_ms = latency
    value.http_latency_ms = latency
    value.tcp_jitter_ms = 1
    value.tcp_loss_rate = 0
    value.speed_mbps = speed
    return value


class ScorerTests(unittest.TestCase):
    def test_fast_node_ranks_first(self) -> None:
        ranked = score_nodes(
            [node("104.16.1.1", latency=200, speed=5), node("104.17.1.1", latency=20, speed=80)],
            OPTIONS,
        )
        self.assertEqual(ranked[0].ip, "104.17.1.1")
        self.assertGreater(ranked[0].score, ranked[1].score)

    def test_diversity_cap_then_fill(self) -> None:
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
        self.assertEqual(len(selected), 5)


if __name__ == "__main__":
    unittest.main()
