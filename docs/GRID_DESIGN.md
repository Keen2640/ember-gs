# Grid-aware EV charging and a virtual power plant: design

## Problem

Texas runs its own grid (ERCOT). Wholesale prices swing from below zero to
thousands of dollars per MWh within a day, and the grid came close to
collapse in February 2021. Millions of EVs will plug in every evening. If they
all charge at once they create a new peak, but they can also serve as a huge
flexible load that soaks up cheap wind and solar and backs off when the grid
is stressed.

**Goal:** software that charges a real Tesla at the best times (cost and
carbon), then scales to coordinate a simulated fleet of EVs as a virtual power
plant, measured against real ERCOT history.

## Architecture

```
 Data (all public)                         Intelligence                         Action
 ───────────────────                       ─────────────────────                ───────────────────────
 ERCOT real-time prices (15 min) ─┐        price + carbon forecasters ─┐        Tesla Fleet API (signed cmds)
 ERCOT fuel mix (5 min)          ─┼──────▶ (seasonal naive → GBM →     ├──────▶  set_charging_amps,
 EIA hourly fuel mix (years)     ─┤         transformer)               │         charge_start/stop,
 GOES-19 imagery (5 min, AWS)    ─┘        solar nowcast from clouds   │         set_charge_limit
                                                                       │
                                           single-car optimizer ───────┤        Fleet simulator (C++/Rust)
                                           (exact LP → stochastic MPC)  └──────▶  100k EVs, coordinated
                                                                                  with ADMM vs. herding
 Car telemetry (Fleet Telemetry, mTLS) ──▶ TimescaleDB ◀── all series, decisions, outcomes
```

## Components and status

| Component | Status |
|---|---|
| ERCOT live prices + yearly 15-min archive parser (DST-safe, UTC throughout) | ✅ |
| ERCOT live fuel mix → average carbon intensity | ✅ |
| EIA v2 client with retry/backoff (multi-year carbon history) | ✅ (EIA was returning 504s on the first try) |
| Exact single-session optimizer (fractional knapsack, brute-force verified) | ✅ |
| Backtester: immediate / off-peak / forecast / oracle, settled at real prices | ✅ |
| Day-ahead strategy, ML residual forecaster, MPC, with train/validate/test protocol | ✅ |
| 5-minute real-time prices (lower latency for spike reaction) | ⏳ |
| Tesla Fleet API: OAuth, virtual key, signed commands, dry-run mode | ⏳ |
| Fleet Telemetry receiver on AWS (mTLS, protobuf) | ⏳ |
| GOES-19 solar nowcasting | ⏳ |
| Fleet simulator + distributed coordination (ADMM), grid-emergency replays | ⏳ |

## Results: single car, Dallas (LZ_NORTH)

Protocol: every forecaster sees only data available at plug-in (18:00 CT).
Model choices were made on 2024 data only (train Jan-Sep, validate Oct-Dec),
then **one** run on 2025, which no model saw. 30 kWh/night, depart 07:00,
11.5 kW, settled at real-time prices. Reproduce with
`ember grid backtest --year 2025 --train-year 2024`.

| Strategy | $/year | vs Tesla-style off-peak | Share of headroom captured |
|---|---|---|---|
| Charge immediately | 701.87 | -110% | n/a |
| Fixed off-peak (23:00 start) | 334.72 | baseline | 0% |
| Seasonal naive (last 7 days) | 313.55 | +6.3% | 34% |
| **Day-ahead market price** | **306.17** | **+8.5%** | **46%** |
| ML: gradient-boosted residual on day-ahead | 305.69 | +8.7% | 47% |
| Re-plan every 15 min (MPC) | 306.17* | +8.5% | 46% |
| Perfect foresight (upper bound) | 272.55 | +18.6% | 100% |

\*MPC with any persistence of the latest real-time/day-ahead gap was *worse*
than ignoring it on 2024 validation (by 4.5–9%), so model selection chose
ρ = 0, which reduces to the day-ahead plan.

### What we learned

1. **"Don't charge at 6 pm" is most of the win** (52%). Any claim has to beat a fixed off-peak schedule, not the naive one.
2. **The day-ahead market is a strong forecaster.** It beats the seasonal naive model and captures almost half the headroom with no ML at all.
3. **ML on top of day-ahead adds little** (+0.2 points on test). Predicting the *direct* price lost to day-ahead; predicting the *residual* won validation but ties on test. At 18:00 the market has already priced what is knowable about the night.
4. **Reacting to spikes doesn't work with 15-minute settlement prices.** ERCOT spikes are short. By the time an interval's price is published the spike is usually over, so chasing it makes things worse.
5. **The remaining ~54% of headroom is unforecastable spike noise at the single-car level.** For one car, the day-ahead plan is close to the best achievable. That redirects the effort to where the leverage is: **fleet coordination**, where thousands of cars following the same day-ahead signal create their own peak, and to carbon.

## The herding problem (the research core)

If every car follows the same price signal, they all switch on in the same
cheapest interval. At scale that creates a new demand spike and moves the
price, so the forecast invalidates itself. The approach:
- model the price response to aggregate EV load (a simple supply curve fit from ERCOT data),
- coordinate cars with **ADMM** (each car solves its own small problem, a
  coordinator updates a shared price, and they iterate), keeping each car's data private,
- compare against the uncoordinated and centralized solutions on peak load,
  total cost and fairness.

## Safety rules for the real car

- Only charging commands (`set_charging_amps`, `charge_start`, `charge_stop`,
  `set_charge_limit`). Never doors, locks or anything related to driving.
- Default to **dry-run**: log what would be sent. Live mode is opt-in.
- Hard floor: never schedule below the user's minimum state of charge, and
  never miss the departure target. When in doubt, charge.
