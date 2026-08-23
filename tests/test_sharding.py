from __future__ import annotations

import unittest

from core.models import NodeResult
from core.sharding import select_scan_batch


def node(index: int, *, required: bool = False) -> NodeResult:
    value = NodeResult(ip=f"10.{index // 65536}.{(index // 256) % 256}.{index % 256}")
    value.add_source("required-source" if required else "official")
    return value


class ShardingTests(unittest.TestCase):
    def test_required_and_history_nodes_are_in_every_batch(self) -> None:
        records = [node(index, required=index < 10) for index in range(100)]
        history_keys = {records[10].key, records[11].key}
        for shard in range(3):
            selected, metadata = select_scan_batch(
                records,
                {
                    "limit": 40,
                    "shards": 3,
                    "required_sources": ["required-source"],
                },
                history_keys=history_keys,
                shard_index=shard,
            )
            keys = {value.key for value in selected}
            self.assertTrue({value.key for value in records[:12]}.issubset(keys))
            self.assertEqual(len(selected), 40)
            self.assertEqual(metadata["shard_index"], shard)

    def test_rotating_shards_cover_the_full_pool(self) -> None:
        records = [node(index, required=index < 10) for index in range(100)]
        covered: set[str] = set()
        for shard in range(3):
            selected, _ = select_scan_batch(
                records,
                {
                    "limit": 40,
                    "shards": 3,
                    "required_sources": ["required-source"],
                },
                history_keys=set(),
                shard_index=shard,
            )
            covered.update(value.key for value in selected)
        self.assertEqual(covered, {value.key for value in records})


if __name__ == "__main__":
    unittest.main()
