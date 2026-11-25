"""Fetch current orbital elements (TLEs) for the direct-broadcast satellites.

Primary source: NOAA's own TLE-AUX files in the public JPSS buckets on AWS
(NOAA Open Data Dissemination). These are the elements NOAA uses operationally,
need no account, and sit in the same cloud as the rest of the system.
Fallback: CelesTrak's GP API.
"""

from __future__ import annotations

import io
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SATELLITES = {
    # name: (NORAD id, NOAA bucket, platform code)
    "NOAA-20": (43013, "noaa-nesdis-n20-pds", "j01"),
    "NOAA-21": (54234, "noaa-nesdis-n21-pds", "j02"),
    "SNPP": (37849, "noaa-nesdis-snpp-pds", "npp"),
    "AQUA": (27424, None, None),
}

_S3 = "https://{bucket}.s3.amazonaws.com"


@dataclass(frozen=True)
class TLE:
    name: str
    line1: str
    line2: str
    source: str

    @property
    def norad_id(self) -> int:
        return int(self.line1[2:7])

    @property
    def epoch(self) -> datetime:
        yy = int(self.line1[18:20])
        day = float(self.line1[20:32])
        year = 2000 + yy if yy < 57 else 1900 + yy
        return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=day - 1)


def _get(url: str, timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ember-gs/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_tle_text(name: str, text: str, source: str) -> TLE:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    l1 = next(ln for ln in lines if ln.startswith("1 "))
    l2 = next(ln for ln in lines if ln.startswith("2 "))
    return TLE(name, l1, l2, source)


def from_noaa_s3(name: str, now: datetime | None = None, lookback_days: int = 45) -> TLE:
    """Newest TLE-AUX file from NOAA's public bucket (an HDF5 file holding the two lines)."""
    import h5py

    _, bucket, _ = SATELLITES[name]
    if bucket is None:
        raise ValueError(f"no NOAA bucket for {name}")
    now = now or datetime.now(timezone.utc)
    base = _S3.format(bucket=bucket)
    for back in range(lookback_days):
        day = now - timedelta(days=back)
        prefix = f"TLE-AUX/{day:%Y/%m/%d}/"
        listing = _get(f"{base}/?list-type=2&prefix={prefix}").decode()
        keys = sorted(re.findall(r"<Key>([^<]+\.h5)</Key>", listing))
        if keys:
            raw = _get(f"{base}/{keys[-1]}")
            with h5py.File(io.BytesIO(raw), "r") as f:
                text = bytes(f["All_Data/TLE-AUX_All/Dataset_Array_0"][:]).decode()
            return parse_tle_text(name, text, f"s3://{bucket}/{keys[-1]}")
    raise LookupError(f"no TLE-AUX for {name} in the last {lookback_days} days")


def from_celestrak(name: str) -> TLE:
    norad = SATELLITES[name][0]
    text = _get(f"https://celestrak.org/NORAD/elements/gp.php?CATNR={norad}&FORMAT=TLE").decode()
    return parse_tle_text(name, text, "celestrak")


def get_tle(name: str) -> TLE:
    errors = []
    for fetch in (from_noaa_s3, from_celestrak):
        try:
            return fetch(name)
        except Exception as e:  # noqa: BLE001 - try the next source
            errors.append(f"{fetch.__name__}: {e}")
    raise RuntimeError(f"could not get a TLE for {name}: " + "; ".join(errors))
