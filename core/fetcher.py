from __future__ import annotations

import hashlib
import ipaddress
import math
import time
import urllib.request
from collections.abc import Iterable
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


def _public_records(records: Iterable[NodeResult], warnings: list[str]) -> list[NodeResult]:
    public: list[NodeResult] = []
    rejected = 0
    for record in records:
        if ipaddress.ip_address(record.ip).is_global:
            public.append(record)
        else:
            rejected += 1
    if rejected:
        warnings.append(f"已排除 {rejected} 条非公网地址")
    return public


def _limit_unique_ips(records: Iterable[NodeResult], target: int) -> list[NodeResult]:
    if target <= 0:
        return list(records)
    allowed: set[str] = set()
    limited: list[NodeResult] = []
    for record in records:
        if record.ip not in allowed and len(allowed) >= target:
            continue
        allowed.add(record.ip)
        limited.append(record)
    return limited


def _resolve_sampling_seed(block: dict[str, Any]) -> int:
    raw = block.get("sampling_seed")
    if raw is None or str(raw).strip() == "":
        return time.time_ns()
    text = str(raw).strip()
    try:
        return int(text)
    except ValueError:
        return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def _parse_remote_payload(entry: dict[str, Any], url: str, payload: bytes) -> list[NodeResult]:
    label = str(entry.get("name") or url)
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


def collect_source_candidates(config: dict[str, Any]) -> tuple[list[NodeResult], list[str]]:
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

    return deduplicate(_public_records(records, warnings)), warnings


def _cached_ranges(
    config: dict[str, Any],
    block: dict[str, Any],
    family: int,
    warnings: list[str],
) -> list[str]:
    cache_key = f"_cached_ipv{family}_ranges"
    cached = block.get(cache_key)
    if isinstance(cached, list):
        return [str(value) for value in cached]
    ranges = _read_range_lines(config, block, family, warnings)
    block[cache_key] = ranges
    return ranges


def _cached_sampling_seed(block: dict[str, Any]) -> int:
    cached = block.get("_resolved_sampling_seed")
    if isinstance(cached, int):
        return cached
    seed = _resolve_sampling_seed(block)
    block["_resolved_sampling_seed"] = seed
    return seed


def collect_official_batch(
    config: dict[str, Any],
    *,
    exclude_ips: set[str],
    round_index: int,
) -> tuple[list[NodeResult], list[str]]:
    block = config["sources"].get("cloudflare_ranges", {})
    warnings: list[str] = []
    wanted = max(0, int(block.get("official_batch_size", 50000)))
    if not block.get("enabled", True) or wanted == 0:
        return [], warnings

    ranges = _cached_ranges(config, block, 4, warnings)
    if not ranges:
        warnings.append("Cloudflare 官方 IPv4 网段为空，无法生成本轮候选")
        return [], warnings

    seed = _cached_sampling_seed(block)
    selected: list[NodeResult] = []
    selected_ips: set[str] = set()
    # Each round has an independent stream. Oversampling absorbs collisions
    # with link sources and every earlier official batch.
    for attempt in range(12):
        request_count = min(
            wanted * (2 + attempt),
            wanted + len(exclude_ips) + len(selected_ips) + 1000,
        )
        generated = sample_ranges(
            ranges,
            request_count,
            ports=list(block.get("ports", [443])),
            seed=seed + round_index * 100_003 + attempt * 7_919,
            source=f"cloudflare-official-ipv4-round-{round_index + 1}",
        )
        for node in generated:
            if node.ip in exclude_ips or node.ip in selected_ips:
                continue
            selected.append(node)
            selected_ips.add(node.ip)
            if len(selected) >= wanted:
                return selected, warnings

    warnings.append(f"第 {round_index + 1} 轮官方段只生成 {len(selected)}/{wanted} 个不重复 IP")
    return selected, warnings
