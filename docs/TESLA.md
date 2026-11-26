# Tesla setup

All Tesla steps run **on your own computer**. The client secret, the private
key and the login tokens never leave it: they live in `~/.ember/` with
owner-only permissions and are never committed.

What the app can do: read charge state, and start/stop charging, set charging
amps and set the charge limit. Nothing else is on the allowlist (see
`src/ember/tesla/fleet.py`), and every command is **dry-run** unless you pass `--live`.

## 0. Install (once)

```bash
git clone https://github.com/Keen2640/ember-gs
```
```bash
cd ember-gs && python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
```

Needs Python 3.10+ (`brew install python@3.12` if `python3 --version` is older).

## 1. Store the app credentials (once)

From developer.tesla.com → your app → Credentials:

```bash
mkdir -p ~/.ember && chmod 700 ~/.ember
```
```bash
nano ~/.ember/.env
```

Put these two lines in the file, save, then lock it down:

```
TESLA_CLIENT_ID=paste-client-id
TESLA_CLIENT_SECRET=paste-client-secret
```
```bash
chmod 600 ~/.ember/.env
```

Load it in each new terminal:

```bash
set -a; source ~/.ember/.env; set +a
```

## 2. Generate the app's key pair (once)

```bash
ember tesla keygen --out pages/.well-known/appspecific/com.tesla.3p.public-key.pem
```

The private key goes to `~/.ember/tesla-private-key.pem`. The **public** key
file is safe to publish; that's its job.

## 3. Publish the public key on GitHub Pages (once)

1. Create a public repo named exactly `Keen2640.github.io` (empty, no README).
2. Copy everything in this repo's `pages/` folder into it (including `.nojekyll` and `.well-known/`) and push.
3. In that repo: Settings → Pages → Source: *Deploy from a branch*, branch `main`, folder `/ (root)`.
4. After a minute, check that this command prints the key:

```bash
curl https://keen2640.github.io/.well-known/appspecific/com.tesla.3p.public-key.pem
```

## 4. Register the domain with Tesla (once)

```bash
ember tesla register --domain keen2640.github.io
```

## 5. Owner login (once; tokens refresh automatically)

The car owner signs in and approves the two permissions:

```bash
ember tesla login
```

If the localhost redirect isn't registered, use the GitHub Pages callback
instead: `ember tesla login --paste`, then paste the code the page shows.

## 6. Pair the virtual key with the car (once)

On the **owner's phone** (with the Tesla app installed), open:

```
https://tesla.com/_ak/keen2640.github.io
```

and approve adding the key. Vehicles on recent firmware need this before they
accept any command.

## 7. Use it

```bash
ember tesla status          # charge state; never wakes a sleeping car
```
```bash
ember tesla plan            # tonight's plan from ERCOT day-ahead prices (dry-run)
```

Live commands come later, through Tesla's signing proxy (`tesla-http-proxy`
from github.com/teslamotors/vehicle-command), once dry-run plans have been
checked for a few nights.

## Usage limits

`~/.ember/tesla_budget.json` counts calls per month and refuses to go over
3,000 data reads, 1,000 commands and 60 wake-ups, which keeps usage inside
Tesla's monthly free credit. `ember tesla status` prints current usage.
