from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.fetcher import collect_candidates, sample_ranges


class FetcherTests(unittest.TestCase):
    def test_sampling_is_deterministic_and_unique(self) -> None:
        first = sample_ranges(
            ["104.16.0.0/20", "172.64.0.0/20"],
            100,
            ports=[443],
            seed=42,
            source="test",
        )
        second = sample_ranges(
            ["104.16.0.0/20", "172.64.0.0/20"],
            100,
            ports=[443],
            seed=42,
            source="test",
        )
        self.assertEqual([node.key for node in first], [node.key for node in second])
        self.assertEqual(len({node.key for node in first}), 100)

    def test_required_remote_falls_back_to_committed_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache.txt"
            cache.write_text("1.1.1.1:443#US\n1.0.0.1:2053#JP\n", encoding="utf-8")
            missing = (root / "missing.txt").as_uri()
            config = {
                "_base_dir": str(root),
                "project": {"user_agent": "Noode-CG-test"},
                "sources": {
                    "local": [],
                    "remote": [
                        {
                            "name": "required-test",
                            "url": missing,
                            "required": True,
                            "min_records": 2,
                            "retries": 1,
                            "retry_delay_seconds": 0,
                            "cache_path": "cache.txt",
                        }
                    ],
                    "cloudflare_ranges": {"enabled": False, "target_pool": 0},
                },
            }
            records, warnings = collect_candidates(config)
            self.assertEqual(len(records), 2)
            self.assertTrue(all("required-test" in node.sources for node in records))
            self.assertTrue(any("仓库缓存" in warning for warning in warnings))

    def test_remote_shrink_does_not_overwrite_full_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache.txt"
            cached_text = "".join(f"192.0.2.{index}:443#US\n" for index in range(1, 11))
            cache.write_text(cached_text, encoding="utf-8")
            online = root / "online.txt"
            online.write_text("198.51.100.1:443#JP\n198.51.100.2:443#JP\n", encoding="utf-8")
            config = {
                "_base_dir": str(root),
                "project": {"user_agent": "Noode-CG-test"},
                "sources": {
                    "local": [],
                    "remote": [
                        {
                            "name": "shrink-test",
                            "url": online.as_uri(),
                            "required": True,
                            "min_records": 1,
                            "min_ratio_to_cache": 0.8,
                            "retries": 1,
                            "retry_delay_seconds": 0,
                            "cache_path": "cache.txt",
                        }
                    ],
                    "cloudflare_ranges": {"enabled": False, "target_pool": 0},
                },
            }
            records, warnings = collect_candidates(config)
            self.assertEqual(len(records), 10)
            self.assertEqual(cache.read_text(encoding="utf-8"), cached_text)
            self.assertTrue(any("防缩量门槛" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
