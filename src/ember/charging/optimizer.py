"""Charging schedule optimizer for one EV.

Problem, for one plug-in session split into 15-minute slots t = 0..T-1:

    minimize    sum_t g_t * (price_t + lambda * carbon_t)
    subject to  0 <= g_t <= P_max * dt                (charger limit, grid-side kWh)
                sum_t eta * g_t = E_needed            (energy into the battery)

g_t is grid energy in slot t. The objective is linear and every slot has its
own independent cap, so this is a fractional knapsack: filling the cheapest
slots first is *exactly* optimal (proved by an exchange argument and checked
against brute force in the tests). A fractional slot maps to a lower charging
current, which the Tesla Fleet API supports via ``set_charging_amps``.

Things that break this simple structure come later: battery taper above about
80% SoC, uncertain prices (stochastic MPC) and many cars sharing a feeder
(the herding problem, coordinated with distributed optimization).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Vehicle:
    charger_kw: float = 11.5  # 48 A at 240 V (Tesla Wall Connector)
    efficiency: float = 0.90  # wall -> battery
    battery_kwh: float = 75.0


@dataclass
class Schedule:
    grid_kwh: np.ndarray  # per slot, energy drawn from the wall
    battery_kwh: float  # energy delivered into the battery
    feasible: bool  # did we reach the requested energy?

    def cost_usd(self, price_usd_mwh: np.ndarray) -> float:
        return float(np.dot(self.grid_kwh, price_usd_mwh) / 1000.0)

    def charging_amps(self, voltage: float = 240.0, slot_h: float = 0.25) -> np.ndarray:
        return self.grid_kwh / slot_h * 1000.0 / voltage


def slot_cap_kwh(v: Vehicle, slot_h: float = 0.25) -> float:
    return v.charger_kw * slot_h


def greedy_schedule(score: np.ndarray, energy_kwh: float, v: Vehicle, slot_h: float = 0.25) -> Schedule:
    """Fill the lowest-score slots first. Exact optimum for the linear problem above."""
    cap = slot_cap_kwh(v, slot_h)
    need_grid = energy_kwh / v.efficiency
    g = np.zeros(len(score))
    for t in np.argsort(score, kind="stable"):
        if need_grid <= 1e-12:
            break
        take = min(cap, need_grid)
        g[t] = take
        need_grid -= take
    return Schedule(g, float(g.sum() * v.efficiency), need_grid <= 1e-9)


def optimal_schedule(
    price: np.ndarray,
    energy_kwh: float,
    v: Vehicle,
    carbon: np.ndarray | None = None,
    carbon_price_usd_per_tonne: float = 0.0,
    slot_h: float = 0.25,
) -> Schedule:
    """Cost (+ optional carbon price) optimal schedule. carbon in kg/MWh, price in USD/MWh."""
    score = np.asarray(price, dtype=float).copy()
    if carbon is not None and carbon_price_usd_per_tonne:
        score += np.asarray(carbon) / 1000.0 * carbon_price_usd_per_tonne
    return greedy_schedule(score, energy_kwh, v, slot_h)


def immediate_schedule(n_slots: int, energy_kwh: float, v: Vehicle, start_slot: int = 0, slot_h: float = 0.25) -> Schedule:
    """Plug in and charge at full power from start_slot (the 'dumb' baseline)."""
    score = np.arange(n_slots, dtype=float)
    score[:start_slot] = np.inf
    s = greedy_schedule(score, energy_kwh, v, slot_h)
    s.grid_kwh[:start_slot] = 0.0
    return s
