"""EIA Open Data v2 client: hourly generation by fuel for any US balancing authority.

Used for multi-year carbon history (ERCOT's own dashboard only covers recent
days). The key comes from the EIA_API_KEY environment variable and is never
logged. The grid-monitor endpoint is slow and returns 504s under load, so
requests retry with exponential backoff.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/"


def _key() -> str:
    k = os.environ.get("EIA_API_KEY")
    if not k:
        raise RuntimeError("set EIA_API_KEY (free: https://www.eia.gov/opendata/)")
    return k


def fetch_fuel_mix(
    respondent: str = "ERCO",
    start: str = "2025-01-01T00",
    end: str | None = None,
    retries: int = 5,
    page: int = 5000,
) -> list[tuple[datetime, str, float]]:
    """Return [(hour_start_utc, fuel_code, MWh)] for a balancing authority."""
    out: list[tuple[datetime, str, float]] = []
    offset = 0
    while True:
        params = [
            ("api_key", _key()),
            ("frequency", "hourly"),
            ("data[0]", "value"),
            ("facets[respondent][]", respondent),
            ("start", start),
            ("sort[0][column]", "period"),
            ("sort[0][direction]", "asc"),
            ("offset", str(offset)),
            ("length", str(page)),
        ]
        if end:
            params.append(("end", end))
        url = API + "?" + urllib.parse.urlencode(params)
        doc = _get_json_with_retry(url, retries)
        rows = doc["response"]["data"]
        for r in rows:
            if r.get("value") is None:
                continue
            t = datetime.strptime(r["period"], "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
            out.append((t, r["fueltype"], float(r["value"])))
        offset += len(rows)
        if len(rows) < page or offset >= int(doc["response"].get("total", 0)):
            return out


def _get_json_with_retry(url: str, retries: int) -> dict:
    delay = 2.0
    last: Exception | None = None
    for _ in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return json.loads(r.read())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            code = getattr(e, "code", None)
            if code is not None and code < 500 and code != 429:
                raise RuntimeError(f"EIA request failed with HTTP {code}") from None
            last = e
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"EIA unavailable after {retries} attempts: {type(last).__name__}")
