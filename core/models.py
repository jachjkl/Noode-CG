from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class NodeResult:
    ip: str
    port: int = 443
    country_hint: str = ""
    sources: list[str] = field(default_factory=list)

    tcp_ok: bool = False
    tcp_latency_ms: float | None = None
    tcp_jitter_ms: float | None = None
    tcp_loss_rate: float = 1.0

    tls_ok: bool = False
    tls_latency_ms: float | None = None
    tls_version: str = ""
    tls_cipher: str = ""

    http_ok: bool = False
    http_status: int | None = None
    http_latency_ms: float | None = None
    cf_ray: str = ""
    colo: str = ""
    country: str = ""
    region: str = ""
    city: str = ""

    websocket_ok: bool | None = None
    speed_mbps: float | None = None
    score: float = 0.0
    probe_results: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.ip}|{self.port}"

    @property
    def display_host(self) -> str:
        return f"[{self.ip}]" if ":" in self.ip else self.ip

    @property
    def ip_port(self) -> str:
        return f"{self.display_host}:{self.port}"

    def add_source(self, source: str) -> None:
        if source and source not in self.sources:
            self.sources.append(source)

    def add_error(self, stage: str, exc: BaseException | str) -> None:
        message = str(exc).strip().replace("\r", " ").replace("\n", " ")
        if message:
            self.errors.append(f"{stage}: {message[:240]}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> NodeResult:
        known = {field.name for field in __import__("dataclasses").fields(cls)}
        return cls(**{key: val for key, val in value.items() if key in known})
