"""ember command line.

  ember passes   --sat NOAA-20 --hours 24        upcoming passes over AWS antennas + cost
  ember synth    out.bin --invert --ber 1e-4     generate a test downlink
  ember decode   capture.bin                     decode a raw bit stream into packets
  ember firms    --bbox W,S,E,N --source ...     record FIRMS detections + latency
  ember grid now --zone LZ_NORTH                 live ERCOT price + grid carbon intensity
  ember grid backtest --year 2025                charging strategies on a year of real prices
"""

from __future__ import annotations

import argparse
import os
import sys
import time


def cmd_passes(a: argparse.Namespace) -> int:
    from .orbit.passes import predict_passes
    from .orbit.stations import AWS_GROUND_STATIONS
    from .orbit.tle import get_tle, parse_tle_text

    if a.tle_file:
        with open(a.tle_file) as f:
            tle = parse_tle_text(a.sat, f.read(), a.tle_file)
    else:
        tle = get_tle(a.sat)
    from datetime import datetime, timezone

    age_days = (datetime.now(timezone.utc) - tle.epoch).total_seconds() / 86400
    if age_days > 7:
        print(f"WARNING: TLE is {age_days:.0f} days old; predictions may be off by minutes.", file=sys.stderr)
    stations = [s for s in AWS_GROUND_STATIONS if not a.station or s.name in a.station]
    print(f"{tle.name} (NORAD {tle.norad_id})  TLE epoch {tle.epoch:%Y-%m-%d %H:%M}Z  source {tle.source}\n")
    passes = predict_passes(tle, stations, hours=a.hours, min_elevation_deg=a.min_el)
    passes = [p for p in passes if p.max_elevation_deg >= a.min_max_el]
    hdr = f"{'AOS (UTC)':<17} {'station':<15} {'region':<15} {'dur':>6} {'max el':>7} {'est. cost':>10}"
    print(hdr)
    print("-" * len(hdr))
    for p in passes:
        print(
            f"{p.aos:%m-%d %H:%M:%S}   {p.station:<15} {p.region:<15} "
            f"{p.duration_s / 60:5.1f}m {p.max_elevation_deg:6.1f}° "
            f"${p.cost_usd(a.price_per_minute):9.2f}"
        )
    print(f"\n{len(passes)} passes. Cost assumes ${a.price_per_minute:.2f}/min incl. 3 min of pre/post-pass padding;")
    print("check the AWS Pricing Calculator for the real rate.")
    return 0


def cmd_synth(a: argparse.Namespace) -> int:
    from .ccsds.synth import Impairments, SynthConfig, generate_stream

    raw, pkts = generate_stream(
        SynthConfig(n_packets=a.packets, seed=a.seed),
        Impairments(bit_shift=a.shift, invert=a.invert, bit_error_rate=a.ber, seed=a.seed + 1),
    )
    with open(a.output, "wb") as f:
        f.write(raw)
    print(f"wrote {len(raw):,} bytes ({len(pkts)} packets) to {a.output}")
    return 0


def cmd_decode(a: argparse.Namespace) -> int:
    from .ccsds.pipeline import decode_stream

    with open(a.input, "rb") as f:
        data = f.read()
    t = time.perf_counter()
    _, report = decode_stream(data, use_rs=not a.no_rs)
    dt = time.perf_counter() - t
    print(report.summary())
    print(f"Throughput             {len(data) * 8 / dt / 1e6:.2f} Mbit/s ({dt:.2f} s)")
    return 0


def cmd_firms(a: argparse.Namespace) -> int:
    from .firms.client import LatencyStore, fetch

    key = a.map_key or os.environ.get("FIRMS_MAP_KEY")
    if not key:
        print("set FIRMS_MAP_KEY (free: https://firms.modaps.eosdis.nasa.gov/api/map_key/)", file=sys.stderr)
        return 2
    bbox = tuple(float(v) for v in a.bbox.split(","))
    store = LatencyStore(a.db)
    while True:
        for src in a.source:
            dets = fetch(key, src, bbox, days=1)
            new = store.record(dets)
            print(f"{time.strftime('%H:%M:%S')} {src}: {len(dets)} detections, {new} new  {store.latency_stats(src)}")
        if not a.watch:
            return 0
        time.sleep(a.interval)


