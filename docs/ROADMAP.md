# Roadmap

The core (Days 1–7) ends with one real satellite contact and a measured head
start over FIRMS. Paid steps are marked 💲; everything else is free.

## Day 1: foundations ✅
- [x] Repo, packaging, CI (pytest, ruff, cfn-lint, PostGIS schema check)
- [x] CCSDS link layer from scratch: ASM sync (bit-slip + inversion tolerant), PN derandomizer, RS(255,223) dual basis I=4, AOS/VCDU parsing, M_PDU packet reassembly with gap recovery
- [x] Synthetic downlink generator + end-to-end round-trip tests
- [x] Pass planner over AWS Ground Station sites, using NOAA's operational TLEs from the public JPSS S3 buckets, with per-pass cost estimates
- [x] CloudFormation: Ground Station mission profile → S3 delivery; budget guardrail
- [x] NASA FIRMS client + latency recorder
- [x] PostGIS schema with the `head_start` view

## Grid track (current focus)
- [x] ERCOT live + historical prices, live fuel mix, carbon intensity, EIA client
- [x] Exact charging optimizer + backtester (immediate / off-peak / forecast / oracle)
- [ ] Forecasters: gradient boosting with load/wind/temperature features, quantile spike model; target the 6.4% → 18.7% gap
- [ ] Multi-year backtests (2021 Winter Storm Uri, 2023 heat emergencies), carbon-weighted objective
- [ ] Tesla Fleet API module: OAuth, virtual key pairing, signed commands, dry-run mode
- [ ] Fleet Telemetry receiver on AWS + TimescaleDB
- [ ] Re-plan every 15 min (MPC) on the real car; 8-week measured results
- [ ] GOES-19 solar nowcasting feature
- [ ] Fleet simulator (C++/Rust) + ADMM coordination vs. herding; grid-emergency replays

## Satellite track

## Day 2: accounts + a free baseline
- [ ] FIRMS MAP_KEY, Earthdata login, AWS MFA, budget stack deployed, onboarding request sent
- [ ] FIRMS latency recorder running continuously (South Africa, Chile/Argentina, Middle East boxes)
- [ ] Download NOAA-20 VIIRS I-band SDRs (I4 3.7 µm, I5 11 µm) from `s3://noaa-nesdis-n20-pds`
- [ ] Implement the classic contextual fire test (bright I4 pixel vs its neighbourhood) and compare with FIRMS for the same overpass

## Day 3: the radio part + speed
- [ ] Extend the synthesizer to produce QPSK IQ samples (rate-1/2 convolutional code, RRC pulse shaping, frequency offset, noise)
- [ ] QPSK demodulator: RRC matched filter, Gardner timing recovery, Costas carrier loop
- [ ] Viterbi decoder (K=7, r=1/2, CCSDS polynomials)
- [ ] C++ (pybind11) Viterbi + RS + sync; benchmark against Python; target > 15 Mbit/s real time
- [ ] Parser for the S3 delivery format (PCAP / VITA-49) to get IQ samples out

## Day 4: packets → pictures
- [ ] Decode VIIRS science packets (APIDs 800–826): scan assembly, I4/I5 counts
- [ ] Calibrate counts → brightness temperature
- [ ] Geolocation from spacecraft ephemeris/attitude packets
- [ ] Cross-check against NOAA's SDR for the same scan

## Day 5: the fire model
- [ ] Training set: VIIRS SDR patches labeled with FIRMS detections (free, months of data)
- [ ] Gradient boosting baseline → small CNN; report precision/recall against FIRMS
- [ ] Model packaged as a container

## Day 6: the cloud pipeline
- [ ] S3 capture → EventBridge → ECS Fargate/Batch decoder → detections in PostGIS (RDS or local) → SNS/webhook alerts
- [ ] Live map (MapLibre) with our detections vs FIRMS
- [ ] Replay mode: run the whole pipeline on a recorded or synthetic pass

## Day 7: first light 💲
- [ ] Choose a pass with `ember passes` over an active fire region outside FIRMS ultra-real-time coverage
- [ ] Reserve it, decode it, detect, alert, and query `head_start`
- [ ] README with the measured head start, a demo video, write-up

## Stretch
- Spectrum + constellation plots and SNR estimate per pass
- Multi-satellite scheduling optimizer (maximize fire-season coverage per dollar)
- Compare against the GOES geostationary fire product (minutes-level latency, coarser resolution)
