from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.result_payload import PAYLOAD_FILES, pack_payload, unpack_payload


class ResultPayloadTests(unittest.TestCase):
    def test_round_trip_only_contains_publish_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            destination = Path(temporary) / "destination"
            archive = Path(temporary) / "result.zip"
            for relative in PAYLOAD_FILES:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(("payload:" + relative).encode())
            secret = root / "do-not-publish.txt"
            secret.write_text("private", encoding="utf-8")

            packed = pack_payload(root, archive)
            unpacked = unpack_payload(archive, destination)

            self.assertEqual(set(packed), set(PAYLOAD_FILES))
            self.assertEqual(set(unpacked), set(PAYLOAD_FILES))
            self.assertFalse((destination / secret.name).exists())
            for relative in PAYLOAD_FILES:
                self.assertEqual((destination / relative).read_bytes(), (root / relative).read_bytes())

    def test_unpack_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "bad.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("../outside.txt", "bad")
            with self.assertRaises(ValueError):
                unpack_payload(archive, Path(temporary) / "destination")


if __name__ == "__main__":
    unittest.main()
