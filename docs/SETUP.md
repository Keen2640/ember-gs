# Setup guide

Everything up to the first satellite contact is **free**. Do the steps in order.

## 1. NASA (free, about 10 minutes)

| What | Where | Why |
|---|---|---|
| **FIRMS MAP_KEY** | https://firms.modaps.eosdis.nasa.gov/api/map_key/ (just your email) | Fire detections API, the baseline you compete against |
| **NASA Earthdata Login** | https://urs.earthdata.nasa.gov/users/new | Downloading VIIRS/MODIS products from LAADS/LANCE later |

Then:

```bash
export FIRMS_MAP_KEY=your_key_here
# Start measuring FIRMS latency over South Africa (runs every 5 minutes)
ember firms --bbox 16,-35,33,-22 --source VIIRS_NOAA20_NRT --source VIIRS_NOAA21_NRT --watch
```

Leave this running for a few days (on your laptop, or later as a scheduled Lambda). It produces the baseline: how many minutes after the satellite pass FIRMS publishes each fire.

NOAA's JPSS data on AWS (`s3://noaa-nesdis-n20-pds` etc.) needs **no account at all**.

## 1b. Grid data keys (free)

| What | Where | Status |
|---|---|---|
| **EIA API key** | https://www.eia.gov/opendata/ | ✅ done |
| **ERCOT API account** | https://apiexplorer.ercot.com | ✅ done (the current code uses ERCOT's public pages and archives, which need no key) |
| NREL API key (solar data, later) | https://developer.nrel.gov/signup/ | ⏳ |

**Keys never go in the repo.** Put them in a local `.env` (already gitignored; see `.env.example`) and load it with `set -a; . ./.env; set +a`.

## 2. AWS account safety (do this before anything else)

1. Turn on **MFA** for the root user and create an IAM user or SSO identity for daily use.
2. **Check whether your $100 in credits covers AWS Ground Station:** Billing → Credits → expand the credit → "Applicable products". Many promotional credits exclude some services. Don't assume.
3. Deploy the budget guardrail (free):

   ```bash
   aws cloudformation deploy --region us-east-2 \
     --stack-name ember-budget --template-file infra/budget-guardrail.yaml \
     --parameter-overrides MonthlyLimitUSD=25 AlertEmail=you@example.com
   ```

   Confirm the subscription email AWS sends you.

## 3. AWS Ground Station access

1. Pick a region with a Ground Station site you want, e.g. `af-south-1` (Cape Town) or `us-east-2` (Ohio, which also schedules Punta Arenas). Some regions (`af-south-1`, `me-south-1`) are opt-in: enable them under Account → AWS Regions.
2. Check which satellites your account can already see:

   ```bash
   aws groundstation list-satellites --region us-east-2
   ```

3. If NOAA-20 (NORAD 43013) / NOAA-21 (54234) aren't listed, request onboarding to the public broadcast satellites. In the AWS Ground Station User Guide, see the "Public broadcast satellites" and "Onboard satellite" sections. If in doubt, open an AWS Support case (Service: Ground Station) using the template in [onboarding-request.md](onboarding-request.md). **Do this now.** It can take days, and the free parts of the project continue in the meantime.
4. Deploy the mission profile stack. It's free to deploy; you only pay for contacts:

   ```bash
   aws cloudformation deploy --region us-east-2 \
     --stack-name ember-gs --template-file infra/groundstation-jpss-s3.yaml \
     --capabilities CAPABILITY_IAM \
     --parameter-overrides BucketSuffix=yourname01
   ```

   Before your first contact, compare it with AWS's official example template `AquaSnppJpss-1TerraDigIfS3DataDelivery.yml` (User Guide → Examples → *Public broadcast satellite utilizing Amazon S3 data delivery*), especially the frequency, bandwidth and S3 recording config.

## 4. Cost plan for $100

`ember passes` estimates about **$125–140 per full pass** at $10/min, which is more than $100. Get the real per-minute rate for your region from the AWS Pricing Calculator. Options:

* **Shorter contact:** a lower-elevation pass (about 7 min) with minimal padding (`PrePassSeconds=60`, `PostPassSeconds=30`) brings billed time to about 8.5 min. Lower elevation means a weaker signal, so aim for 20–35° maximum elevation.
* **More credits (recommended):** apply for the **AWS Cloud Credit for Research** program (students are eligible, and a research project with a clear cost plan is exactly what it funds). Also ask your university or club about AWS Educate or research credits.
* **One pass is enough for the proof.** Build everything on free data first (Days 2–6) and spend credits on a single, carefully chosen contact over an active fire region.

Reserving a contact through this repo requires `--yes` and a budget cap, so there's no accidental spending.

## 5. Local development

```bash
pip install -e ".[dev,aws]"
make test        # 25 tests
make demo        # synthetic downlink → decoder
make db          # PostGIS in Docker with the schema loaded
```
