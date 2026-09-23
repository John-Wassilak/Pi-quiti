"""Postgres writes: queries, domain enrichment, devices, sessions."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

import psycopg
import psycopg.sql

from .enrich import describe
from .pihole import Query
from .services import ServiceMap
from .sessions import Session, sessionize

log = logging.getLogger(__name__)


def to_dt(ts: float) -> datetime:
    # Millisecond precision so the API and the FTL database agree on the key.
    return datetime.fromtimestamp(round(ts, 3), tz=timezone.utc)


def get_state(conn: psycopg.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM collector_state WHERE key = %s", (key,)).fetchone()
    return row[0] if row else default


def set_state(conn: psycopg.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO collector_state VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def set_timezone(conn: psycopg.Connection, tz: str) -> None:
    """Store the local timezone as a database setting read by local_hour()."""
    conn.execute("SELECT now() AT TIME ZONE %s", (tz,))  # fails fast on a bad name
    dbname = conn.info.dbname
    conn.execute(psycopg.sql.SQL("ALTER DATABASE {} SET piquiti.tz = {}").format(
        psycopg.sql.Identifier(dbname), psycopg.sql.Literal(tz)))
    conn.execute("SELECT set_config('piquiti.tz', %s, false)", (tz,))


class Store:
    def __init__(self, conn: psycopg.Connection, services: ServiceMap):
        self.conn = conn
        self.services = services
        self.known: set[str] = {r[0] for r in conn.execute("SELECT domain FROM domain_info")}

    def ensure_domains(self, first_seen: dict[str, datetime]) -> None:
        new = [d for d in first_seen if d not in self.known]
        if not new:
            return
        rows = []
        for d in new:
            i = describe(d, self.services)
            rows.append((i.domain, i.root_domain, i.app, i.company, i.category, i.background, i.source, first_seen[d]))
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO domain_info VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (domain) DO NOTHING", rows
            )
        self.known.update(new)
        log.info("added %d new domains", len(new))

    def insert_queries(self, queries: Iterable[Query]) -> int:
        queries = list(queries)
        if not queries:
            return 0
        first_seen: dict[str, datetime] = {}
        for q in queries:
            t = to_dt(q.ts)
            if q.domain not in first_seen or t < first_seen[q.domain]:
                first_seen[q.domain] = t
        self.ensure_domains(first_seen)

        with self.conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE IF NOT EXISTS q_in (id bigint, ts timestamptz, client_ip inet,"
                " domain text, qtype text, status text, blocked boolean) ON COMMIT DELETE ROWS"
            )
            with cur.copy("COPY q_in FROM STDIN") as copy:
                for q in sorted(queries, key=lambda q: q.ts):
                    copy.write_row((q.id, to_dt(q.ts), q.client_ip, q.domain, q.qtype, q.status, q.blocked))
            # Re-fetched rows may have a final status now (IN_PROGRESS -> FORWARDED).
            cur.execute(
                """
                INSERT INTO dns_query (id, ts, client_ip, domain, qtype, status, blocked)
                SELECT id, ts, client_ip, domain, qtype, status, blocked FROM q_in ORDER BY ts
                ON CONFLICT (id, ts) DO UPDATE
                    SET status = excluded.status, blocked = excluded.blocked
                    WHERE dns_query.status IS DISTINCT FROM excluded.status
                """
            )
            return cur.rowcount

    def upsert_devices(self, config: dict[str, dict], seen: dict[str, str | None]) -> None:
        """Every client IP gets a row so Grafana can list it. Names from
        devices.yaml win, then Pi-hole hostnames, then the bare IP."""
        with self.conn.cursor() as cur:
            for ip, name in seen.items():
                if ip in config:
                    continue
                cur.execute(
                    "INSERT INTO device VALUES (%s, %s, NULL, %s) ON CONFLICT (client_ip) DO UPDATE"
                    " SET name = excluded.name, source = excluded.source"
                    " WHERE device.source = 'ip' OR (device.source = 'pihole' AND excluded.source = 'pihole')",
                    (ip, name or ip, "pihole" if name else "ip"),
                )
            for ip, d in config.items():
                cur.execute(
                    "INSERT INTO device VALUES (%s, %s, %s, 'config') ON CONFLICT (client_ip) DO UPDATE"
                    " SET name = excluded.name, owner = excluded.owner, source = 'config'",
                    (ip, d.get("name", ip), d.get("owner")),
                )

    def remap(self) -> int:
        """Re-label every known domain with the current service map."""
        rows = self.conn.execute("SELECT domain FROM domain_info").fetchall()
        updates = []
        for (d,) in rows:
            i = describe(d, self.services)
            updates.append((i.root_domain, i.app, i.company, i.category, i.background, i.source, d))
        with self.conn.cursor() as cur:
            cur.executemany(
                "UPDATE domain_info SET root_domain=%s, app=%s, company=%s, category=%s, background=%s, source=%s"
                " WHERE domain=%s",
                updates,
            )
        return len(updates)


# Held for the length of a transaction so the live collector and a rebuild
# never fold the same rows into the session table at the same time.
SESSION_LOCK = 0x5E55_1011

ACTIVITY_SQL = """
    SELECT q.seq, q.ts, host(q.client_ip), coalesce(i.app, i.root_domain, q.domain),
           q.blocked OR coalesce(i.background, false)
    FROM dns_query q LEFT JOIN domain_info i ON i.domain = q.domain
