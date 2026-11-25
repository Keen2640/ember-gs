"""End-to-end link-layer decoder: raw bits -> CADUs -> VCDUs -> space packets."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from . import reed_solomon as rs
from .aos import FILL_VCID, SPACECRAFT_IDS, parse_vcdu
from .framing import SyncStats, find_cadus
from .packets import PacketReassembler, SpacePacket
from .randomizer import derandomize


@dataclass
class DecodeReport:
    sync: SyncStats
    cadus: int = 0
    rs_clean: int = 0
    rs_corrected_frames: int = 0
    rs_corrected_symbols: int = 0
    rs_failed: int = 0
    fill_frames: int = 0
    spacecraft: Counter = field(default_factory=Counter)
    vcids: Counter = field(default_factory=Counter)
    apids: Counter = field(default_factory=Counter)
    packets: int = 0
    frames_lost: int = 0

    def summary(self) -> str:
        sc = ", ".join(f"{SPACECRAFT_IDS.get(k, k)}: {v}" for k, v in self.spacecraft.items())
        lines = [
            f"CADUs found            {self.cadus}  (inverted {self.sync.inverted_frames}, resyncs {self.sync.resyncs})",
            (
                f"Reed-Solomon           clean {self.rs_clean}, corrected {self.rs_corrected_frames} "
                f"({self.rs_corrected_symbols} symbols), failed {self.rs_failed}"
            ),
            f"Spacecraft             {sc or '-'}",
            f"Virtual channels       {dict(sorted(self.vcids.items()))}  (fill frames {self.fill_frames})",
            f"Space packets          {self.packets}  (frames lost to gaps {self.frames_lost})",
            f"Packets per APID       {dict(sorted(self.apids.items()))}",
        ]
        return "\n".join(lines)


def decode_stream(
    data: bytes,
    cadu_len: int = 1024,
    rs_depth: int = 4,
    use_rs: bool = True,
) -> tuple[list[tuple[int, SpacePacket]], DecodeReport]:
    """Decode a raw (Viterbi-decoded, unaligned) bit stream into (vcid, packet) pairs."""
    cadus, sync = find_cadus(data, cadu_len=cadu_len)
    report = DecodeReport(sync=sync, cadus=len(cadus))
    reassemblers: dict[int, PacketReassembler] = {}
    out: list[tuple[int, SpacePacket]] = []

    for cadu in cadus:
        block = derandomize(cadu[4:]).tobytes()
        if use_rs:
            vcdu_bytes, corrections = rs.decode_interleaved(block, depth=rs_depth)
            if any(c < 0 for c in corrections):
                report.rs_failed += 1
                continue
            fixed = sum(corrections)
            if fixed:
                report.rs_corrected_frames += 1
                report.rs_corrected_symbols += fixed
            else:
                report.rs_clean += 1
        else:
            vcdu_bytes = block[: rs_depth * rs.KK]

        vcdu = parse_vcdu(vcdu_bytes)
        report.spacecraft[vcdu.spacecraft_id] += 1
        report.vcids[vcdu.vcid] += 1
        if vcdu.vcid == FILL_VCID:
            report.fill_frames += 1
            continue
        r = reassemblers.setdefault(vcdu.vcid, PacketReassembler())
        for pkt in r.push(vcdu):
            out.append((vcdu.vcid, pkt))

    for r in reassemblers.values():
        report.packets += r.stats.packets
        report.frames_lost += r.stats.frames_lost
        report.apids.update(r.stats.per_apid)
    return out, report
