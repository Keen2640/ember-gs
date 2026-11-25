import random

import numpy as np
import pytest

from ember.ccsds import reed_solomon as rs
from ember.ccsds.aos import FHP_NO_PACKET_START, build_vcdu, parse_vcdu
from ember.ccsds.framing import ASM, find_cadus
from ember.ccsds.packets import PacketReassembler, build_packet, parse_packet_header
from ember.ccsds.pipeline import decode_stream
from ember.ccsds.randomizer import derandomize, pn_sequence
from ember.ccsds.synth import Impairments, SynthConfig, generate_stream


def repack(decoded):
    return [build_packet(p.apid, p.sequence_count, p.data) for _, p in decoded]


# --- randomizer ------------------------------------------------------------------

def test_pn_sequence_matches_ccsds_standard():
    # First 16 bytes of the CCSDS 131.0-B pseudo-randomizer sequence
    expected = bytes.fromhex("FF480EC09A0D70BC8E2C93ADA7B746CE")
    assert pn_sequence(16).tobytes() == expected


def test_pn_sequence_period_255_bits():
    bits = np.unpackbits(pn_sequence(64))
    assert np.array_equal(bits[:255], bits[255:510])


def test_derandomize_is_self_inverse():
    data = bytes(range(256)) * 4
    assert derandomize(derandomize(data)).tobytes() == data


# --- Reed-Solomon ----------------------------------------------------------------

def test_dual_basis_table_known_values():
    # Berlekamp dual-basis table, first entries as published for CCSDS
    assert rs.TAL_TAB[:6] == [0x00, 0x7B, 0xAF, 0xD4, 0x99, 0xE2]
    assert all(rs.TAL1_TAB[rs.TAL_TAB[i]] == i for i in range(256))


def test_generator_polynomial_degree():
    assert len(rs.GENERATOR) == 33 and rs.GENERATOR[0] == 1


@pytest.mark.parametrize("nerr", [0, 1, 5, 16])
def test_rs_corrects_up_to_16_errors(nerr):
    rnd = random.Random(nerr)
    data = bytes(rnd.randrange(256) for _ in range(223))
    cw = bytearray(rs.encode(data))
    for p in rnd.sample(range(255), nerr):
        cw[p] ^= rnd.randrange(1, 256)
    res = rs.decode(bytes(cw))
    assert res.ok and res.data == data and res.corrected == nerr


def test_rs_flags_uncorrectable():
    rnd = random.Random(99)
    flagged = 0
    for _ in range(20):
        data = bytes(rnd.randrange(256) for _ in range(223))
        cw = bytearray(rs.encode(data))
        for p in rnd.sample(range(255), 24):
            cw[p] ^= rnd.randrange(1, 256)
        flagged += not rs.decode(bytes(cw)).ok
    assert flagged == 20


def test_rs_interleaved_burst_error():
    # A 64-byte burst spread over interleave depth 4 = 16 errors per codeword: still correctable
    rnd = random.Random(7)
    data = bytes(rnd.randrange(256) for _ in range(892))
    block = bytearray(rs.encode_interleaved(data))
    for i in range(300, 364):
        block[i] ^= 0xFF
    out, corr = rs.decode_interleaved(bytes(block))
    assert out == data and corr == [16, 16, 16, 16]


# --- frames and packets ----------------------------------------------------------

def test_vcdu_header_roundtrip():
    zone = bytes(884)
    v = parse_vcdu(build_vcdu(159, 16, 0xABCDEF, 42, zone))
    assert (v.spacecraft_id, v.vcid, v.counter, v.first_header_pointer) == (159, 16, 0xABCDEF, 42)


def test_packet_header_roundtrip():
    pkt = build_packet(826, 1234, b"x" * 100, sec_hdr=True)
    apid, flags, count, sec, total = parse_packet_header(pkt[:6])
    assert (apid, flags, count, sec, total) == (826, 3, 1234, True, 106)


def test_reassembler_waits_for_first_header_pointer():
    r = PacketReassembler()
    v = parse_vcdu(build_vcdu(159, 16, 0, FHP_NO_PACKET_START, bytes(884)))
    assert r.push(v) == [] and not r.synced


# --- sync and end-to-end ---------------------------------------------------------

def test_sync_finds_unaligned_inverted_frames():
    raw, _ = generate_stream(SynthConfig(n_packets=30), Impairments(bit_shift=5, invert=True))
    cadus, stats = find_cadus(raw)
    assert stats.frames == len(cadus) > 0
    assert stats.inverted_frames == stats.frames
    assert all(c[:4] == ASM.to_bytes(4, "big") for c in cadus)


@pytest.mark.parametrize("shift,invert", [(0, False), (3, False), (7, True)])
def test_end_to_end_clean(shift, invert):
    raw, pkts = generate_stream(SynthConfig(n_packets=60, seed=shift), Impairments(bit_shift=shift, invert=invert))
    decoded, rep = decode_stream(raw)
    assert repack(decoded) == pkts
    assert rep.rs_failed == 0 and rep.rs_corrected_frames == 0


def test_end_to_end_with_bit_errors():
    raw, pkts = generate_stream(SynthConfig(n_packets=80), Impairments(bit_shift=2, bit_error_rate=1e-3))
    decoded, rep = decode_stream(raw)
    assert repack(decoded) == pkts
    assert rep.rs_corrected_symbols > 0


def test_end_to_end_dropped_frame_resyncs():
    cfg = SynthConfig(n_packets=80, min_len=100, max_len=400)
    raw, pkts = generate_stream(cfg, Impairments(drop_frames=(10,)))
    decoded, rep = decode_stream(raw)
    got = repack(decoded)
    assert rep.frames_lost == 1
    # packets touching the lost frame are gone; everything decoded is genuine and in order
    assert 0 < len(got) < len(pkts)
    idx = [pkts.index(g) for g in got]
    assert idx == sorted(idx)
