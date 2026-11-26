"""Tesla Fleet API client: OAuth, partner registration, charge state, charging commands.

Design rules (see docs/TESLA.md):
  * Secrets (client secret, private key, tokens) live only on the owner's machine:
    environment variables plus ~/.ember/ with 0600 permissions. Never in the repo, never printed.
  * Only an allowlist of charging commands can be sent. Anything else raises.
  * Commands are DRY-RUN by default: logged, not sent. Live mode is an explicit opt-in.
  * A persistent monthly request budget keeps usage inside Tesla's free monthly credit.

Vehicles on recent firmware only accept commands signed with the app's private
key (Tesla's Vehicle Command Protocol). For those, point ``command_base_url``
at a local ``tesla-http-proxy`` (from github.com/teslamotors/vehicle-command),
which signs requests with the same REST shape used here.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

AUTHORIZE_URL = "https://auth.tesla.com/oauth2/v3/authorize"
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
FLEET_API = {
    "na": "https://fleet-api.prd.na.vn.cloud.tesla.com",
    "eu": "https://fleet-api.prd.eu.vn.cloud.tesla.com",
}
SCOPES = ("openid", "offline_access", "vehicle_device_data", "vehicle_charging_cmds")

# The only commands this project will ever send.
ALLOWED_COMMANDS = {
    "charge_start": set(),
    "charge_stop": set(),
    "set_charging_amps": {"charging_amps"},
    "set_charge_limit": {"percent"},
}

HOME = Path(os.environ.get("EMBER_HOME", Path.home() / ".ember"))


def _private_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


@dataclass
class Credentials:
    client_id: str
    client_secret: str
    redirect_uri: str = "http://localhost:8080/callback"
    region: str = "na"

    @classmethod
    def from_env(cls) -> Credentials:
        cid, sec = os.environ.get("TESLA_CLIENT_ID"), os.environ.get("TESLA_CLIENT_SECRET")
        if not cid or not sec:
            raise RuntimeError("set TESLA_CLIENT_ID and TESLA_CLIENT_SECRET (in ~/.ember/.env, never in the repo)")
        return cls(cid, sec, os.environ.get("TESLA_REDIRECT_URI", cls.redirect_uri), os.environ.get("TESLA_REGION", "na"))

    @property
    def api(self) -> str:
        return FLEET_API[self.region]


# --- OAuth ---------------------------------------------------------------------

def _post_form(url: str, form: dict) -> dict:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Tesla auth failed (HTTP {e.code}): {body}") from None


def authorize_url(creds: Credentials, state: str | None = None) -> tuple[str, str]:
    state = state or secrets.token_urlsafe(16)
    q = {
        "response_type": "code",
        "client_id": creds.client_id,
        "redirect_uri": creds.redirect_uri,
        "scope": " ".join(SCOPES),
        "state": state,
        "prompt_missing_scopes": "true",
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(q)}", state


def partner_token(creds: Credentials) -> str:
    """App-level token (client credentials) used once to register the domain."""
    r = _post_form(TOKEN_URL, {
        "grant_type": "client_credentials",
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scope": "openid vehicle_device_data vehicle_charging_cmds",
        "audience": creds.api,
    })
    return r["access_token"]


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float

    PATH = HOME / "tesla_tokens.json"

    def save(self) -> None:
        _private_write(self.PATH, json.dumps(self.__dict__))

    @classmethod
    def load(cls) -> Tokens:
        if not cls.PATH.exists():
            raise RuntimeError("not logged in: run `ember tesla login` first")
        return cls(**json.loads(cls.PATH.read_text()))

    @classmethod
    def from_response(cls, r: dict) -> Tokens:
        return cls(r["access_token"], r["refresh_token"], time.time() + float(r.get("expires_in", 28800)) - 60)


def exchange_code(creds: Credentials, code: str) -> Tokens:
    r = _post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "code": code,
        "audience": creds.api,
        "redirect_uri": creds.redirect_uri,
    })
    return Tokens.from_response(r)


def refresh(creds: Credentials, tokens: Tokens) -> Tokens:
    r = _post_form(TOKEN_URL, {
        "grant_type": "refresh_token",
        "client_id": creds.client_id,
        "refresh_token": tokens.refresh_token,
    })
    return Tokens.from_response(r)


def wait_for_callback(port: int = 8080, expected_state: str | None = None, timeout: float = 300) -> str:
    """Run a one-shot local HTTP server and return the ?code= from Tesla's redirect."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ok = "code" in q and (expected_state is None or q.get("state", [""])[0] == expected_state)
            if ok:
                result["code"] = q["code"][0]
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ember: login complete, you can close this tab." if ok else b"ember: bad callback")

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", port), Handler)
    srv.timeout = 5
    deadline = time.time() + timeout
    while "code" not in result and time.time() < deadline:
        srv.handle_request()
    srv.server_close()
    if "code" not in result:
        raise TimeoutError("no OAuth callback received")
    return result["code"]


