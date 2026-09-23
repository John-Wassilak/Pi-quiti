"""Generate the Grafana dashboard JSON in grafana/dashboards/.

    python3 grafana/build_dashboards.py

Edit panels here rather than in the JSON, then re-run. Grafana picks up the
files on its next provisioning scan (about 10s).
"""

from __future__ import annotations

import json
from pathlib import Path

DS = {"type": "grafana-postgresql-datasource", "uid": "piquiti-pg"}
OUT = Path(__file__).parent / "dashboards"

# Shared filters. Device and category variables are multi-select with "All".
F_DEVICE = "device IN (${device:sqlstring})"
F_CATEGORY = "category IN (${category:sqlstring})"
F_TIME = "$__timeFilter(ts)"
F_BG = "(${include_background} OR NOT background)"
F_SESSION = "end_ts > $__timeFrom() AND start_ts < $__timeTo() AND query_count >= ${min_queries}"


# Fixed color per category, shared by every panel so a category keeps its
# color. Eight slots, validated for the dark theme (CVD and contrast);
# smaller categories fold into "Other".
CATEGORY_COLORS = {
    "Tech Platforms": "#3987e5",
    "Streaming & Entertainment": "#d95926",
    "Social": "#199e70",
    "Ads & Tracking": "#c98500",
    "Gaming": "#d55181",
    "IoT": "#008300",
    "Communication": "#9085e9",
    "AI": "#e66767",
    "Other": "#8a8984",
}
MAIN_CATEGORIES = ", ".join(f"'{c}'" for c in CATEGORY_COLORS if c != "Other")
CATEGORY_GROUP = f"CASE WHEN category IN ({MAIN_CATEGORIES}) THEN category ELSE 'Other' END"
CATEGORY_OVERRIDES = [
    {"matcher": {"id": "byName", "options": c},
     "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": hexcolor}}]}
    for c, hexcolor in CATEGORY_COLORS.items()
]


def link(uid: str, var: str, title: str) -> list[dict]:
    return [{"title": title, "url": f"/d/{uid}?var-{var}=${{__value.raw}}&${{__url_time_range}}"}]


class Dash:
    def __init__(self, uid: str, title: str, description: str):
        self.uid, self.title, self.description = uid, title, description
        self.panels: list[dict] = []
        self.vars: list[dict] = []
        self.y = 0
        self.x = 0
        self.row_h = 0

    # layout: panels flow left to right on a 24-column grid
    def _pos(self, w: int, h: int) -> dict:
        if self.x + w > 24:
            self.x, self.y = 0, self.y + self.row_h
            self.row_h = 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_h = max(self.row_h, h)
        return pos

    def add(self, panel: dict, w: int, h: int) -> None:
        panel["id"] = len(self.panels) + 1
        panel["gridPos"] = self._pos(w, h)
        panel.setdefault("datasource", DS)
        self.panels.append(panel)

    def json(self) -> dict:
        return {
            "uid": self.uid,
            "title": self.title,
            "description": self.description,
            "tags": ["pi-quiti"],
            "timezone": "browser",
            "schemaVersion": 41,
            "time": {"from": "now-24h", "to": "now"},
            "refresh": "5m",
            "graphTooltip": 1,
            "templating": {"list": self.vars},
            "links": [
                {"title": "Pi-quiti", "type": "dashboards", "tags": ["pi-quiti"], "asDropdown": True, "includeVars": False, "keepTime": True}
            ],
            "panels": self.panels,
        }


# ---------- variables ----------

def v_query(name: str, label: str, sql: str, multi: bool = True) -> dict:
    return {
        "name": name, "label": label, "type": "query", "datasource": DS, "query": sql, "definition": sql,
        "refresh": 1, "sort": 1, "multi": multi, "includeAll": multi,
        "current": {"text": "All", "value": "$__all"} if multi else {},
    }


def v_text(name: str, label: str, default: str) -> dict:
    return {"name": name, "label": label, "type": "textbox", "query": default,
            "current": {"text": default, "value": default}}


