"""AWS Ground Station antenna sites.

AWS doesn't publish exact antenna coordinates. These are city-level
approximations, good to within tens of kilometres. That is accurate enough for
pass planning: it shifts acquisition time by seconds. Once your account is
onboarded, `aws groundstation list-contacts` gives the authoritative pass times.

Check `aws groundstation list-ground-stations` for the current site list. AWS
adds sites over time, and each satellite is only licensed at some of them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GroundStation:
    name: str  # AWS ground station name
    region: str  # AWS region the contact is scheduled from
    lat: float
    lon: float
    elevation_m: float = 0.0


AWS_GROUND_STATIONS: tuple[GroundStation, ...] = (
    GroundStation("Ohio 1", "us-east-2", 40.0, -83.0, 250),
    GroundStation("Oregon 1", "us-west-2", 45.6, -121.2, 100),
    GroundStation("Hawaii 1", "us-west-2", 21.4, -158.0, 100),
    GroundStation("Alaska 1", "us-west-2", 64.8, -147.7, 150),
    GroundStation("Ireland 1", "eu-west-1", 53.3, -6.3, 50),
    GroundStation("Stockholm 1", "eu-north-1", 59.3, 18.1, 30),
    GroundStation("Bahrain 1", "me-south-1", 26.1, 50.5, 10),
    GroundStation("Cape Town 1", "af-south-1", -33.9, 18.5, 50),
    GroundStation("Seoul 1", "ap-northeast-2", 37.5, 127.0, 50),
    GroundStation("Singapore 1", "ap-southeast-1", 1.35, 103.8, 20),
    GroundStation("Dubbo 1", "ap-southeast-2", -32.2, 148.6, 280),
    GroundStation("Punta Arenas 1", "us-east-2", -53.2, -70.9, 30),
)


def by_name(name: str) -> GroundStation:
    for gs in AWS_GROUND_STATIONS:
        if gs.name.lower() == name.lower():
            return gs
    raise KeyError(f"unknown ground station {name!r}")
