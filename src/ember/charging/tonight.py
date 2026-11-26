"""Plan tonight's charging session from ERCOT day-ahead prices and the car's charge state.

Output is a list of timed charging commands (set_charging_amps / charge_start /
charge_stop) that the Tesla client executes, or just logs in dry-run mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

import numpy as np

from ..grid.ercot import CENTRAL
from .optimizer import Schedule, Vehicle, optimal_schedule

SLOT = timedelta(minutes=15)


@dataclass
class PlannedCommand:
    at_utc: datetime
    command: str
    params: dict

    def __str__(self) -> str:
        p = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.at_utc.astimezone(CENTRAL):%a %H:%M} CT  {self.command}({p})"


@dataclass
class Plan:
    slots_utc: list[datetime]
    price: np.ndarray
    schedule: Schedule
    commands: list[PlannedCommand]
    energy_needed_kwh: float
    vehicle: Vehicle

    def summary(self) -> str:
        cost = self.schedule.cost_usd(self.price)
        naive = optimal_schedule(np.arange(len(self.price), dtype=float), self.energy_needed_kwh, self.vehicle).cost_usd(
            self.price)
        charging = [t for t, g in zip(self.slots_utc, self.schedule.grid_kwh) if g > 0]
        lines = [
            f"Energy to add      {self.energy_needed_kwh:.1f} kWh"
            + ("" if self.schedule.feasible else "  (NOT reachable before departure; charging every slot)"),
            f"Charging windows   {len(charging)} x 15 min"
            + (f", first {charging[0].astimezone(CENTRAL):%H:%M} CT" if charging else ""),
            f"Wholesale cost     ${cost:.2f} planned vs ${naive:.2f} if charging immediately",
            "",
            "Commands:",
        ] + [f"  {c}" for c in self.commands]
        return "\n".join(lines)


def expand_hourly(hourly: list[tuple[datetime, float]]) -> dict[datetime, float]:
    out = {}
    for start, p in hourly:
        for k in range(4):
            out[start + k * SLOT] = p
    return out


def next_departure(now: datetime, depart: time) -> datetime:
    local = now.astimezone(CENTRAL)
    d = datetime.combine(local.date(), depart, tzinfo=CENTRAL)
    if d <= local:
        d = datetime.combine(local.date() + timedelta(days=1), depart, tzinfo=CENTRAL)
    return d.astimezone(timezone.utc)


def to_commands(slots: list[datetime], sched: Schedule, voltage: float = 240.0, min_amps: int = 5,
                max_amps: int = 48) -> list[PlannedCommand]:
    amps = sched.charging_amps(voltage)
    cmds: list[PlannedCommand] = []
    current: int | None = None  # None = not charging
    for t, a in zip(slots, amps):
        want = None if a < 0.5 else int(np.clip(round(a), min_amps, max_amps))
        if want == current:
            continue
        if want is None:
            cmds.append(PlannedCommand(t, "charge_stop", {}))
        else:
            cmds.append(PlannedCommand(t, "set_charging_amps", {"charging_amps": want}))
            if current is None:
                cmds.append(PlannedCommand(t, "charge_start", {}))
        current = want
    if current is not None:
        cmds.append(PlannedCommand(slots[-1] + SLOT, "charge_stop", {}))
    return cmds


def plan_tonight(
    hourly_prices: list[tuple[datetime, float]],
    soc_pct: float,
    target_pct: float,
    vehicle: Vehicle,
    now: datetime,
    depart: time = time(7, 0),
) -> Plan:
    prices = expand_hourly(hourly_prices)
    start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0) + SLOT
    end = next_departure(now, depart)
    slots, price = [], []
    t = start
    while t < end:
        if t not in prices:
            raise LookupError(f"no day-ahead price for {t.astimezone(CENTRAL):%Y-%m-%d %H:%M} CT")
        slots.append(t)
        price.append(prices[t])
        t += SLOT
    need = max(0.0, (target_pct - soc_pct) / 100.0 * vehicle.battery_kwh)
    price_arr = np.array(price)
    sched = optimal_schedule(price_arr, need, vehicle)
    return Plan(slots, price_arr, sched, to_commands(slots, sched), need, vehicle)
