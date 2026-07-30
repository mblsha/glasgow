# SL-C860 SDRAM resume probe

This preview applet is a dedicated, input-only, source-synchronous capture
engine for the Sharp SL-C860 deep-resume experiment. It is not a general
logic analyzer and must not be loaded onto wired hardware until the
unit-specific continuity map, 3.3 V electrical review, and passive loading
tests are complete.

The observed control and bank signals are always:

```text
SDCLK1 SDCKE1 nSDCS0 nSDRAS nSDCAS nSDWE
BA0 BA1
```

Exactly five address signals are selected by the mandatory build-time signal
profile:

| Profile | Prepared target | Address pin order | Active-row bits | READ bits |
| --- | --- | --- | --- | --- |
| `v9-a0018330` | `0xa0018330` | A2,A3,A5,A6,A7 | A5,A6 | A2,A3,A6,A7 |
| `v14-a00302c4` | `0xa00302c4` | A0,A4,A5,A6,A7 | A6,A7 | A0,A4,A5,A7 |
| `v15-a0030280` | `0xa0030280` | A0,A4,A5,A6,A7 | A6,A7 | A0,A4,A5,A7 |

V14 observed the physical counterpart of Sharp's later MMU-on
`Awake_address`; it was not the factory PSPR entry. V15 keeps the same five
physical address inputs but changes the prepared target to the exact factory
`cpu_pxa_resume` PSPR value. Its target signature is `0xcc` rather than
v14's `0xfc`, and its mapping attestation SHA-256 is
`53ed90c4e779bfc9733fc513d49ea3cdbfdc2f76575910cc7fc0ca9f2206e997`.

The current v14 Glasgow synthesis fixture is:

```text
SDCLK1=A1 SDCKE1=A0 nSDCS0=A2 nSDRAS=A3 nSDCAS=A4 nSDWE=A5
BA0=A6 BA1=A7 A0=B0 A4=B1 A5=B2 A6=B3 A7=B4
```

Actual assignment follows the reviewed physical wiring record. All 13
translators are permanently input-only and all programmable pulls are
disabled. Both used VIO rails must be fixed at 3.3 V.

