"""AOS transfer frames (VCDUs) and the M_PDU packet zone (CCSDS 732.0-B).

Layout used by JPSS (SNPP / NOAA-20 / NOAA-21) and Aqua direct broadcast:

  CADU  (1024 B) = ASM (4) + Reed-Solomon codeblock (1020)
  codeblock      = VCDU (892) + RS parity (128)        [interleave depth 4]
  VCDU  (892 B)  = primary header (6) + M_PDU header (2) + packet zone (884)

Primary header bits:
  version(2) | spacecraft id(8) | virtual channel id(6) | frame counter(24) |
  replay flag(1) | spare(7)
M_PDU header bits:
  spare(5) | first header pointer(11)
"""

from __future__ import annotations

from dataclasses import dataclass

VCDU_LEN = 892
PRIMARY_HEADER_LEN = 6
MPDU_HEADER_LEN = 2
FILL_VCID = 63
FHP_NO_PACKET_START = 0x7FF  # no packet header starts in this frame
FHP_IDLE = 0x7FE  # packet zone holds only idle data

# Spacecraft IDs as they appear in the VCDU header. Check these against real
# frames before relying on them for filtering.
SPACECRAFT_IDS = {
    157: "SNPP",
    159: "NOAA-20 (JPSS-1)",
    177: "NOAA-21 (JPSS-2)",
    154: "Aqua",
    42: "Terra",
}


@dataclass(frozen=True)
class VCDU:
    version: int
    spacecraft_id: int
    vcid: int
    counter: int
    replay: bool
    first_header_pointer: int
    packet_zone: bytes

    @property
    def is_fill(self) -> bool:
        return self.vcid == FILL_VCID


def parse_vcdu(data: bytes, insert_zone_len: int = 0) -> VCDU:
    if len(data) < PRIMARY_HEADER_LEN + insert_zone_len + MPDU_HEADER_LEN:
        raise ValueError("VCDU too short")
    h = int.from_bytes(data[0:2], "big")
    version = h >> 14
    scid = (h >> 6) & 0xFF
    vcid = h & 0x3F
    counter = int.from_bytes(data[2:5], "big")
    replay = bool(data[5] & 0x80)
    m = PRIMARY_HEADER_LEN + insert_zone_len
    fhp = int.from_bytes(data[m : m + 2], "big") & 0x7FF
    return VCDU(version, scid, vcid, counter, replay, fhp, bytes(data[m + 2 :]))


def build_vcdu(
    spacecraft_id: int,
    vcid: int,
    counter: int,
    first_header_pointer: int,
    packet_zone: bytes,
    version: int = 1,
) -> bytes:
    zone_len = VCDU_LEN - PRIMARY_HEADER_LEN - MPDU_HEADER_LEN
    if len(packet_zone) != zone_len:
        raise ValueError(f"packet zone must be {zone_len} bytes")
    h = (version << 14) | (spacecraft_id << 6) | vcid
    return (
        h.to_bytes(2, "big")
        + (counter & 0xFFFFFF).to_bytes(3, "big")
        + b"\x00"
        + (first_header_pointer & 0x7FF).to_bytes(2, "big")
        + packet_zone
    )
