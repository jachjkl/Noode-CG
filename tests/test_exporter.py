from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from core.exporter import publish_outputs
from core.models import NodeResult


class ExporterTests(unittest.TestCase):
    def test_writes_edgetunnel_feed_and_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            node = NodeResult(ip="104.16.1.2", port=443, country="JP", colo="NRT", score=900)
            node.add_source("local-cfdata")
            report = publish_outputs(
                output,
                [node],
                {"status": "ok"},
                {"minimum_publish": 1, "preserve_last_good": True, "write_compatibility_zip": True},
            )
            self.assertTrue(report["published"])
            self.assertEqual((output / "nodes.txt").read_text(encoding="utf-8"), "104.16.1.2:443#JP\n")
            self.assertEqual(json.loads((output / "api.json").read_text(encoding="utf-8"))["count"], 1)
            self.assertEqual(
                json.loads((output / "nodes.json").read_text(encoding="utf-8"))[0]["sources"],
                ["local-cfdata"],
            )
            with zipfile.ZipFile(output / "ip.zip") as archive:
                self.assertEqual(archive.read("443/JP.txt"), b"104.16.1.2\n")

    def test_preserves_previous_good_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "nodes.txt").write_text("1.1.1.1:443#US\n", encoding="utf-8")
            report = publish_outputs(
                output,
                [],
                {"status": "degraded"},
                {"minimum_publish": 10, "preserve_last_good": True},
            )
            self.assertFalse(report["published"])
            self.assertEqual((output / "nodes.txt").read_text(encoding="utf-8"), "1.1.1.1:443#US\n")

    def test_does_not_create_empty_subscription_on_first_failed_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            report = publish_outputs(
                output,
                [],
                {"status": "degraded"},
                {"minimum_publish": 200, "preserve_last_good": True},
            )
            self.assertFalse(report["published"])
            self.assertFalse((output / "nodes.txt").exists())


if __name__ == "__main__":
    unittest.main()
