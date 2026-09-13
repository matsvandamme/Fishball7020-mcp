# fishball-sdr-mcp

Ask an LLM what's on the air, and have it actually go and look.

An [MCP](https://modelcontextprotocol.io) server for the **Fishball7020 /
PlutoSky** software-defined radio (Zynq-7020 + AD9361). It turns the board into
16 tools an assistant can use: tune it, sweep a band, measure a spectrum,
capture IQ, engage the FPGA channel filter, and — if you deliberately switch it
on — transmit.

```
> what FM stations can I actually receive here?

  sdr_scan_band(start_hz=87500000, stop_hz=108000000)

  | Frequency  | Level      | Above floor |
  |------------|------------|-------------|
  | 102.1 MHz  | -31.5 dBFS | 73.9 dB     |
  | 100.7 MHz  | -44.2 dBFS | 61.2 dB     |
  ...
```

The board's firmware and FPGA build system live in a companion repository,
[fishball7020-fpga-devkit](https://github.com/matsvandamme/fishball7020-fpga-devkit).

---

## Quick start

```bash
git clone https://github.com/matsvandamme/Fishball7020-mcp.git
cd Fishball7020-mcp
python3 -m venv .venv
.venv/bin/pip install mcp

claude mcp add fishball-sdr -- "$PWD/.venv/bin/python" -m fishball_sdr_mcp
```

Then ask for `sdr_get_status`. If the board answers, you're done.

Not sure where your board is? `iio_attr -S` scans and prints it. The default is
`ip:192.168.2.1`, the USB Ethernet gadget.

## Requirements

Python 3.10+, and a Fishball7020 reachable over libiio. That's it.

The board is reached over libiio's **network protocol using plain sockets**, so
there is no `pylibiio` to install and no libiio version to match against your
firmware. `numpy` is used for the FFT if it happens to be importable, and a
pure-Python transform otherwise — the server runs with only `mcp` installed.

## Tools

**Look at things** — `sdr_get_status` · `sdr_spectrum` · `sdr_scan_band` ·
`sdr_capture_iq` · `sdr_board_health` · `sdr_list_devices` ·
`sdr_read_attribute`

**Change things** — `sdr_tune` · `sdr_configure_rx` · `sdr_set_fpga_filter`

**Transmit** (off by default) — `sdr_tx_tone` · `sdr_transmit_iq` ·
`sdr_transmit_waveform` · `sdr_tx_status` · `sdr_tx_chain_state` ·
`sdr_tx_disable`

Every tool takes `response_format`: `markdown` to read, `json` to parse.

### Three design decisions worth knowing

**`sdr_capture_iq` writes to a file and returns the path.** It never returns
samples inline — even a short capture is megabytes, and putting that through a
context window helps nobody. The format is interleaved little-endian `int16`,
which GNU Radio reads as a file source of type *short* and which
`sdr_transmit_iq` accepts straight back.

**`sdr_set_fpga_filter` works by setting a sample rate.** There is no "filter
on" attribute anywhere. Writing `cf-ad9361-lpc`'s `sampling_frequency` to one
eighth of the converter rate is precisely what drives `GP_CONTROL` bit 0 and
flips the bypass mux in the bitstream. See
[the channelizer write-up](https://github.com/matsvandamme/fishball7020-fpga-devkit/blob/main/docs/wbfm-channelizer.md).

**Receive levels are dBFS against a 12-bit converter**, so full scale is ±2047.
Transmit is *not*: the DAC takes the full 16-bit range. That asymmetry is
measured, not assumed — see [Notes from the hardware](#notes-from-the-hardware).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SDR_MCP_URI` | `ip:192.168.2.1` | Where the board is |
| `SDR_MCP_TIMEOUT` | `10` | Socket timeout, seconds |
| `SDR_MCP_CAPTURE_DIR` | `~/.cache/fishball-sdr` | Where `sdr_capture_iq` writes |
| `SDR_MCP_ALLOW_TX` | unset | Set to `1` to permit transmitting |
| `SDR_MCP_TX_BANDS` | unset | Restrict TX, e.g. `2400-2483.5` (MHz) |
| `SDR_MCP_NO_TX_QUIESCE` | unset | Leave the transmitter exactly as found |

`.mcp.json.example` is a drop-in config if you'd rather not use `claude mcp add`.

## Transmitting

**Off unless `SDR_MCP_ALLOW_TX=1`.** This board tunes the FM broadcast band,
where transmitting without a licence is illegal, and an assistant that can call
a transmitter should not be able to do so by accident. Refusals name the exact
variable to set.

- `sdr_tx_disable` and `sdr_tx_status` are **never** gated. An off switch that
  can be unavailable is not an off switch.
- `sdr_tx_disable` also runs on server shutdown, so a crashed client cannot
  leave the board transmitting a cyclic buffer.
- `cyclic=true` keeps transmitting **after the call returns**. That's the point
  of it, and it still surprises people; `sdr_tx_status` shows what's running.
- Every transmit call is logged to stderr with frequency, gain and sample count.

> **A TX→RX loopback cable is the one setup that can damage the board — and the
> receiver is the fragile end.** The AD9361's RX input is rated to roughly
> **+2.5 dBm**; its transmitter reaches about **+7 dBm** at 0 dB attenuation.
> Connect with TX attenuation at maximum, fit a 20–30 dB inline attenuator if
> you have one, and raise power in steps.

## Testing

```bash
.venv/bin/python evaluation/smoke_test.py          # protocol only, no radio
.venv/bin/python evaluation/smoke_test.py --live   # also call read-only tools
```

This stands in for MCP Inspector, which needs Node 18 while Ubuntu 22.04
packages Node 12. It speaks JSON-RPC over stdio using only the standard library
and checks the handshake, tool schemas and annotations, that the transmit gate
refuses and names its variable, that diagnostics stay off stdout, and — with
`--live` — that every read-only tool returns real data. It never transmits and
never retunes your radio.

`evaluation/questions.xml` holds ten evaluation questions with verified answers,
per the [mcp-builder](https://github.com/anthropics/skills/tree/main/skills/mcp-builder)
skill this server was built to.

## Notes from the hardware

Things that cost real time to work out, recorded so they cost you none.

**The IIOD channel mask is fixed-width.** Exactly 8 hex characters per 32 scan
channels. `00000003` enables channels 0 and 1. Both `3` and
`0000000000000003` fail with `-22 EINVAL` and no hint as to why.

**`WRITEBUF` is acknowledged twice** — once before the payload and once after.
Skip the first status and the stream desyncs, with your samples arriving as the
next "response line".

**Transmit full scale is 16-bit; receive is 12-bit.** Measured over a cable:
digital amplitudes of 8191 and 32767 produced +12.7 dB and +24.8 dB relative to
2047 (expected +12.0 and +24.1) with no rise in distortion. Scaling transmit to
±2047, as the receive side does, emits 24 dB low.

**The transmitter idles hot on stock firmware.** The AD9361 comes up in ENSM
`fdd` with the synthesiser running and 10 dB of attenuation, so the TX port
leaks LO with nothing in the DAC. This server quiets it at startup unless
transmitting is enabled; the companion devkit
[fixes it properly in firmware](https://github.com/matsvandamme/fishball7020-fpga-devkit#transmitter-safety).

## License

Same terms as the companion devkit: GPL-2.0.
