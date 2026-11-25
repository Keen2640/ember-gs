from datetime import datetime, timezone

from ember.firms.client import LatencyStore, parse_csv
from ember.orbit.passes import predict_passes
from ember.orbit.stations import AWS_GROUND_STATIONS, by_name
from ember.orbit.tle import parse_tle_text

# Real NOAA-20 elements from NOAA's TLE-AUX file of 2026-11-24 (s3://noaa-nesdis-n20-pds)
NOAA20 = """1 43013U 17073A   26328.00000000  .00000136  00000-0  64698-4 0  2787
2 43013 098.7368 266.1325 0001593 129.1590 211.0776 14.19550784467144"""


def test_tle_parse_and_epoch():
    tle = parse_tle_text("NOAA-20", NOAA20, "test")
    assert tle.norad_id == 43013
    assert tle.epoch == datetime(2026, 11, 24, tzinfo=timezone.utc)


def test_passes_are_physical():
    tle = parse_tle_text("NOAA-20", NOAA20, "test")
    start = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
    passes = predict_passes(tle, AWS_GROUND_STATIONS, start=start, hours=24)
    # ~14.2 orbits/day over 12 sites: dozens of passes, each a few to ~16 minutes
    assert 40 < len(passes) < 200
    for p in passes:
        assert p.aos < p.tca < p.los
        assert 60 < p.duration_s < 17 * 60
        assert 10 <= p.max_elevation_deg <= 90


def test_polar_orbiter_sees_alaska_more_than_singapore():
    tle = parse_tle_text("NOAA-20", NOAA20, "test")
    start = datetime(2026, 11, 24, tzinfo=timezone.utc)
    ak = predict_passes(tle, [by_name("Alaska 1")], start=start, hours=48)
    sg = predict_passes(tle, [by_name("Singapore 1")], start=start, hours=48)
    assert len(ak) > len(sg)


def test_cost_estimate():
    tle = parse_tle_text("NOAA-20", NOAA20, "test")
    p = predict_passes(tle, [by_name("Ohio 1")], start=datetime(2026, 11, 24, tzinfo=timezone.utc), hours=24)[0]
    assert abs(p.cost_usd(10.0) - (p.duration_s + 180) / 60 * 10.0) < 1e-9


FIRMS_CSV = """latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight
-25.12345,30.54321,340.5,0.39,0.36,2026-11-24,1142,N20,VIIRS,n,2.0NRT,295.1,8.4,D
-26.00000,31.00000,367.0,0.40,0.37,2026-11-24,0905,N20,VIIRS,h,2.0NRT,300.2,25.0,D
"""


def test_firms_parse_and_latency(tmp_path):
    dets = parse_csv(FIRMS_CSV, "VIIRS_NOAA20_NRT")
    assert len(dets) == 2
    assert dets[1].acquired == datetime(2026, 11, 24, 9, 5, tzinfo=timezone.utc)
    store = LatencyStore(str(tmp_path / "l.sqlite"))
    seen = datetime(2026, 11, 24, 12, 5, tzinfo=timezone.utc)
    assert store.record(dets, seen) == 2
    assert store.record(dets, seen) == 0  # first-seen time is never overwritten
    stats = store.latency_stats()
    assert stats["n"] == 2 and stats["median_min"] == 180.0
