---
name: fishball-sdr-mcp
description: Work on the MCP server for the Fishball7020 / PlutoSky SDR (Zynq-7020 + AD9361) - adding or changing tools, the libiio/IIOD client, DSP and spectrum code, response formatting, the transmit gate, and the evaluation and smoke tests. Use when editing this server, when a tool returns wrong levels or wrong frequencies, when a capture times out, or when anything here transmits. Carries the hardware facts a server author keeps needing: this board has a power amplifier and its receive port is the fragile end, receive is 12-bit while transmit is 16-bit, the AD9361 gain label is not proportional to gain, and stdout belongs to JSON-RPC so a stray print breaks the protocol.
license: GPL-2.0
compatibility: Python 3.10+ and the mcp SDK. A board reachable over libiio (default ip:192.168.2.1) is needed only for --live tests; everything else runs without hardware.
metadata:
  repository: Fishball7020-mcp
  companion: fishball7020-fpga-devkit
---

# The Fishball7020 MCP server

Seventeen tools over stdio that let an assistant drive a real SDR: tune it, sweep
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

**So check what is connected before transmitting.** `sdr_check_rf_setup`
reports what the ports appear to be attached to — and states the limit plainly:
the transmit socket has no detector, so whether an antenna is on *it* cannot be
measured by anything. Run it whenever the cabling is not already known.

**`sdr_find_board` is the answer to "it cannot reach the radio".** The default
`ip:192.168.2.1` is the USB gadget; a board on Ethernet with DHCP is elsewhere
and the only symptom is a connection error. That tool tries the usual addresses
and reports what to set `SDR_MCP_URI` to.

**`sdr_sample_gpio` is not a transmit tool and is not gated.** It only flips a
routing bit: the four bits the 12-bit DAC discards from each sample either
reach four header pins or they do not. Nothing is emitted by turning it on —
the pins move only while a buffer is already streaming, and what they do is
whatever is in the low nibble of those samples. Two things to tell a user who
asks for a clock on those pins: the nibble has to be OR-ed in **last**, after
any scaling, and the whole thing can be exercised with TX attenuation at
maximum, because the nibble never reaches the analog chain. `sdr_sample_gpio_clock`
authors a square wave and frame marker for them; its frame marker is one sample
wide, so it needs a scope — not seeing it through sysfs proves nothing.

**`sdr_tx_disable` and `sdr_tx_status` are never gated.** An off switch that can
be unavailable is not an off switch.

**Both transmit chains are reachable.** `sdr_tx_tone`, `sdr_transmit_iq` and
`sdr_transmit_waveform` take `channel` = `"0"` (TX1), `"1"` (TX2) or `"both"`,
which is the default.

**This board can destroy its own receiver.** It ships in a variant with a
Mini-Circuits PGA-102+ power amplifier and reaches about **+19 dBm**, against a
receive port rated to **+2.5 dBm**. Any loopback needs at least 20 dB of
attenuation. Refusals and docs should say this plainly rather than repeat the
generic "+7 dBm" figure that applies to a bare AD9361.

**Never return raw IQ inline.** Even a short capture is megabytes. Write to
`SDR_MCP_CAPTURE_DIR` and return the path plus statistics.

**Levels are dBFS against a 12-bit converter** (full scale ±2047) on receive,
but transmit takes the **full 16 bits**. Scaling transmit to ±2047 emits 24 dB
low. This asymmetry is measured, not assumed.

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
- Anything that transmits: check the gate, check `SDR_MCP_TX_BANDS`, log the
  call to stderr with frequency, gain and sample count.
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
