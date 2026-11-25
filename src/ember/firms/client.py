"""NASA FIRMS client and latency monitor: the baseline this project has to beat.

FIRMS (Fire Information for Resource Management System) publishes active-fire
detections from VIIRS and MODIS. Each detection carries the time the satellite
*observed* it (acq_date + acq_time) but not when it was *published*. So we poll
the API and record when each detection first appears:

    FIRMS latency = first_seen_by_us - acquisition_time

That gives a measured baseline per region and satellite to compare against our
own ground-station alerts. Poll often (every 5 minutes) or the latency estimate
gets coarser.

Note: FIRMS has an ultra-real-time feed for the US and Canada that already uses
direct-broadcast stations, so the meaningful comparison is in regions without
that coverage (southern Africa, South America, the Middle East, ...).

Needs a free FIRMS MAP_KEY: https://firms.modaps.eosdis.nasa.gov/api/map_key/
"""

from __future__ import annotations

import csv
import io
import sqlite3
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

FIRMS_API = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{area}/{days}"
SOURCES = ("VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "VIIRS_SNPP_NRT", "MODIS_NRT")


@dataclass(frozen=True)
class FireDetection:
    source: str
    latitude: float
    longitude: float
    acquired: datetime
    satellite: str
    confidence: str
    frp_mw: float | None  # fire radiative power
    daynight: str
    bright_ti4_k: float | None = None

    @property
    def key(self) -> str:
        return f"{self.source}|{self.latitude:.5f}|{self.longitude:.5f}|{self.acquired:%Y%m%dT%H%M}"


def _f(row: dict, name: str) -> float | None:
    v = row.get(name)
    return float(v) if v not in (None, "") else None


def parse_csv(text: str, source: str) -> list[FireDetection]:
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        hhmm = row["acq_time"].zfill(4)
        acquired = datetime.strptime(f"{row['acq_date']} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)
        out.append(
            FireDetection(
                source=source,
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
                acquired=acquired,
                satellite=row.get("satellite", ""),
                confidence=str(row.get("confidence", "")),
                frp_mw=_f(row, "frp"),
                daynight=row.get("daynight", ""),
                bright_ti4_k=_f(row, "bright_ti4") or _f(row, "brightness"),
            )
        )
    return out


def fetch(map_key: str, source: str, bbox: tuple[float, float, float, float], days: int = 1) -> list[FireDetection]:
    """bbox = (west, south, east, north) in degrees."""
    area = ",".join(f"{v:g}" for v in bbox)
    url = FIRMS_API.format(key=map_key, source=source, area=area, days=days)
    with urllib.request.urlopen(url, timeout=60) as r:
        text = r.read().decode()
    if not text.startswith("latitude"):
        raise RuntimeError(f"FIRMS error: {text[:200]}")
    return parse_csv(text, source)


SCHEMA = """
CREATE TABLE IF NOT EXISTS firms_detections (
    key          TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    latitude     REAL NOT NULL,
    longitude    REAL NOT NULL,
    acquired_utc TEXT NOT NULL,
    first_seen_utc TEXT NOT NULL,
    latency_s    REAL NOT NULL,
    confidence   TEXT,
    frp_mw       REAL,
    daynight     TEXT
);
CREATE INDEX IF NOT EXISTS firms_acq ON firms_detections(acquired_utc);
"""


class LatencyStore:
    """Records the first time each FIRMS detection is seen (SQLite; PostGIS later)."""

    def __init__(self, path: str = "firms_latency.sqlite") -> None:
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def record(self, detections: list[FireDetection], seen: datetime | None = None) -> int:
        seen = seen or datetime.now(timezone.utc)
        new = 0
        for d in detections:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO firms_detections VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    d.key,
                    d.source,
                    d.latitude,
                    d.longitude,
                    d.acquired.isoformat(),
                    seen.isoformat(),
                    (seen - d.acquired).total_seconds(),
                    d.confidence,
                    d.frp_mw,
                    d.daynight,
                ),
            )
            new += cur.rowcount
        self.db.commit()
        return new

    def latency_stats(self, source: str | None = None) -> dict[str, float]:
        q = "SELECT latency_s FROM firms_detections"
        args: tuple = ()
        if source:
            q += " WHERE source = ?"
            args = (source,)
        vals = sorted(r[0] for r in self.db.execute(q, args))
        if not vals:
            return {"n": 0}

        def pct(p: float) -> float:
            return vals[min(len(vals) - 1, int(p * len(vals)))] / 60.0

        return {"n": len(vals), "p10_min": pct(0.10), "median_min": pct(0.5), "p90_min": pct(0.9)}
