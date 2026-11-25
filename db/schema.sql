-- ember-gs PostGIS schema
-- Tracks the whole chain: contact -> raw capture -> decoded granule -> fire detection -> alert,
-- plus the FIRMS baseline, so "minutes faster than FIRMS" is a single SQL query.

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE satellites (
    norad_id     INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,          -- 'NOAA-20'
    spacecraft_id INTEGER,                       -- CCSDS SCID in VCDU headers
    aws_satellite_arn TEXT
);

CREATE TABLE ground_stations (
    name         TEXT PRIMARY KEY,               -- 'Cape Town 1'
    aws_region   TEXT NOT NULL,
    location     GEOGRAPHY(POINT, 4326) NOT NULL
);

-- One row per scheduled/executed AWS Ground Station contact
CREATE TABLE contacts (
    contact_id   TEXT PRIMARY KEY,               -- AWS contact id
    norad_id     INTEGER NOT NULL REFERENCES satellites,
    station      TEXT NOT NULL REFERENCES ground_stations,
    status       TEXT NOT NULL,                  -- SCHEDULED | COMPLETED | FAILED ...
    aos          TIMESTAMPTZ NOT NULL,
    los          TIMESTAMPTZ NOT NULL,
    max_elevation_deg REAL,
    est_cost_usd NUMERIC(8,2),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Raw files delivered to S3 by Ground Station
CREATE TABLE captures (
    id           BIGSERIAL PRIMARY KEY,
    contact_id   TEXT REFERENCES contacts,
    s3_uri       TEXT NOT NULL UNIQUE,
    bytes        BIGINT,
    delivered_at TIMESTAMPTZ NOT NULL,           -- S3 event time: when data reached the cloud
    decoded_at   TIMESTAMPTZ,
    cadus        INTEGER,
    rs_corrected INTEGER,
    rs_failed    INTEGER,
    packets      INTEGER
);

-- A geolocated chunk of instrument data (e.g. a few VIIRS scans)
CREATE TABLE granules (
    id           BIGSERIAL PRIMARY KEY,
    capture_id   BIGINT NOT NULL REFERENCES captures,
    instrument   TEXT NOT NULL,                  -- 'VIIRS'
    start_time   TIMESTAMPTZ NOT NULL,
    end_time     TIMESTAMPTZ NOT NULL,
    footprint    GEOGRAPHY(POLYGON, 4326),
    processed_at TIMESTAMPTZ
);

-- Our own fire detections
CREATE TABLE detections (
    id           BIGSERIAL PRIMARY KEY,
    granule_id   BIGINT NOT NULL REFERENCES granules,
    location     GEOGRAPHY(POINT, 4326) NOT NULL,
    observed_at  TIMESTAMPTZ NOT NULL,           -- when the satellite looked
    detected_at  TIMESTAMPTZ NOT NULL,           -- when our pipeline flagged it
    confidence   REAL NOT NULL,                  -- 0..1 from the model
    brightness_temp_k REAL,
    frp_mw       REAL,
    model_version TEXT NOT NULL
);
CREATE INDEX detections_loc ON detections USING GIST (location);
CREATE INDEX detections_time ON detections (observed_at);

CREATE TABLE alerts (
    id           BIGSERIAL PRIMARY KEY,
    detection_id BIGINT NOT NULL REFERENCES detections,
    channel      TEXT NOT NULL,                  -- 'sns' | 'webhook' | ...
    sent_at      TIMESTAMPTZ NOT NULL
);

-- FIRMS baseline: when each FIRMS detection first became visible to us
CREATE TABLE firms_detections (
    key          TEXT PRIMARY KEY,
    source       TEXT NOT NULL,                  -- 'VIIRS_NOAA20_NRT'
    location     GEOGRAPHY(POINT, 4326) NOT NULL,
    observed_at  TIMESTAMPTZ NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    confidence   TEXT,
    frp_mw       REAL
);
CREATE INDEX firms_loc ON firms_detections USING GIST (location);
CREATE INDEX firms_time ON firms_detections (observed_at);

-- The headline number: for each of our detections, the matching FIRMS detection
-- (same satellite overpass, within 1 km) and how much earlier we alerted.
CREATE VIEW head_start AS
SELECT d.id                                   AS detection_id,
       d.observed_at,
       d.detected_at,
       f.first_seen_at                        AS firms_first_seen,
       EXTRACT(EPOCH FROM (f.first_seen_at - d.detected_at)) / 60.0 AS minutes_ahead
FROM detections d
JOIN LATERAL (
    SELECT * FROM firms_detections f
    WHERE ST_DWithin(f.location, d.location, 1000)
      AND abs(EXTRACT(EPOCH FROM (f.observed_at - d.observed_at))) < 600
    ORDER BY f.location <-> d.location
    LIMIT 1
) f ON true;
