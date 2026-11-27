"""Backtest charging strategies on historical ERCOT prices.

Each night the car plugs in at ``plug_in`` local time and must have
``energy_kwh`` added by ``depart`` the next morning. Strategies:

  immediate   charge at full power as soon as it's plugged in
  offpeak     wait until a fixed start time (like Tesla's scheduled charging)
  naive       optimize against the mean of the previous 7 days
  dayahead    optimize against the day-ahead market price (known by 13:30 CT)
  ml          optimize against a gradient-boosted forecast (trained on an earlier year)
  oracle      optimize against the real prices (perfect foresight, an upper bound)

Every strategy is *settled* at real-time prices. Strategies whose inputs are
missing (no day-ahead data, no trained model) are skipped.

Prices are wholesale real-time prices. A retail plan only passes these through
if it's wholesale-indexed or time-of-use, so the dollar figures show the
opportunity, not anyone's bill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

import numpy as np

from ..grid.ercot import CENTRAL, PriceSeries
from . import forecast as fc
from .optimizer import Vehicle, immediate_schedule, optimal_schedule

ALL_STRATEGIES = ("immediate", "offpeak", "naive", "dayahead", "ml", "oracle")


@dataclass
class BacktestConfig:
    plug_in: time = time(18, 0)
    depart: time = time(7, 0)
    offpeak_start: time = time(23, 0)
    energy_kwh: float = 30.0  # about 100 miles a day
    vehicle: Vehicle = field(default_factory=Vehicle)


@dataclass
class BacktestResult:
    sessions: int
    total_usd: dict[str, float]
    worst_session_usd: dict[str, float]
    energy_kwh: float

    @property
    def strategies(self) -> list[str]:
        return [s for s in ALL_STRATEGIES if s in self.total_usd]

    def cents_per_kwh(self, strategy: str) -> float:
        return 100.0 * self.total_usd[strategy] / self.energy_kwh

    def savings_vs(self, strategy: str, baseline: str) -> float:
        return 1.0 - self.total_usd[strategy] / self.total_usd[baseline]

    def captured(self, strategy: str) -> float:
        """Share of the off-peak -> oracle headroom that a strategy captures."""
        head = self.total_usd["offpeak"] - self.total_usd["oracle"]
        return (self.total_usd["offpeak"] - self.total_usd[strategy]) / head if head else 0.0

    def table(self) -> str:
        header = (f"{'strategy':<10} {'total $':>9} {'¢/kWh':>7} {'worst night $':>14} {'vs immediate':>13} "
                  f"{'vs offpeak':>11} {'headroom':>9}")
        lines = [header]
        for s in self.strategies:
            lines.append(
                f"{s:<10} {self.total_usd[s]:9.2f} {self.cents_per_kwh(s):7.2f} {self.worst_session_usd[s]:14.2f} "
                f"{self.savings_vs(s, 'immediate'):12.1%} {self.savings_vs(s, 'offpeak'):10.1%} "
                f"{self.captured(s):8.0%}"
            )
        return "\n".join(lines)


def _utc(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=CENTRAL).astimezone(timezone.utc)


def _offsets(g: fc.Grid, a: int, b: int) -> np.ndarray:
    """Local-minus-UTC hours for each slot (handles DST inside a window)."""
    out = np.empty(b - a)
    for i in range(b - a):
        t = g.slot_time(a + i).astype(datetime).replace(tzinfo=timezone.utc)
        out[i] = t.astimezone(CENTRAL).utcoffset().total_seconds() / 3600
    return out


def sessions(g: fc.Grid, cfg: BacktestConfig, first: date, last: date):
    """Yield (day, a, b, off) slot windows for each night in [first, last]."""
    d = first
    while d <= last:
        a = g.slot(_utc(d, cfg.plug_in))
        b = g.slot(_utc(d + timedelta(days=1), cfg.depart))
        off = g.slot(_utc(d, cfg.offpeak_start)) - a
        if a >= 0 and b <= len(g.rt) and not np.isnan(g.rt[a:b]).any():
            yield d, a, b, off
        d += timedelta(days=1)


def training_set(g: fc.Grid, cfg: BacktestConfig, first: date, last: date) -> tuple[np.ndarray, np.ndarray]:
    X, y = [], []
    for _, a, b, _ in sessions(g, cfg, first, last):
        f = fc.features(g, a, b, _offsets(g, a, b))
        if f is not None:
            X.append(f)
            y.append(g.rt[a:b])
    return np.vstack(X), np.concatenate(y)


def run(
    rt: PriceSeries,
    cfg: BacktestConfig | None = None,
    dam: PriceSeries | None = None,
    model: fc.MLForecaster | None = None,
    first: date | None = None,
    last: date | None = None,
) -> BacktestResult:
    cfg = cfg or BacktestConfig()
    g = fc.Grid.build(rt, dam)
    v = cfg.vehicle
    to_local = lambda t64: t64.astype(datetime).replace(tzinfo=timezone.utc).astimezone(CENTRAL).date()
    first = first or to_local(rt.start_utc[0]) + timedelta(days=7)
    last = last or to_local(rt.start_utc[-1]) - timedelta(days=1)

    totals: dict[str, float] = {}
    worst: dict[str, float] = {}
    n_sessions = 0
    for _, a, b, off in sessions(g, cfg, first, last):
        actual = g.rt[a:b]
        n = len(actual)
        forecasts = {"naive": fc.naive(g, a, b), "dayahead": fc.dayahead(g, a, b), "ml": None}
        if model is not None:
            X = fc.features(g, a, b, _offsets(g, a, b))
            forecasts["ml"] = model.predict(X) if X is not None else None
        if forecasts["naive"] is None or (dam is not None and forecasts["dayahead"] is None) or \
                (model is not None and forecasts["ml"] is None):
            continue  # keep every strategy on the same set of nights
        scheds = {
            "immediate": immediate_schedule(n, cfg.energy_kwh, v),
            "offpeak": immediate_schedule(n, cfg.energy_kwh, v, start_slot=off),
            "oracle": optimal_schedule(actual, cfg.energy_kwh, v),
        }
        for name, f in forecasts.items():
            if f is not None:
                scheds[name] = optimal_schedule(f, cfg.energy_kwh, v)
        for s, sch in scheds.items():
            c = sch.cost_usd(actual)
            totals[s] = totals.get(s, 0.0) + c
            worst[s] = max(worst.get(s, 0.0), c)
        n_sessions += 1
    return BacktestResult(n_sessions, totals, worst, n_sessions * cfg.energy_kwh)
