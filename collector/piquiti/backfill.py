"""Import history from a copy of the Pi-hole FTL database.

    scp pi@pihole:/etc/pihole/pihole-FTL.db ./data/
    docker compose run --rm collector backfill /data/pihole-FTL.db --days 365

Copy the file rather than reading it live: FTL writes to it every minute.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Iterator

from .pihole import QTYPE_BY_ID, STATUS_BY_ID, Query

log = logging.getLogger(__name__)


def qtype_name(n: int | None) -> str | None:
    if n is None:
        return None
    if n in QTYPE_BY_ID:
        return QTYPE_BY_ID[n]
    return f"TYPE{n - 100}" if n > 100 else "OTHER"


def read_ftl(path: Path, days: int, batch: int = 50_000) -> Iterator[tuple[list[Query], dict[str, str]]]:
    """Yield (queries, client names) batches, oldest first."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    names = {ip: name for ip, name in db.execute("SELECT ip, name FROM client_by_id WHERE name IS NOT NULL AND name != ''")}
    since = time.time() - days * 86400
    cur = db.execute(
        "SELECT id, timestamp, type, status, domain, client FROM queries WHERE timestamp >= ? ORDER BY timestamp",
        (since,),
    )
    while rows := cur.fetchmany(batch):
        yield (
            [
                Query(
                    id=r[0], ts=r[1], client_ip=r[5], client_name=names.get(r[5]), domain=r[4],
                    qtype=qtype_name(r[2]), status=STATUS_BY_ID.get(r[3], "UNKNOWN"),
                )
                for r in rows
            ],
            names,
        )
    db.close()
