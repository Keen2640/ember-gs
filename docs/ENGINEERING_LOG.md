# Engineering log

Findings, dead ends and debugging notes. Interviewers ask about these.

## Day 1

**Terra is out; JPSS is in.** NASA announced Terra science collection ends
December 1, 2026, and both MODIS platforms are winding down, with Aqua's
projected end of science in 2027. NASA is steering users to VIIRS. The primary
targets are therefore NOAA-20 and NOAA-21 (VIIRS, X-band HRD at 7812 MHz,
15 Mbps), not Terra/Aqua.

**SNPP's TLEs in the NOAA bucket go stale in early November 2026**, and no
recent SNPP VIIRS SDRs appear there either. `ember passes` now warns when the
TLE is more than 7 days old. Treat SNPP as secondary.

**Orbital elements without CelesTrak.** NOAA publishes its operational TLEs as
tiny HDF5 files (`TLE-AUX/…/*.h5`, dataset `All_Data/TLE-AUX_All/Dataset_Array_0`)
in the public JPSS buckets. Using them keeps the whole system on AWS with no account.

**Cost reality check.** At an estimated $10/min, a typical 10–11 minute pass
plus 3 minutes of padding is about $125–140. The $100 credit doesn't cover a
full pass. Plan: build everything on synthetic and public data, apply for
research credits, and spend on a single well-chosen contact.

**PN randomizer bit order.** The first implementation produced
`FF 5A EA B2…` instead of the standard `FF 48 0E C0…`. Rather than guess, I
brute-forced every tap set / output bit / shift direction for an 8-bit LFSR and
matched the published sequence: output the LSB, feedback = s0⊕s3⊕s5⊕s7, shift
right. Now pinned by a unit test.

**Reed-Solomon dual basis.** CCSDS RS symbols are in Berlekamp's dual basis.
The conversion table built from the T matrix reproduces the published first
entries (00 7B AF D4 99 E2), which is also pinned by a test. The codec corrects
exactly 16 errors per codeword and flags all 24-error cases as uncorrectable.
It still has to be confirmed on real frames: real JPSS frames should decode
with mostly 0 corrections.

**Throughput.** The pure-Python decoder does about 2 Mbit/s, well short of the
15 Mbit/s link. That's expected for a reference implementation; the C++ port
(Day 3) is checked against it bit for bit.

**FIRMS baseline caveat.** FIRMS already has ultra-real-time coverage for the
US and Canada built on direct-readout stations. A fair "faster than FIRMS"
claim has to be measured elsewhere: Cape Town, Punta Arenas and Bahrain are
AWS sites covering fire-prone regions.
