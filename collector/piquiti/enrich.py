"""Turn raw hostnames into domain_info rows and client IPs into device names."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tldextract
import yaml

from .services import ServiceMap

# Uses the Public Suffix List snapshot bundled with tldextract; no network fetch.
_extract = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=False)


def root_domain(hostname: str) -> str:
    """eTLD+1: 'a.b.bbc.co.uk' -> 'bbc.co.uk'. Falls back to the hostname for
    single-label names ('printer', 'lan') and reverse-lookup zones."""
    host = hostname.lower().rstrip(".")
    if host.endswith(".arpa"):
        return "in-addr.arpa" if host.endswith("in-addr.arpa") else "ip6.arpa"
    ext = _extract(host)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
    return host


@dataclass
class DomainInfo:
    domain: str
    root_domain: str
    app: str | None
    company: str | None
    category: str | None
    background: bool
    source: str


def describe(hostname: str, services: ServiceMap) -> DomainInfo:
    root = root_domain(hostname)
    m = services.lookup(hostname)
    return DomainInfo(
        domain=hostname,
        root_domain=root,
        app=m.app,
        company=m.company,
        category=m.category,
        background=m.background,
        source=m.source,
    )


def load_devices(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {str(ip): (v or {}) for ip, v in (data.get("devices") or {}).items()}
