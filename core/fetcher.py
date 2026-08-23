from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import time
import urllib.request
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from .config import resolve_path
from .io_utils import atomic_write_bytes
from .models import NodeResult
from .parser import deduplicate, parse_bytes


def _download(url: str, *, user_agent: str, timeout: float, max_bytes: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > max_bytes:
            raise ValueError(f"下载内容超过上限: {url}")
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"下载内容超过上限: {url}")
    return data


def _read_range_lines(
    config: dict[str, Any],
    block: dict[str, Any],
    family: int,
    warnings: list[str],
) -> list[str]:
    kind = "ipv4" if family == 4 else "ipv6"
    text = ""
    if block.get("refresh", True):
        try:
            text = _download(
                str(block[f"{kind}_url"]),
                user_agent=str(config["project"].get("user_agent", "Noode-CG/2.0")),
                timeout=15,
                max_bytes=1024 * 1024,
            ).decode("ascii", errors="strict")
        except Exception as exc:  # Network fallback is intentional.
            warnings.append(f"刷新 Cloudflare {kind.upper()} 网段失败，使用内置快照: {exc}")
    if not text:
        fallback = resolve_path(config, block[f"{kind}_fallback"])
        text = fallback.read_text(encoding="utf-8")

    ranges: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            network = ipaddress.ip_network(line, strict=False)
        except ValueError:
            continue
        if network.version == family:
            ranges.append(str(network))
    return ranges


def sample_ranges(
    cidrs: Iterable[str],
    count: int,
    *,
    ports: list[int],
    seed: int,
    source: str,
) -> list[NodeResult]:
    networks = [ipaddress.ip_network(value, strict=False) for value in cidrs]
    networks = [network for network in networks if network.num_addresses > 2]
    if not networks or count <= 0:
        return []
    ports = [int(port) for port in ports if 1 <= int(port) <= 65535] or [443]
    results: list[NodeResult] = []
    capacities = [network.num_addresses - (2 if network.version == 4 else 1) for network in networks]
    total_capacity = sum(capacities)
    count = min(count, total_capacity)
    exact_quotas = [count * capacity / total_capacity for capacity in capacities]
    quotas = [min(capacity, int(quota)) for capacity, quota in zip(capacities, exact_quotas)]
    remaining = count - sum(quotas)
    remainder_order = sorted(
        range(len(networks)),
        key=lambda index: exact_quotas[index] - quotas[index],
        reverse=True,
    )
    for index in remainder_order:
        if not remaining:
            break
        if quotas[index] < capacities[index]:
            quotas[index] += 1
            remaining -= 1

    for index, network in enumerate(networks):
        usable = capacities[index]
        digest = hashlib.sha256(f"{seed}:{network}".encode()).digest()
        start = int.from_bytes(digest[:8], "big") % usable
        step = (int.from_bytes(digest[8:16], "big") | 1) % usable or 1
        while math.gcd(step, usable) != 1:
            step = (step + 2) % usable or 1
        for round_index in range(quotas[index]):
            offset = (start + round_index * step) % usable
            numeric = int(network.network_address) + 1 + offset
            address = str(ipaddress.ip_address(numeric))
            port = ports[len(results) % len(ports)]
            node = NodeResult(ip=address, port=port)
            node.add_source(source)
            results.append(node)
    return results


def _parse_remote_payload(entry: dict[str, Any], url: str, payload: bytes) -> list[NodeResult]:
    label = str(entry.get("name") or url)
    if entry.get("format") == "vps789":
        records = parse_vps789_payload(payload, source=label, max_age_days=int(entry.get("max_age_days", 7)))
    else:
        records = parse_bytes(
            url.split("?", 1)[0],
            payload,
            source=label,
            default_port=int(entry.get("default_port", 443)),
        )
    minimum = int(entry.get("min_records", 1))
    if len(records) < minimum:
        raise ValueError(f"{label} 只解析出 {len(records)} 条，低于要求的 {minimum} 条")
    return records


def parse_vps789_payload(payload: bytes, *, source: str, max_age_days: int) -> list[NodeResult]:
    value = json.loads(payload.decode("utf-8-sig"))
    if not isinstance(value, dict) or value.get("code") != 0 or not isinstance(value.get("data"), dict):
        raise ValueError("VPS789 接口返回格式无效")

    records: list[NodeResult] = []
    newest: datetime | None = None
    for group, rows in value["data"].items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                address = str(ipaddress.ip_address(str(row.get("ip", "")).strip()))
            except ValueError:
                continue
            timestamp = str(row.get("createdTime", "")).strip()
            try:
                observed = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                newest = observed if newest is None or observed > newest else newest
            except ValueError:
                pass

            node = NodeResult(ip=address, port=443)
            node.add_source(source)
            for vantage, latency_key, loss_key in (
                ("CT", "dxLatencyAvg", "dxPkgLostRateAvg"),
                ("CU", "ltLatencyAvg", "ltPkgLostRateAvg"),
                ("CM", "ydLatencyAvg", "ydPkgLostRateAvg"),
            ):
                try:
                    latency = float(row[latency_key])
                    loss = max(0.0, min(1.0, float(row[loss_key]) / 100))
                except (KeyError, TypeError, ValueError):
                    continue
                node.probe_results[f"vps789-{vantage}"] = {
                    "tcp_ok": loss < 1.0,
                    "latency_ms": latency,
                    "loss_rate": loss,
                    "observed_at": timestamp,
                    "list": str(group),
                }
            records.append(node)

    if not records:
        raise ValueError("VPS789 接口没有可解析的 IP")
    if newest is None:
        raise ValueError("VPS789 接口缺少有效测速时间")
    age_days = (datetime.now(UTC) - newest).total_seconds() / 86400
    if age_days > max_age_days:
        raise ValueError(f"VPS789 最新测速数据已过期 {age_days:.1f} 天（上限 {max_age_days} 天）")
    return deduplicate(records)


