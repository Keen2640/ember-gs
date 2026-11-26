"""ERCOT (Texas grid) data: real-time prices, historical prices, live fuel mix.

Prices are ERCOT's real-time Settlement Point Prices (SPP) in USD/MWh at
15-minute resolution. Dallas homes sit in load zone ``LZ_NORTH``.

Sources (all public, no account needed):
  * live prices:  https://www.ercot.com/content/cdr/html/real_time_spp.html
  * yearly 15-min price archive: MIS report 13061 ("RTMLZHBSPP_<year>.zip")
  * live 5-min fuel mix: https://www.ercot.com/api/1/services/read/dashboards/fuel-mix.json

ERCOT timestamps are Central Prevailing Time with "hour ending" conventions.
Delivery hour 1 covers 00:00-01:00, and on the autumn DST day the repeated
hour is flagged. Everything here is converted to UTC interval *start* times,
so downstream code never has to think about DST.
"""

from __future__ import annotations

import io
import re
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

CENTRAL = ZoneInfo("America/Chicago")
BASE = "https://www.ercot.com"
LIVE_SPP_URL = f"{BASE}/content/cdr/html/real_time_spp.html"
FUEL_MIX_URL = f"{BASE}/api/1/services/read/dashboards/fuel-mix.json"
REPORT_LIST_URL = f"{BASE}/misapp/GetReports.do?reportTypeId={{report_id}}"
HISTORICAL_RTM_REPORT = 13061
INTERVAL = timedelta(minutes=15)


