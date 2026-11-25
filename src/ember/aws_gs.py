"""Thin wrapper around the AWS Ground Station API (boto3).

Reserving a contact costs real money, so `reserve` refuses to run unless the
caller passes confirm=True (the CLI asks for --yes) and the estimate is under
a hard budget cap.
"""

from __future__ import annotations

from datetime import datetime


def client(region: str):
    import boto3

    return boto3.client("groundstation", region_name=region)


def list_satellites(region: str) -> list[dict]:
    return client(region).list_satellites()["satellites"]


def list_ground_stations(region: str, satellite_id: str | None = None) -> list[dict]:
    kw = {"satelliteId": satellite_id} if satellite_id else {}
    return client(region).list_ground_stations(**kw)["groundStationList"]


def available_contacts(
    region: str,
    mission_profile_arn: str,
    satellite_arn: str,
    ground_station: str,
    start: datetime,
    end: datetime,
) -> list[dict]:
    resp = client(region).list_contacts(
        statusList=["AVAILABLE"],
        startTime=start,
        endTime=end,
        groundStation=ground_station,
        satelliteArn=satellite_arn,
        missionProfileArn=mission_profile_arn,
    )
    return resp["contactList"]


def reserve(
    region: str,
    mission_profile_arn: str,
    satellite_arn: str,
    ground_station: str,
    start: datetime,
    end: datetime,
    estimated_cost_usd: float,
    budget_cap_usd: float,
    confirm: bool = False,
) -> str:
    if not confirm:
        raise PermissionError("reserving a contact costs money; pass confirm=True / --yes")
    if estimated_cost_usd > budget_cap_usd:
        raise PermissionError(f"estimated ${estimated_cost_usd:.2f} exceeds cap ${budget_cap_usd:.2f}")
    resp = client(region).reserve_contact(
        missionProfileArn=mission_profile_arn,
        satelliteArn=satellite_arn,
        startTime=start,
        endTime=end,
        groundStation=ground_station,
    )
    return resp["contactId"]