def v_bool(name: str, label: str, default: str) -> dict:
    return {"name": name, "label": label, "type": "custom", "query": "false,true",
            "current": {"text": default, "value": default},
            "options": [{"text": v, "value": v, "selected": v == default} for v in ("false", "true")]}


V_DEVICE = v_query("device", "Device", "SELECT name FROM device ORDER BY 1")
V_CATEGORY = v_query("category", "Category",
                     "SELECT DISTINCT coalesce(category, 'Uncategorized') FROM domain_info ORDER BY 1")
V_BG = v_bool("include_background", "Background chatter", "true")
V_MINQ = v_text("min_queries", "Min lookups per session", "3")


# ---------- panels ----------

def target(sql: str, fmt: str = "table") -> list[dict]:
    return [{"refId": "A", "datasource": DS, "format": fmt, "rawQuery": True, "editorMode": "code", "rawSql": sql.strip()}]


def stat(title: str, sql: str, unit: str = "short", description: str = "") -> dict:
    return {
        "type": "stat", "title": title, "description": description, "targets": target(sql),
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "none", "graphMode": "none", "textMode": "value", "justifyMode": "center"},
        "fieldConfig": {"defaults": {"unit": unit, "decimals": 1 if unit == "percent" else 0}, "overrides": []},
    }


def bars(title: str, sql: str, unit: str = "short", links: list | None = None, description: str = "") -> dict:
    """Horizontal ranked bars, one measure, one color."""
    return {
        "type": "bargauge", "title": title, "description": description, "targets": target(sql),
        "options": {"orientation": "horizontal", "displayMode": "basic", "valueMode": "text",
                    "showUnfilled": True, "namePlacement": "left", "sizing": "manual", "minVizHeight": 18,
                    "maxVizHeight": 22, "reduceOptions": {"calcs": [], "fields": "/^value$/", "values": True}},
        "fieldConfig": {"defaults": {"unit": unit, "color": {"mode": "fixed", "fixedColor": "blue"},
                                     "min": 0, "links": links or []}, "overrides": []},
    }


def donut(title: str, sql: str, overrides: list | None = None) -> dict:
    return {
        "type": "piechart", "title": title, "targets": target(sql),
        "options": {"pieType": "donut", "reduceOptions": {"calcs": [], "fields": "/^value$/", "values": True},
                    "legend": {"displayMode": "table", "placement": "right", "values": ["percent"]},
                    "tooltip": {"mode": "single"}},
        "fieldConfig": {"defaults": {"color": {"mode": "palette-classic"}}, "overrides": overrides or []},
    }


def stacked(title: str, sql: str, description: str = "", overrides: list | None = None) -> dict:
    """Stacked bars over time; series colored by name so a category keeps its color."""
    return {
        "type": "timeseries", "title": title, "description": description, "targets": target(sql, "time_series"),
        "maxDataPoints": 150, "interval": "5m",
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi", "sort": "desc"}},
        "fieldConfig": {"defaults": {
            "color": {"mode": "palette-classic-by-name"},
            "custom": {"drawStyle": "bars", "fillOpacity": 80, "lineWidth": 0, "stacking": {"mode": "normal"},
                       "barAlignment": 0, "showPoints": "never"},
            "unit": "short", "min": 0}, "overrides": overrides or []},
    }


def table(title: str, sql: str, overrides: list | None = None, description: str = "") -> dict:
    return {
        "type": "table", "title": title, "description": description, "targets": target(sql),
        "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
        "fieldConfig": {"defaults": {"custom": {"filterable": True}}, "overrides": overrides or []},
    }


def col(name: str, **props) -> dict:
    return {"matcher": {"id": "byName", "options": name}, "properties": [{"id": k, "value": v} for k, v in props.items()]}


def text(title: str, md: str) -> dict:
    return {"type": "text", "title": title, "options": {"mode": "markdown", "content": md}, "datasource": None}


