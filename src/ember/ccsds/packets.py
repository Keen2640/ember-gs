"""CCSDS space packets and their reassembly from the M_PDU stream.

Instrument data (VIIRS, ATMS, CrIS, ...) is sent as space packets that are cut
into fixed-size frame packet zones without regard for packet boundaries. One
packet can span several frames, and one frame can hold the end of one packet
and the starts of others. The M_PDU *first header pointer* says where the first
new packet starts in each frame, which lets us resynchronize after a dropped
frame (detected through the 24-bit VCDU counter).

Packet primary header (6 bytes):
  version(3) | type(1) | sec-hdr flag(1) | APID(11) |
  sequence flags(2) | sequence count(14) | data length - 1 (16)
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .aos import FHP_IDLE, FHP_NO_PACKET_START, VCDU

PRIMARY_HEADER_LEN = 6
IDLE_APID = 0x7FF


@dataclass(frozen=True)
class SpacePacket:
    apid: int
    sequence_flags: int
    sequence_count: int
    secondary_header: bool
    data: bytes  # everything after the 6-byte primary header

    @property
    def raw_length(self) -> int:
        return PRIMARY_HEADER_LEN + len(self.data)


def parse_packet_header(h: bytes) -> tuple[int, int, int, bool, int]:
    """Return (apid, seq_flags, seq_count, sec_hdr, total_length)."""
    w0 = int.from_bytes(h[0:2], "big")
    w1 = int.from_bytes(h[2:4], "big")
    length = int.from_bytes(h[4:6], "big") + 1
    return w0 & 0x7FF, w1 >> 14, w1 & 0x3FFF, bool(w0 & 0x0800), PRIMARY_HEADER_LEN + length


def build_packet(apid: int, seq_count: int, data: bytes, seq_flags: int = 3, sec_hdr: bool = False) -> bytes:
    if not data:
        raise ValueError("packet data field must not be empty")
    w0 = (int(sec_hdr) << 11) | (apid & 0x7FF)
    w1 = (seq_flags << 14) | (seq_count & 0x3FFF)
    return w0.to_bytes(2, "big") + w1.to_bytes(2, "big") + (len(data) - 1).to_bytes(2, "big") + data


@dataclass
class ReassemblyStats:
    frames: int = 0
    frame_gaps: int = 0
    frames_lost: int = 0
    packets: int = 0
    packets_dropped: int = 0
    per_apid: Counter = field(default_factory=Counter)


class PacketReassembler:
    """Stateful M_PDU reassembler for a single virtual channel."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.synced = False
        self.last_counter: int | None = None
        self.stats = ReassemblyStats()

    def push(self, vcdu: VCDU) -> list[SpacePacket]:
        self.stats.frames += 1
        if self.last_counter is not None:
            expected = (self.last_counter + 1) & 0xFFFFFF
            if vcdu.counter != expected:
                self.stats.frame_gaps += 1
                self.stats.frames_lost += (vcdu.counter - expected) & 0xFFFFFF
                if self.buf:
                    self.stats.packets_dropped += 1
                self.buf.clear()
                self.synced = False
        self.last_counter = vcdu.counter

        fhp = vcdu.first_header_pointer
        zone = vcdu.packet_zone
        if fhp == FHP_IDLE:
            return []
        if not self.synced:
            if fhp == FHP_NO_PACKET_START:
                return []
            self.buf = bytearray(zone[fhp:])
            self.synced = True
        else:
            self.buf.extend(zone)
        return self._drain()

    def _drain(self) -> list[SpacePacket]:
        out: list[SpacePacket] = []
        while len(self.buf) >= PRIMARY_HEADER_LEN:
            apid, flags, count, sec, total = parse_packet_header(self.buf[:PRIMARY_HEADER_LEN])
            if len(self.buf) < total:
                break
            body = bytes(self.buf[PRIMARY_HEADER_LEN:total])
            del self.buf[:total]
            if apid == IDLE_APID:
                continue
            out.append(SpacePacket(apid, flags, count, sec, body))
            self.stats.packets += 1
            self.stats.per_apid[apid] += 1
        return out