"""


def write_sessions(cur: psycopg.Cursor, changed: list[Session], tail: timedelta) -> None:
    for s in changed:
        if s.original_start and s.original_start != s.start_ts:
            cur.execute(
                "DELETE FROM session WHERE client_ip=%s AND service=%s AND start_ts=%s",
                (s.client_ip, s.service, s.original_start),
            )
        cur.execute(
            """
            INSERT INTO session VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (client_ip, service, start_ts) DO UPDATE
                SET end_ts = excluded.end_ts, last_query = excluded.last_query,
                    query_count = excluded.query_count
            """,
            (s.client_ip, s.service, s.start_ts, s.end_ts(tail), s.last_query, s.query_count),
        )


def update_sessions(conn: psycopg.Connection, gap: timedelta, tail: timedelta, chunk: int = 200_000) -> int:
    """Fold queries inserted since the last run into the session table."""
    total = 0
    while True:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (SESSION_LOCK,))
        last_seq = int(get_state(conn, "session_seq", "0"))
        rows = conn.execute(ACTIVITY_SQL + " WHERE q.seq > %s ORDER BY q.seq LIMIT %s", (last_seq, chunk)).fetchall()
        if not rows:
            conn.commit()  # releases the lock
            return total
        activity = [(ts, ip, svc) for _, ts, ip, svc, skip in rows if not skip]
        if activity:
            earliest = min(r[0] for r in activity)
            latest: dict[tuple[str, str], Session] = {}
            for ip, svc, start, last, n in conn.execute(
                """
                SELECT DISTINCT ON (client_ip, service) host(client_ip), service, start_ts, last_query, query_count
                FROM session WHERE last_query >= %s
                ORDER BY client_ip, service, start_ts DESC
                """,
                (earliest - gap,),
            ):
                latest[(ip, svc)] = Session(ip, svc, start, last, n, original_start=start)
            changed = sessionize(activity, latest, gap)
            with conn.cursor() as cur:
                write_sessions(cur, changed, tail)
            total += len(changed)
        set_state(conn, "session_seq", str(rows[-1][0]))
        conn.commit()
        if len(rows) < chunk:
            return total


def rebuild_sessions(conn: psycopg.Connection, gap: timedelta, tail: timedelta, chunk: int = 200_000) -> int:
    """Rebuild the session table from every stored query, in time order.

    update_sessions walks rows in insertion order, which is wrong for a full
    rebuild once older history has been backfilled after newer rows. Runs as
    one transaction, so dashboards keep the old sessions until it commits.
    Returns the number of sessions written.
    """
    conn.commit()
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (SESSION_LOCK,))
    conn.execute("DELETE FROM session")  # not TRUNCATE: its lock would block dashboard reads
    max_seq = conn.execute("SELECT coalesce(max(seq), 0) FROM dns_query").fetchone()[0]
    latest: dict[tuple[str, str], Session] = {}
    with conn.cursor(name="session_rebuild") as rows, conn.cursor() as cur:
        rows.execute(ACTIVITY_SQL + " WHERE q.seq <= %s ORDER BY q.ts, q.seq", (max_seq,))
        while batch := rows.fetchmany(chunk):
            activity = [(ts, ip, svc) for _, ts, ip, svc, skip in batch if not skip]
            write_sessions(cur, sessionize(activity, latest, gap), tail)
    # Rows inserted after max_seq are picked up by the next update_sessions.
    set_state(conn, "session_seq", str(max_seq))
    count = conn.execute("SELECT count(*) FROM session").fetchone()[0]
    conn.commit()
    return count
