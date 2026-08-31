from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.fetcher import collect_official_batch, collect_source_candidates, sample_ranges


class FetcherTests(unittest.TestCase):
    def test_each_online_source_is_downloaded_once_when_successful(self) -> None:
        config = {
            "_base_dir": str(Path.cwd()),
            "project": {"user_agent": "Noode-CG-test"},
            "sources": {
                "local": [],
                "remote": [
                    {"name": "first", "url": "https://example.com/first.txt", "min_records": 1},
                    {"name": "second", "url": "https://example.com/second.txt", "min_records": 1},
                ],
                "cloudflare_ranges": {"enabled": False, "official_batch_size": 0},
            },
        }
        with patch(
            "core.fetcher._download",
            side_effect=[b"1.1.1.1:443#JP\n", b"8.8.8.8:443#US\n"],
        ) as download:
            records, warnings = collect_source_candidates(config)

        self.assertEqual(len(records), 2)
        self.assertEqual(download.call_count, 2)
        self.assertEqual(warnings, [])

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
                    "cloudflare_ranges": {"enabled": False, "official_batch_size": 0},
                },
            }
            records, warnings = collect_source_candidates(config)
            self.assertEqual(len(records), 2)
            self.assertTrue(all("required-test" in node.sources for node in records))
            self.assertTrue(any("仓库缓存" in warning for warning in warnings))

    def test_remote_shrink_does_not_overwrite_full_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache.txt"
            cached_text = "".join(f"1.1.1.{index}:443#US\n" for index in range(1, 11))
            cache.write_text(cached_text, encoding="utf-8")
            online = root / "online.txt"
            online.write_text("8.8.8.1:443#JP\n8.8.8.2:443#JP\n", encoding="utf-8")
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
                    "cloudflare_ranges": {"enabled": False, "official_batch_size": 0},
                },
            }
            records, warnings = collect_source_candidates(config)
            self.assertEqual(len(records), 10)
            self.assertEqual(cache.read_text(encoding="utf-8"), cached_text)
            self.assertTrue(any("防缩量门槛" in warning for warning in warnings))

    def test_official_batch_is_additional_to_all_source_ips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ranges = root / "ranges.txt"
            ranges.write_text("104.16.0.0/20\n", encoding="utf-8")
            source = root / "source.txt"
            source.write_text("1.1.1.1:443\n8.8.8.8:443\n", encoding="utf-8")
            config = {
                "_base_dir": str(root),
                "project": {"user_agent": "Noode-CG-test"},
                "sources": {
                    "local": ["source.txt"],
                    "remote": [],
                    "cloudflare_ranges": {
                        "enabled": True,
                        "refresh": False,
                        "ipv4_fallback": "ranges.txt",
                        "official_batch_size": 10,
                        "ports": [443],
                        "sampling_seed": "test-fixed",
                        "include_ipv6": False,
                    },
                },
            }

            sources, _warnings = collect_source_candidates(config)
            official, _warnings = collect_official_batch(
                config,
                exclude_ips={node.ip for node in sources},
                round_index=0,
            )

            self.assertEqual({node.ip for node in sources}, {"1.1.1.1", "8.8.8.8"})
            self.assertEqual(len({record.ip for record in official}), 10)
            self.assertTrue({node.ip for node in sources}.isdisjoint({node.ip for node in official}))

    def test_official_batches_are_different_and_disjoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ranges = root / "ranges.txt"
            ranges.write_text("104.16.0.0/16\n", encoding="utf-8")
            base = {
                "_base_dir": str(root),
                "project": {"user_agent": "Noode-CG-test"},
                "sources": {
                    "local": [],
                    "remote": [],
                    "cloudflare_ranges": {
                        "enabled": True,
                        "refresh": False,
                        "ipv4_fallback": "ranges.txt",
                        "official_batch_size": 100,
                        "ports": [443],
                        "include_ipv6": False,
                    },
                },
            }
            first, _ = collect_official_batch(base, exclude_ips=set(), round_index=0)
            first_ips = {record.ip for record in first}
            second, _ = collect_official_batch(base, exclude_ips=first_ips, round_index=1)
            first_ips = {record.ip for record in first}
            second_ips = {record.ip for record in second}

            self.assertEqual(len(first_ips), 100)
            self.assertEqual(len(second_ips), 100)
            self.assertNotEqual(first_ips, second_ips)
            self.assertTrue(first_ips.isdisjoint(second_ips))

    def test_non_public_source_addresses_do_not_consume_pool_slots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            source.write_text("127.0.0.1:1234\n1.1.1.1:443\n", encoding="utf-8")
            config = {
                "_base_dir": str(root),
                "project": {"user_agent": "Noode-CG-test"},
                "sources": {
                    "local": ["source.txt"],
                    "remote": [],
                    "cloudflare_ranges": {"enabled": False, "official_batch_size": 0},
                },
            }

            records, _warnings = collect_source_candidates(config)

            self.assertEqual([record.ip for record in records], ["1.1.1.1"])


if __name__ == "__main__":
    unittest.main()