def hour_heatmap(title: str, where: str, row_expr: str, row_label: str, limit: int = 20) -> dict:
    """Rows x 24 hour columns, cells shaded on one sequential hue."""
    # Aggregate by hour first so local_hour() runs once per row, not 24 times.
    hours = ",\n  ".join(f'sum(n) FILTER (WHERE h = {h}) AS "{h:02d}"' for h in range(24))
    sql = f"""
SELECT r AS "{row_label}",
  {hours}
FROM (SELECT {row_expr} AS r, local_hour(ts) AS h, count(*) AS n FROM v_query WHERE {where} GROUP BY 1, 2) x
GROUP BY 1 ORDER BY sum(n) DESC LIMIT {limit}"""
    return {
        "type": "table", "title": title, "targets": target(sql),
        "description": "Lookups per hour of day (local time), summed over the selected range. Darker = busier.",
        "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
        "fieldConfig": {
            "defaults": {"custom": {"cellOptions": {"type": "color-background", "mode": "basic"}, "align": "center",
                                    "width": 44},
                         "color": {"mode": "continuous-blues"}, "min": 0},
            "overrides": [col(row_label, **{"custom.cellOptions": {"type": "auto"}, "custom.width": 200,
                                            "custom.align": "left"})],
        },
    }


# ---------- dashboards ----------

def overview() -> Dash:
    d = Dash("piquiti-overview", "Pi-quiti / Overview",
             "What the network talked to, grouped into services. Counts are DNS lookups, not bytes.")
    d.vars = [V_DEVICE, V_CATEGORY]
    base = f"{F_TIME} AND {F_DEVICE} AND {F_CATEGORY}"
    d.add(stat("Lookups", f"SELECT count(*) FROM v_query WHERE {base}"), 6, 4)
    d.add(stat("Blocked", f"SELECT 100.0 * count(*) FILTER (WHERE blocked) / greatest(count(*), 1) FROM v_query WHERE {base}",
               unit="percent"), 6, 4)
    d.add(stat("Active devices", f"SELECT count(DISTINCT client_ip) FROM v_query WHERE {base}"), 6, 4)
    d.add(stat("Services", f"SELECT count(DISTINCT service) FROM v_query WHERE {base} AND NOT blocked"), 6, 4)
    d.add(bars("Top services",
               f"SELECT service AS name, count(*) AS value FROM v_query WHERE {base} AND NOT blocked"
               " GROUP BY 1 ORDER BY 2 DESC LIMIT 15",
               links=link("piquiti-service", "service", "Open service")), 10, 14)
    d.add(donut("Categories",
                f"SELECT {CATEGORY_GROUP} AS name, count(*) AS value FROM v_query WHERE {base} AND NOT blocked"
                " GROUP BY 1 ORDER BY 2 DESC", overrides=CATEGORY_OVERRIDES), 7, 14)
    d.add(bars("Top devices",
               f"SELECT device AS name, count(*) AS value FROM v_query WHERE {base} GROUP BY 1 ORDER BY 2 DESC LIMIT 15",
               links=link("piquiti-device", "device", "Open device")), 7, 14)
    d.add(stacked("Lookups over time by category",
                  f"SELECT $__timeGroupAlias(ts, $__interval), {CATEGORY_GROUP} AS metric, count(*) AS value"
                  f" FROM v_query WHERE {base} AND NOT blocked GROUP BY 1, 2 ORDER BY 1",
                  overrides=CATEGORY_OVERRIDES), 24, 9)
    d.add(table("Top companies",
                f"SELECT company AS \"Company\", count(*) AS \"Lookups\", count(DISTINCT service) AS \"Services\","
                f" count(DISTINCT client_ip) AS \"Devices\" FROM v_query WHERE {base} AND NOT blocked"
                " GROUP BY 1 ORDER BY 2 DESC LIMIT 25"), 8, 10)
    d.add(table("Most blocked",
                f"SELECT service AS \"Service\", root_domain AS \"Root domain\", count(*) AS \"Blocked\""
                f" FROM v_query WHERE {base} AND blocked GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 25"), 8, 10)
    d.add(table("Unrecognised root domains",
                f"SELECT root_domain AS \"Root domain\", count(*) AS \"Lookups\", count(DISTINCT client_ip) AS \"Devices\""
                f" FROM v_query WHERE {F_TIME} AND {F_DEVICE} AND unmapped GROUP BY 1 ORDER BY 2 DESC LIMIT 25",
                description="Not in the v2fly lists. Add the noisy ones to config/services.override.yaml."), 8, 10)
    return d


