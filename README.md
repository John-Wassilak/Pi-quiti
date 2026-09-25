# Pi-quiti

A poor man's UniFi traffic view, built from Pi-hole's DNS log.

UniFi gateways show traffic grouped into readable apps ("Netflix", "YouTube", "Amazon") instead of hundreds of CDN hostnames. Pi-quiti builds a similar view from Pi-hole v6: it pulls every DNS lookup, rolls hostnames up into apps and companies, and shows them per device in Grafana. It also estimates activity sessions and shows what runs overnight.

## What it can and cannot see

Pi-hole only answers DNS. Pi-quiti therefore knows **which device looked up which hostname, and when**. It does not see bytes, bandwidth, or how long a connection stayed open.

- "Lookups" on every dashboard means DNS queries, not traffic volume. A chatty app with little data can outrank a video stream.
- Sessions are estimates built from bursts of lookups (see [Sessions](#sessions)).
- Devices that use DNS-over-HTTPS or a hard-coded resolver (some Chromecasts, Android Private DNS, Firefox DoH) bypass Pi-hole and are invisible.

For real bandwidth per app you need a packet-level vantage point; see [Alternatives](#alternatives).

## How it works

```
Raspberry Pi (Pi-hole v6)           Home server (docker compose)
  FTL --- /api/queries ----->  collector (Python, every 60s)
                                  |  hostname -> root domain -> app -> company -> category
                                  |  client IP -> device name
                                  v
                               PostgreSQL  (queries, domain labels, devices, sessions)
                                  |
                               Grafana (5 provisioned dashboards)
```

Grouping happens in two steps:

1. **Root domain** from the Public Suffix List (`tldextract`): `ipv4-c001.1.oca.nflxvideo.net` becomes `nflxvideo.net`, `a.b.bbc.co.uk` becomes `bbc.co.uk`.
2. **App, company, category** from [v2fly/domain-list-community](https://github.com/v2fly/domain-list-community), a community list with one file per service. `nflxvideo.net` and `nflxso.net` map to **Netflix**; `googlevideo.com` maps to **YouTube** under the company **Google**. Its `category-*` files supply categories such as Streaming, Social and Gaming. The lists are re-downloaded weekly.

Anything not in the lists shows up under its root domain and in the "Unrecognised root domains" panel, so nothing is hidden. Your own fixes go in `config/services.override.yaml`.

## Setup

Requirements: Pi-hole **v6** and a machine with Docker Compose that can reach the Pi.

1. **Create a Pi-hole app password.** In the Pi-hole web UI this is under Settings > Web interface / API (Expert mode), "Configure app password". Copy it. (Generating one logs out existing web sessions.)
2. **Configure:**
   ```sh
   cp .env.example .env
   # set PIHOLE_URL, PIHOLE_PASSWORD, TZ and the two database passwords
   ```
3. **Start:**
   ```sh
   docker compose up -d --build
   docker compose logs -f collector   # expect "fetched N queries"
   ```
4. Open Grafana at `http://<server>:3000`, or at the proxy URL if you set one up (see [below](#behind-nginx-at-a-sub-path-optional)). Log in with user `admin` and password `GRAFANA_ADMIN_PASSWORD`. The Overview dashboard is the home page.

On first start the collector pulls the last 24 hours (`INITIAL_HOURS`) from the API.

### Behind nginx at a sub-path (optional)

To serve Grafana at `http://<host>/pi-quiti/` on port 80 through an nginx already running on the host:

1. Add to `.env`:
   ```sh
   GRAFANA_ROOT_URL=http://<host>/pi-quiti/
   GRAFANA_SERVE_FROM_SUB_PATH=true
   GRAFANA_BIND=127.0.0.1
   ```
2. Include [`deploy/nginx/pi-quiti.conf`](deploy/nginx/pi-quiti.conf) inside your `server { }` block, for example by copying it to `/etc/nginx/locations/` and adding `include /etc/nginx/locations/*.conf;` to the block.
3. Apply:
   ```sh
   sudo nginx -t && sudo systemctl reload nginx
   docker compose up -d grafana
   ```

With `GRAFANA_BIND=127.0.0.1`, port 3000 is no longer reachable from the LAN.

### Import history (optional)

Pi-hole keeps up to a year of queries in its database. Copy it over and import it:

```sh
mkdir -p data
scp pi@pihole:/etc/pihole/pihole-FTL.db data/
docker compose run --rm collector backfill /data/pihole-FTL.db --days 365
```

Copy the file first; don't point the importer at the live database, which FTL writes to every minute. Rows with the same Pi-hole query ID and timestamp are skipped, so overlapping ranges are safe.

### Device names

Pi-hole only knows hostnames if something answers reverse lookups for your LAN. The TP-Link router hands out DHCP leases, so in order of effort:

1. Put names in `config/devices.yaml` (keyed by IP). This always works, and the names win over anything Pi-hole reports. Give those devices DHCP reservations on the router so their IPs stay fixed.
2. Enable conditional forwarding in Pi-hole (Settings > DNS) pointing at the router, if the router answers PTR queries.
3. Move DHCP from the router to Pi-hole.

Restart the collector after editing `devices.yaml` (`docker compose restart collector`).

## Dashboards

| Dashboard | Shows |
|---|---|
| **Overview** | Top services, category split, top devices, lookups over time by category, top companies, most blocked, unrecognised domains. Device and category filters. |
| **Device** | One device: its services over time, services table, hour-of-day heatmap, the raw hostnames it looked up. |
| **Service** | One service: which devices use it, and every hostname grouped into it. Use this to check the grouping. |
| **Night watch** | Activity inside quiet hours (default 00:00-06:00, local time, can wrap midnight): busiest services and devices, service x hour and device x hour heatmaps, and root domains seen for the first time in the selected range. |
| **Sessions** | Timeline of estimated activity per device and service, estimated time per service and device, longest sessions. |

Service and device names in the tables link to their drill-down dashboards. The "Background chatter" toggle hides services marked `background: true`.

Panel colors are tuned for Grafana's dark theme. The dashboards are generated by `grafana/build_dashboards.py`; edit that file and re-run it instead of editing the JSON.

## Sessions

A session is a run of lookups for one app on one device with no gap longer than `SESSION_GAP_MINUTES` (default 10). It ends `SESSION_TAIL_MINUTES` (default 2) after the last lookup. Blocked lookups and background services are skipped. The Sessions dashboard hides sessions with fewer than 3 lookups by default.

Expect errors in both directions:

- A long video stream that reuses one connection may make few new lookups, so it can split into several shorter sessions. Raising the gap merges them.
- Apps that poll in the background (phones checking in with Apple or Google, smart speakers) look like use. Mark them `background: true` in the overrides file.

Test the numbers against a known case: stream something on one device for 20 minutes and see what the Sessions dashboard reports.

## Customising the grouping

Edit `config/services.override.yaml`:

```yaml
names:            # prettier display names for v2fly list names
  youtube: YouTube
domains:          # suffix match; the most specific entry wins
  tplinkcloud.com: {app: TP-Link, category: IoT}
  ring.com: {app: Ring, company: Amazon, category: IoT}
background:       # left out of Sessions
  - Reverse DNS
```

Then re-label everything already stored:

```sh
docker compose run --rm collector remap            # uses current lists
docker compose run --rm collector remap --refresh  # re-downloads v2fly first
```

`remap` also rebuilds the session table.

## Commands

```sh
docker compose run --rm collector run            # the default: poll forever
docker compose run --rm collector backfill FILE [--days N]
docker compose run --rm collector remap [--refresh]
docker compose run --rm collector resessionize   # e.g. after changing SESSION_GAP_MINUTES
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PIHOLE_URL` | | e.g. `http://192.168.0.2` |
| `PIHOLE_PASSWORD` | | Pi-hole app password |
| `PIHOLE_VERIFY_TLS` | `true` | `false` for Pi-hole's self-signed HTTPS certificate |
| `TZ` | `UTC` | Local timezone for hour-of-day and quiet-hours panels |
| `POLL_SECONDS` | `60` | How often to pull new queries |
| `INITIAL_HOURS` | `24` | History to pull from the API on first start |
| `SESSION_GAP_MINUTES` | `10` | Max gap inside one session |
| `SESSION_TAIL_MINUTES` | `2` | Time added after a session's last lookup |
| `GRAFANA_ROOT_URL` | Grafana default | Public URL when behind a proxy, e.g. `http://server/pi-quiti/` |
| `GRAFANA_SERVE_FROM_SUB_PATH` | `false` | `true` when `GRAFANA_ROOT_URL` has a sub-path |
| `GRAFANA_BIND` | `0.0.0.0` | Host address for port 3000; `127.0.0.1` restricts it to the proxy |

## Storage and performance

Each query is one row. A busy household makes 20k-100k lookups a day, so a year of history is roughly 7-35 million rows, or about 1-8 GB in Postgres including indexes. Ranges up to a week load quickly. Month-long ranges on the heatmap panels can take several seconds. If that becomes a problem, the next step is an hourly rollup table.

Retention is unbounded. To trim, for example to 180 days:

```sh
docker compose exec postgres psql -U piquiti -c "DELETE FROM dns_query WHERE ts < now() - interval '180 days'"
```

## Alternatives

Research done before building this, in case one of these fits better:

| Option | Groups by service | Bytes / durations | Works with a plain router | Notes |
|---|---|---|---|---|
| NextDNS (cloud) | Root domains | No | Yes | Closest turnkey DNS option; logs leave your network; paid above 300k queries/month |
| AdGuard Home | No (top domains, top clients) | No | Yes | Has a curated services list, used for blocking only |
| Technitium DNS | No | No | Yes | More DNS features, similar stats |
| [Firewalla](https://help.firewalla.com/hc/en-us/articles/24739086338323-Firewalla-Feature-Network-Flows) | Yes, per-device flows | Yes | Yes (Simple mode) | Hardware box; closest to UniFi without replacing the router |
| ntopng with nDPI | Yes | Yes | No: the Pi must become the gateway, or you need a switch with port mirroring | Real DPI |
| OPNsense / OpenWrt / UniFi gateway | Yes | Yes | Replaces the router | The full answer; costs hardware |

Existing Pi-hole projects checked: [pihole6_exporter](https://github.com/bazmonk/pihole6_exporter) with [Grafana dashboard 21043](https://grafana.com/grafana/dashboards/21043-pi-hole-ver6-stats/) (Prometheus aggregates, no grouping), [PiHoleLongTermStats](https://github.com/davistdaniel/PiHoleLongTermStats) (per-client and day/hour heatmaps, no grouping or sessions), and [Pi-hole-Top-Level-Domains](https://github.com/robcmo/Pi-hole-Top-Level-Domains) (root-domain roll-up script). None of them group by service.

## Development

```sh
cd collector
python -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
pytest
```

Layout:

- `collector/piquiti/pihole.py`: Pi-hole v6 API client and status codes
- `collector/piquiti/services.py`: v2fly parsing, app/company/category lookup, overrides
- `collector/piquiti/enrich.py`: root domains, device config
- `collector/piquiti/sessions.py`: session stitching
- `collector/piquiti/db.py`: Postgres writes and the incremental sessionizer
- `collector/piquiti/backfill.py`: FTL database import
- `db/`: schema and views (`v_query`, `v_session`) that Grafana reads, plus the read-only `grafana` role
- `grafana/build_dashboards.py`: generates `grafana/dashboards/*.json`

The schema in `db/` runs only when the Postgres volume is first created. After changing it on an existing install, apply the change by hand or recreate the volume and re-import.
