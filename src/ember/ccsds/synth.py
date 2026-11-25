"""Synthetic downlink generator: the transmit side of the link, in software.

Builds a byte-exact JPSS-style CADU stream (packets -> M_PDU -> VCDU ->
Reed-Solomon -> randomizer -> ASM), then optionally damages it the way a real
channel does: bit slips, phase inversion, random bit errors, dropped frames.
This lets the decoder be tested end to end before paying for a satellite pass.
Every test compares decoded packets against the ones fed in here.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import numpy as np

from . import reed_solomon as rs
from .aos import FHP_NO_PACKET_START, VCDU_LEN, build_vcdu
from .framing import ASM
from .packets import build_packet
from .randomizer import randomize

ZONE_LEN = VCDU_LEN - 8  # 884


@dataclass
class SynthConfig:
    spacecraft_id: int = 159  # NOAA-20
    vcid: int = 16
    n_packets: int = 200
    apids: tuple[int, ...] = (800, 801, 802, 826)
    min_len: int = 50
    max_len: int = 3000
    seed: int = 0


def make_packets(cfg: SynthConfig) -> list[bytes]:
    rnd = random.Random(cfg.seed)
    counters = {a: 0 for a in cfg.apids}
    pkts = []
    for _ in range(cfg.n_packets):
        apid = rnd.choice(cfg.apids)
        body = bytes(rnd.getrandbits(8) for _ in range(rnd.randint(cfg.min_len, cfg.max_len)))
        pkts.append(build_packet(apid, counters[apid], body))
        counters[apid] = (counters[apid] + 1) & 0x3FFF
    return pkts


def packets_to_vcdus(pkts: list[bytes], cfg: SynthConfig) -> list[bytes]:
    """Pack packets back-to-back into packet zones with correct first-header pointers."""
    stream = bytearray()
    starts = []
    for p in pkts:
        starts.append(len(stream))
        stream.extend(p)
    # pad the end with an idle packet so the last real packet is flushed
    pad = (-len(stream)) % ZONE_LEN
    if pad:
        if pad < 7:
            pad += ZONE_LEN
        starts.append(len(stream))
        stream.extend(build_packet(0x7FF, 0, b"\x00" * (pad - 6)))
    starts_arr = np.array(starts)
    vcdus = []
    for n, off in enumerate(range(0, len(stream), ZONE_LEN)):
        inside = starts_arr[(starts_arr >= off) & (starts_arr < off + ZONE_LEN)]
        fhp = int(inside[0] - off) if len(inside) else FHP_NO_PACKET_START
        vcdus.append(build_vcdu(cfg.spacecraft_id, cfg.vcid, n, fhp, bytes(stream[off : off + ZONE_LEN])))
    return vcdus


def vcdu_to_cadu(vcdu: bytes) -> bytes:
    block = rs.encode_interleaved(vcdu, depth=4)
    return ASM.to_bytes(4, "big") + randomize(block).tobytes()


@dataclass
class Impairments:
    bit_shift: int = 0  # leading junk bits, so frames are not byte-aligned
    invert: bool = False  # QPSK phase ambiguity
    bit_error_rate: float = 0.0
    drop_frames: tuple[int, ...] = ()
    seed: int = 1


def impair(cadus: list[bytes], imp: Impairments) -> bytes:
    rng = np.random.default_rng(imp.seed)
    kept = [c for i, c in enumerate(cadus) if i not in set(imp.drop_frames)]
    bits = np.unpackbits(np.frombuffer(b"".join(kept), dtype=np.uint8))
    if imp.bit_error_rate > 0:
        flips = rng.random(bits.size) < imp.bit_error_rate
        bits = bits ^ flips.astype(np.uint8)
    if imp.invert:
        bits = 1 - bits
    lead = rng.integers(0, 2, imp.bit_shift, dtype=np.uint8)
    bits = np.concatenate([lead, bits])
    bits = np.concatenate([bits, np.zeros((-bits.size) % 8, dtype=np.uint8)])
    return np.packbits(bits).tobytes()


def generate_stream(cfg: SynthConfig | None = None, imp: Impairments | None = None) -> tuple[bytes, list[bytes]]:
    """Return (raw downlink bytes, the original packets) for round-trip testing."""
    cfg = cfg or SynthConfig()
    imp = imp or Impairments()
    pkts = make_packets(cfg)
    cadus = [vcdu_to_cadu(v) for v in packets_to_vcdus(pkts, cfg)]
    return impair(cadus, imp), pkts
