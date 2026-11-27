"""Real-time price forecasters for the overnight charging window.

Every forecaster sees only information available at plug-in time (18:00 CT):
  * real-time prices up to plug-in,
  * day-ahead prices for the whole window (ERCOT publishes the next day at
    about 13:30 CT, so the overnight hours are already known).

Why forecast the *mean*: charging cost is linear in price, so the decision that
minimizes expected cost is the one that minimizes cost under the conditional
expected price. A squared-error model targets exactly that.

Forecasters:
  naive     mean of the same window over the previous 7 days
  dayahead  the day-ahead market price for each slot
  ml        gradient-boosted trees on day-ahead + recent real-time features,
            trained on an earlier year and tested on a later one
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SLOTS_PER_DAY = 96


@dataclass
class Grid:
    """Real-time and day-ahead prices on one shared 15-minute UTC grid."""

    t0: np.datetime64
    rt: np.ndarray
    dam: np.ndarray | None

    @classmethod
    def build(cls, rt_series, dam_series=None) -> Grid:
        step = np.timedelta64(15, "m")
        t0 = rt_series.start_utc[0]
        n = int((rt_series.start_utc[-1] - t0) / step) + 1
        rt = np.full(n, np.nan)
        rt[((rt_series.start_utc - t0) / step).astype(int)] = rt_series.price
        dam = None
        if dam_series is not None:
            dam = np.full(n, np.nan)
            idx = ((dam_series.start_utc - t0) / step).astype(int)
            ok = (idx >= 0) & (idx < n)
            dam[idx[ok]] = dam_series.price[ok]
        return cls(t0, rt, dam)

    def slot(self, t_utc) -> int:
        return int((np.datetime64(t_utc.replace(tzinfo=None), "s") - self.t0) / np.timedelta64(15, "m"))

    def slot_time(self, i: int) -> np.datetime64:
        return self.t0 + i * np.timedelta64(15, "m")


def naive(g: Grid, a: int, b: int, days: int = 7) -> np.ndarray | None:
    if a - SLOTS_PER_DAY * days < 0:
        return None
    past = np.vstack([g.rt[a - SLOTS_PER_DAY * k: b - SLOTS_PER_DAY * k] for k in range(1, days + 1)])
    f = np.nanmean(past, axis=0)
    return None if np.isnan(f).any() else f


def dayahead(g: Grid, a: int, b: int) -> np.ndarray | None:
    if g.dam is None:
        return None
    f = g.dam[a:b]
    return None if np.isnan(f).any() else f.copy()


FEATURES = (
    "dam", "dam_rank", "dam_minus_window_mean", "slots_ahead", "local_hour", "dow", "month",
    "rt_at_plugin", "rt_mean_24h", "rt_max_24h", "spread_7d_same_slot", "naive_7d",
)


def features(g: Grid, a: int, b: int, utc_offset_h: np.ndarray) -> np.ndarray | None:
    """One row per slot in [a, b). utc_offset_h: local-minus-UTC hours per slot (DST aware)."""
    if g.dam is None or a < SLOTS_PER_DAY * 7:
        return None
    dam = g.dam[a:b]
    nv = naive(g, a, b)
    hist = g.rt[a - SLOTS_PER_DAY: a]
    if nv is None or np.isnan(dam).any() or np.isnan(hist).any():
        return None
    n = b - a
    spreads = np.vstack([g.rt[a - SLOTS_PER_DAY * k: b - SLOTS_PER_DAY * k] - g.dam[a - SLOTS_PER_DAY * k: b - SLOTS_PER_DAY * k]
                         for k in range(1, 8)])
    times = g.slot_time(a) + np.arange(n) * np.timedelta64(15, "m")
    local = times + (utc_offset_h * 3600).astype("timedelta64[s]")
    hour = (local.astype("datetime64[m]").astype(int) // 60) % 24 + (local.astype("datetime64[m]").astype(int) % 60) / 60
    dow = (local.astype("datetime64[D]").astype(int) + 4) % 7  # 1970-01-01 was a Thursday
    month = local.astype("datetime64[M]").astype(int) % 12 + 1
    cols = [
        dam,
        np.argsort(np.argsort(dam)) / max(n - 1, 1),
        dam - dam.mean(),
        np.arange(n, dtype=float),
        hour,
        dow.astype(float),
        month.astype(float),
        np.full(n, hist[-1]),
        np.full(n, hist.mean()),
        np.full(n, hist.max()),
        np.nanmean(spreads, axis=0),
        nv,
    ]
    return np.column_stack(cols)


class MLForecaster:
    """Gradient-boosted trees for the real-time price of each slot.

    target="residual" (default) learns the real-time minus day-ahead gap and adds
    it back to the day-ahead price, optionally shrunk toward day-ahead. It won
    model selection on 2024 validation data (see docs/GRID_DESIGN.md).
    target="direct" predicts the price itself.
    """

    def __init__(self, target: str = "residual", shrink: float = 0.5, **params) -> None:
        from sklearn.ensemble import HistGradientBoostingRegressor

        if target not in ("residual", "direct"):
            raise ValueError("target must be 'residual' or 'direct'")
        self.target, self.shrink = target, shrink
        defaults = {"max_iter": 200, "learning_rate": 0.05, "max_leaf_nodes": 15, "min_samples_leaf": 100,
                    "l2_regularization": 1.0, "random_state": 0}
        defaults.update(params)
        self.model = HistGradientBoostingRegressor(**defaults)
        self.clip = None

    def fit(self, X: np.ndarray, y: np.ndarray, clip_quantile: float = 0.999) -> MLForecaster:
        # Clip the most extreme training targets so a handful of $3,000+ intervals don't dominate the fit
        t = y - X[:, 0] if self.target == "residual" else y
        lo, hi = np.quantile(t, [1 - clip_quantile, clip_quantile])
        self.clip = (float(lo), float(hi))
        self.model.fit(X, np.clip(t, lo, hi))
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        p = self.model.predict(X)
        return X[:, 0] + self.shrink * p if self.target == "residual" else p


def mpc_schedule(dam: np.ndarray, rt: np.ndarray, energy_kwh: float, vehicle, rho: float = 0.8,
                 last_rt: float | None = None, last_dam: float | None = None, lag: int = 1) -> np.ndarray:
    """Receding-horizon control: re-plan every slot as real-time prices arrive.

    At slot k the controller knows real-time prices up to slot k - lag (ERCOT
    publishes 15-min settlement prices just after each interval). It forecasts
    the remaining slots as day-ahead plus the latest real-time/day-ahead gap,
    decaying by ``rho`` per slot (spikes and dips persist briefly, then revert),
    solves the remaining problem exactly, and commits only the current slot.
    Returns grid kWh per slot.
    """
    from .optimizer import optimal_schedule

    n = len(dam)
    cap = vehicle.charger_kw * 0.25
    need = energy_kwh / vehicle.efficiency  # grid-side kWh still to draw
    g = np.zeros(n)
    for k in range(n):
        if need <= 1e-9:
            break
        j = k - lag
        if j >= 0:
            gap = rt[j] - dam[j]
        elif last_rt is not None and last_dam is not None:
            gap = last_rt - last_dam
        else:
            gap = 0.0
        steps = np.arange(n - k) + (k - j if j >= 0 else lag)
        f = dam[k:] + gap * rho ** steps
        sched = optimal_schedule(f, need * vehicle.efficiency, vehicle)
        take = min(sched.grid_kwh[0], cap, need)
        # never leave more than the remaining slots can deliver
        must = need - cap * (n - k - 1)
        take = max(take, min(cap, must))
        g[k] = take
        need -= take
    return g
