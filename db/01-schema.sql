-- Pi-quiti schema. Runs once when the postgres volume is first created.

-- Local timezone for hour-of-day panels. The collector overwrites this from
-- $TZ on startup; new connections pick it up.
ALTER DATABASE piquiti SET piquiti.tz = 'UTC';

-- One row per DNS query, as reported by Pi-hole.
CREATE TABLE dns_query (
    id        bigint      NOT NULL,          -- Pi-hole query id
    ts        timestamptz NOT NULL,
    seq       bigserial,                     -- insertion order, drives the sessionizer
    client_ip inet        NOT NULL,
    domain    text        NOT NULL,
    qtype     text,
    status    text,
    blocked   boolean     NOT NULL,
    PRIMARY KEY (id, ts)
);
CREATE INDEX dns_query_ts ON dns_query (ts);
CREATE INDEX dns_query_client_ts ON dns_query (client_ip, ts);
CREATE INDEX dns_query_domain_ts ON dns_query (domain, ts);
CREATE INDEX dns_query_seq ON dns_query (seq);

-- Enrichment per hostname. Re-labelling = update this table, not dns_query.
CREATE TABLE domain_info (
    domain      text PRIMARY KEY,
    root_domain text NOT NULL,
    app         text,
    company     text,
    category    text,
    background  boolean NOT NULL DEFAULT false,
    source      text NOT NULL,               -- v2fly | override | none
    first_seen  timestamptz NOT NULL
);
CREATE INDEX domain_info_root ON domain_info (root_domain);

CREATE TABLE device (
    client_ip inet PRIMARY KEY,
    name      text NOT NULL,
    owner     text,
    source    text NOT NULL                  -- config | pihole | ip
);

-- Estimated activity windows per device and app (see collector/piquiti/sessions.py).
CREATE TABLE session (
    client_ip   inet        NOT NULL,
    service     text        NOT NULL,
    start_ts    timestamptz NOT NULL,
    end_ts      timestamptz NOT NULL,
    last_query  timestamptz NOT NULL,
    query_count integer     NOT NULL,
    PRIMARY KEY (client_ip, service, start_ts)
);
CREATE INDEX session_end ON session (end_ts);

CREATE TABLE collector_state (
    key   text PRIMARY KEY,
    value text NOT NULL
);

-- Hour of day in the configured local timezone.
CREATE FUNCTION local_hour(t timestamptz) RETURNS int
LANGUAGE sql STABLE AS $$
    SELECT extract(hour FROM t AT TIME ZONE coalesce(current_setting('piquiti.tz', true), 'UTC'))::int
$$;

-- True when t falls inside [start_h, end_h), wrapping past midnight.
CREATE FUNCTION in_quiet_hours(t timestamptz, start_h int, end_h int) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN start_h <= end_h
                THEN local_hour(t) >= start_h AND local_hour(t) < end_h
                ELSE local_hour(t) >= start_h OR local_hour(t) < end_h END
$$;

-- What Grafana queries. Unmapped domains fall back to their root domain.
CREATE VIEW v_query AS
SELECT q.ts,
       q.client_ip,
       coalesce(d.name, host(q.client_ip))       AS device,
       d.owner,
       q.domain,
       i.root_domain,
       coalesce(i.app, i.root_domain, q.domain)  AS service,
       coalesce(i.company, i.app, i.root_domain) AS company,
       coalesce(i.category, 'Uncategorized')     AS category,
       coalesce(i.background, false)             AS background,
       i.source = 'none'                         AS unmapped,
       q.qtype,
       q.status,
       q.blocked
FROM dns_query q
LEFT JOIN domain_info i ON i.domain = q.domain
LEFT JOIN device d ON d.client_ip = q.client_ip;

CREATE VIEW v_session AS
SELECT s.client_ip,
       coalesce(d.name, host(s.client_ip)) AS device,
       s.service,
       s.start_ts,
       s.end_ts,
       s.end_ts - s.start_ts AS duration,
       s.query_count
FROM session s
LEFT JOIN device d ON d.client_ip = s.client_ip;
