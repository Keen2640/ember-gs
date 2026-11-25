"""Frame synchronization: find CADUs in a raw bit stream.

After demodulation and Viterbi decoding the receiver has an unaligned bit stream:
it doesn't know where frames start, the stream may start mid-byte, and QPSK phase
ambiguity can leave every bit inverted. Each CADU starts with the 32-bit attached
sync marker (ASM) 0x1ACFFC1D, so we:

1. correlate the stream against the ASM at every bit offset (tolerating a few
   bit errors),
2. accept a candidate only if another ASM appears exactly one CADU later
   (a "flywheel" check that kills false locks in random data),
3. cut out each CADU, undo any inversion and realign it to byte boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

ASM = 0x1ACFFC1D
ASM_BITS = np.unpackbits(np.frombuffer(ASM.to_bytes(4, "big"), dtype=np.uint8))
ASM_PM = ASM_BITS.astype(np.int16) * 2 - 1  # bits as +1/-1


@dataclass
class SyncStats:
    frames: int = 0
    inverted_frames: int = 0
    asm_bit_errors: int = 0
    resyncs: int = 0
    bit_offsets: dict[int, int] = field(default_factory=dict)


def _correlate(bits: np.ndarray) -> np.ndarray:
    pm = bits.astype(np.int16) * 2 - 1
    return np.correlate(pm, ASM_PM, mode="valid")


def find_cadus(
    data: bytes,
    cadu_len: int = 1024,
    max_asm_errors: int = 3,
) -> tuple[list[bytes], SyncStats]:
    """Extract byte-aligned CADUs (ASM + codeblock) from an unaligned stream."""
    bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8))
    nbits = cadu_len * 8
    corr = _correlate(bits)
    threshold = 32 - 2 * max_asm_errors
    candidates = np.flatnonzero(np.abs(corr) >= threshold)

    stats = SyncStats()
    frames: list[bytes] = []
    cand_set = set(candidates.tolist())
    pos = None
    i = 0
    while i < len(candidates):
        start = int(candidates[i])
        if pos is not None and start < pos:
            i += 1
            continue
        # flywheel check: the next ASM must be exactly one CADU later
        # (the last frame in the stream is accepted without a successor)
        nxt = start + nbits
        has_next = nxt in cand_set
        is_last = nxt + 32 > len(bits)
        if not (has_next or is_last):
            i += 1
            continue
        if pos is not None and start != pos:
            stats.resyncs += 1
        # follow the flywheel as long as ASMs keep appearing
        cur = start
        while cur + nbits <= len(bits):
            c = int(corr[cur]) if cur < len(corr) else 0
            if abs(c) < threshold:
                break
            inverted = c < 0
            frame_bits = bits[cur : cur + nbits]
            if inverted:
                frame_bits = 1 - frame_bits
            frames.append(np.packbits(frame_bits).tobytes())
            stats.frames += 1
            stats.inverted_frames += int(inverted)
            stats.asm_bit_errors += (32 - abs(c)) // 2
            off = cur % 8
            stats.bit_offsets[off] = stats.bit_offsets.get(off, 0) + 1
            cur += nbits
        pos = cur
        i = int(np.searchsorted(candidates, pos))
    return frames, stats
