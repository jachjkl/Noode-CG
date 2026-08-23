from __future__ import annotations

import csv
import io
import ipaddress
import json
import re
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from .models import NodeResult

COUNTRY_PATTERN = re.compile(r"^[A-Za-z]{2}$")


def _normalise_country(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if COUNTRY_PATTERN.fullmatch(text) else ""


def _valid_port(value: Any, default: int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        port = default
    if not 1 <= port <= 65535:
        raise ValueError(f"无效端口: {value}")
    return port


def _make_node(ip: str, port: Any, country: Any, source: str, default_port: int) -> NodeResult:
    address = str(ip).strip().strip("[]")
    address = str(ipaddress.ip_address(address))
    node = NodeResult(
        ip=address,
        port=_valid_port(port, default_port),
        country_hint=_normalise_country(country),
    )
    node.add_source(source)
    return node


def parse_line(
    raw_line: str,
    *,
    source: str,
    default_port: int = 443,
    default_country: str = "",
) -> NodeResult | None:
    line = raw_line.strip().lstrip("\ufeff")
    if not line or line.startswith(("#", ";", "//")):
        return None

    country = default_country
    if "#" in line:
        line, possible_country = line.rsplit("#", 1)
        country = _normalise_country(possible_country) or country
        line = line.strip()

    # [IPv6]:port
    bracketed = re.fullmatch(r"\[([^]]+)](?::(\d+))?", line)
    if bracketed:
        return _make_node(bracketed.group(1), bracketed.group(2), country, source, default_port)

    # IP whitespace/comma/pipe port and optional country.
    fields = [item for item in re.split(r"[\s,|]+", line) if item]
    if len(fields) >= 2:
        try:
            ipaddress.ip_address(fields[0].strip("[]"))
            port = fields[1] if fields[1].isdigit() else default_port
            hint = fields[2] if len(fields) >= 3 else country
            return _make_node(fields[0], port, hint, source, default_port)
        except ValueError:
            pass

    # IPv4:port. Unbracketed IPv6 is treated as an address without a port.
    if line.count(":") == 1:
        host, possible_port = line.rsplit(":", 1)
        if possible_port.isdigit():
            try:
                return _make_node(host, possible_port, country, source, default_port)
            except ValueError:
                return None

    try:
        return _make_node(line, default_port, country, source, default_port)
    except ValueError:
        return None


def parse_text(
    text: str,
    *,
    source: str,
    default_port: int = 443,
    default_country: str = "",
) -> list[NodeResult]:
    results: list[NodeResult] = []
    for line in text.splitlines():
        node = parse_line(
            line,
            source=source,
            default_port=default_port,
            default_country=default_country,
        )
        if node:
            results.append(node)
    return results


def _walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _walk_json(item)
    elif isinstance(value, dict):
        lowered = {str(key).lower(): item for key, item in value.items()}
        if any(key in lowered for key in ("ip", "address", "host", "ipport")):
            yield lowered
        else:
            for item in value.values():
                yield from _walk_json(item)


def parse_json_bytes(data: bytes, *, source: str, default_port: int = 443) -> list[NodeResult]:
    value = json.loads(data.decode("utf-8-sig"))
    results: list[NodeResult] = []
    for item in _walk_json(value):
        compact = item.get("ipport")
        if compact:
            node = parse_line(
                str(compact),
                source=source,
                default_port=default_port,
                default_country=str(item.get("country") or item.get("loc") or item.get("dccountry") or ""),
            )
        else:
            try:
                node = _make_node(
                    item.get("ip") or item.get("address") or item.get("host"),
                    item.get("port"),
                    item.get("country") or item.get("loc") or item.get("dccountry"),
                    source,
                    default_port,
                )
            except (TypeError, ValueError):
                node = None
        if node:
            results.append(node)
    return results


def parse_csv_bytes(data: bytes, *, source: str, default_port: int = 443) -> list[NodeResult]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return parse_text(text, source=source, default_port=default_port)
    results: list[NodeResult] = []
    for original in reader:
        item = {str(key).strip().lower(): value for key, value in original.items() if key is not None}
        try:
            if item.get("ipport") or item.get("ip:port"):
                node = parse_line(
                    str(item.get("ipport") or item.get("ip:port")),
                    source=source,
                    default_port=default_port,
                    default_country=str(item.get("country") or item.get("loc") or item.get("落地区域") or ""),
                )
            else:
                node = _make_node(
                    item.get("ip") or item.get("ip地址") or item.get("address"),
                    item.get("port") or item.get("端口号"),
                    item.get("country") or item.get("loc") or item.get("落地区域"),
                    source,
                    default_port,
                )
        except (TypeError, ValueError):
            node = None
        if node:
            results.append(node)
    return results


def parse_zip_bytes(data: bytes, *, source: str, default_port: int = 443) -> list[NodeResult]:
    results: list[NodeResult] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.is_dir() or info.file_size > 20 * 1024 * 1024:
                continue
            member = PurePosixPath(info.filename)
            suffix = member.suffix.lower()
            if suffix not in {".txt", ".csv", ".json"}:
                continue
            member_port = default_port
            for part in member.parts[:-1]:
                if part.isdigit() and 1 <= int(part) <= 65535:
                    member_port = int(part)
                    break
            member_country = _normalise_country(member.stem)
            payload = archive.read(info)
            member_source = f"{source}!{info.filename}"
            if suffix == ".json":
                parsed = parse_json_bytes(payload, source=member_source, default_port=member_port)
            elif suffix == ".csv":
                parsed = parse_csv_bytes(payload, source=member_source, default_port=member_port)
            else:
                parsed = parse_text(
                    payload.decode("utf-8-sig", errors="replace"),
                    source=member_source,
                    default_port=member_port,
                    default_country=member_country,
                )
            results.extend(parsed)
    return results


def parse_bytes(name: str, data: bytes, *, source: str | None = None, default_port: int = 443) -> list[NodeResult]:
    label = source or name
    suffix = Path(name).suffix.lower()
    if data.startswith(b"PK\x03\x04") or suffix == ".zip":
        return parse_zip_bytes(data, source=label, default_port=default_port)
    if suffix == ".json":
        return parse_json_bytes(data, source=label, default_port=default_port)
    if suffix == ".csv":
        return parse_csv_bytes(data, source=label, default_port=default_port)
    return parse_text(data.decode("utf-8-sig", errors="replace"), source=label, default_port=default_port)


def deduplicate(records: Iterable[NodeResult]) -> list[NodeResult]:
    unique: dict[str, NodeResult] = {}
    for record in records:
        current = unique.get(record.key)
        if current is None:
            unique[record.key] = record
            continue
        for source in record.sources:
            current.add_source(source)
        if not current.country_hint and record.country_hint:
            current.country_hint = record.country_hint
        current.probe_results.update(record.probe_results)
    return list(unique.values())
