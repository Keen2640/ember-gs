# Engineering log

Findings, dead ends and debugging notes. Interviewers ask about these.

## Day 1

**Terra is out; JPSS is in.** NASA announced Terra science collection ends
December 1, 2026, and both MODIS platforms are winding down, with Aqua's
projected end of science in 2027. NASA is steering users to VIIRS. The primary
targets are therefore NOAA-20 and NOAA-21 (VIIRS, X-band HRD at 7812 MHz,
15 Mbps), not Terra/Aqua.

**SNPP's TLEs in the NOAA bucket go stale in early November 2026**, and no
recent SNPP VIIRS SDRs appear there either. `ember passes` now warns when the
TLE is more than 7 days old. Treat SNPP as secondary.

**Orbital elements without CelesTrak.** NOAA publishes its operational TLEs as
tiny HDF5 files (`TLE-AUX/…/*.h5`, dataset `All_Data/TLE-AUX_All/Dataset_Array_0`)
in the public JPSS buckets. Using them keeps the whole system on AWS with no account.

**Cost reality check.** At an estimated $10/min, a typical 10–11 minute pass
plus 3 minutes of padding is about $125–140. The $100 credit doesn't cover a
full pass. Plan: build everything on synthetic and public data, apply for
research credits, and spend on a single well-chosen contact.

**PN randomizer bit order.** The first implementation produced
`FF 5A EA B2…` instead of the standard `FF 48 0E C0…`. Rather than guess, I
brute-forced every tap set / output bit / shift direction for an 8-bit LFSR and
matched the published sequence: output the LSB, feedback = s0⊕s3⊕s5⊕s7, shift
right. Now pinned by a unit test.

**Reed-Solomon dual basis.** CCSDS RS symbols are in Berlekamp's dual basis.
The conversion table built from the T matrix reproduces the published first
entries (00 7B AF D4 99 E2), which is also pinned by a test. The codec corrects
exactly 16 errors per codeword and flags all 24-error cases as uncorrectable.
It still has to be confirmed on real frames: real JPSS frames should decode
with mostly 0 corrections.

**Throughput.** The pure-Python decoder does about 2 Mbit/s, well short of the
15 Mbit/s link. That's expected for a reference implementation; the C++ port
(Day 3) is checked against it bit for bit.

**FIRMS baseline caveat.** FIRMS already has ultra-real-time coverage for the
US and Canada built on direct-readout stations. A fair "faster than FIRMS"
claim has to be measured elsewhere: Cape Town, Punta Arenas and Bahrain are
AWS sites covering fire-prone regions.

## Day 2: pivot to grid-aware EV charging

**Why the pivot.** Fires are rare; charging happens every day. ERCOT gives
public 15-minute prices, a 5-minute fuel mix and yearly archives, which is
enough to build and measure a real system immediately. The satellite pipeline
stays (GOES imagery will feed solar nowcasting).

**Load zones are listed twice.** The yearly archive has every load-zone price
under two settlement-point types: `LZ` and `LZEW` (energy-weighted). The first
parse produced 70,080 intervals for a 35,040-interval year. The dollar result
barely moved because the two series are nearly identical, but it was wrong.
The parser now filters on `LZ` by default and *raises* on duplicate
timestamps, so this can't come back silently.

**DST, the classic bug source.** ERCOT uses hour-ending Central Prevailing
Time with a "repeated hour" flag on the autumn DST day. Everything is
converted once to UTC interval starts (using `fold=1` for the repeated hour),
and a test pins the November 2, 2025 case.

**EIA returned 504s.** The v2 grid-monitor endpoint timed out repeatedly
(server side; the key itself was valid). The client retries with exponential
backoff, and live carbon comes from ERCOT's own 5-minute fuel mix, which is
fresher anyway.

**First backtest (2025, Dallas).** Off-peak scheduling alone saves 52% versus
charging at plug-in. A seasonal-naive forecast + exact optimizer adds 6.4% on
top. Perfect foresight would add 18.7%. Price data has 891 negative-price
intervals and a maximum of $3,735/MWh, so spikes dominate: a forecaster that
predicts *spike risk* (quantiles) matters more than one that nails the median.

## Day 3: Tesla integration

**Tesla approved the app** (`ember-gs`) with the minimum two scopes:
vehicle data and charging management. "Vehicle Commands" (unlock, remote
start) was deliberately left out, and the client also enforces an allowlist
of four charging commands, so a bug can't do anything else.

**The market already forecasts tonight.** ERCOT publishes next-day hourly
day-ahead prices around 13:30 CT, so an evening plan can be built on the
market's own forecast instead of the seasonal-naive one. The page for the
next day exists before it's published, just empty, and the parser returns
nothing rather than crashing.

**Cost discipline.** Tesla meters API calls; wake-ups are the most expensive.
`status` never wakes the car, and a persistent per-month budget refuses calls
past the caps.

## Day 4: can ML beat the market?

**Protocol first.** Every model choice was made on 2024 (train Jan-Sep,
validate Oct-Dec), then tested once on 2025. Without that discipline it's
easy to tune on the test year and report a number that won't hold up.

**Day-ahead beats seasonal naive** (+8.5% vs +6.3% over off-peak on 2025).
The market's forecast is better than "same as last week."

**Direct ML lost to day-ahead** (+5.9%). Switching the target to the
*residual* (real-time minus day-ahead), with shrinkage toward day-ahead, won
validation (+12.7% vs +9.7%) but only ties day-ahead on test (+8.7% vs +8.5%).

**MPC made things worse.** Re-planning every 15 minutes with the latest
real-time/day-ahead gap carried forward lost 4.5-9% on validation for every
persistence value. ERCOT spikes are short and mean-revert, and a 15-minute
settlement price arrives after the spike. Model selection picked "ignore it"
(ρ = 0). The code and tests stay so the result is reproducible, and 5-minute
prices are the next thing to try.

**Takeaway.** For one car, the day-ahead plan is close to the best
achievable; the remaining headroom is noise you can't see coming. The
leverage is at fleet scale, where everyone following the same cheap hours
creates a new peak.