def device() -> Dash:
    d = Dash("piquiti-device", "Pi-quiti / Device", "One device: which services it uses, when, and the raw hostnames behind them.")
    d.vars = [v_query("device", "Device", "SELECT name FROM device ORDER BY 1", multi=False), V_BG]
    base = f"{F_TIME} AND device = '${{device}}' AND {F_BG}"
    d.add(stat("Lookups", f"SELECT count(*) FROM v_query WHERE {base}"), 6, 4)
    d.add(stat("Blocked", f"SELECT 100.0 * count(*) FILTER (WHERE blocked) / greatest(count(*), 1) FROM v_query WHERE {base}",
               unit="percent"), 6, 4)
    d.add(stat("Services", f"SELECT count(DISTINCT service) FROM v_query WHERE {base} AND NOT blocked"), 6, 4)
    d.add(stat("Est. time on services",
               f"SELECT coalesce(extract(epoch FROM sum(duration)), 0) FROM v_session"
               f" WHERE device = '${{device}}' AND end_ts > $__timeFrom() AND start_ts < $__timeTo() AND query_count >= 3",
               unit="s", description="Sum of estimated session lengths across services; overlapping services each count."),
          6, 4)
    d.add(stacked("Top services over time (others folded into Other)", f"""
WITH top AS (
  SELECT service FROM v_query WHERE {base} AND NOT blocked GROUP BY 1 ORDER BY count(*) DESC LIMIT 8
)
SELECT $__timeGroupAlias(ts, $__interval),
       CASE WHEN service IN (SELECT service FROM top) THEN service ELSE 'Other' END AS metric,
       count(*) AS value
FROM v_query WHERE {base} AND NOT blocked GROUP BY 1, 2 ORDER BY 1"""), 24, 9)
    d.add(table("Services", f"""
SELECT service AS "Service", company AS "Company", category AS "Category",
       count(*) AS "Lookups", count(*) FILTER (WHERE blocked) AS "Blocked",
       min(ts) AS "First", max(ts) AS "Last"
FROM v_query WHERE {base} GROUP BY 1, 2, 3 ORDER BY 4 DESC""",
                overrides=[col("Service", links=link("piquiti-service", "service", "Open service"))]), 24, 10)
    d.add(hour_heatmap("Hour of day", base, "service", "Service", limit=15), 24, 12)
    d.add(table("Raw hostnames", f"""
SELECT domain AS "Hostname", service AS "Service", count(*) AS "Lookups", bool_or(blocked) AS "Blocked"
FROM v_query WHERE {base} GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 200"""), 24, 10)
    return d


