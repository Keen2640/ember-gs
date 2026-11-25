"""CCSDS pseudo-randomizer (CCSDS 131.0-B, section 10).

The transmitter XORs every channel-coded frame (everything after the 32-bit
attached sync marker) with a fixed pseudo-noise sequence so the bit stream has
enough transitions for the receiver's clock recovery. Undoing it is the same XOR.

Generator polynomial h(x) = x^8 + x^7 + x^5 + x^3 + 1, register seeded with all
ones. The sequence repeats every 255 bits and starts FF 48 0E C0 9A 0D 70 BC ...
(the unit test pins these bytes).
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np


def _generate(nbytes: int) -> bytes:
    state = 0xFF
    out = bytearray(nbytes)
    for i in range(nbytes):
        byte = 0
        for _ in range(8):
            byte = (byte << 1) | (state & 1)
            feedback = (state ^ (state >> 3) ^ (state >> 5) ^ (state >> 7)) & 1
            state = (state >> 1) | (feedback << 7)
        out[i] = byte
    return bytes(out)


@lru_cache(maxsize=16)
def pn_sequence(nbytes: int) -> np.ndarray:
    """Return the first ``nbytes`` of the CCSDS PN sequence as a uint8 array."""
    arr = np.frombuffer(_generate(nbytes), dtype=np.uint8)
    arr.setflags(write=False)
    return arr


def derandomize(frame: bytes | np.ndarray) -> np.ndarray:
    """XOR a frame (without ASM) with the PN sequence. Self-inverse."""
    data = np.frombuffer(bytes(frame), dtype=np.uint8) if not isinstance(frame, np.ndarray) else frame
    return np.bitwise_xor(data, pn_sequence(len(data)))


randomize = derandomize
