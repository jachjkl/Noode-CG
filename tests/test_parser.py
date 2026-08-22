from __future__ import annotations

import io
import unittest
import zipfile

from core.parser import deduplicate, parse_bytes, parse_line, parse_text


class ParserTests(unittest.TestCase):
    def test_compact_format(self) -> None:
        node = parse_line("101.108.49.121:443#th", source="fixture")
        self.assertIsNotNone(node)
        assert node is not None
        self.assertEqual(node.ip, "101.108.49.121")
        self.assertEqual(node.port, 443)
        self.assertEqual(node.country_hint, "TH")

    def test_ipv6_formats(self) -> None:
        bracketed = parse_line("[2606:4700::1111]:8443#US", source="fixture")
        plain = parse_line("2606:4700::1111", source="fixture", default_port=2053)
        self.assertEqual(bracketed.ip, "2606:4700::1111")
        self.assertEqual(bracketed.port, 8443)
        self.assertEqual(plain.port, 2053)

    def test_invalid_lines_are_ignored(self) -> None:
        values = parse_text("bad\n999.1.1.1:443\n1.1.1.1:443#US\n", source="fixture")
        self.assertEqual(len(values), 1)

    def test_zip_path_supplies_port_and_country(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("2053/JP.txt", "1.1.1.1\n2606:4700::1111\n")
        values = parse_bytes("ip.zip", buffer.getvalue(), source="fixture")
        self.assertEqual([(node.port, node.country_hint) for node in values], [(2053, "JP"), (2053, "JP")])

    def test_deduplicate_merges_sources(self) -> None:
        first = parse_line("1.1.1.1:443#US", source="a")
        second = parse_line("1.1.1.1:443", source="b")
        values = deduplicate([first, second])
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].sources, ["a", "b"])
        self.assertEqual(values[0].country_hint, "US")


if __name__ == "__main__":
    unittest.main()