def service() -> Dash:
    d = Dash("piquiti-service", "Pi-quiti / Service",
             "One service: who uses it, and which hostnames were grouped into it (use this to check the grouping).")
    d.vars = [v_query("service", "Service", "SELECT DISTINCT coalesce(app, root_domain) FROM domain_info ORDER BY 1",
                      multi=False), V_DEVICE]
    base = f"{F_TIME} AND service = '${{service}}' AND {F_DEVICE}"
    d.add(stat("Lookups", f"SELECT count(*) FROM v_query WHERE {base}"), 6, 4)
    d.add(stat("Devices", f"SELECT count(DISTINCT client_ip) FROM v_query WHERE {base}"), 6, 4)
    d.add(stat("Hostnames", f"SELECT count(DISTINCT domain) FROM v_query WHERE {base}"), 6, 4)
    d.add(stat("Blocked", f"SELECT 100.0 * count(*) FILTER (WHERE blocked) / greatest(count(*), 1) FROM v_query WHERE {base}",
               unit="percent"), 6, 4)
    d.add(stacked("Lookups over time by device",
                  f"SELECT $__timeGroupAlias(ts, $__interval), device AS metric, count(*) AS value"
                  f" FROM v_query WHERE {base} GROUP BY 1, 2 ORDER BY 1"), 24, 9)
    d.add(table("Devices", f"""
SELECT device AS "Device", count(*) AS "Lookups", min(ts) AS "First", max(ts) AS "Last"
FROM v_query WHERE {base} GROUP BY 1 ORDER BY 2 DESC""",
                overrides=[col("Device", links=link("piquiti-device", "device", "Open device"))]), 10, 12)
    d.add(table("Hostnames grouped into this service", f"""
SELECT q.domain AS "Hostname", q.root_domain AS "Root domain", i.source AS "Matched by",
       count(*) AS "Lookups", min(i.first_seen) AS "First seen"
FROM v_query q JOIN domain_info i USING (domain)
WHERE {base.replace('ts)', 'q.ts)')} GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 500"""), 14, 12)
    return d


def night() -> Dash:
    d = Dash("piquiti-night", "Pi-quiti / Night watch",
             "What talks while everyone is asleep. Quiet hours are local time; the window may wrap midnight.")
    d.vars = [v_text("quiet_start", "Quiet from (hour)", "0"), v_text("quiet_end", "Quiet until (hour)", "6"),
              V_DEVICE, V_BG]
    d.vars[0]["description"] = "0-23, local time"
    base = f"{F_TIME} AND {F_DEVICE} AND {F_BG} AND in_quiet_hours(ts, ${{quiet_start}}, ${{quiet_end}})"
    d.add(stat("Lookups in quiet hours", f"SELECT count(*) FROM v_query WHERE {base}"), 6, 4)
    d.add(stat("Share of all lookups",
               f"SELECT 100.0 * count(*) FILTER (WHERE in_quiet_hours(ts, ${{quiet_start}}, ${{quiet_end}}))"
               f" / greatest(count(*), 1) FROM v_query WHERE {F_TIME} AND {F_DEVICE} AND {F_BG}", unit="percent"), 6, 4)
    d.add(stat("Devices awake", f"SELECT count(DISTINCT client_ip) FROM v_query WHERE {base}"), 6, 4)
    d.add(stat("Blocked in quiet hours", f"SELECT count(*) FROM v_query WHERE {base} AND blocked"), 6, 4)
    d.add(bars("Busiest services at night",
               f"SELECT service AS name, count(*) AS value FROM v_query WHERE {base} AND NOT blocked"
               " GROUP BY 1 ORDER BY 2 DESC LIMIT 15",
               links=link("piquiti-service", "service", "Open service")), 12, 12)
    d.add(table("Devices at night", f"""
SELECT device AS "Device", count(*) AS "Lookups", count(DISTINCT service) AS "Services",
       mode() WITHIN GROUP (ORDER BY service) AS "Most looked up", max(ts) AS "Latest"
FROM v_query WHERE {base} GROUP BY 1 ORDER BY 2 DESC""",
                overrides=[col("Device", links=link("piquiti-device", "device", "Open device"))]), 12, 12)
    d.add(hour_heatmap("Service x hour of day (whole day, for contrast)",
                       f"{F_TIME} AND {F_DEVICE} AND {F_BG} AND NOT blocked", "service", "Service"), 24, 14)
    d.add(hour_heatmap("Device x hour of day", f"{F_TIME} AND {F_DEVICE} AND {F_BG}", "device", "Device", limit=30), 24, 10)
    d.add(table("New root domains first seen in this range", """
SELECT root_domain AS "Root domain", service AS "Service", category AS "Category", first_seen AS "First seen"
FROM (
  SELECT root_domain, min(coalesce(app, root_domain)) AS service, min(category) AS category,
         min(first_seen) AS first_seen
  FROM domain_info GROUP BY 1
) x
WHERE $__timeFilter(first_seen)
ORDER BY 4 DESC LIMIT 200""", description="A new IoT device phoning home usually shows up here first."), 24, 10)
    return d


