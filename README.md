# ember-gs: satellite and grid intelligence on AWS

Two systems built on one data platform:

1. **Grid-aware EV charging / virtual power plant.** Forecasts Texas (ERCOT) electricity prices and carbon intensity, optimizes when a real Tesla charges, and scales to coordinating a simulated EV fleet during grid emergencies. Design: [docs/GRID_DESIGN.md](docs/GRID_DESIGN.md)
2. **Cloud satellite ground station for wildfire detection** (below).

### Tesla integration (charging only, dry-run by default)

`ember tesla plan` builds tonight's charging plan from ERCOT **day-ahead** prices and the car's charge state, then converts it into timed `set_charging_amps` / `charge_start` / `charge_stop` commands. Signed commands, an allowlist and a monthly request budget are built in. Setup: [docs/TESLA.md](docs/TESLA.md)

### Grid quickstart

```bash
ember grid now                       # live Dallas price + grid carbon intensity
ember grid backtest --year 2025 --train-year 2024   # train on 2024, test on 2025
```

```
LZ_NORTH 2025: 364 nights, 30 kWh/night, 11.5 kW charger, settled at real-time wholesale prices

strategy     total $   ¢/kWh  worst night $  vs immediate  vs offpeak  headroom
immediate     701.87    6.43          28.31         0.0%    -109.7%    -591%
offpeak       334.72    3.07           4.20        52.3%       0.0%       0%
naive         313.55    2.87           4.14        55.3%       6.3%      34%
dayahead      306.17    2.80           6.99        56.4%       8.5%      46%
ml            305.69    2.80           6.73        56.4%       8.7%      47%
oracle        272.55    2.50           3.77        61.2%      18.6%     100%
```

ML trained on 2024, tested once on 2025. The day-ahead market turns out to be very hard to beat for a single car; see the findings in [docs/GRID_DESIGN.md](docs/GRID_DESIGN.md).

---

## Satellite ground station

**ember-gs receives the raw radio downlink from NOAA/NASA polar-orbiting satellites through AWS Ground Station antennas, decodes it with a link-layer stack written from scratch, and flags new wildfires minutes after the satellite flies overhead.** The goal is to deliver alerts faster than NASA's standard FIRMS fire product in regions without existing direct-readout coverage.

```
 NOAA-20 / NOAA-21 (VIIRS)                AWS Ground Station antenna
   X-band 7812 MHz, 15 Mbps   ───────▶   (Cape Town, Bahrain, Punta Arenas, ...)
                                                  │ digital IF
                                                  ▼
                                         S3 (aws-groundstation-*)  ── EventBridge
                                                  │
     ┌────────────────────────────────────────────┴──────────────────────────────┐
     │  ember decoder                                                              │
     │  QPSK demod → Viterbi → ASM sync → derandomize → Reed-Solomon (255,223)    │
     │  → AOS/VCDU → space packets → VIIRS scans → radiances + geolocation       │
     └────────────────────────────────────────────┬──────────────────────────────┘
                                                  ▼
                              fire detector (I4/I5 bands, ML) → PostGIS → alerts + live map
                                                  ▲
                         NASA FIRMS poller ───────┘  (baseline: "how much earlier were we?")
```

## Status

| Component | State |
|---|---|
| CCSDS link layer: sync, PN derandomizer, RS(255,223) with dual basis and I=4, AOS frames, packet reassembly | ✅ written from scratch, tested |
| Synthetic downlink generator (bit slips, phase inversion, bit errors, dropped frames) | ✅ |
| Pass planner over AWS Ground Station sites, with cost estimates, using NOAA's operational TLEs from S3 | ✅ |
| AWS infra: Ground Station mission profile → S3 delivery, budget guardrail (CloudFormation) | ✅ cfn-lint clean, not yet deployed |
| NASA FIRMS client + latency recorder | ✅ |
| PostGIS schema incl. `head_start` view (minutes ahead of FIRMS) | ✅ validated on PostGIS |
| QPSK demodulator + Viterbi decoder | ⏳ Day 3 |
| C++ hot path (target: faster than the 15 Mbps link) | ⏳ Day 3 |
| VIIRS packet → image + geolocation | ⏳ Day 4 |
| Fire detection model | ⏳ Day 5 |
| Event-driven AWS pipeline + live map | ⏳ Day 6 |
| First real satellite contact | ⏳ Day 7 |

See [docs/ROADMAP.md](docs/ROADMAP.md) for the plan and [docs/ENGINEERING_LOG.md](docs/ENGINEERING_LOG.md) for findings and debugging notes.

## Try it

```bash
pip install -e ".[dev,aws]"
pytest -q

# Generate a damaged downlink (5-bit slip, inverted, BER 5e-4) and decode it
ember synth /tmp/demo.bin --packets 1000 --shift 5 --invert --ber 5e-4
ember decode /tmp/demo.bin

# Upcoming NOAA-20 passes over AWS antennas (live TLE from NOAA's public S3 bucket)
ember passes --sat NOAA-20 --hours 24 --min-max-el 30
```

Example decode output:

```
CADUs found            266  (inverted 266, resyncs 0)
Reed-Solomon           clean 3, corrected 263 (1072 symbols), failed 0
Spacecraft             NOAA-20 (JPSS-1): 266
Space packets          150  (frames lost to gaps 0)
```

## Why this is hard

* **No library does the link layer for you here.** Reed-Solomon over GF(2⁸) in Berlekamp's dual basis, the CCSDS randomizer and frame sync with phase ambiguity are all implemented and tested from the standard (CCSDS 131.0-B, 732.0-B).
* **Each contact costs real money** (roughly $100+ per pass), so the whole pipeline is built and tested against synthetic and public NOAA data before the first paid contact.
* **Real time:** a pass delivers about 1 GB in about 10 minutes. The Python reference decoder runs at about 2 Mbit/s; the C++ version has to beat the 15 Mbit/s link rate.
* **The baseline is strong:** FIRMS already has ultra-real-time coverage for the US and Canada, so the claim is measured where it isn't (southern Africa, South America, the Middle East).

## Repository layout

```
src/ember/ccsds/    link layer: randomizer, reed_solomon, framing, aos, packets, pipeline, synth
src/ember/orbit/    TLEs (NOAA S3 / CelesTrak), AWS station sites, pass prediction + cost
src/ember/firms/    NASA FIRMS client + latency store
src/ember/grid/     ERCOT prices + fuel mix, carbon intensity, EIA client
src/ember/charging/ exact charging optimizer, historical backtester, tonight planner
src/ember/tesla/    Fleet API: OAuth, partner registration, allowlisted commands, budget
pages/              GitHub Pages site: public key + OAuth callback
src/ember/aws_gs.py AWS Ground Station API wrapper (reservations need --yes and a budget cap)
infra/              CloudFormation: mission profile + S3 delivery, budget guardrail
db/schema.sql       PostGIS schema
docs/               setup guide, roadmap, engineering log
```

## Setup

Accounts, AWS onboarding and cost controls are covered in **[docs/SETUP.md](docs/SETUP.md)**.

## License

MIT
