"""CCSDS Reed-Solomon (255,223) codec, written from scratch.

Parameters (CCSDS 131.0-B, section 4):
  * Galois field GF(2^8), field polynomial x^8 + x^7 + x^2 + x + 1 (0x187)
  * 32 parity symbols -> corrects up to 16 byte errors per codeword
  * code generator roots beta^(11*j), j = 112..143, where beta = alpha^11
  * symbols on the wire are in Berlekamp's *dual basis*, not the conventional
    polynomial basis, so every byte is mapped through a fixed 8x8 bit matrix
    before decoding and back again afterwards.

JPSS/Aqua direct broadcast uses interleave depth I=4: a 1020-byte codeblock
holds four interleaved codewords (byte k belongs to codeword k % 4).

This is the slow, readable reference implementation. A C++ version comes later
and is checked against this one.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

NN = 255
KK = 223
NROOTS = NN - KK  # 32
FCR = 112
PRIM = 11
GFPOLY = 0x187

# --- GF(2^8) tables ------------------------------------------------------------
EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= GFPOLY
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return EXP[LOG[a] + LOG[b]]


def gf_div(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError("GF division by zero")
    if a == 0:
        return 0
    return EXP[(LOG[a] - LOG[b]) % 255]


def gf_pow(a: int, n: int) -> int:
    if a == 0:
        return 0
    return EXP[(LOG[a] * n) % 255]


def gf_inv(a: int) -> int:
    return EXP[(255 - LOG[a]) % 255]


# --- Dual-basis conversion (Berlekamp T matrix, CCSDS 131.0-B Annex F) ----------
_TAL = (0x8D, 0xEF, 0xEC, 0x86, 0xFA, 0x99, 0xAF, 0x7B)
TAL_TAB = [0] * 256  # conventional -> dual
TAL1_TAB = [0] * 256  # dual -> conventional
for _i in range(256):
    _v = 0
    for _k in range(8):
        if _i & (1 << _k):
            _v ^= _TAL[7 - _k]
    TAL_TAB[_i] = _v
    TAL1_TAB[_v] = _i
_TO_CONV = np.array(TAL1_TAB, dtype=np.uint8)
_TO_DUAL = np.array(TAL_TAB, dtype=np.uint8)

# --- generator polynomial ------------------------------------------------------
# g(x) = prod_{j=0}^{31} (x - beta^(FCR+j)),  beta = alpha^PRIM
ROOTS = [EXP[(PRIM * (FCR + j)) % 255] for j in range(NROOTS)]
_gen = [1]
for _r in ROOTS:
    # multiply _gen by (x + r); coefficients stored highest degree first
    _new = _gen + [0]
    for _j in range(1, len(_new)):
        _new[_j] ^= gf_mul(_gen[_j - 1], _r)
    _gen = _new
GENERATOR = _gen  # length 33, GENERATOR[0] == 1 (x^32 coefficient)


def _encode_conventional(data: list[int]) -> list[int]:
    """Systematic encoding: parity = data(x) * x^32 mod g(x)."""
    parity = [0] * NROOTS
    for d in data:
        feedback = d ^ parity[0]
        parity = parity[1:] + [0]
        if feedback:
            for j in range(NROOTS):
                parity[j] ^= gf_mul(feedback, GENERATOR[j + 1])
    return parity


@dataclass
class DecodeResult:
    data: bytes  # 223 corrected data bytes (dual basis, i.e. as on the wire)
    corrected: int  # number of symbols fixed, or -1 if uncorrectable

    @property
    def ok(self) -> bool:
        return self.corrected >= 0


def encode(data: bytes) -> bytes:
    """Encode 223 dual-basis bytes into a 255-byte dual-basis codeword."""
    if len(data) != KK:
        raise ValueError(f"expected {KK} bytes, got {len(data)}")
    conv = [TAL1_TAB[b] for b in data]
    parity = _encode_conventional(conv)
    return bytes(data) + bytes(TAL_TAB[p] for p in parity)


def _syndromes(cw: list[int]) -> list[int]:
    # codeword symbol cw[i] is the coefficient of x^(254-i)
    synd = []
    for r in ROOTS:
        s = 0
        for c in cw:
            s = gf_mul(s, r) ^ c
        synd.append(s)
    return synd


def _berlekamp_massey(synd: list[int]) -> list[int]:
    """Return error locator Lambda(x) as low-order-first coefficients."""
    lam = [1] + [0] * NROOTS
    prev = [1] + [0] * NROOTS
    length = 0
    shift = 1
    prev_disc = 1
    for n in range(NROOTS):
        disc = synd[n]
        for i in range(1, length + 1):
            disc ^= gf_mul(lam[i], synd[n - i])
        if disc == 0:
            shift += 1
            continue
        coef = gf_div(disc, prev_disc)
        new = lam[:]
        for i in range(NROOTS + 1 - shift):
            new[i + shift] ^= gf_mul(coef, prev[i])
        if 2 * length <= n:
            prev = lam
            length = n + 1 - length
            prev_disc = disc
            shift = 1
        else:
            shift += 1
        lam = new
    return lam[: length + 1]


def _poly_eval_low_first(poly: list[int], x: int) -> int:
    y = 0
    for c in reversed(poly):
        y = gf_mul(y, x) ^ c
    return y


def decode(codeword: bytes) -> DecodeResult:
    """Decode one 255-byte dual-basis codeword. Corrects up to 16 symbol errors."""
    if len(codeword) != NN:
        raise ValueError(f"expected {NN} bytes, got {len(codeword)}")
    cw = [TAL1_TAB[b] for b in codeword]
    synd = _syndromes(cw)
    if not any(synd):
        return DecodeResult(bytes(codeword[:KK]), 0)

    lam = _berlekamp_massey(synd)
    nerr = len(lam) - 1
    if nerr == 0 or nerr > NROOTS // 2:
        return DecodeResult(bytes(codeword[:KK]), -1)

    # Error evaluator Omega(x) = S(x) * Lambda(x) mod x^32
    omega = [0] * NROOTS
    for i, s in enumerate(synd):
        if s == 0:
            continue
        for j, l in enumerate(lam):
            if i + j < NROOTS:
                omega[i + j] ^= gf_mul(s, l)

    # Formal derivative of Lambda (odd terms survive in characteristic 2)
    lam_deriv = [lam[i] if i % 2 == 1 else 0 for i in range(1, len(lam))]

    # Chien search: an error at polynomial degree d has locator X = beta^d
    beta = EXP[PRIM]
    positions = []
    for d in range(NN):
        x_inv = gf_inv(gf_pow(beta, d))
        if _poly_eval_low_first(lam, x_inv) == 0:
            positions.append(d)
    if len(positions) != nerr:
        return DecodeResult(bytes(codeword[:KK]), -1)

    for d in positions:
        x = gf_pow(beta, d)
        x_inv = gf_inv(x)
        num = gf_mul(_poly_eval_low_first(omega, x_inv), gf_pow(x, (1 - FCR) % 255))
        den = _poly_eval_low_first(lam_deriv, x_inv)
        if den == 0:
            return DecodeResult(bytes(codeword[:KK]), -1)
        cw[NN - 1 - d] ^= gf_div(num, den)

    if any(_syndromes(cw)):
        return DecodeResult(bytes(codeword[:KK]), -1)
    return DecodeResult(bytes(TAL_TAB[c] for c in cw[:KK]), nerr)


# --- interleaving --------------------------------------------------------------

def encode_interleaved(data: bytes, depth: int = 4) -> bytes:
    """Encode depth*223 bytes into a depth*255 interleaved codeblock."""
    if len(data) != depth * KK:
        raise ValueError(f"expected {depth * KK} bytes")
    words = [encode(data[i::depth]) for i in range(depth)]
    out = bytearray(depth * NN)
    for i, w in enumerate(words):
        out[i::depth] = w
    return bytes(out)


def decode_interleaved(block: bytes, depth: int = 4) -> tuple[bytes, list[int]]:
    """Decode a depth*255 codeblock. Returns (data, per-codeword corrections)."""
    if len(block) != depth * NN:
        raise ValueError(f"expected {depth * NN} bytes")
    results = [decode(block[i::depth]) for i in range(depth)]
    out = bytearray(depth * KK)
    for i, r in enumerate(results):
        out[i::depth] = r.data
    return bytes(out), [r.corrected for r in results]
