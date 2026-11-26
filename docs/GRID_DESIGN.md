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
| Better forecasters (gradient boosting with weather + load features; quantiles for spikes) | ⏳ |
| Stochastic MPC: re-plan every 15 min as new prices arrive | ⏳ |
| Tesla Fleet API: OAuth, virtual key, signed commands, dry-run mode | ⏳ |
| Fleet Telemetry receiver on AWS (mTLS, protobuf) | ⏳ |
| GOES-19 solar nowcasting | ⏳ |
| Fleet simulator + distributed coordination (ADMM), grid-emergency replays | ⏳ |

## First result (2025, Dallas load zone LZ_NORTH, wholesale prices)

30 kWh per night, plug in 18:00, depart 07:00, 11.5 kW charger, 357 nights:

| Strategy | $/year | ¢/kWh | Worst night | vs Tesla-style off-peak |
|---|---|---|---|---|
| Charge immediately | 693.97 | 6.48 | $28.31 | -110% |
| Fixed off-peak (23:00 start) | 330.84 | 3.09 | $4.20 | baseline |
| Seasonal-naive forecast + optimizer | 309.72 | 2.89 | $4.14 | **+6.4%** |
| Perfect foresight (upper bound) | 269.06 | 2.51 | $3.77 | +18.7% |

Takeaways:
1. **"Don't charge at 6 pm" is most of the win** (52%). A fixed off-peak
   schedule is a strong baseline, and any claim has to beat it, not the
   naive one.
2. **The forecast-to-oracle gap (6.4% → 18.7%) is the headroom for ML.** That
   gap is the forecasting target.
3. **One car's dollars are small.** The interesting numbers are at fleet
   scale (MW of peak shifted during grid emergencies) and in carbon.

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