def _get(url: str, timeout: float = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ember-gs/0.2 (research)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


@dataclass
class PriceSeries:
    """Regular 15-minute price series for one settlement point."""

    point: str
    start_utc: np.ndarray  # datetime64[s], interval start, UTC
    price: np.ndarray  # USD/MWh, float64

    def __len__(self) -> int:
        return len(self.price)

    def save(self, path: str) -> None:
        np.savez_compressed(path, point=self.point, start_utc=self.start_utc, price=self.price)

    @classmethod
    def load(cls, path: str) -> PriceSeries:
        d = np.load(path, allow_pickle=False)
        return cls(str(d["point"]), d["start_utc"], d["price"])


# --- historical archive --------------------------------------------------------

def interval_start_utc(date_mdy: str, hour_ending: int, interval: int, repeated: bool) -> datetime:
    """Convert ERCOT (date, hour ending 1-24, interval 1-4, DST repeat flag) to a UTC start time."""
    m, d, y = (int(x) for x in date_mdy.split("/"))
    # aware + timedelta is wall-clock arithmetic in CENTRAL; fold picks the 2nd pass of a repeated hour
    local = datetime(y, m, d, tzinfo=CENTRAL) + timedelta(hours=hour_ending - 1, minutes=15 * (interval - 1))
    local = local.replace(fold=1 if repeated else 0)
    return local.astimezone(timezone.utc)


def default_point_type(point: str) -> str | None:
    # Load zones are published twice: plain "LZ" and energy-weighted "LZEW". Use LZ.
    return "LZ" if point.startswith("LZ_") else None


def parse_archive_rows(rows, point: str, point_type: str | None = None) -> PriceSeries:
    """rows: iterable of (date, hour, interval, repeated_flag, point, type, price)."""
    if point_type is None:
        point_type = default_point_type(point)  # "" disables the type filter
    times, prices = [], []
    for r in rows:
        if not r or r[4] != point or (point_type and r[5] != point_type):
            continue
        t = interval_start_utc(r[0], int(r[1]), int(r[2]), str(r[3]).upper() == "Y")
        times.append(np.datetime64(t.replace(tzinfo=None), "s"))
        prices.append(float(r[6]))
    order = np.argsort(np.array(times))
    t_sorted = np.array(times)[order]
    if len(t_sorted) > 1 and (np.diff(t_sorted) == np.timedelta64(0, "s")).any():
        raise ValueError(f"duplicate intervals for {point}; pass point_type to disambiguate")
    return PriceSeries(point, t_sorted, np.array(prices)[order])


def parse_archive_zip(raw: bytes, point: str = "LZ_NORTH") -> PriceSeries:
    import openpyxl

    z = zipfile.ZipFile(io.BytesIO(raw))
    wb = openpyxl.load_workbook(io.BytesIO(z.read(z.namelist()[0])), read_only=True)

    def rows():
        for ws in wb.worksheets:
            it = ws.iter_rows(values_only=True)
            next(it, None)  # header
            yield from it

    return parse_archive_rows(rows(), point)


def list_report_files(report_id: int = HISTORICAL_RTM_REPORT) -> list[tuple[str, str]]:
    """Return [(filename, download_url)] for an ERCOT MIS public report."""
    html = _get(REPORT_LIST_URL.format(report_id=report_id)).decode("utf-8", "replace")
    names = re.findall(r"class='labelOptional_ind'>([^<]+)</td>", html)
    links = re.findall(r"href='(/misdownload/servlets/mirDownload\?[^']+)'>zip<", html)
    return [(n, BASE + link) for n, link in zip(names, links)]


def download_year(year: int, point: str = "LZ_NORTH") -> PriceSeries:
    for name, url in list_report_files():
        if name.endswith(f"_{year}.zip"):
            return parse_archive_zip(_get(url, timeout=300), point)
    raise LookupError(f"no ERCOT price archive for {year}")


# --- live data -----------------------------------------------------------------

def parse_live_spp(html: str) -> dict[str, list[tuple[datetime, float]]]:
    """Parse today's real-time SPP table -> {point: [(interval_start_utc, price)]}."""
    rows = re.findall(r"<tr>(.*?)</tr>", html, re.DOTALL)
    table = [[re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.DOTALL)] for r in rows]
    table = [r for r in table if r]
    header, body = table[0], table[1:]
    out: dict[str, list[tuple[datetime, float]]] = {h: [] for h in header[2:]}
    for r in body:
        m, d, y = (int(x) for x in r[0].split("/"))
        hhmm = int(r[1])
        end_local = datetime(y, m, d, tzinfo=CENTRAL) + timedelta(hours=hhmm // 100, minutes=hhmm % 100)
        start = (end_local - INTERVAL).astimezone(timezone.utc)
        for name, val in zip(header[2:], r[2:]):
            if val:
                out[name].append((start, float(val)))
    return out


def live_prices() -> dict[str, list[tuple[datetime, float]]]:
    return parse_live_spp(_get(LIVE_SPP_URL).decode())


def parse_fuel_mix(doc: dict) -> list[tuple[datetime, dict[str, float]]]:
    """Fuel-mix dashboard JSON -> [(timestamp_utc, {fuel: MW})] sorted by time."""
    out = []
    for day in doc["data"].values():
        for ts, fuels in day.items():
            t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S%z").astimezone(timezone.utc)
            out.append((t, {f: float(v["gen"]) for f, v in fuels.items()}))
    out.sort(key=lambda x: x[0])
    return out


def live_fuel_mix() -> list[tuple[datetime, dict[str, float]]]:
    import json

    return parse_fuel_mix(json.loads(_get(FUEL_MIX_URL)))


# --- day-ahead market (the market's own forecast) ------------------------------

def dam_url(day) -> str:
    return f"{BASE}/content/cdr/html/{day:%Y%m%d}_dam_spp.html"


def parse_dam(html: str) -> dict[str, list[tuple[datetime, float]]]:
    """Day-ahead hourly SPP table -> {point: [(hour_start_utc, price)]}.

    ERCOT publishes the next operating day's prices around 13:30 Central, so in
    the evening the planner already knows the market's price for every hour
    until morning.
    """
    rows = re.findall(r"<tr>(.*?)</tr>", html, re.DOTALL)
    table = [[re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.DOTALL)]
             for r in rows]
    table = [r for r in table if r]
    if not table:
        return {}  # page exists but the day hasn't been published yet
    header, body = table[0], table[1:]
    out: dict[str, list[tuple[datetime, float]]] = {h: [] for h in header[2:]}
    for r in body:
        m, d, y = (int(x) for x in r[0].split("/"))
        he = r[1].rstrip("*")
        start = (datetime(y, m, d, tzinfo=CENTRAL) + timedelta(hours=int(he) - 1)).astimezone(timezone.utc)
        for name, val in zip(header[2:], r[2:]):
            if val:
                out[name].append((start, float(val)))
    return out


def day_ahead_prices(day, point: str = "LZ_NORTH") -> list[tuple[datetime, float]]:
    """Hourly day-ahead prices for one point; empty if not yet published."""
    return parse_dam(_get(dam_url(day)).decode()).get(point, [])
