"""Minimal Pi-hole v6 REST API client."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

# Status names from the v6 API; numeric ids are the FTL database codes.
# https://docs.pi-hole.net/database/query-database/
STATUS_BY_ID = {
    0: "UNKNOWN", 1: "GRAVITY", 2: "FORWARDED", 3: "CACHE", 4: "REGEX", 5: "DENYLIST",
    6: "EXTERNAL_BLOCKED_IP", 7: "EXTERNAL_BLOCKED_NULL", 8: "EXTERNAL_BLOCKED_NXRA",
    9: "GRAVITY_CNAME", 10: "REGEX_CNAME", 11: "DENYLIST_CNAME", 12: "RETRIED",
    13: "RETRIED_DNSSEC", 14: "IN_PROGRESS", 15: "DBBUSY", 16: "SPECIAL_DOMAIN",
    17: "CACHE_STALE", 18: "EXTERNAL_BLOCKED_EDE15",
}
BLOCKED_IDS = {1, 4, 5, 6, 7, 8, 9, 10, 11, 15, 16, 18}
BLOCKED_NAMES = {STATUS_BY_ID[i] for i in BLOCKED_IDS}

QTYPE_BY_ID = {
    1: "A", 2: "AAAA", 3: "ANY", 4: "SRV", 5: "SOA", 6: "PTR", 7: "TXT", 8: "NAPTR",
    9: "MX", 10: "DS", 11: "RRSIG", 12: "DNSKEY", 13: "NS", 14: "OTHER", 15: "SVCB", 16: "HTTPS",
}

# FTL keeps roughly the last 24h in memory; older data needs disk=true.
MEMORY_WINDOW = 23 * 3600


@dataclass
class Query:
    id: int
    ts: float
    client_ip: str
    client_name: str | None
    domain: str
    qtype: str | None
    status: str | None

    @property
    def blocked(self) -> bool:
        return self.status in BLOCKED_NAMES


class PiHole:
    def __init__(self, base_url: str, password: str, verify_tls: bool = True, timeout: float = 60):
        self.base = base_url.rstrip("/") + "/api"
        self.password = password
        self.http = httpx.Client(verify=verify_tls, timeout=timeout)
        self.sid: str | None = None

    def login(self) -> None:
        r = self.http.post(f"{self.base}/auth", json={"password": self.password})
        r.raise_for_status()
        session = r.json()["session"]
        if not session.get("valid"):
            raise RuntimeError(f"Pi-hole login failed: {session.get('message')}")
        self.sid = session["sid"]
        log.info("logged in to Pi-hole, session valid for %ss", session.get("validity"))

    def logout(self) -> None:
        if self.sid:
            self.http.delete(f"{self.base}/auth", headers={"X-FTL-SID": self.sid})
            self.sid = None

    def _get(self, path: str, params: dict) -> dict:
        for attempt in (1, 2):
            if not self.sid:
                self.login()
            r = self.http.get(f"{self.base}{path}", params=params, headers={"X-FTL-SID": self.sid})
            if r.status_code == 401 and attempt == 1:
                self.sid = None  # session expired, log in again
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("unreachable")

    def queries_since(self, since: float, until: float | None = None, page: int = 10000) -> list[Query]:
        """All queries with since <= time <= until. The API returns newest first."""
        until = until or time.time()
        params = {"from": int(since), "until": int(until) + 1, "length": page}
        if time.time() - since > MEMORY_WINDOW:
            params["disk"] = "true"
        out: list[Query] = []
        start = 0
        while True:
            data = self._get("/queries", {**params, "start": start})
            batch = data.get("queries", [])
            out.extend(_parse(q) for q in batch)
            start += len(batch)
            if len(batch) < page or start >= data.get("recordsFiltered", 0):
                return out


def _parse(q: dict) -> Query:
    client = q.get("client") or {}
    return Query(
        id=int(q["id"]),
        ts=float(q["time"]),
        client_ip=client.get("ip"),
        client_name=client.get("name") or None,
        domain=q["domain"],
        qtype=q.get("type"),
        status=q.get("status"),
    )
