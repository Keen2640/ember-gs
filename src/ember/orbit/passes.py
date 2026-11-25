"""Predict satellite passes over AWS Ground Station antennas, with a cost estimate.

A pass is the window where the satellite is above the antenna's elevation mask.
AWS bills the whole contact: the pass plus the pre-pass and post-pass padding
set in your mission profile. Choosing passes well (high elevation for a strong
signal, over a region where you want fresh fire data) matters a lot when each
one costs real money.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from skyfield.api import EarthSatellite, load, wgs84

from .stations import GroundStation
from .tle import TLE

_ts = load.timescale()

# On-demand narrowband price per minute in USD. AWS doesn't publish a single
# number on its pricing page: check the AWS Pricing Calculator for your region
# and pass the real value with --price-per-minute.
DEFAULT_PRICE_PER_MIN = 10.0


@dataclass(frozen=True)
class Pass:
    satellite: str
    station: str
    region: str
    aos: datetime  # acquisition of signal (rises above the mask)
    tca: datetime  # time of closest approach (max elevation)
    los: datetime  # loss of signal
    max_elevation_deg: float

    @property
    def duration_s(self) -> float:
        return (self.los - self.aos).total_seconds()

    def billed_minutes(self, pre_pass_s: int = 120, post_pass_s: int = 60) -> float:
        return (self.duration_s + pre_pass_s + post_pass_s) / 60.0

    def cost_usd(self, price_per_min: float = DEFAULT_PRICE_PER_MIN, pre_pass_s: int = 120, post_pass_s: int = 60) -> float:
        return self.billed_minutes(pre_pass_s, post_pass_s) * price_per_min


def predict_passes(
    tle: TLE,
    stations: tuple[GroundStation, ...] | list[GroundStation],
    start: datetime | None = None,
    hours: float = 24,
    min_elevation_deg: float = 10.0,
) -> list[Pass]:
    sat = EarthSatellite(tle.line1, tle.line2, tle.name, _ts)
    start = start or datetime.now(timezone.utc)
    t0 = _ts.from_datetime(start)
    t1 = _ts.from_datetime(start + timedelta(hours=hours))
    passes: list[Pass] = []
    for gs in stations:
        site = wgs84.latlon(gs.lat, gs.lon, elevation_m=gs.elevation_m)
        times, events = sat.find_events(site, t0, t1, altitude_degrees=min_elevation_deg)
        aos = tca = None
        max_el = 0.0
        for t, ev in zip(times, events):
            if ev == 0:
                aos, tca, max_el = t, None, 0.0
            elif ev == 1 and aos is not None:
                tca = t
                alt, _, _ = (sat - site).at(t).altaz()
                max_el = float(alt.degrees)
            elif ev == 2 and aos is not None and tca is not None:
                passes.append(
                    Pass(
                        tle.name,
                        gs.name,
                        gs.region,
                        aos.utc_datetime(),
                        tca.utc_datetime(),
                        t.utc_datetime(),
                        round(max_el, 1),
                    )
                )
                aos = tca = None
    passes.sort(key=lambda p: p.aos)
    return passes
