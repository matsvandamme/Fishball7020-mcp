---
name: fishball-sdr-mcp
description: Work on the MCP server for the Fishball7020 / PlutoSky SDR (Zynq-7020 + AD9361) - adding or changing tools, the libiio/IIOD client, DSP and spectrum code, response formatting, the transmit gate, and the evaluation and smoke tests. Use when editing this server, when a tool returns wrong levels or wrong frequencies, when a capture times out, or when anything here transmits. Carries the hardware facts a server author keeps needing - this board has a power amplifier and its receive port is the fragile end, receive is 12-bit while transmit is 16-bit, the AD9361 gain label is not proportional to gain, and stdout belongs to JSON-RPC so a stray print breaks the protocol.
license: GPL-2.0
compatibility: Python 3.10+ and the mcp SDK. A board reachable over libiio (default ip:fishball.local) is needed only for --live tests; everything else runs without hardware.
metadata:
  repository: Fishball7020-mcp
  companion: fishball7020-fpga-devkit
---

# The Fishball7020 MCP server

Twenty tools over stdio that let an assistant drive a real SDR: tune it, sweep
a band, measure a spectrum, capture IQ, engage the FPGA channel filter, and —
only when deliberately enabled — transmit.

| | |
|---|---|
| [`server-design.md`](references/server-design.md) | Layout, tool conventions, the transmit gate, testing |
| [`sdr-hardware.md`](references/sdr-hardware.md) | **Read before touching levels, gain or transmit.** What the radio does that surprises people |

The firmware and FPGA build system live in the companion repository
`fishball7020-fpga-devkit`, which carries the full hardware reference. This
skill holds only what a server author keeps needing.

## The rules

**stdout is the JSON-RPC channel.** A stray `print()` does not look untidy, it
breaks the protocol. All diagnostics go to stderr. The smoke test asserts this,
because it is an easy mistake to make while debugging `iiod.py`.

**Transmitting is enabled by default.** The gate is opt-OUT: every transmit
tool works unless `SDR_MCP_ALLOW_TX=0` is set in the *server's* environment.
It was opt-in; the board's owner asked for it available without ceremony.

**A safety gate advises before each tone, IQ file or waveform** (`txgate.py`).
Code refuses a transmit that leaves the EU licence-free bands (433.05–434.79,
863–870, 2400–2483.5, 5725–5875 MHz; LO leakage, chirp sweep and an IQ file's
full sample-rate width all count) or exceeds the band's power limit, estimated
as +19 dBm + gain + 20·log10(scale). `override_reason` gets past it; with
`TYPESAFE_API_KEY` set, TypeSafe's Jev model must first read the reason as a
safe setup (conducted/no antenna, shielded, or licensed *for that frequency*),
and a TypeSafe outage refuses. `force=true` transmits regardless, with a
WARNING in the reply and `TX GATE FORCED` in the log - use it only when the
operator has said to. The operator decides; the gate only advises.

**So check what is connected before transmitting.** `sdr_check_rf_setup`
reports what the ports appear to be attached to — and states the limit plainly:
the transmit socket has no detector, so whether an antenna is on *it* cannot be
measured by anything. Run it whenever the cabling is not already known.

**Prefer a NAME over an address.** `SDR_MCP_URI=ip:fishball.local` survives
the router handing out a different address; a hard-coded IP does not, and the
only symptom is a connection error. `fishball` is the devkit's default
hostname (its `firmware/patches/0013`); a board on an older rootfs answers to
`pluto.local`. Two separate names are in play - mDNS, which is what these URIs
use, and the DHCP hostname a router displays, which stock firmware never sends.

**`sdr_find_board` is the answer to "it cannot reach the radio".** The default
`ip:fishball.local` follows the board over USB, a router or a cable straight to
the PC (the PC serving DHCP), but needs mDNS on this machine; `192.168.2.1` is
only the USB gadget, and only while the PC's USB network is up. When the name
fails, the only symptom is a connection error. It tries the configured URI,
`192.168.2.1`, `fishball.local`, `Fishball7020.local`, `pluto.local` and up to
16 ARP neighbours concurrently with an 8 s deadline,
and reports `hw_model` plus what to set `SDR_MCP_URI` to. A file error (missing
IQ file, unwritable capture dir) is reported as a file problem, not as
"cannot reach the radio" - `errors.describe` tells them apart.