# --- request budget ------------------------------------------------------------

@dataclass
class Budget:
    """Persistent per-month request counter with a hard cap per category."""

    caps: dict[str, int] = field(default_factory=lambda: {"data": 3000, "command": 1000, "wake": 60})
    path: Path = HOME / "tesla_budget.json"

    def _state(self) -> dict:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        try:
            s = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            s = {}
        return s if s.get("month") == month else {"month": month, "counts": {}}

    def spend(self, kind: str) -> None:
        s = self._state()
        n = s["counts"].get(kind, 0)
        if n >= self.caps[kind]:
            raise RuntimeError(f"monthly {kind} budget exhausted ({n}/{self.caps[kind]}); refusing to call Tesla")
        s["counts"][kind] = n + 1
        _private_write(self.path, json.dumps(s))

    def usage(self) -> dict[str, str]:
        c = self._state()["counts"]
        return {k: f"{c.get(k, 0)}/{cap}" for k, cap in self.caps.items()}


# --- Fleet API client ------------------------------------------------------------

class FleetClient:
    def __init__(
        self,
        creds: Credentials,
        tokens: Tokens | None = None,
        dry_run: bool = True,
        command_base_url: str | None = None,
        budget: Budget | None = None,
        transport=None,
    ) -> None:
        self.creds = creds
        self.tokens = tokens
        self.dry_run = dry_run
        self.command_base = command_base_url or os.environ.get("TESLA_COMMAND_PROXY") or creds.api
        self.budget = budget or Budget()
        self.transport = transport or self._http
        self.log: list[dict] = []

    def _token(self) -> str:
        if self.tokens is None:
            self.tokens = Tokens.load()
        if time.time() >= self.tokens.expires_at:
            self.tokens = refresh(self.creds, self.tokens)
            self.tokens.save()
        return self.tokens.access_token

    def _http(self, method: str, url: str, body: dict | None, token: str) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Fleet API {method} {urllib.parse.urlparse(url).path} -> HTTP {e.code}: "
                               f"{e.read().decode(errors='replace')[:200]}") from None

    def _call(self, kind: str, method: str, url: str, body: dict | None = None) -> dict:
        self.budget.spend(kind)
        return self.transport(method, url, body, self._token())

    # data
    def vehicles(self) -> list[dict]:
        return self._call("data", "GET", f"{self.creds.api}/api/1/vehicles")["response"]

    def charge_state(self, vin: str) -> dict:
        url = f"{self.creds.api}/api/1/vehicles/{vin}/vehicle_data?endpoints=charge_state"
        return self._call("data", "GET", url)["response"]["charge_state"]

    def wake_up(self, vin: str) -> dict:
        return self._call("wake", "POST", f"{self.creds.api}/api/1/vehicles/{vin}/wake_up")["response"]

    # commands
    def command(self, vin: str, name: str, **params) -> dict:
        if name not in ALLOWED_COMMANDS:
            raise PermissionError(f"command {name!r} is not on the charging allowlist")
        if set(params) != ALLOWED_COMMANDS[name]:
            raise ValueError(f"{name} takes exactly {sorted(ALLOWED_COMMANDS[name])}")
        if name == "set_charging_amps" and not 5 <= params["charging_amps"] <= 48:
            raise ValueError("charging_amps must be 5..48")
        if name == "set_charge_limit" and not 50 <= params["percent"] <= 100:
            raise ValueError("percent must be 50..100")
        entry = {"time": datetime.now(timezone.utc).isoformat(), "vin": vin[-4:], "command": name, "params": params,
                 "mode": "dry-run" if self.dry_run else "live"}
        self.log.append(entry)
        if self.dry_run:
            return {"result": True, "reason": "dry-run"}
        url = f"{self.command_base}/api/1/vehicles/{vin}/command/{name}"
        return self._call("command", "POST", url, params or {})["response"]


def register_partner(creds: Credentials, domain: str) -> dict:
    """One-time: tell Tesla which domain hosts this app's public key."""
    token = partner_token(creds)
    req = urllib.request.Request(
        f"{creds.api}/api/1/partner_accounts",
        data=json.dumps({"domain": domain}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"partner registration failed (HTTP {e.code}): {e.read().decode(errors='replace')[:300]}") from None