def cmd_grid_now(a: argparse.Namespace) -> int:
    from .grid import carbon, ercot

    prices = ercot.live_prices()[a.zone]
    t, p = prices[-1]
    mix_t, mix = ercot.live_fuel_mix()[-1]
    ci = carbon.intensity(mix)
    top = ", ".join(f"{f} {v:.0%}" for f, v in list(carbon.shares(mix).items())[:4])
    print(f"{a.zone} real-time price   ${p:,.2f}/MWh  ({p / 10:.2f} ¢/kWh wholesale), interval from {t:%H:%M} UTC")
    print(f"Today's range              ${min(x for _, x in prices):,.2f} to ${max(x for _, x in prices):,.2f}/MWh")
    print(f"Grid carbon intensity      {ci:.0f} kg CO2/MWh at {mix_t:%H:%M} UTC  ({top})")
    return 0


def cmd_grid_backtest(a: argparse.Namespace) -> int:
    from .charging.backtest import BacktestConfig, run
    from .charging.optimizer import Vehicle
    from .grid.ercot import PriceSeries, download_year

    path = os.path.join(a.data_dir, f"ercot_{a.zone}_{a.year}.npz")
    if os.path.exists(path):
        series = PriceSeries.load(path)
    else:
        print(f"downloading ERCOT {a.year} prices (about 15 MB, ~1 min to parse)...", file=sys.stderr)
        series = download_year(a.year, a.zone)
        os.makedirs(a.data_dir, exist_ok=True)
        series.save(path)
    cfg = BacktestConfig(energy_kwh=a.kwh, vehicle=Vehicle(charger_kw=a.charger_kw))
    r = run(series, cfg)
    print(f"{a.zone} {a.year}: {r.sessions} nights, {a.kwh:g} kWh/night, {a.charger_kw:g} kW charger, wholesale prices\n")
    print(r.table())
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ember", description="Cloud satellite ground station for wildfire detection")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("passes", help="predict passes over AWS Ground Station sites")
    s.add_argument("--sat", default="NOAA-20", choices=["NOAA-20", "NOAA-21", "SNPP", "AQUA"])
    s.add_argument("--hours", type=float, default=24)
    s.add_argument("--min-el", type=float, default=10.0, help="antenna elevation mask (deg)")
    s.add_argument("--min-max-el", type=float, default=0.0, help="only show passes peaking above this (deg)")
    s.add_argument("--station", action="append", help="limit to station name (repeatable)")
    s.add_argument("--price-per-minute", type=float, default=10.0)
    s.add_argument("--tle-file")
    s.set_defaults(func=cmd_passes)

    s = sub.add_parser("synth", help="generate a synthetic downlink for testing")
    s.add_argument("output")
    s.add_argument("--packets", type=int, default=500)
    s.add_argument("--shift", type=int, default=3)
    s.add_argument("--invert", action="store_true")
    s.add_argument("--ber", type=float, default=0.0)
    s.add_argument("--seed", type=int, default=0)
    s.set_defaults(func=cmd_synth)

    s = sub.add_parser("decode", help="decode a raw (post-Viterbi) bit stream")
    s.add_argument("input")
    s.add_argument("--no-rs", action="store_true")
    s.set_defaults(func=cmd_decode)

    s = sub.add_parser("firms", help="record FIRMS detections and measure their latency")
    s.add_argument("--bbox", required=True, help="W,S,E,N e.g. 16,-35,33,-22 for South Africa")
    s.add_argument("--source", action="append", default=None)
    s.add_argument("--map-key")
    s.add_argument("--db", default="firms_latency.sqlite")
    s.add_argument("--watch", action="store_true")
    s.add_argument("--interval", type=int, default=300)
    s.set_defaults(func=cmd_firms)

    g = sub.add_parser("grid", help="Texas grid: live prices, carbon, charging backtests")
    gsub = g.add_subparsers(dest="grid_cmd", required=True)
    s = gsub.add_parser("now", help="live ERCOT price and carbon intensity")
    s.add_argument("--zone", default="LZ_NORTH", help="LZ_NORTH (Dallas), LZ_HOUSTON, LZ_SOUTH, LZ_WEST, ...")
    s.set_defaults(func=cmd_grid_now)
    s = gsub.add_parser("backtest", help="compare charging strategies on a year of ERCOT prices")
    s.add_argument("--year", type=int, default=2025)
    s.add_argument("--zone", default="LZ_NORTH")
    s.add_argument("--kwh", type=float, default=30.0, help="energy added per night")
    s.add_argument("--charger-kw", type=float, default=11.5)
    s.add_argument("--data-dir", default="data")
    s.set_defaults(func=cmd_grid_backtest)

    a = p.parse_args(argv)
    if a.cmd == "firms" and not a.source:
        a.source = ["VIIRS_NOAA20_NRT"]
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