**`sdr_sample_gpio` is not a transmit tool and is not gated.** It only flips a
routing bit: the four bits the 12-bit DAC discards from each sample either
reach four header pins (JP5 7/9/11/13; sysfs GPIO 978–981 on the vendor's 5.15
kernel, 584–587 on the 6.12 one — resolve it by label, not with `gpiofind`, which
the Debian rootfs does not ship) or they do not. Nothing
is emitted by turning it on — the pins move only while a buffer is streaming,
and what they do is whatever is in the low nibble of those samples, OR-ed in
**last** after any scaling. `sdr_sample_gpio_clock` authors such a pattern (and
*is* gated, see above); its frame marker is one sample wide, so it needs a
scope — not seeing it through sysfs proves nothing. The pins lead the RF by a
constant offset of roughly a microsecond; do not describe them as simultaneous. `sdr_sample_gpio_clock`
authors a square wave and frame marker for them; its frame marker is one sample
wide, so it needs a scope — not seeing it through sysfs proves nothing.

**`sdr_tx_disable` and `sdr_tx_status` are never gated.** An off switch that can
be unavailable is not an off switch.

**Everything that opens a TX buffer or keys a tone IS gated - including the two
that are easy to think of as harmless.** `sdr_check_rf_setup`'s probe streams
at 60 dB attenuation (≈ −41 dBm) on the *receive* LO and runs the band check;
with the gate closed it stays passive and says so. `sdr_sample_gpio_clock`
sends zeros to the DAC but starting a buffer brings the LO up on devkit
firmware, so it refuses when the gate is closed. If you add a tool that streams
a buffer, gate it, and make the smoke test's closed-server section call it.

**Set TX attenuation AFTER a buffer starts, and read it back.** On devkit
firmware, starting a buffer runs the kernel's unmute, which restores a CACHED
attenuation - whatever the previous stream ended with. A value written before
the stream is overwritten. Every streaming path here writes the gain after
`transmit_samples()` returns and verifies it; the one exception is a one-shot
(`cyclic=false`) buffer, which has already finished by then, so it sets the
gain first (patch 0005 keeps it) and calls `tx_disable()` after it plays out.

**A cyclic transmit this server starts is bounded at 60 s, by the board.** The
devkit rootfs arms `tx_cyclic_timeout_ms` at boot from `fishball-rf-quiesce`, before
`iiod` starts — the kernel's own default is still `0`, off. So a cyclic buffer that
outlives this server, or whose client vanishes, mutes itself after a minute rather
than repeating forever in hardware. Two consequences for anything written here: do
not tell a user a cyclic transmit runs indefinitely on this board, and if a carrier
this server started disappears after about a minute, that is the backstop, not a
fault. `fw_setenv tx_cyclic_bound <ms>` changes it and `0` disables it, from the next
boot; it is a separate switch from `tx_quiesce`.

**If a second receiver is used to check this board, three measurement traps.** All
three produced a confident wrong answer on the devkit bench on 2026-09-30, and any of
them can turn up in an answer this server helps write.

