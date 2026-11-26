"""Grid carbon intensity from the generation fuel mix.

Average intensity = sum(gen_f * EF_f) / sum(gen_f), in kg CO2 per MWh.

The emission factors are typical values for each fuel's ERCOT fleet: coal and
lignite plants at about 10-11 MMBtu/MWh, gas as a blend of combined-cycle and
peaker plants. They're approximations meant for *comparing hours*, not for
carbon accounting. Zero-carbon sources count as 0. Battery discharge also
counts as 0 here because its true intensity depends on when it charged; that
is a documented simplification.

Roadmap: switch from *average* to *marginal* emissions (which plant actually
ramps when you add load). Marginal is the right signal for shifting EV
charging, and it's a harder estimation problem.
"""

from __future__ import annotations

EMISSION_FACTORS_KG_PER_MWH = {
    "Coal and Lignite": 1050.0,
    "Natural Gas": 420.0,
    "Other": 500.0,
    "Nuclear": 0.0,
    "Wind": 0.0,
    "Solar": 0.0,
    "Hydro": 0.0,
    "Power Storage": 0.0,
    "Power Storage Discharging": 0.0,
}
# Rows that would double count (net storage is already in "Power Storage") or that are load, not generation
_SKIP = {"Power Storage Charging", "Power Storage Discharging"}

# EIA hourly fuel codes -> the same factors
EIA_FUEL_FACTORS = {"COL": 1050.0, "NG": 420.0, "OTH": 500.0, "OIL": 800.0, "NUC": 0.0, "WND": 0.0, "SUN": 0.0, "WAT": 0.0, "BAT": 0.0}


def intensity(mix_mw: dict[str, float], factors: dict[str, float] = EMISSION_FACTORS_KG_PER_MWH) -> float:
    """Average carbon intensity (kg CO2/MWh) of a fuel mix in MW."""
    total = emitted = 0.0
    for fuel, mw in mix_mw.items():
        if fuel in _SKIP or mw <= 0:
            continue
        total += mw
        emitted += mw * factors.get(fuel, 500.0)
    if total == 0:
        raise ValueError("empty fuel mix")
    return emitted / total


def shares(mix_mw: dict[str, float]) -> dict[str, float]:
    gen = {f: mw for f, mw in mix_mw.items() if f not in _SKIP and mw > 0}
    total = sum(gen.values())
    return {f: mw / total for f, mw in sorted(gen.items(), key=lambda x: -x[1])}