Input-only does not mean electrically load-free. On revC3, every target net
still sees the external port of an SN74LVC1T45 (`Cio=6 pF` typical at
3.3 V/25 °C), an SP3012 ESD channel (`0.5 pF` typical, `0.65 pF` maximum),
and the PCB/connector/loom. The 33 ohm series resistor is damping, not
isolation. TI does not specify a maximum for the translator's 6 pF entry or
channel-to-channel skew among separate translators. The 100 MHz FPGA timing
result therefore excludes a material external loading/skew question. Qualify
one clock input, then representative control/address inputs, with waveform
and awake memory-integrity evidence before attaching the complete harness.
The component sources are Glasgow
`docs/manual/src/revisions/revC3.rst`,
`hardware/boards/glasgow/revC3/bom.csv`,
the [TI SN74LVC1T45 datasheet](https://www.ti.com/lit/ds/symlink/sn74lvc1t45.pdf),
and the [Littelfuse SP3012 datasheet](https://www.littelfuse.com/assetdocs/littelfuse_tvs_diode_array_sp3012_datasheet.pdf).

## Capture model

The iCE40 input flip-flops sample every command/address pin on the rising edge
of target `SDCLK1`. A stopped clock therefore leaves the final physical
sample retained at the pins. The 48 MHz host domain synchronizes that value,
so a self-refresh entry remains observable even if the target never produces
another memory clock.

After the clock resumes, a two-stage pipeline:

1. tracks the partial open-row state for all four SDRAM banks;
2. retains raw non-NOP commands in 1,024 × 32-bit dual-port RAM;
3. counts calibrated zero-like and target-like READ signatures; and
4. freezes after the configured NOR boundary and a later target-like READ.

The SL-C860 discriminator encodes a profile-selected signature as:

```text
bits 1:0 = BA1:BA0 value encoded by Cat(BA0, BA1)
bits 3:2 = the two active-row signals in profile order
bits 7:4 = the four READ signals in profile order
```

All profiles give `0x00` for physical zero. Historical v9 and v14 use `0xfc`
for their prepared targets, while current v15 uses `0xcc`; the bits have
profile-specific meanings. These are pair discriminators, not globally
unique physical addresses. Every build therefore requires an explicit
`--signal-profile`; every arm request must repeat both the profile name and
its canonical mapping SHA-256. Status and schema-2 drain reports embed the
complete profile metadata. The applet also refuses deep mode without explicit
wiring/loading/awake-calibration attestations.

The v14 mapping SHA-256 is:

```text
df5616916c1e0520271b1f1d4e5e253d88f2b07bdedd47082e0dc045d860b5cd
```

Each retained RAM record is:

```text
bits 11:0  raw CKE, command, bank and reduced address sample
bits 13:12 profile-selected partial active row
bit  14    active-row valid
bits 30:15 SDCLK1 index modulo 65536
bit  31    reserved zero
```

The self-refresh trigger has a separate retained sample, because writing a
block RAM record on the final sleep edge would require an unsafe
pin-to-fabric timing path. A JSON drain prepends that trigger to the RAM
records and labels its source.

## Modes

- `deep`: wait for a self-refresh entry; capture every later non-NOP command;
  freeze after the accepted zero-read boundary and a target-like READ.
- `calibration`: start immediately, but retain only zero-like or target-like
  READ observations. This prevents ordinary 100 MHz traffic from consuming
  the buffer before the target calibration loop.
- `raw`: start immediately and retain every non-NOP command. This is a short
  loading/bring-up aid and normally fills very quickly.

If RAM fills, the applet sets sticky overflow and freezes. Absence of a target
in an overflowed prefix is inconclusive.

## Host invocation

After installing the modified Glasgow tree, build the exact revC3 fixture:

```sh
glasgow build --rev C3 memory-sdram-resume-probe \
  -V A=3.3,B=3.3 \
  --signal-profile v15-a0030280 \
  --clk A1 --cke A0 --cs A2 --ras A3 --cas A4 --we A5 \
  --ba A6,A7 --address B0:4
```

Runtime uses a newline-delimited JSON Unix socket:

```sh
glasgow run memory-sdram-resume-probe \
  -V A=3.3,B=3.3 \
  --signal-profile v15-a0030280 \
  --clk A1 --cke A0 --cs A2 --ras A3 --cas A4 --we A5 \
  --ba A6,A7 --address B0:4 \
  --control /tmp/zaurus-sdram-probe.sock
```

Supported commands are `status`, `arm`, `freeze`, `drain`, and `disarm`.
An arm request for v15 includes:

```json
{
  "command": "arm",
  "mode": "calibration",
  "generation": 1,
  "address_profile": "v15-a0030280",
  "mapping_sha256": "53ed90c4e779bfc9733fc513d49ea3cdbfdc2f76575910cc7fc0ca9f2206e997",
  "wiring_attested": true,
  "loading_qualified": true,
  "awake_calibration_attested": false
}
```

Arm requests must carry a unique generation and the required attestations.
Attestations must be literal JSON `true`/`false`, and numeric fields must be
literal JSON integers; strings such as `"true"` or `"8"` are rejected.
Omitted signatures are taken from the selected profile, so the request above
arms v15 with zero `0x00` and target `0xcc`. Explicit signatures that do not
match the profile are rejected; in particular, historical v14 target `0xfc`
cannot be armed while claiming `v15-a0030280`.
If `SDCLK1` is stopped, `freeze` and `disarm` cannot cross into the capture
domain. Drain only with an explicit stopped-clock attestation; afterward,
power off the target and reload the input-only bitstream instead of silently
clearing volatile evidence.

## Current host-only qualification

The checked-in simulations cover:

- final self-refresh pin sample retained with no later target clock;
- eight zero-like reads followed by target-like READ and immutable freeze;
- target-like READ before the NOR boundary rejected;
- filtered calibration behavior; and
- exact v14 `0xa00302c4` versus v15 `0xa0030280`
  active-row/READ discriminator behavior;
- v15's omitted arm signature resolving to `0xcc`, with a cross-profile
  `0xfc` request rejected;
- mandatory build profile and arm-time profile/hash mismatch rejection; and
- bounded overflow failing closed.

The current v15 revC3 synthesis fixture routes at 100 MHz with 1,439/7,680
logic cells and 8/32 block RAMs. The final nextpnr report gives a 112.57 MHz maximum
for the target-clock domain and 75.63 MHz for the 48 MHz applet domain. The
minimum-read boundary check is pipelined so it does not form a 16-bit
comparison on the target-match/freeze path. The input signals use `SB_IO`
input registers clocked directly by `SDCLK1`; this removes the rejected
asynchronous pin-to-fabric path.

The host-only v15 timing-qualified bitstream has SHA-256
`ad9fa889d3be257f7bee71ca3a2a7bd84743ae8321d766f58dd9d44b1dad4121`.
Its complete synthesis log has SHA-256
`e6282813d56b14538d8440e5b69c1fe826852a6311fbbcf0c783d4e68301b359`.
The log retains an initial 93.99 MHz placement that failed the 100 MHz
constraint and the timing-driven reroute that passed at 112.57 MHz; only the
final routed bitstream above is accepted.

These are host-only gateware results. They do not qualify a physical probe,
signal integrity, board loading, external setup/hold margin, or the
correctness of a real-device trace.