- **Something sits on exactly 2400.000 MHz** at 8 dB over the floor even with nothing
  connected to the receiver - and it is NOT established whose. 2400.000 is 96 x 25 MHz
  (a HackRF reference), 60 x 40 MHz (this board's Y3 VCTCXO), 48 x 50 MHz, 100 x 24 MHz
  and 5 x 480 MHz (USB). An earlier version of this file blamed the instrument; that is
  withdrawn. That is the frequency this board is usually tuned to, so present and
  absent read alike there. Sweeping the transmit attenuator, retuning the receive LO
  and opening the receiver's input establish only what it is NOT - none of the three
  tests the board. The two that would: capture with the **board powered off**, and
  terminate the receiver in **50 ohm** rather than leaving it open.
- **A stream of zeros is not an RF signal.** I = 0, Q = 0 emits nothing but residual
  leakage, so a zero-fed buffer cannot serve as a positive control. Use a real tone.
- **The peak of ONE FFT of noise sits 8-16 dB above the median**, which reads exactly
  like a carrier. Average before believing a peak, and say how many FFTs went into it.

**Tool calls are serialised.** The SDK runs each `tools/call` in its own worker
thread, so without a lock a client batching `sdr_spectrum` and `sdr_tx_tone`
would interleave multi-step radio sequences. `_serialized` in `server.py`
wraps every tool body in one re-entrant lock; keep it on anything you add.

**The startup quiesce runs only when the gate is CLOSED.** With the default
(transmit permitted) the server leaves the transmitter exactly as it found it.
`sdr_tx_chain_state` reports whether it is live.

**On shutdown the server silences the radio if *it* ever touched TX** - a clean
EOF or SIGTERM (handled) both reach that code. It cannot know about a buffer
some other process started, and a SIGKILL skips it; say so rather than promise
more.

What catches a SIGKILL is the **firmware**, not this server, and only on a
board running devkit patch `0015` or later: the driver mutes when the converter
stops being fed (`tx_starve_timeout_ms`, 250 ms by default), which covers a
killed client, a stalled one, and a buffer enabled and never fed. Measured at
0.27 s. **Cyclic transmits are exempt by design** - the hardware repeats one
buffer forever, so a kill is indistinguishable from a normal return - and that
is the one case where "stop it explicitly" is still the only protection.
Stock firmware has none of this. Do not promise it without checking:

```bash
# run on the board
cat /sys/bus/iio/devices/iio:device2/tx_starve_timeout_ms   # absent on stock
```

**Two limits on that watchdog, both measured on the bench 2026-09-29.**

*It does not re-arm.* Once it has fired, the driver believes the transmitter is
muted, and data resuming does not change that - only a fresh buffer enable does.
So after a starve-mute, a gain write raises the attenuator and **nothing
re-mutes it**, not even stream stop:

```
atten0=-30.000000  LO_pd=1  buf=1      (gain written AFTER the watchdog fired)
```

What keeps that silent is the powered-down TX LO, not the attenuator. Do not read
a loud `hardwaregain` as "transmitting" or a quiet one as "safe" without also
reading `out_altvoltage1_TX_LO_powerdown`.

*A stream must be fed to be protected by it, and the host link may not manage
that.* Direct from a host over Ethernet at 3.072 MSPS with 256 K-sample buffers:
5 underflows and **one starve mute in 10 s**, with nothing wrong. The same test
on the board over loopback: 2 underflows, **no** starve mute. So a slow link does
not merely lose samples - it silently mutes the transmitter mid-stream, and the
client sees no error. This is the same conclusion as "buffer duration is the
defence" in `references/sdr-hardware.md`, with a number on it.

**Every power-on transmits, and nothing in software can stop it.** Measured
2026-09-29 with a second receiver cabled to each transmit port through a pad: about
**1 second after power is applied, both TX1A and TX2A emit a narrowband burst of
~4 ms at the TX LO frequency**, on both ports, every power cycle.

**How strong is not known.** An earlier version of this file said "+8 dBm, reproducible
to 0.4 dB, within 0.3 dB between the ports". Those numbers are withdrawn: the receiver
was saturated, so every reading was the clip ceiling rather than a level. The test that
showed it - a synthetic tone fed in at x2 and at x100 read the same -4.39 dBFS - also
explains the agreement, which was two saturated readings agreeing rather than a
reproducible measurement. What the capture supports is that the burst is present on both
ports at or above the equivalent of -20 dB attenuation, and unbounded above.

It is the AD9361's own **TX quadrature calibration**: `ad9361_tx_quad_calib()` drives an
NCO tone through the transmit path to correct I/Q imbalance, and it runs *before*
`ad9361_set_tx_atten()` applies the device tree's attenuation. Transmitting is the
mechanism, not a bug - the function aborts if the TX LO is powered down - so there is
no fix and no knob. What makes it loud on this board is the PGA-102+ on transmit.

Consequences for anything this server says about safety:

- Never tell a user the transmitter is silent "from power-on". It is silent from the
  moment `ad9361_setup()` finishes, which is after the calibration.
- If an antenna is on a transmit port, **plugging the board in radiates**. Negligible
  duty cycle in the ISM band, but say it rather than implying otherwise.
- `tx_quiesce`, any affirmation gate and the starve watchdog are all far too late to
  affect this. Do not cite them as covering the boot window.

**Mute BEFORE closing a TX buffer, never after.** The kernel's stream-stop hook
snapshots whatever attenuation it finds into a cache and then applies maximum; the
next buffer enable - by *any* program, with no affirmation asked for - restores
what was snapshotted. Measured: a bare buffer enable on a board reading
`-89.750000` came up at **`-61.500000`**, a 28.25 dB raise nobody asked for,
because the previous run tore its buffer down before muting. `radio.py`'s
`_mute_before_close()` exists for this and is called at every close site. If you
add another, call it.

**Both transmit chains are reachable.** `sdr_tx_tone`, `sdr_transmit_iq` and
`sdr_transmit_waveform` take `channel` = `"0"` (TX1), `"1"` (TX2) or `"both"`,
and it is **REQUIRED** - no default since 2026-09-30 (it was `"both"`). Name the
port you mean to key; ask the operator which port is terminated if you do not
know. Never pass `"both"` just to make a call go through.

**This board can destroy its own receiver.** It ships in a variant with a
Mini-Circuits PGA-102+ power amplifier and reaches about **+19 dBm**, against a
receive port rated to **+2.5 dBm**. Any loopback needs at least 20 dB of
attenuation. Refusals and docs should say this plainly rather than repeat the
generic "+7 dBm" figure that applies to a bare AD9361.

**`sdr_set_fpga_filter` is a two-channel decision, not a one-channel one.** On
**stock** firmware, engaging the decimator ruins channel 1. Upstream routes
channel 0 through `rx_fir_decimator` and sends channel 1 straight to `cpack`
inputs 2 and 3 - so the moment decimation engages, channel 1 is sampled at one
eighth the rate with **no anti-alias filter of its own**. Measured on the board:
stock channel 1 is flat at +1.4 dB right across a 0.2-20 MHz sweep (no
attenuation at all), and a 10 MHz tone at 7.68 MSPS arrives as a tall alias at
+2.32 MHz.

The devkit's patch `0021` filters both channels and takes that to about -70 dB
beyond 5 MHz. **It is applied by default**, so a bitstream built from the devkit
today is safe here; it was `optional/0004` until it was promoted, and the opt-out
`STOCK_RX_FILTER=1` reproduces upstream's channel-0-only wiring for anyone who
wants it. So: engaging the filter is safe on a default devkit build, and quietly
corrupts channel 1 on factory firmware and on a `STOCK_RX_FILTER=1` build. If a
caller asks for both channels with the filter on, say which firmware that needs
rather than assuming.

**Never return raw IQ inline.** Even a short capture is megabytes. Write to
`SDR_MCP_CAPTURE_DIR` and return the path plus statistics.

**And write the metadata beside the samples, not only in the reply.** A capture
returned through MCP outlives the conversation that produced it, and whoever
opens it next cannot scroll back. `sdr_capture_iq` writes a **SigMF pair** —
`<name>.sigmf-data` holding the untouched interleaved int16, and
`<name>.sigmf-meta` holding rate, centre frequency, gain, AGC mode, RSSI,
bandwidth and the capture statistics. Every field is read back off the board
after configuration rather than echoed from the request, because gain quantises
to the AD9361's table and `rf_bandwidth` snaps to what the filter supports.
`fishball_sdr_mcp/sigmf.py` builds it. Two things that broke when this was
added and will break again: **pruning must delete the sidecar with its data**,
or a `.sigmf-meta` survives describing a file that no longer exists; and the
transmit path's extension table must recognise `.sigmf-data`, or a capture
cannot be retransmitted.

**Levels are dBFS against a 12-bit converter** (full scale ±2047) on receive,
but transmit takes the **full 16 bits**. Scaling transmit to ±2047 emits 24 dB
low. This asymmetry is measured, not assumed.

## Aircraft (ADS-B) are decoded in the devkit, not here

This server has no ADS-B tool. Point a user who wants aircraft at the devkit's
`./devkit adsb` (`docs/adsb.md`): RX1 or RX2 (`--channel 2`) at 1090 MHz,
4 MSPS, a live table, receive only. A capture from here decodes there, because
both write SigMF `ci16_le`. Use 1090 MHz, 4 MSPS (a multiple of 2 MSPS is
required) and the FPGA filter off (`sdr_set_fpga_filter` false); a decimated
capture smears the 0.5 us pulses. Then run
`./devkit adsb --replay <file>.sigmf-meta --text`. `sdr_capture_iq`'s ceiling
of 4,194,304 samples is about one second at 4 MSPS: enough for a few messages
from each aircraft in range.

## Scripted measurements belong to the devkit's automation server

When a user wants a measurement they can run again (a sweep, a regression
check, a script for the bench), point them at the devkit's automation server
(`docs/automation.md`, `./devkit automation install`): a gRPC server on the
board, port 7020, with a Python client. It has `Transmit` and
`TransmitCapture` (play a waveform and record both receivers in one call), and
it enforces the devkit's transmit rules itself: a `tx-guard` affirmation per
channel, `pad_db >= 20` louder than -10 dB and for any TransmitCapture,
attenuation written after the buffer starts, mute before release. Its worked
example is `tools/automation/examples/loopback_sweep.py`
(`docs/radio/sweep-a-loopback.md`). Direction finding with more than one board
is its `examples/beamformer.py` (`docs/radio/beamform-two-boards.md`): boards on
one reference clock, calibrated with a beacon. This server cannot do it: it
talks to one board, and a shared reference clock is a hardware rework.

It and this server drive the same radio by different routes (it uses sysfs
and libiio in-process on the board; this server talks to `iiod`):

- **One buffer, one owner.** While the automation server captures or
  transmits, a capture or transmit here fails with EBUSY, and while this
  server holds a buffer through `iiod`, the automation server refuses and
  names `iiod` as the holder. `./devkit automation status` lists the holders.
- **Mute always works from either side.** `sdr_tx_disable` during an
  automation transmit ends it: that server reads the channel at the floor and
  stops with "the board muted TXn by itself". Raising attenuation from here
  during one makes it mute both channels and fail the call.
- **Settings are shared.** An `sdr_tune` or `sdr_configure_rx` here changes
  what a running automation script measures, without telling it. Do not drive
  the radio from here while a user's script is running.

## Layout

| | |
|---|---|
| `fishball_sdr_mcp/iiod.py` | libiio's network protocol over a plain socket, stdlib only |
| `fishball_sdr_mcp/radio.py` | radio operations built on it; holds the one reused connection |
| `fishball_sdr_mcp/dsp.py` | window, FFT, peak finding; numpy if importable, pure Python otherwise |
| `fishball_sdr_mcp/formatting.py` | markdown/json rendering, `CHARACTER_LIMIT` |
| `fishball_sdr_mcp/errors.py` | IIOD errno → an actionable message |
| `fishball_sdr_mcp/server.py` | the MCP instance and every tool |
| `evaluation/smoke_test.py` | drives the server over stdio with the standard library |
| `evaluation/questions.xml` | ten Q&A pairs, each verified against real hardware |

There is deliberately **no `pylibiio` dependency**: the protocol is line-based
text and the whole client is one small module, so there is no libiio version to
match against the firmware and no C extension to build.

## Conventions for a new tool

- Name it `sdr_<verb>`; take `response_format: Format = "markdown"`.
- Set `ToolAnnotations` honestly — `readOnlyHint` for anything that only reads,
  `destructiveHint` and `openWorldHint` for anything that transmits.
- Validate inputs against the radio's own `*_available` attributes rather than
  hardcoding ranges, so an illegal request is refused with the legal options.
- Render through `formatting.render`, which enforces `CHARACTER_LIMIT`.
- Anything that transmits: check the gate, run `_gate(...)` with every
  frequency it emits (LO included) and pass `override_reason` and `force`
  through, check `SDR_MCP_TX_BANDS`, log the call to stderr with frequency,
  gain and sample count.
- **Add the tool's name to `EXPECTED_TOOLS` in `evaluation/smoke_test.py`, in
  the same commit.** The smoke test asserts the advertised set matches that
  list by name. Forgetting it is not theoretical: `sdr_rfid_field` landed
  without it and CI was red for four pushes, failing on arithmetic that had
  nothing to do with the commits it blocked. A unit test now catches the
  drift in a tenth of a second and names the missing tool.
- Add a question to `evaluation/questions.xml` if the tool exposes a fact worth
  checking, and make sure `smoke_test.py` still passes.

## Testing

```bash
python evaluation/smoke_test.py            # protocol only, no radio needed
python evaluation/smoke_test.py --live     # also calls the read-only tools
```

This stands in for MCP Inspector, which needs Node 18 while Ubuntu 22.04 ships
Node 12. CI runs the protocol test on Python 3.10 and 3.13, with and without
numpy — the pure-Python FFT fallback is the path most users take, so it is the
one that must not rot.
