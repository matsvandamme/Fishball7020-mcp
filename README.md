# fishball-sdr-mcp

An [MCP](https://modelcontextprotocol.io) server that puts the Fishball7020 /
PlutoSky SDR behind a set of tools an LLM can use: tune it, measure the
spectrum, sweep a band, capture IQ, engage the FPGA channel filter, and — if
you deliberately enable it — transmit.

Built following the
[mcp-builder skill](https://github.com/anthropics/skills/tree/main/skills/mcp-builder).

The board's firmware and FPGA build system live in a companion repository,
[fishball7020-fpga-devkit](https://github.com/matsvandamme/fishball7020-fpga-devkit).

## What it needs

Only the MCP SDK. The board is reached over libiio's **network protocol using
plain sockets** (`fishball_sdr_mcp/iiod.py`), so there is no `pylibiio` to
install and no libiio version to match against the firmware. `numpy` is used
for the FFT if it happens to be importable, and a pure-Python transform is used
otherwise.

```bash
# run from: the repository root
python3 -m venv .venv
.venv/bin/pip install mcp        # add numpy for a faster FFT, optional
```

If `python3 -m venv` fails, install `python3-venv` and `python3-pip` first.

## Registering it

Copy `.mcp.json.example` to `.mcp.json` in whichever project you want the
server available from, adjust the paths, or:

```bash
claude mcp add fishball-sdr -- /absolute/path/to/Fishball7020-mcp/.venv/bin/python -m fishball_sdr_mcp
```

| Variable | Default | Meaning |
|---|---|---|
| `SDR_MCP_URI` | `ip:192.168.2.1` | Where the board is. `192.168.2.1` is the USB Ethernet gadget. |
| `SDR_MCP_TIMEOUT` | `10` | Socket timeout in seconds. |
| `SDR_MCP_CAPTURE_DIR` | `~/.cache/fishball-sdr` | Where `sdr_capture_iq` writes. |
| `SDR_MCP_ALLOW_TX` | unset | Set to `1` to permit transmitting. |
| `SDR_MCP_TX_BANDS` | unset | Restrict TX, e.g. `2400-2483.5,868-868.6` (MHz). |
| `SDR_MCP_NO_TX_QUIESCE` | unset | Leave the transmitter exactly as found at startup. |

## Tools

**Read-only** — `sdr_get_status`, `sdr_tx_chain_state`, `sdr_list_devices`,
`sdr_read_attribute`, `sdr_board_health`, `sdr_tx_status`

**Control** — `sdr_tune`, `sdr_configure_rx`, `sdr_set_fpga_filter`

**Measurement** — `sdr_capture_iq`, `sdr_spectrum`, `sdr_scan_band`

**Transmit** (gated) — `sdr_tx_tone`, `sdr_transmit_iq`,
`sdr_transmit_waveform`, `sdr_tx_disable`

Three things worth knowing about the design:

**`sdr_capture_iq` never returns samples inline.** It writes interleaved
little-endian int16 to a file and returns the path plus level statistics. Even
a short capture is megabytes; putting that through a context window helps
nobody. The format is what GNU Radio reads as a file source of type *short*,
and what `sdr_transmit_iq` accepts back.

**`sdr_set_fpga_filter` works by setting a sample rate.** There is no "filter
on" attribute. Writing `cf-ad9361-lpc`'s `sampling_frequency` to one eighth of
the converter rate is exactly what drives `GP_CONTROL` bit 0 and switches the
bypass mux in the bitstream. See
[docs/wbfm-channelizer.md](https://github.com/matsvandamme/fishball7020-fpga-devkit/blob/main/docs/wbfm-channelizer.md).

**Levels are dBFS against the 12-bit converter**, so full scale is ±2047, not
±32767.

## Transmitting

Off unless `SDR_MCP_ALLOW_TX=1`. The board tunes the FM broadcast band, where
transmitting without a licence is illegal, and an agent that can call a
transmitter should not be able to do so by accident.

- `sdr_tx_disable` and `sdr_tx_status` are **never** gated. An off switch that
  can be unavailable is not an off switch.
- `sdr_tx_disable` also runs on server shutdown, so a crashed client cannot
  leave the board transmitting a cyclic buffer.
- `cyclic=true` keeps transmitting **after the call returns**. That is the
  point of it, but it surprises people. `sdr_tx_status` shows what is running.
- Every transmit call is logged to stderr with frequency, scale and sample
  count.

### The transmitter is live at boot, and this server turns it off

Measured on real hardware: the board comes up in ENSM `fdd` with the TX
synthesiser **running** and only 10 dB of attenuation
(`adi,tx-attenuation-mdB = <0x2710>` in the device tree). So the TX port emits
LO leakage from power-on, with nothing in the DAC DMA and no DDS tone. The
device tree's `adi,tx-lo-powerdown-managed-enable` only acts on ENSM
transitions and does not help while the mode is `fdd`.

At these levels (the AD9361's own drivers, no external PA) an unterminated
output is not a damage risk, but there is no reason to leave a transmitter
running that you are not using.

So **unless transmitting is enabled, this server quiesces the transmitter at
startup**: DDS scales to zero, both attenuators to −89.75 dB, TX LO powered
down. Measured effect on reception: **0.08 dB**, i.e. none. Set
`SDR_MCP_NO_TX_QUIESCE=1` to leave the radio untouched.

## Testing

```bash
# run from: the repository root
.venv/bin/python evaluation/smoke_test.py          # protocol only, no radio
.venv/bin/python evaluation/smoke_test.py --live   # also call read-only tools
```

This replaces MCP Inspector, which needs Node 18+ while Ubuntu 22.04 packages
Node 12. It speaks JSON-RPC over stdio with nothing but the standard library
and checks the handshake, tool schemas and annotations, that the transmit gate
refuses and names the variable it needs, that diagnostics stay off stdout, and
— with `--live` — that every read-only tool returns real data. It never
transmits and never retunes the radio.
