"""Backtest charging strategies on historical ERCOT prices.

Each night the car plugs in at ``plug_in`` local time and must have
``energy_kwh`` added by ``depart`` the next morning. Strategies:

  immediate   charge at full power as soon as it's plugged in
  offpeak     wait until a fixed start time (like Tesla's scheduled charging)
  forecast    optimize against a forecast made with only past data
  oracle      optimize against the real prices (perfect foresight, an upper bound)

Every strategy is *settled* at the real prices. The gap between forecast and
oracle is the value of a better forecaster, which is the ML work that comes next.

Prices are wholesale real-time prices. A retail plan only passes these through
if it's wholesale-indexed or time-of-use, so the dollar figures show the
opportunity, not anyone's bill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

import numpy as np

from ..grid.ercot import CENTRAL, PriceSeries
from .optimizer import Vehicle, immediate_schedule, optimal_schedule

STRATEGIES = ("immediate", "offpeak", "forecast", "oracle")


@dataclass
class BacktestConfig:
    plug_in: time = time(18, 0)
    depart: time = time(7, 0)
    offpeak_start: time = time(23, 0)
    energy_kwh: float = 30.0  # about 100 miles a day
    vehicle: Vehicle = field(default_factory=Vehicle)
    forecast_days: int = 7  # seasonal-naive window


@dataclass
class BacktestResult:
    sessions: int
    total_usd: dict[str, float]
    worst_session_usd: dict[str, float]
    energy_kwh: float

    def cents_per_kwh(self, strategy: str) -> float:
        return 100.0 * self.total_usd[strategy] / self.energy_kwh

    def savings_vs(self, strategy: str, baseline: str) -> float:
        return 1.0 - self.total_usd[strategy] / self.total_usd[baseline]

    def table(self) -> str:
        lines = [f"{'strategy':<11} {'total $':>9} {'¢/kWh':>7} {'worst night $':>14} {'vs immediate':>13} {'vs offpeak':>11}"]
        for s in STRATEGIES:
            lines.append(
                f"{s:<11} {self.total_usd[s]:9.2f} {self.cents_per_kwh(s):7.2f} {self.worst_session_usd[s]:14.2f} "
                f"{self.savings_vs(s, 'immediate'):12.1%} {self.savings_vs(s, 'offpeak'):10.1%}"
            )
        return "\n".join(lines)


class _Index:
    def __init__(self, series: PriceSeries) -> None:
        self.t0 = series.start_utc[0]
        self.step = np.timedelta64(15, "m")
        n = int((series.start_utc[-1] - self.t0) / self.step) + 1
        self.price = np.full(n, np.nan)
        idx = ((series.start_utc - self.t0) / self.step).astype(int)
        self.price[idx] = series.price

    def slot(self, t_utc: datetime) -> int:
        return int((np.datetime64(t_utc.replace(tzinfo=None), "s") - self.t0) / self.step)


def _utc(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=CENTRAL).astimezone(timezone.utc)


def run(series: PriceSeries, cfg: BacktestConfig | None = None) -> BacktestResult:
    cfg = cfg or BacktestConfig()
    ix = _Index(series)
    v = cfg.vehicle
    first = (series.start_utc[0].astype(datetime).replace(tzinfo=timezone.utc).astimezone(CENTRAL).date()
             + timedelta(days=cfg.forecast_days))
    last = series.start_utc[-1].astype(datetime).replace(tzinfo=timezone.utc).astimezone(CENTRAL).date() - timedelta(days=1)

    totals = dict.fromkeys(STRATEGIES, 0.0)
    worst = dict.fromkeys(STRATEGIES, 0.0)
    sessions = 0
    d = first
    while d <= last:
        a = ix.slot(_utc(d, cfg.plug_in))
        b = ix.slot(_utc(d + timedelta(days=1), cfg.depart))
        off = ix.slot(_utc(d, cfg.offpeak_start)) - a
        d += timedelta(days=1)
        if a < 0 or b > len(ix.price):
            continue
        actual = ix.price[a:b]
        # seasonal naive: mean of the same window on each of the previous N days (96 slots/day in UTC)
        past = [ix.price[a - 96 * k : b - 96 * k] for k in range(1, cfg.forecast_days + 1)]
        if np.isnan(actual).any() or any(len(p) != len(actual) for p in past):
            continue
        forecast = np.nanmean(np.vstack(past), axis=0)
        if np.isnan(forecast).any():
            continue
        n = len(actual)
        scheds = {
            "immediate": immediate_schedule(n, cfg.energy_kwh, v),
            "offpeak": immediate_schedule(n, cfg.energy_kwh, v, start_slot=off),
            "forecast": optimal_schedule(forecast, cfg.energy_kwh, v),
            "oracle": optimal_schedule(actual, cfg.energy_kwh, v),
        }
        for s, sch in scheds.items():
            c = sch.cost_usd(actual)
            totals[s] += c
            worst[s] = max(worst[s], c)
        sessions += 1
    return BacktestResult(sessions, totals, worst, sessions * cfg.energy_kwh)
