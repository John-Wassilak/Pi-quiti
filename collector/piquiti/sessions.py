"""Estimate activity windows from DNS lookups.

DNS shows when a device looked something up, not how long it stayed
connected. We treat lookups for the same (device, app) that are no more than
`gap` apart as one session. The session ends `tail` after its last lookup,
since a connection usually outlives the lookup that opened it.

Blocked and background lookups are skipped: they are not user activity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class Session:
    client_ip: str
    service: str
    start_ts: datetime
    last_query: datetime
    query_count: int
    original_start: datetime | None = None  # start_ts as stored, None if new

    def end_ts(self, tail: timedelta) -> datetime:
        return self.last_query + tail


def sessionize(
    rows: list[tuple[datetime, str, str]],
    latest: dict[tuple[str, str], Session],
    gap: timedelta,
) -> list[Session]:
    """Fold (ts, client_ip, service) rows into sessions.

    `latest` holds the most recent stored session per (client_ip, service) and
    is updated in place. Returns every session created or changed.
    """
    changed: dict[int, Session] = {}
    for ts, ip, service in sorted(rows):
        key = (ip, service)
        cur = latest.get(key)
        if cur and cur.start_ts - gap <= ts <= cur.last_query + gap:
            if ts < cur.start_ts:
                cur.start_ts = ts
            if ts > cur.last_query:
                cur.last_query = ts
            cur.query_count += 1
        else:
            late = cur is not None and ts < cur.start_ts
            cur = Session(ip, service, ts, ts, 1)
            if not late:  # a late straggler must not replace the newer session
                latest[key] = cur
        changed[id(cur)] = cur
    return list(changed.values())