def _load_remote_source(
    config: dict[str, Any],
    entry: dict[str, Any],
    *,
    user_agent: str,
    max_bytes: int,
    warnings: list[str],
) -> list[NodeResult]:
    url = str(entry.get("url", "")).strip()
    if not url:
        if entry.get("required", False):
            raise ValueError("必选远程源缺少 URL")
        return []
    label = str(entry.get("name") or url)
    retries = max(1, int(entry.get("retries", 3)))
    timeout = float(entry.get("timeout_seconds", 20))
    retry_delay = max(0.0, float(entry.get("retry_delay_seconds", 2)))
    failures: list[str] = []
    cache_value = entry.get("cache_path")
    cache_path = resolve_path(config, cache_value) if cache_value else None
    cached_records: list[NodeResult] = []
    if cache_path and cache_path.is_file():
        try:
            cached_records = _parse_remote_payload(entry, url, cache_path.read_bytes())
        except Exception as exc:
            failures.append(f"现有缓存无效: {exc}")

    for attempt in range(1, retries + 1):
        try:
            payload = _download(url, user_agent=user_agent, timeout=timeout, max_bytes=max_bytes)
            records = _parse_remote_payload(entry, url, payload)
            minimum_ratio = float(entry.get("min_ratio_to_cache", 0.0))
            if cached_records and minimum_ratio > 0:
                ratio_floor = int(len(cached_records) * minimum_ratio)
                if len(records) < ratio_floor:
                    raise ValueError(
                        f"{label} 本次只有 {len(records)} 条，低于缓存 {len(cached_records)} 条的 "
                        f"{minimum_ratio:.0%} 防缩量门槛"
                    )
            if cache_path:
                atomic_write_bytes(cache_path, payload)
            return records
        except Exception as exc:
            failures.append(f"第 {attempt} 次: {exc}")
            if attempt < retries and retry_delay:
                time.sleep(retry_delay)

    if cached_records:
        warnings.append(f"{label} 在线刷新失败，已使用仓库缓存；{'；'.join(failures)}")
        return cached_records

    message = f"远程源 {label} 不可用；{'；'.join(failures)}"
    if entry.get("required", False):
        raise ValueError(message)
    warnings.append(message)
    return []


def collect_candidates(config: dict[str, Any]) -> tuple[list[NodeResult], list[str]]:
    source_config = config["sources"]
    warnings: list[str] = []
    records: list[NodeResult] = []
    max_bytes = int(source_config.get("max_download_bytes", 20 * 1024 * 1024))
    user_agent = str(config["project"].get("user_agent", "Noode-CG/2.0"))

    for configured in source_config.get("local", []):
        path = resolve_path(config, configured)
        if not path.is_file():
            warnings.append(f"本地输入不存在，已跳过: {path}")
            continue
        try:
            records.extend(parse_bytes(path.name, path.read_bytes(), source=str(path)))
        except Exception as exc:
            warnings.append(f"解析本地输入失败 {path}: {exc}")

    for configured in source_config.get("remote", []):
        entry = {"url": configured} if isinstance(configured, str) else configured
        records.extend(
            _load_remote_source(
                config,
                entry,
                user_agent=user_agent,
                max_bytes=max_bytes,
                warnings=warnings,
            )
        )

    records = deduplicate(records)
    ranges = source_config.get("cloudflare_ranges", {})
    target = int(ranges.get("target_pool", 0))
    unique_ips = {record.ip for record in records}
    if ranges.get("enabled", True) and len(unique_ips) < target:
        ipv4 = _read_range_lines(config, ranges, 4, warnings)
        seed = int(ranges.get("deterministic_seed", 0))
        for attempt in range(5):
            generated = sample_ranges(
                ipv4,
                target,
                ports=list(ranges.get("ports", [443])),
                seed=seed + attempt,
                source="cloudflare-official-ipv4",
            )
            for node in generated:
                if node.ip in unique_ips:
                    continue
                records.append(node)
                unique_ips.add(node.ip)
                if len(unique_ips) >= target:
                    break
            if len(unique_ips) >= target:
                break

        if ranges.get("include_ipv6", False) and len(unique_ips) < target:
            ipv6 = _read_range_lines(config, ranges, 6, warnings)
            for node in sample_ranges(
                ipv6,
                target - len(unique_ips),
                ports=list(ranges.get("ports", [443])),
                seed=seed + 100,
                source="cloudflare-official-ipv6",
            ):
                if node.ip not in unique_ips:
                    records.append(node)
                    unique_ips.add(node.ip)

    return deduplicate(records), warnings