def sessions() -> Dash:
    d = Dash("piquiti-sessions", "Pi-quiti / Sessions",
             "Estimated activity windows. A session is a run of lookups for one service on one device "
             "with no gap longer than SESSION_GAP_MINUTES.")
    d.vars = [V_DEVICE, v_query("service", "Service", "SELECT DISTINCT service FROM session ORDER BY 1"), V_MINQ]
    where = f"{F_SESSION} AND {F_DEVICE} AND service IN (${{service:sqlstring}})"
    d.add(text("How to read this", (
        "Pi-hole sees **DNS lookups**, not connections. Sessions are stitched from bursts of lookups, so they are "
        "estimates: a long video stream that keeps reusing a connection can look shorter, and apps that poll in the "
        "background can look like use. Blocked lookups and services flagged `background` in "
        "`config/services.override.yaml` are excluded.")), 24, 3)
    d.add({
        "type": "state-timeline", "title": "Timeline (top 25 device / service pairs by time)",
        "targets": target(f"""
WITH s AS (SELECT * FROM v_session WHERE {where}),
top AS (SELECT device, service FROM s GROUP BY 1, 2 ORDER BY sum(duration) DESC LIMIT 25)
SELECT t AS time, metric, value FROM (
  SELECT greatest(start_ts, $__timeFrom()) AS t, device || ' / ' || service AS metric, 1 AS value FROM s JOIN top USING (device, service)
  UNION ALL
  SELECT least(end_ts, $__timeTo()), device || ' / ' || service, 0 FROM s JOIN top USING (device, service)
) x ORDER BY 1""", "time_series"),
        "options": {"showValue": "never", "rowHeight": 0.8, "mergeValues": True, "alignValue": "left",
                    "legend": {"showLegend": False}, "tooltip": {"mode": "single"}},
        "fieldConfig": {"defaults": {
            "custom": {"fillOpacity": 85, "lineWidth": 0, "spanNulls": True},
            "color": {"mode": "thresholds"},
            "thresholds": {"mode": "absolute", "steps": [{"color": "transparent", "value": None},
                                                         {"color": "blue", "value": 1}]},
            "mappings": [{"type": "value", "options": {"0": {"text": "idle", "color": "transparent"},
                                                       "1": {"text": "active", "color": "blue"}}}],
        }, "overrides": []},
    }, 24, 16)
    d.add(bars("Estimated time by service",
               f"SELECT service AS name, extract(epoch FROM sum(duration)) AS value FROM v_session WHERE {where}"
               " GROUP BY 1 ORDER BY 2 DESC LIMIT 12", unit="s"), 12, 12)
    d.add(bars("Estimated time by device",
               f"SELECT device AS name, extract(epoch FROM sum(duration)) AS value FROM v_session WHERE {where}"
               " GROUP BY 1 ORDER BY 2 DESC LIMIT 15", unit="s",
               description="Services overlap, so this can exceed wall-clock time."), 12, 12)
    d.add(table("Longest sessions", f"""
SELECT device AS "Device", service AS "Service", start_ts AS "Start", end_ts AS "End",
       extract(epoch FROM duration) AS "Duration", query_count AS "Lookups"
FROM v_session WHERE {where} ORDER BY duration DESC LIMIT 100""",
                overrides=[col("Duration", unit="s")]), 24, 12)
    return d


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, build in [("overview", overview), ("device", device), ("service", service), ("night", night),
                        ("sessions", sessions)]:
        (OUT / f"{name}.json").write_text(json.dumps(build().json(), indent=2) + "\n")
        print(f"wrote {name}.json")
