"""Pi-quiti collector.

    python -m piquiti run                     poll Pi-hole forever (default)
    python -m piquiti backfill FILE [--days N] import a copied pihole-FTL.db
    python -m piquiti remap                   re-label domains after editing overrides
    python -m piquiti resessionize            rebuild the session table from scratch
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import time
from datetime import timedelta
from pathlib import Path

import psycopg

from . import backfill, db
from .enrich import load_devices
from .pihole import PiHole
from .services import build

log = logging.getLogger("piquiti")


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"missing environment variable {name}")
    return value


CONFIG_DIR = Path(env("CONFIG_DIR", "/config"))
DATA_DIR = Path(env("DATA_DIR", "/data"))
V2FLY_DIR = DATA_DIR / "v2fly"
V2FLY_MAX_AGE = 7 * 86400
GAP = timedelta(minutes=float(env("SESSION_GAP_MINUTES", "10")))
TAIL = timedelta(minutes=float(env("SESSION_TAIL_MINUTES", "2")))


def services(refresh: bool = False):
    stale = not V2FLY_DIR.exists() or time.time() - V2FLY_DIR.stat().st_mtime > V2FLY_MAX_AGE
    if refresh or stale:
        log.info("downloading v2fly domain lists")
    sm = build(V2FLY_DIR, CONFIG_DIR / "services.override.yaml", refresh=refresh or stale)
    if refresh or stale:
        V2FLY_DIR.touch()
    return sm


def connect() -> psycopg.Connection:
    conn = psycopg.connect(env("DATABASE_URL"))
    db.set_timezone(conn, env("TZ", "UTC"))
    conn.commit()
    return conn


def cmd_run(args) -> None:
    poll = float(env("POLL_SECONDS", "60"))
    overlap = 180  # re-read recent queries whose status was still pending
    pihole = PiHole(env("PIHOLE_URL"), env("PIHOLE_PASSWORD"), verify_tls=env("PIHOLE_VERIFY_TLS", "true") == "true")
    conn = connect()
    store = db.Store(conn, services())
    devices_cfg = load_devices(CONFIG_DIR / "devices.yaml")
    store.upsert_devices(devices_cfg, {})
    conn.commit()

    stop = False

    def handle(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)

    last_ts = float(db.get_state(conn, "last_ts") or time.time() - float(env("INITIAL_HOURS", "24")) * 3600)
    while not stop:
        started = time.time()
        try:
            if time.time() - V2FLY_DIR.stat().st_mtime > V2FLY_MAX_AGE:
                store.services = services(refresh=True)
            queries = pihole.queries_since(last_ts - overlap, started)
            n = store.insert_queries(queries)
            store.upsert_devices(devices_cfg, {q.client_ip: q.client_name for q in queries})
            if queries:
                last_ts = max(last_ts, max(q.ts for q in queries))
                db.set_state(conn, "last_ts", str(last_ts))
            conn.commit()
            s = db.update_sessions(conn, GAP, TAIL)
            log.info("fetched %d queries, %d new/changed rows, %d sessions touched", len(queries), n, s)
        except Exception:
            conn.rollback()
            log.exception("poll failed; retrying next cycle")
        time.sleep(max(0.0, poll - (time.time() - started)) if not stop else 0)
    pihole.logout()


def cmd_backfill(args) -> None:
    conn = connect()
    store = db.Store(conn, services())
    total = 0
    names: dict[str, str] = {}
    for queries, names in backfill.read_ftl(Path(args.file), args.days):
        total += store.insert_queries(queries)
        conn.commit()
        log.info("imported %d rows so far", total)
    store.upsert_devices(load_devices(CONFIG_DIR / "devices.yaml"), names)
    store.upsert_devices({}, {ip: None for (ip,) in conn.execute("SELECT DISTINCT host(client_ip) FROM dns_query")})
    conn.commit()
    log.info("rebuilding sessions")
    db.reset_sessions(conn)
    db.update_sessions(conn, GAP, TAIL)


def cmd_remap(args) -> None:
    conn = connect()
    store = db.Store(conn, services(refresh=args.refresh))
    n = store.remap()
    store.upsert_devices(load_devices(CONFIG_DIR / "devices.yaml"), {})
    conn.commit()
    log.info("re-labelled %d domains; rebuilding sessions", n)
    db.reset_sessions(conn)
    db.update_sessions(conn, GAP, TAIL)


def cmd_resessionize(args) -> None:
    conn = connect()
    db.reset_sessions(conn)
    log.info("%d sessions written", db.update_sessions(conn, GAP, TAIL))


def main() -> None:
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="piquiti")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run")
    b = sub.add_parser("backfill")
    b.add_argument("file")
    b.add_argument("--days", type=int, default=365)
    r = sub.add_parser("remap")
    r.add_argument("--refresh", action="store_true", help="re-download v2fly lists first")
    sub.add_parser("resessionize")
    args = p.parse_args()
    {"run": cmd_run, None: cmd_run, "backfill": cmd_backfill, "remap": cmd_remap, "resessionize": cmd_resessionize}[
        args.cmd
    ](args)


if __name__ == "__main__":
    main()
