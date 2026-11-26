from datetime import datetime, time, timezone

import numpy as np
import pytest

from ember.charging.optimizer import Vehicle
from ember.charging.tonight import next_departure, plan_tonight
from ember.grid.ercot import parse_dam
from ember.tesla import fleet
from ember.tesla.fleet import Budget, Credentials, FleetClient, Tokens, authorize_url

CREDS = Credentials("client-id", "client-secret")


@pytest.fixture
def client(tmp_path):
    calls = []

    def transport(method, url, body, token):
        calls.append((method, url, body, token))
        if url.endswith("/vehicles"):
            return {"response": [{"vin": "5YJ3E1EA0000TEST", "state": "online"}]}
        if "vehicle_data" in url:
            return {"response": {"charge_state": {"battery_level": 55, "charge_limit_soc": 80}}}
        return {"response": {"result": True}}

    tokens = Tokens("access", "refresh", expires_at=9e12)
    c = FleetClient(CREDS, tokens, budget=Budget(path=tmp_path / "b.json"), transport=transport)
    c.calls = calls
    return c


def test_authorize_url_has_scopes_and_state():
    url, state = authorize_url(CREDS)
    assert "client_id=client-id" in url and f"state={state}" in url
    assert "vehicle_charging_cmds" in url and "vehicle_cmds" not in url.replace("vehicle_charging_cmds", "")


def test_secret_never_in_authorize_url():
    url, _ = authorize_url(CREDS)
    assert "client-secret" not in url


def test_dry_run_sends_nothing(client):
    r = client.command("5YJ3E1EA0000TEST", "set_charging_amps", charging_amps=32)
    assert r["reason"] == "dry-run" and client.calls == []
    assert client.log[0]["vin"] == "TEST"  # only the last 4 VIN chars are logged


def test_live_command_hits_proxy(client):
    client.dry_run = False
    client.command_base = "https://localhost:4443"
    client.command("VIN", "charge_start")
    method, url, body, token = client.calls[-1]
    assert (method, url, token) == ("POST", "https://localhost:4443/api/1/vehicles/VIN/command/charge_start", "access")
    assert body == {}


@pytest.mark.parametrize("name,params", [("door_unlock", {}), ("remote_start_drive", {}), ("honk_horn", {})])
def test_non_charging_commands_refused(client, name, params):
    client.dry_run = False
    with pytest.raises(PermissionError):
        client.command("VIN", name, **params)
    assert client.calls == []


@pytest.mark.parametrize("params", [{"charging_amps": 80}, {"charging_amps": 2}, {"amps": 10}])
def test_bad_amps_rejected(client, params):
    with pytest.raises(ValueError):
        client.command("VIN", "set_charging_amps", **params)


def test_budget_hard_cap(tmp_path):
    b = Budget(caps={"data": 2, "command": 1, "wake": 0}, path=tmp_path / "b.json")
    b.spend("data")
    b.spend("data")
    with pytest.raises(RuntimeError):
        b.spend("data")
    with pytest.raises(RuntimeError):
        b.spend("wake")
    assert b.usage()["data"] == "2/2"


def test_read_charge_state(client):
    vin = client.vehicles()[0]["vin"]
    assert client.charge_state(vin)["battery_level"] == 55
    assert client.budget.usage()["data"].startswith("2/")


def test_token_file_is_private(tmp_path, monkeypatch):
    monkeypatch.setattr(Tokens, "PATH", tmp_path / "t.json")
    Tokens("a", "r", 1.0).save()
    assert (tmp_path / "t.json").stat().st_mode & 0o077 == 0


def test_only_charging_commands_allowlisted():
    assert set(fleet.ALLOWED_COMMANDS) == {"charge_start", "charge_stop", "set_charging_amps", "set_charge_limit"}


# --- day-ahead + tonight planner ----------------------------------------------------

DAM_HTML = "<table><tr><th>Oper Day</th><th>Hour Ending</th><th>LZ_NORTH</th></tr>" + "".join(
    f"<tr><td>11/26/2026</td><td>{h}</td><td>{p}</td></tr>"
    for h, p in zip(range(1, 25), [30, 25, 10, 5, 8, 40] + [50] * 18)
) + "</table>"


def test_parse_dam_hour_ending():
    p = parse_dam(DAM_HTML)["LZ_NORTH"]
    assert p[0] == (datetime(2026, 11, 26, 6, tzinfo=timezone.utc), 30.0)  # HE1 = 00:00 CST
    assert len(p) == 24


def test_parse_dam_unpublished_day():
    assert parse_dam("<html><body>no table yet</body></html>") == {}


def test_next_departure_rolls_to_tomorrow():
    now = datetime(2026, 11, 26, 14, tzinfo=timezone.utc)  # 08:00 CST
    assert next_departure(now, time(7, 0)) == datetime(2026, 11, 27, 13, tzinfo=timezone.utc)


def test_plan_picks_cheapest_hours_and_emits_commands():
    hourly = parse_dam(DAM_HTML)["LZ_NORTH"]
    now = datetime(2026, 11, 26, 6, 1, tzinfo=timezone.utc)  # 00:01 CST, depart 07:00
    v = Vehicle(charger_kw=11.5, efficiency=0.9, battery_kwh=75)
    plan = plan_tonight(hourly, soc_pct=70, target_pct=80, vehicle=v, now=now)
    assert plan.schedule.feasible
    assert plan.energy_needed_kwh == pytest.approx(7.5)
    charged = [t for t, g in zip(plan.slots_utc, plan.schedule.grid_kwh) if g > 0]
    # cheapest hour is HE4 (03:00-04:00 CST = 09:00 UTC)
    assert all(t.hour == 9 for t in charged)
    names = [c.command for c in plan.commands]
    assert names[0] == "set_charging_amps" and "charge_start" in names and names[-1] == "charge_stop"
    assert plan.schedule.cost_usd(plan.price) < np.inf


def test_plan_missing_prices_raises():
    hourly = parse_dam(DAM_HTML)["LZ_NORTH"][:3]
    with pytest.raises(LookupError):
        plan_tonight(hourly, 50, 80, Vehicle(), datetime(2026, 11, 26, 6, 1, tzinfo=timezone.utc))


def test_plan_already_full_does_nothing():
    hourly = parse_dam(DAM_HTML)["LZ_NORTH"]
    plan = plan_tonight(hourly, 85, 80, Vehicle(), datetime(2026, 11, 26, 6, 1, tzinfo=timezone.utc))
    assert plan.commands == [] and plan.energy_needed_kwh == 0
