import itertools
from datetime import datetime, timezone

import numpy as np
import pytest

from ember.charging.backtest import BacktestConfig, run
from ember.charging.optimizer import Vehicle, immediate_schedule, optimal_schedule
from ember.grid import carbon
from ember.grid.ercot import PriceSeries, interval_start_utc, parse_archive_rows, parse_fuel_mix, parse_live_spp

# --- ERCOT parsing ---------------------------------------------------------------

def test_hour_ending_conversion_cst():
    # Jan 1, hour ending 1, interval 1 = 00:00-00:15 CST = 06:00 UTC
    assert interval_start_utc("01/01/2025", 1, 1, False) == datetime(2025, 1, 1, 6, tzinfo=timezone.utc)
    # hour ending 24, interval 4 = 23:45 CST
    assert interval_start_utc("01/01/2025", 24, 4, False) == datetime(2025, 1, 2, 5, 45, tzinfo=timezone.utc)


def test_dst_repeated_hour_is_distinct():
    # 2025-11-02: 01:00-02:00 local happens twice (CDT then CST)
    first = interval_start_utc("11/02/2025", 2, 1, False)
    second = interval_start_utc("11/02/2025", 2, 1, True)
    assert (second - first).total_seconds() == 3600
    assert first == datetime(2025, 11, 2, 6, tzinfo=timezone.utc)


def test_archive_filters_lz_and_rejects_duplicates():
    rows = [
        ("01/01/2025", 1, 1, "N", "LZ_NORTH", "LZ", 17.9),
        ("01/01/2025", 1, 1, "N", "LZ_NORTH", "LZEW", 17.95),
        ("01/01/2025", 1, 2, "N", "LZ_NORTH", "LZ", 18.0),
        ("01/01/2025", 1, 1, "N", "HB_NORTH", "HU", 99.0),
    ]
    s = parse_archive_rows(rows, "LZ_NORTH")
    assert list(s.price) == [17.9, 18.0]
    with pytest.raises(ValueError):
        parse_archive_rows(rows[:2], "LZ_NORTH", point_type="")  # no filter -> duplicates


LIVE_HTML = """<table><tr><th>Oper Day</th><th>Interval Ending</th><th>HB_NORTH</th><th>LZ_NORTH</th></tr>
<tr><td>11/26/2026</td><td>0015</td><td>20.00</td><td>21.50</td></tr>
<tr><td>11/26/2026</td><td>0030</td><td>22.00</td><td>-3.25</td></tr></table>"""


def test_live_spp_parse():
    p = parse_live_spp(LIVE_HTML)
    assert p["LZ_NORTH"][0] == (datetime(2026, 11, 26, 6, 0, tzinfo=timezone.utc), 21.5)
    assert p["LZ_NORTH"][1][1] == -3.25


def test_fuel_mix_and_carbon():
    doc = {"data": {"2026-11-26": {"2026-11-26 02:45:00-0600": {
        "Natural Gas": {"gen": 600.0}, "Wind": {"gen": 300.0}, "Coal and Lignite": {"gen": 100.0},
        "Power Storage": {"gen": -50.0}, "Power Storage Charging": {"gen": -50.0}}}}}
    (t, mix), = parse_fuel_mix(doc)
    assert t == datetime(2026, 11, 26, 8, 45, tzinfo=timezone.utc)
    assert carbon.intensity(mix) == pytest.approx((600 * 420 + 100 * 1050) / 1000)
    assert list(carbon.shares(mix)) == ["Natural Gas", "Wind", "Coal and Lignite"]


# --- optimizer ---------------------------------------------------------------------

V = Vehicle(charger_kw=4.0, efficiency=1.0)  # 1 kWh per 15-min slot: easy arithmetic


def test_greedy_matches_brute_force():
    rng = np.random.default_rng(0)
    for _ in range(50):
        price = rng.normal(30, 40, size=10)
        k = int(rng.integers(1, 10))
        best = min(sum(price[list(c)]) for c in itertools.combinations(range(10), k))
        s = optimal_schedule(price, float(k), V)
        assert s.feasible
        assert s.cost_usd(price) * 1000 == pytest.approx(best)


def test_fractional_schedule_is_never_beaten():
    rng = np.random.default_rng(1)
    price = rng.normal(30, 40, size=12)
    s = optimal_schedule(price, 4.3, V)
    assert s.battery_kwh == pytest.approx(4.3)
    for _ in range(2000):
        g = rng.random(12)
        g = g / g.sum() * 4.3
        if g.max() <= 1.0:
            assert np.dot(g, price) >= np.dot(s.grid_kwh, price) - 1e-9


def test_carbon_price_shifts_schedule():
    price = np.array([10.0, 12.0])
    co2 = np.array([900.0, 0.0])
    assert optimal_schedule(price, 1.0, V).grid_kwh[0] == 1.0
    assert optimal_schedule(price, 1.0, V, carbon=co2, carbon_price_usd_per_tonne=100).grid_kwh[1] == 1.0


def test_infeasible_when_not_enough_time():
    s = optimal_schedule(np.zeros(3), 5.0, V)
    assert not s.feasible and s.battery_kwh == pytest.approx(3.0)


def test_immediate_respects_start_slot():
    s = immediate_schedule(8, 2.0, V, start_slot=5)
    assert list(s.grid_kwh) == [0, 0, 0, 0, 0, 1, 1, 0]


def test_amps_conversion():
    s = optimal_schedule(np.array([1.0]), 2.875 * 0.9, Vehicle())  # one full slot at 11.5 kW
    assert s.charging_amps()[0] == pytest.approx(11.5 * 1000 / 240)


# --- backtest ----------------------------------------------------------------------

def synthetic_series(days=30):
    t0 = np.datetime64("2025-03-01T06:00:00")
    n = 96 * days
    t = t0 + np.arange(n) * np.timedelta64(15, "m")
    local_hour = (np.arange(n) // 4) % 24  # t0 is 00:00 CST
    price = np.where((local_hour >= 2) & (local_hour < 6), 5.0, 50.0)  # cheap 02:00-06:00 local
    price[np.arange(n) % 97 == 0] += 500  # occasional spikes
    return PriceSeries("TEST", t, price)


def test_backtest_ordering_on_synthetic_prices():
    r = run(synthetic_series(), BacktestConfig(energy_kwh=20.0))
    assert r.sessions > 15
    t = r.total_usd
    assert t["oracle"] <= t["forecast"] + 1e-9
    assert t["oracle"] <= t["offpeak"] + 1e-9
    assert t["forecast"] < t["immediate"]
