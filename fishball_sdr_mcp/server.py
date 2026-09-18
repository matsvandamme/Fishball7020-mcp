"""MCP server exposing the Fishball7020 SDR.

Transport is stdio, which means STDOUT IS THE JSON-RPC CHANNEL. Nothing may
print to it. All diagnostics go to stderr via `log`.
"""

from __future__ import annotations

import array
import functools
import math
import os
import pathlib
import signal
import struct
import sys
import threading
import time
import wave
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import dsp, errors, formatting
from . import radio as radio_module
from .radio import PHY, PROBE_ATTEN_DB, TX, TX_LO, Radio, tx_allowed

Format = Annotated[
    Literal["markdown", "json"],
    Field(description="markdown for reading, json for structured output"),
]

server = MCPServer(
    name="fishball_sdr_mcp",
    version="0.1.0",
    instructions=(
        "Controls a Fishball7020 / PlutoSky SDR (Zynq-7020 + AD9361) over libiio.\n\n"
        "Start with sdr_get_status to see how the radio is configured, including "
        "whether the FPGA channel filter is engaged. Use sdr_spectrum to see what is "
        "on the air and sdr_scan_band to find signals across a range.\n\n"
        + ("TRANSMITTING IS ENABLED on this server. Before transmitting, run "
           "sdr_check_rf_setup unless you already know how the board is cabled: it "
           "reports what the ports appear to be attached to, and says plainly what "
           "it cannot determine. This board reaches about +19 dBm and covers the FM "
           "broadcast band, where transmitting without a licence is illegal, so the "
           "operator is responsible for what leaves the antenna port. Set "
           "SDR_MCP_ALLOW_TX=0 to turn transmitting off."
           if tx_allowed() else
           "TRANSMITTING IS DISABLED on this server (SDR_MCP_ALLOW_TX=0): every tool "
           "that would open a TX buffer or key a tone refuses, sdr_check_rf_setup "
           "stays passive, and the transmitter was attenuated to maximum at startup. "
           "sdr_tx_disable and sdr_tx_status always work.")),
)

_radio: Radio | None = None

# The MCP SDK dispatches each tools/call as its own task and runs these sync
# functions in worker threads, so two tool calls CAN interleave - and most of
# them are multi-step sequences on one shared radio (set gain, then LO, then
# DDS; transmit, capture, then tx_disable). One lock around every tool body
# makes each call atomic with respect to the others. Reentrant, because tools
# call radio() which may call quiesce, and because a tool may call another.
_OP = threading.RLock()


def _serialized(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _OP:
            return fn(*args, **kwargs)
    return wrapper



def log(message: str) -> None:
    print(f"[fishball_sdr_mcp] {message}", file=sys.stderr, flush=True)


def _describe_tx_state(st: dict) -> str:
    if not st.get("active"):
        return "no"
    age = time.time() - st.get("started_at", time.time())
    if st.get("kind") == "dds":
        return f"yes - DDS tone, {age:.0f} s ago"
    return (f"yes - {st.get('samples', '?')} samples, "
            f"{'cyclic' if st.get('cyclic') else 'one shot'}, {age:.0f} s ago")


def radio() -> Radio:
    global _radio
    if _radio is None:
        _radio = Radio()
        log(f"radio target {_radio.uri}, transmit {'ENABLED' if tx_allowed() else 'disabled'}")
        # The board boots with the TX synthesiser running and only 10 dB of
        # attenuation, so the transmit chain is live from power-on even with
        # nothing in the DAC DMA. Measured on hardware, silencing it costs
        # 0.08 dB of received channel power - nothing - so do it by default
        # whenever this server is not permitted to transmit anyway.
        quiesced = _radio.quiesce_tx_if_not_allowed()
        if quiesced:
            log(f"transmitter quiesced at startup: {'; '.join(quiesced['stopped'])}")
    return _radio


def capture_dir() -> pathlib.Path:
    path = pathlib.Path(
        os.environ.get("SDR_MCP_CAPTURE_DIR", pathlib.Path.home() / ".cache" / "fishball-sdr"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def prune_captures(keep: int | None = None) -> int:
    """Delete all but the newest `keep` captures. Returns how many went.

    A capture is megabytes and nothing ever removed them, so a long session
    quietly filled the user's cache directory. Keep a working set, drop the
    rest, and let SDR_MCP_CAPTURE_KEEP=0 turn pruning off for anyone who wants
    every file retained.
    """
    if keep is None:
        try:
            keep = int(os.environ.get("SDR_MCP_CAPTURE_KEEP", "20"))
        except ValueError:
            keep = 20
    if keep <= 0:
        return 0
    try:
        files = sorted((p for p in capture_dir().glob("*.iq16") if p.is_file()),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    removed = 0
    for old in files[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass                        # in use, or not ours to delete
    return removed


def fail(exc: BaseException) -> str:
    log(f"error: {exc!r}")
    return f"ERROR: {errors.describe(exc)}"


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------

@server.tool(
    name="sdr_get_status",
    title="Radio status",
    description=(
        "Report how the SDR is currently configured: RX local oscillator, converter "
        "and delivered sample rates, whether the FPGA channel filter is engaged, RF "
        "bandwidth, gain, RSSI, die temperature and firmware version. Start here."),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=True),
)
@_serialized
def sdr_get_status(response_format: Format = "markdown") -> str:
    try:
        s = radio().status()
        return formatting.render(s, formatting.status_markdown(s), response_format)
    except Exception as exc:
        return fail(exc)


@server.tool(
    name="sdr_tx_chain_state",
    title="Is the transmitter live?",
    description=(
        "Report whether the AD9361's transmit chain is actually energised, and why. "
        "The board boots into ENSM 'fdd' with the TX synthesiser running and only "
        "10 dB of attenuation, so TX is live from power-on even with no data in the "
        "DAC DMA - it emits LO leakage rather than silence. This tool shows the ENSM "
        "mode, TX LO powerdown state and both attenuator settings, and says plainly "
        "whether anything is being emitted. Use sdr_tx_disable to silence it."),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=True),
)
@_serialized
def sdr_tx_chain_state(response_format: Format = "markdown") -> str:
    try:
        r = radio()
        ensm = r.read_dev(PHY, "ensm_mode")
        powerdown = r.read(PHY, TX_LO, "powerdown", output=True).strip()
        atten = [r.read(PHY, ch, "hardwaregain", output=True).split()[0]
                 for ch in ("voltage0", "voltage1")]
        dds_live = []
        for ch in r.dds_channels():
            try:
                if abs(float(r.read(TX, ch, "scale", output=True))) > 0:
                    dds_live.append(ch)
            except Exception:
                continue
        lo_running = powerdown in {"0", "0.0"}
        emitting = lo_running and ensm in {"fdd", "tx", "pinctrl", "pinctrl_fdd_indep"}
        payload = {"ensm_mode": ensm, "tx_lo_powerdown": powerdown,
                   "tx_lo_running": lo_running, "tx_attenuation_db": atten,
                   "dds_channels_emitting": dds_live, "emitting": emitting}
        verdict = ("**The transmit chain is live.** The synthesiser is running and the "
                   "ENSM enables TX, so the port emits LO leakage even with no data. "
                   "Call `sdr_tx_disable` to silence it."
                   if emitting else
                   "**The transmit chain is quiet.** The synthesiser is powered down "
                   "or the ENSM does not enable TX.")
        rows = [("ENSM mode", ensm), ("TX LO powerdown", powerdown),
                ("TX LO running", "yes" if lo_running else "no"),
                ("TX0 attenuation", f"{atten[0]} dB"),
                ("TX1 attenuation", f"{atten[1]} dB"),
                ("DDS tones emitting", ", ".join(dds_live) if dds_live else "none")]
        return formatting.render(
            payload, "## Transmit chain\n\n" + formatting.table(rows) + "\n\n" + verdict,
            response_format)
    except Exception as exc:
        return fail(exc)


@server.tool(
    name="sdr_list_devices",
    title="List IIO devices",
    description=(
        "List every IIO device, channel and attribute the board exposes. Use this to "
        "discover exact names before calling sdr_read_attribute, or to check what a "
        "particular firmware build provides."),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=True),
)
@_serialized
def sdr_list_devices(response_format: Format = "markdown") -> str:
    try:
        r = radio()
        devices = r.devices(refresh=True)
        payload = {
            "context": r.context_attrs(),
            "devices": [
                {"name": d.name, "id": d.id, "attributes": d.attrs,
                 "channels": [{"id": c.id, "output": c.output, "attributes": c.attrs,
                               "scan_index": c.scan_index} for c in d.channels]}
                for d in devices.values()],
        }
        return formatting.render(
            payload, formatting.devices_markdown(devices, r.context_attrs()),
            response_format)
    except Exception as exc:
        return fail(exc)


@server.tool(
    name="sdr_read_attribute",
    title="Read an IIO attribute",
    description=(
        "Read any single IIO attribute by name - an escape hatch for anything the "
        "other tools do not cover. Use sdr_list_devices to find valid names. Reading "
        "an attribute called '<name>_available' shows the legal values for '<name>'."),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=True),
)
@_serialized
def sdr_read_attribute(
    device: Annotated[str, Field(description="Device name, e.g. 'ad9361-phy'")],
    attribute: Annotated[str, Field(description="Attribute name, e.g. 'rf_bandwidth'")],
    channel: Annotated[str | None, Field(
        description="Channel id, e.g. 'voltage0'. Omit for a device-level attribute.")] = None,
    output: Annotated[bool, Field(description="True for an output channel")] = False,
    response_format: Format = "markdown",
) -> str:
    try:
        r = radio()
        value = (r.read(device, channel, attribute, output) if channel
                 else r.read_dev(device, attribute))
        payload = {"device": device, "channel": channel, "attribute": attribute,
                   "value": value}
        where = f"{device}/{channel}" if channel else device
        return formatting.render(payload, f"`{where}` **{attribute}** = `{value}`",
                                 response_format)
    except Exception as exc:
        return fail(exc)


@server.tool(
    name="sdr_board_health",
    title="Board health",
    description=(
        "Read the Zynq XADC: internal supply rails and die temperature. Useful for "
        "checking the board is healthy, or whether it is running hot."),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=True),
)
@_serialized
def sdr_board_health(response_format: Format = "markdown") -> str:
    try:
        rails = radio().board_health()
        rows = [(f"{e.get('label', e['channel'])}",
                 f"{e.get('value', e.get('error', '?'))} {e.get('unit', '')}".strip())
                for e in rails]
        return formatting.render({"xadc": rails},
                                 "## Board health\n\n" + formatting.table(rows),
                                 response_format)
    except Exception as exc:
        return fail(exc)


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------

@server.tool(
    name="sdr_tune",
    title="Set the RX frequency",
    description=(
        "Set the receive local oscillator, in Hz. Valid range 70 MHz to 6 GHz.\n\n"
        "Note this tunes the LO, which becomes the CENTRE of the captured band. To "
        "keep the AD9361's LO leakage and DC offset out of a signal, tune "
        "deliberately off it and look at the resulting offset instead."),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                idempotentHint=True, openWorldHint=True),
)
@_serialized
def sdr_tune(
    frequency_hz: Annotated[int, Field(ge=70_000_000, le=6_000_000_000,
                                       description="RX LO frequency in Hz")],
    response_format: Format = "markdown",
) -> str:
    try:
        actual = radio().tune(frequency_hz)
        payload = {"requested_hz": frequency_hz, "actual_hz": actual}
        note = "" if actual == frequency_hz else (
            f"\n\n> Requested {formatting.hz(frequency_hz)}, the synthesiser landed on "
            f"{formatting.hz(actual)}.")
        return formatting.render(
            payload, f"RX LO set to **{formatting.hz(actual)}**.{note}", response_format)
    except Exception as exc:
        return fail(exc)


@server.tool(
    name="sdr_configure_rx",
    title="Configure the receiver",
    description=(
        "Set any of: converter sample rate (Hz), RF analog bandwidth (Hz), gain "
        "control mode, and manual gain (dB). Omitted settings are left alone. Values "
        "gain_mode is checked against the radio's own '*_available' list; the other "
        "values go straight to the device, which rejects an illegal "
        "request is rejected with the legal options rather than silently ignored.\n\n"
        "Manual gain only applies when gain_mode is 'manual'."),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                idempotentHint=True, openWorldHint=True),
)
@_serialized
def sdr_configure_rx(
    sample_rate_hz: Annotated[int | None, Field(
        default=None, ge=520_000, le=61_440_000,
        description="Converter sample rate in Hz")] = None,
    bandwidth_hz: Annotated[int | None, Field(
        default=None, ge=200_000, le=56_000_000,
        description="Analog RF bandwidth in Hz")] = None,
    gain_mode: Annotated[str | None, Field(
        default=None,
        description="e.g. manual, slow_attack, fast_attack, hybrid")] = None,
    gain_db: Annotated[float | None, Field(
        default=None, ge=-3, le=73, description="Manual gain in dB")] = None,
    response_format: Format = "markdown",
) -> str:
    try:
        result = radio().configure_rx(sample_rate_hz, bandwidth_hz, gain_mode, gain_db)
        rows = [("Converter rate", formatting.hz(result["converter_rate_hz"])),
                ("RF bandwidth", formatting.hz(result["rf_bandwidth_hz"])),
                ("Gain mode", result["gain_control_mode"]),
                ("Gain", f"{result['hardwaregain_db']} dB")]
        return formatting.render(result, "## Receiver configured\n\n"
                                 + formatting.table(rows), response_format)
    except Exception as exc:
        return fail(exc)


@server.tool(
    name="sdr_set_fpga_filter",
    title="Engage or bypass the FPGA channel filter",
    description=(
        "Switch the FPGA's decimate-by-8 channel filter into or out of the receive "
        "path.\n\n"
        "On firmware built from this devkit's channelizer patch, engaging it selects a "
        "sharp FIR that removes everything outside the wanted channel, and drops the "
        "delivered rate to one eighth of the converter rate. It works by writing the "
        "cf-ad9361-lpc sample rate, which is what drives GP_CONTROL bit 0 and the "
        "bypass mux in the bitstream - there is no separate on/off attribute.\n\n"
        "Bypassing is the way to A/B whether the filter is really doing anything: a "
        "neighbouring signal that reappears when bypassed is the proof."),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                idempotentHint=True, openWorldHint=True),
)
@_serialized
def sdr_set_fpga_filter(
    engaged: Annotated[bool, Field(description="True to engage (÷8), False to bypass")],
    response_format: Format = "markdown",
) -> str:
    try:
        result = radio().set_fpga_filter(engaged)
        rows = [("Converter rate", formatting.hz(result["converter_rate_hz"])),
                ("Delivered rate", formatting.hz(result["delivered_rate_hz"])),
                ("Decimation", f"÷{result['fpga_decimation']}"),
                ("Filter", "**engaged**" if result["fpga_filter_engaged"] else "bypassed")]
        warn = ""
        if engaged and not result["fpga_filter_engaged"]:
            warn = ("\n\n> Asked to engage the filter but the delivered rate did not "
                    "change. This firmware may not have the decimation core, or the "
                    "driver refused the rate.")
        return formatting.render(result, "## FPGA channel filter\n\n"
                                 + formatting.table(rows) + warn, response_format)
    except Exception as exc:
        return fail(exc)


@server.tool(
    name="sdr_sample_gpio",
    title="Sample-locked GPIO outputs on the expansion header",
    description=(
        "Read or set the four header pins that carry the low nibble of every transmit "
        "sample, so their edges are locked to the RF sample that carried them.\n\n"
        "The AD9361's transmit DAC is 12 bits and reads only the top 12 of each 16-bit "
        "sample; the bottom four reach the FPGA and are discarded. This feature routes "
        "them to JP5 pins 7/9/11/13 instead, at no analog cost - useful as a master "
        "clock, frame marker or sync line for hardware that must stay aligned with the "
        "transmitted waveform.\n\n"
        "Enabling it changes nothing on its own: the pins only move while a TX buffer "
        "is streaming, and what they do is whatever pattern is in the low nibble of "
        "the samples being sent. The nibble must be OR-ed in LAST, after any scaling, "
        "or the scaling overwrites it. With the feature off the same pins are ordinary "
        "Linux GPIO (978-981).\n\n"
        "Needs firmware built from the devkit with patches 0006/0007; older builds have "
        "no such attribute and the tool says so."),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                idempotentHint=True, openWorldHint=True),
)
@_serialized
def sdr_sample_gpio(
    enable: Annotated[bool | None, Field(
        description="True to route sample bits to the pins, False for plain GPIO, "
                    "omit to just read the current state")] = None,
    response_format: Format = "markdown",
) -> str:
    try:
        result = radio().sample_gpio(enable)
        rows = [("State", "**routing sample bits**" if result["enabled"]
                 else "ordinary GPIO (feature off)")]
        for name, p in result["pins"].items():
            rows.append((name, f"{p['header_net']} · JP5 pin {p['jp5_pin']} · "
                               f"ball {p['fpga_ball']} · gpio {p['linux_gpio']}"))
        note = ("\n\n> The pins carry the low nibble of channel 0's I samples, one "
                "nibble per sample, and only while a buffer is streaming. Maximum "
                "toggle rate is half the sample rate. Ground a probe on JP5 pin 2 "
                "or 20."
                if result["enabled"] else
                "\n\n> With this off the four pins are plain Linux GPIO and the "
                "transmit path is untouched.")
        return formatting.render(result, "## Sample-locked GPIO\n\n"
                                 + formatting.table(rows) + note, response_format)
    except Exception as exc:
        return fail(exc)


@server.tool(
    name="sdr_sample_gpio_clock",
    title="Drive a clock and frame marker out of the header pins",
    description=(
        "Transmit a cyclic buffer authored so the sample-locked GPIO pins carry a "
        "square wave, and optionally a frame marker on a second pin.\n\n"
        "A clock on these pins is not a mode you select - it is a pattern in the low "
        "nibble of the samples being transmitted. This writes that pattern for you: "
        "pin 0 (JP5 pin 7) toggles every `divider` samples, giving sample_rate/(2x"
        "divider); pin 1 (JP5 pin 9) pulses one sample every `frame_every` if you ask "
        "for it.\n\n"
        "Nothing meaningful is radiated: the nibble occupies bits the 12-bit DAC "
        "discards, the top 12 bits are zero throughout, and TX attenuation is pinned "
        "to maximum and read back. What does leave the port is LO leakage at "
        "-89.75 dB, the same as the board idles with - which is why this still "
        "honours SDR_MCP_ALLOW_TX. Stop it with sdr_tx_disable.\n\n"
        "The buffer length must be a whole number of cycles, or the pattern glitches "
        "where the cyclic buffer wraps; the tool refuses rather than producing a "
        "clock with a stutter once per buffer.\n\n"
        "Note the frame marker is ONE SAMPLE wide - 33 ns at 30.72 MSPS - so it needs "
        "a scope or logic analyser. Polling the pin through sysfs will never catch "
        "it, and seeing nothing there does not mean it is absent. The clock on pin 0 "
        "is a square wave and is visible to polling if you make it slow enough."),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                idempotentHint=True, openWorldHint=True),
)
@_serialized
def sdr_sample_gpio_clock(
    divider: Annotated[int, Field(
        ge=1, le=1 << 20,
        description="Toggle pin 0 every N samples. Clock = sample_rate/(2N).")] = 1,
    frame_every: Annotated[int | None, Field(
        description="Pulse pin 1 for one sample every N samples. Omit for no "
                    "frame marker.")] = None,
    buffer_samples: Annotated[int, Field(
        ge=1024, le=1 << 20,
        description="Cyclic buffer length; must be a whole number of clock "
                    "cycles and of frames.")] = 8192,
    response_format: Format = "markdown",
) -> str:
    # Behind the gate. The DAC data is zero, but opening a TX buffer brings the
    # LO up on this firmware, and LO leakage at maximum attenuation is still
    # "transmitting" to an operator who said SDR_MCP_ALLOW_TX=0.
    if not tx_allowed():
        return "ERROR: " + errors.tx_gate_message("drive the sample-locked GPIO clock")
    try:
        r = radio().sample_gpio_pattern(divider, frame_every, buffer_samples)
        rows = [("Sample rate", formatting.hz(r["sample_rate_hz"])),
                ("Pin 0 (JP5 7)", f"{formatting.hz(r['clock_hz'])} square wave"),
                ("Pin 1 (JP5 9)", formatting.hz(r["frame_hz"]) + " frame marker"
                 if r["frame_hz"] else "not driven"),
                ("Buffer", f"{r['buffer_samples']} samples, cyclic"),
                ("TX attenuation", f"{r['tx_attenuation_db']} dB (maximum)")]
        return formatting.render(
            r, "## Sample-locked GPIO clock\n\n" + formatting.table(rows)
            + "\n\n> Running until `sdr_tx_disable`. Ground a probe on JP5 pin 2 "
              "or 20. Nothing meaningful is on the RF port: the pattern lives in "
              "bits the DAC discards and the transmitter is at maximum "
              "attenuation.", response_format)
    except Exception as exc:
        return fail(exc)


@server.tool(
    name="sdr_find_board",
    title="Find the board when the default address does not answer",
    description=(
        "Look for a Fishball7020 on the usual addresses and report what to set "
        "SDR_MCP_URI to.\n\n"
        "The server defaults to ip:192.168.2.1, which is the USB gadget's address. A "
        "board on Ethernet with DHCP is somewhere else entirely, and the only symptom "
        "is a connection error with no hint about where to look. This tries the USB "
        "address, pluto.local, and any address already in the host's ARP table, and "
        "reports which of them actually answers IIOD."),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=True),
)
@_serialized
def sdr_find_board(response_format: Format = "markdown") -> str:
    try:
        found = radio_module.find_boards()
        if not found:
            body = ("## No board found\n\n"
                    "Tried the USB gadget address, `pluto.local`, and the host's ARP "
                    "neighbours. Check that the board is powered, that its USB or "
                    "Ethernet cable is connected, and that it has finished booting "
                    "(about 30 seconds). If it is on a network this host cannot see, "
                    "set `SDR_MCP_URI=ip:<address>` in the server's environment.")
            return formatting.render({"boards": []}, body, response_format)
        rows = [(b["uri"], f"{b.get('hw_model', 'responded to IIOD')}"
                           f"{'  ← currently in use' if b['current'] else ''}")
                for b in found]
        body = ("## Boards found\n\n" + formatting.table(rows)
                + "\n\n> Set `SDR_MCP_URI` in the server's environment to use a "
                  "different one; it is read at startup.")
        return formatting.render({"boards": found}, body, response_format)
    except Exception as exc:
        return fail(exc)


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def _capture(nsamples: int, channel_pair: int, settle: bool = True):
    r = radio()
    if settle:
        r.capture(min(4096, nsamples), channel_pair)   # discard: let the AGC settle
    raw = r.capture(nsamples, channel_pair)
    if not raw:
        raise ValueError("capture returned no samples - is the receive DMA running? "
                         "sdr_get_status shows the delivered rate.")
    return dsp.interleaved_to_complex(raw)


@server.tool(
    name="sdr_capture_iq",
    title="Capture IQ to a file",
    description=(
        "Capture raw IQ samples and WRITE THEM TO A FILE, returning the path plus "
        "level statistics. Samples are never returned inline - even a short capture is "
        "megabytes. The file is interleaved little-endian int16 (I,Q,I,Q,...), which "
        "GNU Radio reads as a file source of type short, and which sdr_transmit_iq "
        "accepts directly."),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                idempotentHint=False, openWorldHint=True),
)
@_serialized
def sdr_capture_iq(
    samples: Annotated[int, Field(ge=256, le=4_194_304,
                                  description="Samples per channel")] = 65536,
    channel_pair: Annotated[int, Field(ge=0, le=1, description="0 or 1")] = 0,
    filename: Annotated[str | None, Field(
        default=None, description="Name within the capture directory")] = None,
    response_format: Format = "markdown",
) -> str:
    try:
        r = radio()
        raw = r.capture(samples, channel_pair)
        iq = dsp.interleaved_to_complex(raw)
        name = filename or f"capture_{int(time.time())}_{samples}.iq16"
        if pathlib.Path(name).name != name or name.startswith(".") or not name.strip():
            raise ValueError("filename must be a bare name, not a path or a dotfile.")
        path = capture_dir() / name
        path.write_bytes(struct.pack(f"<{len(raw)}h", *raw))
        pruned = prune_captures()
        if pruned:
            log(f"pruned {pruned} older capture(s) from {capture_dir()}")
        stats = dsp.iq_statistics(iq)
        payload = {"path": str(path), "format": "interleaved int16 (I,Q)",
                   "sample_rate_hz": r.delivered_rate(), "center_hz": r.rx_lo(), **stats}
        rows = [("File", f"`{path}`"), ("Format", "interleaved int16 (I,Q)"),
                ("Sample rate", formatting.hz(payload["sample_rate_hz"])),
                ("Centre", formatting.hz(payload["center_hz"]))]
        rows += [(k.replace("_", " "), v) for k, v in stats.items()]
        return formatting.render(payload, "## Capture written\n\n"
                                 + formatting.table(rows), response_format)
    except Exception as exc:
        return fail(exc)


@server.tool(
    name="sdr_spectrum",
    title="Measure the spectrum",
    description=(
        "Capture IQ and return the strongest signals as a table of frequency and "
        "level, with the noise floor and capture statistics, optionally with a compact "
        "ASCII plot. This is the tool for 'what is on this frequency right now'.\n\n"
        "Levels are dBFS referred to the 12-bit converter's full scale. Only the "
        "sample rate's worth of spectrum around the current LO is visible - use "
        "sdr_scan_band to cover more."),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                idempotentHint=False, openWorldHint=True),
)
@_serialized
def sdr_spectrum(
    samples: Annotated[int, Field(ge=1024, le=262_144,
                                  description="Samples for the transform")] = 16384,
    peaks: Annotated[int, Field(ge=1, le=25, description="How many peaks")] = 8,
    plot: Annotated[bool, Field(description="Include an ASCII plot")] = True,
    channel_pair: Annotated[int, Field(ge=0, le=1)] = 0,
    response_format: Format = "markdown",
) -> str:
    try:
        r = radio()
        iq = _capture(samples, channel_pair)
        rate, center = r.delivered_rate(), r.rx_lo()
        freqs, mags = dsp.spectrum(iq, rate, center)
        floor = dsp.noise_floor_db(mags)
        separation = max(rate / 128.0, 1000.0)
        found = dsp.find_peaks(freqs, mags, peaks, separation)
        stats = dsp.iq_statistics(iq)
        art = dsp.ascii_spectrum(freqs, mags) if plot else None
        payload = {"center_hz": center, "sample_rate_hz": rate,
                   "noise_floor_dbfs": round(floor, 2),
                   "peaks": [{"frequency_hz": round(f, 1), "level_dbfs": round(d, 2)}
                             for f, d in found],
                   "statistics": stats}
        return formatting.render(
            payload, formatting.peaks_markdown(found, floor, stats, art), response_format)
    except Exception as exc:
        return fail(exc)


@server.tool(
    name="sdr_scan_band",
    title="Scan a frequency range",
    description=(
        "Step the LO across a range and report the signals found, strongest first. "
        "Use it to discover what is receivable - for example which FM broadcast "
        "stations are audible at this location.\n\n"
        "The radio is left tuned to the last step, so re-tune afterwards if you care "
        "where it sits. A wide scan at a low sample rate takes many steps and is slow; "
        "raise the sample rate to cover ground faster at coarser resolution."),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                idempotentHint=False, openWorldHint=True),
)
@_serialized
def sdr_scan_band(
    start_hz: Annotated[int, Field(ge=70_000_000, le=6_000_000_000)],
    stop_hz: Annotated[int, Field(ge=70_000_000, le=6_000_000_000)],
    step_hz: Annotated[int | None, Field(
        default=None, description="Defaults to 80% of the sample rate")] = None,
    samples: Annotated[int, Field(ge=1024, le=65536)] = 8192,
    threshold_db: Annotated[float, Field(
        description="Report signals at least this far above the noise floor")] = 12.0,
    max_results: Annotated[int, Field(ge=1, le=100)] = 25,
    response_format: Format = "markdown",
) -> str:
    try:
        if stop_hz <= start_hz:
            raise ValueError("stop_hz must be greater than start_hz.")
        r = radio()
        rate = r.delivered_rate()
        step = step_hz or int(rate * 0.8)
        if step <= 0:
            raise ValueError("Could not determine a step size; set step_hz explicitly.")
        steps = -(-(stop_hz - start_hz) // step)          # ceil: cover the top too
        if steps > 200:
            raise ValueError(
                f"That range needs {steps} steps of {formatting.hz(step)}. Raise "
                f"step_hz or the sample rate, or scan a narrower range (limit 200).")

        hits: list[tuple[float, float, float]] = []
        for i in range(steps):
            # Centre each capture window on its slice of the band. Tuning to
            # the slice's START wasted half of every window below the band and
            # never looked at the top of it - an 88-108 MHz scan tuned to 88
            # and covered 76-100, missing anything above 100 MHz.
            center = int(start_hz + step / 2 + i * step)
            if center - rate / 2 >= stop_hz:
                break
            r.tune(center)
            iq = _capture(samples, 0, settle=(i == 0))
            freqs, mags = dsp.spectrum(iq, rate, center)
            floor = dsp.noise_floor_db(mags)
            for f, d in dsp.find_peaks(freqs, mags, 5, max(rate / 32.0, 50e3)):
                # The bin at the LO carries the receiver's own DC offset and
                # LO leakage at every step, and is not a signal.
                if abs(f - center) < max(rate / 64.0, 50e3):
                    continue
                if d - floor >= threshold_db and start_hz <= f <= stop_hz:
                    hits.append((f, d, d - floor))

        hits.sort(key=lambda t: -t[2])
        merged: list[tuple[float, float, float]] = []
        for f, d, snr in hits:
            if any(abs(f - mf) < step / 4 for mf, _, _ in merged):
                continue
            merged.append((f, d, snr))
            if len(merged) >= max_results:
                break

        payload = {"start_hz": start_hz, "stop_hz": stop_hz, "step_hz": step,
                   "steps": steps, "threshold_db": threshold_db,
                   "signals": [{"frequency_hz": round(f, 1),
                                "level_dbfs": round(d, 2),
                                "snr_db": round(s, 2)} for f, d, s in merged]}
        lines = [f"## Scan {formatting.hz(start_hz)} to {formatting.hz(stop_hz)}", "",
                 f"{steps} steps of {formatting.hz(step)}, "
                 f"{len(merged)} signal(s) at least {threshold_db:g} dB above the floor.", ""]
        if merged:
            lines += ["| Frequency | Level | Above floor |", "|---|---|---|"]
            lines += [f"| {formatting.hz(f)} | {d:.1f} dBFS | {s:.1f} dB |"
                      for f, d, s in merged]
        else:
            lines.append("_Nothing found. Lower threshold_db, raise the gain, or "
                         "check the antenna._")
        lines += ["", f"> The radio is left tuned to {formatting.hz(r.rx_lo())}."]
        return formatting.render(payload, "\n".join(lines), response_format)
    except Exception as exc:
        return fail(exc)


# ---------------------------------------------------------------------------
# Transmit - gated
# ---------------------------------------------------------------------------

def _load_iq(path: pathlib.Path, fmt: str) -> list[complex]:
    if fmt == "auto":
        suffix = path.suffix.lower()
        fmt = {".iq16": "int16", ".bin": "int16", ".raw": "int16", ".sc16": "int16",
               ".cf32": "complex64", ".fc32": "complex64", ".iq": "complex64",
               ".wav": "wav"}.get(suffix, "int16")
    data = path.read_bytes()
    if fmt == "int16":
        vals = struct.unpack(f"<{len(data) // 2}h", data[:len(data) // 2 * 2])
        return [complex(vals[i], vals[i + 1]) for i in range(0, len(vals) - 1, 2)]
    if fmt == "complex64":
        floats = array.array("f")
        floats.frombytes(data[:len(data) // 4 * 4])
        if sys.byteorder != "little":
            floats.byteswap()
        return [complex(floats[i], floats[i + 1]) for i in range(0, len(floats) - 1, 2)]
    if fmt == "wav":
        with wave.open(str(path), "rb") as w:
            if w.getnchannels() != 2:
                raise ValueError("WAV must be 2 channels (I and Q).")
            if w.getsampwidth() != 2:
                raise ValueError("WAV must be 16-bit.")
            frames = w.readframes(w.getnframes())
        vals = struct.unpack(f"<{len(frames) // 2}h", frames)
        return [complex(vals[i], vals[i + 1]) for i in range(0, len(vals) - 1, 2)]
    raise ValueError(f"unknown format '{fmt}'.")


# Transmit full scale is the FULL 16-bit range, not the 12 bits the receive
# side uses. Measured on hardware over a TX->RX cable: digital amplitudes of
# 8191 and 32767 produced +12.7 dB and +24.8 dB relative to 2047 (expected
# +12.0 and +24.1) with no rise in distortion, so the DAC is not clipping at
# 2047. Scaling to 2047 as the receive path does would transmit 24 dB low.
DAC_FULL_SCALE = 32767.0
DAC_MIN = -32768


def _to_dac(iq: list[complex], scale: float) -> tuple[list[int], dict]:
    """Scale complex samples to the DAC's signed 16-bit range."""
    peak = max((max(abs(s.real), abs(s.imag)) for s in iq), default=0.0)
    if peak == 0:
        raise ValueError("the IQ file contains only zeros.")
    gain = (DAC_FULL_SCALE * scale) / peak
    out: list[int] = []
    clipped = 0
    for s in iq:
        for v in (s.real * gain, s.imag * gain):
            iv = int(round(v))
            if iv > DAC_FULL_SCALE:
                iv, clipped = int(DAC_FULL_SCALE), clipped + 1
            elif iv < DAC_MIN:
                iv, clipped = DAC_MIN, clipped + 1
            out.append(iv)
    return out, {"input_peak": round(peak, 1), "applied_gain": round(gain, 4),
                 "clipped_values": clipped}


@server.tool(
    name="sdr_check_rf_setup",
    title="What is connected to the antenna ports?",
    description=(
        "Work out what the RF ports are attached to, BEFORE transmitting. Run "
        "this whenever you are about to transmit and do not already know how "
        "the board is cabled.\n\n"
        "READ THE LIMIT FIRST: this board has no directional coupler and no "
        "detector on the transmit port, so whether an antenna is attached to TX "
        "cannot be measured. Nothing can measure it. What this does instead is "
        "gather the evidence that IS available - ambient RF on the receive port, "
        "and whether a low-power probe finds its way back - and say plainly what "
        "that does and does not establish.\n\n"
        f"The probe transmits briefly at {PROBE_ATTEN_DB:.0f} dB attenuation - about "
        "-41 dBm at the port on a PA board, 44 dB under what the receive port "
        "survives and negligible off an antenna - on the RECEIVE LO frequency, so "
        "the band restriction applies. Set probe=false to stay entirely passive; "
        "with SDR_MCP_ALLOW_TX=0 it stays passive regardless."),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                idempotentHint=True, openWorldHint=True),
)
@_serialized
def sdr_check_rf_setup(
    probe: Annotated[bool, Field(
        description="Also transmit a brief minimum-power tone to detect a "
                    "loopback. False stays passive.")] = True,
    channel_pair: Annotated[int, Field(ge=0, le=1)] = 0,
    response_format: Format = "markdown",
) -> str:
    try:
        r = radio()
        rate, center = r.delivered_rate(), r.rx_lo()

        # 1. Passive: is anything out there? An antenna on the receive port
        #    almost always picks up something; a load or an open port does not.
        iq = _capture(16384, channel_pair)
        freqs, mags = dsp.spectrum(iq, rate, center)
        floor = dsp.noise_floor_db(mags)
        found = dsp.find_peaks(freqs, mags, 5, max(rate / 128.0, 1000.0))
        strongest = max((d for _f, d in found), default=floor)
        ambient = strongest - floor

        loop_db = None
        probe_note = ""
        if probe and not tx_allowed():
            probe = False
            probe_note = ("\n\n> Probe skipped: transmitting is disabled on this server "
                          "(SDR_MCP_ALLOW_TX=0), so only the passive evidence is above.")
            log("rf setup probe skipped - transmit gate closed")
        if probe:
            # 2. Active: a minimum-power tone on the receive LO. If it comes
            #    back, TX and RX are connected to each other.
            loop_db = r.probe_loopback(channel_pair)
            log(f"rf setup probe: return {loop_db:.1f} dB above floor")

        # 25 dB is well clear of both measured cases: with no cable at all the
        # probe returns 4-7 dB (on-board TX->RX leakage), and through a 20 dB
        # pad it returns about 70 dB. There is no ambiguous middle to worry
        # about, so the threshold is placed for margin rather than precision.
        # UNVERIFIED above 3 GHz: the devkit's leak measurement (2026-09-18)
        # predicts a no-cable return of ~19-28 dB on channel 0 at 3-6 GHz, near
        # this threshold. See references/sdr-hardware.md, "Open question".
        if loop_db is not None and loop_db > 25:
            verdict = "loopback"
            says = (f"TX and RX are connected to each other. The probe came back "
                    f"{loop_db:.0f} dB above the noise floor.")
        elif ambient > 15:
            verdict = "antenna on RX"
            says = (f"The receive port is picking up ambient signals "
                    f"{ambient:.0f} dB above its noise floor, which normally "
                    f"means an antenna is attached to it.")
        else:
            verdict = "quiet"
            says = ("The receive port is quiet and a probe did not come back. "
                    "That is consistent with a terminated load, or with nothing "
                    "attached." if probe else
                    "The receive port is quiet, which is consistent with a load "
                    "or with nothing attached.")

        payload = {"verdict": verdict, "ambient_above_floor_db": round(ambient, 1),
                   "noise_floor_dbfs": round(floor, 2),
                   "loopback_return_db": None if loop_db is None else round(loop_db, 1),
                   "transmit_port_sensed": False}
        md = "\n".join([
            f"## RF setup: {verdict}", "", says, "",
            formatting.table([
                ("Ambient on RX, above its floor", f"{ambient:.1f} dB"),
                ("RX noise floor", f"{floor:.1f} dBFS"),
                ("Probe return", "not run" if loop_db is None else f"{loop_db:.1f} dB"),
            ]), "",
            "**The transmit port itself was not sensed and cannot be.** There is "
            "no coupler or detector on it. If this board has the PGA-102+ fitted "
            "it reaches about +19 dBm, so before transmitting satisfy yourself by "
            "other means that TX is not feeding an antenna you did not intend, "
            "and that any loopback has at least 20 dB of attenuation in it." + probe_note,
        ])
        return formatting.render(payload, md, response_format)
    except Exception as exc:
        return fail(exc)


@server.tool(
    name="sdr_tx_status",
    title="Transmit status",
    description=(
        "Report what the transmitter is doing: whether transmitting is enabled at all, "
        "the TX LO and its powerdown state, sample rate, bandwidth, every DDS tone "
        "generator, and whether this server started a cyclic buffer that is still "
        "running. Always available, even when transmitting is disabled."),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=True),
)
@_serialized
def sdr_tx_status(response_format: Format = "markdown") -> str:
    try:
        info = radio().tx_status()
        rows = [("Transmitting allowed", "yes" if info["allowed_by_env"] else
                 "no (SDR_MCP_ALLOW_TX=0 in this server's environment)"),
                ("TX LO", formatting.hz(info.get("tx_lo_hz", "?"))),
                ("TX LO powerdown", info.get("tx_lo_powerdown", "?")),
                ("TX sample rate", formatting.hz(info.get("tx_sample_rate_hz", "?"))),
                ("Buffer started here", _describe_tx_state(info["started_by_this_server"]))]
        md = "## Transmit status\n\n" + formatting.table(rows)
        if info.get("dds"):
            md += "\n\n### DDS tones\n\n| Channel | Frequency | Scale |\n|---|---|---|\n"
            md += "\n".join(f"| {k} | {formatting.hz(v['frequency_hz'])} | {v['scale']} |"
                            for k, v in sorted(info["dds"].items()))
        return formatting.render(info, md, response_format)
    except Exception as exc:
        return fail(exc)


@server.tool(
    name="sdr_tx_disable",
    title="Stop transmitting",
    description=(
        "Stop all transmission immediately: silence every DDS tone, close any sample "
        "buffer, and power down the TX local oscillator. Never gated - it works even "
        "when transmitting is otherwise disabled, because an off switch that can be "
        "unavailable is not an off switch. Safe to call at any time."),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                idempotentHint=True, openWorldHint=True),
)
@_serialized
def sdr_tx_disable(response_format: Format = "markdown") -> str:
    try:
        result = radio().tx_disable()
        log("transmit disabled by request")
        md = "## Transmitter stopped\n\n" + "\n".join(f"- {s}" for s in result["stopped"])
        if result["failed"]:
            md += "\n\n**Could not complete:**\n" + "\n".join(
                f"- {s}" for s in result["failed"])
        return formatting.render(result, md, response_format)
    except Exception as exc:
        return fail(exc)


@server.tool(
    name="sdr_tx_tone",
    title="Transmit a tone",
    description=(
        "Transmit a continuous single-tone carrier using the FPGA's DDS generators, at "
        "an offset from the TX local oscillator.\n\n"
        "TRANSMITS UNTIL STOPPED. Call sdr_tx_disable to stop it. "
        "Only transmit into a dummy load or on frequencies you are "
        "licensed to use."),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                idempotentHint=True, openWorldHint=True),
)
@_serialized
def sdr_tx_tone(
    lo_hz: Annotated[int, Field(ge=70_000_000, le=6_000_000_000,
                                description="TX local oscillator in Hz")],
    tone_offset_hz: Annotated[float, Field(
        description="Tone offset from the LO, in Hz")] = 100_000.0,
    scale: Annotated[float, Field(ge=0.0, le=1.0,
                                  description="Amplitude, 0 to 1")] = 0.1,
    tx_gain_db: Annotated[float, Field(
        ge=-89.75, le=0.0,
        description="TX attenuation in dB; 0 is full output. Must be set, "
                    "because the firmware idles at maximum attenuation.")] = -30.0,
    channel: Annotated[Literal["0", "1", "both"], Field(
        description="Which transmit port: channel 0 (TX1), channel 1 (TX2), "
                    "or both.")] = "both",
    response_format: Format = "markdown",
) -> str:
    if not tx_allowed():
        return "ERROR: " + errors.tx_gate_message("transmit a tone")
    try:
        r = radio()
        r.check_tx_frequency(lo_hz + tone_offset_hz)
        r.stop_buffer()                 # a running IQ buffer would otherwise keep playing
        applied_gain = r.set_tx_gain(tx_gain_db, channel=channel)
        r.write(PHY, TX_LO, "powerdown", 0, output=True)
        r.write(PHY, TX_LO, "frequency", int(lo_hz), output=True)
        channels = r.dds_tone(channel, tone_offset_hz, scale)
        if not channels:
            raise ValueError("this firmware exposes no DDS channels.")
        r.tx_state = {"active": True, "kind": "dds", "lo_hz": lo_hz,
                      "offset_hz": tone_offset_hz, "scale": scale,
                      "started_at": time.time()}
        log(f"TX TONE lo={lo_hz} offset={tone_offset_hz} scale={scale} "
            f"gain={applied_gain} channel={channel}")
        payload = {"lo_hz": lo_hz, "tone_offset_hz": tone_offset_hz, "scale": scale,
                   "emitted_at_hz": lo_hz + tone_offset_hz, "channels": channels,
                   "tx_gain_db": applied_gain}
        return formatting.render(
            payload,
            f"## Transmitting\n\nTone at **{formatting.hz(lo_hz + tone_offset_hz)}** "
            f"(LO {formatting.hz(lo_hz)} + {formatting.hz(tone_offset_hz)}), "
            f"scale {scale}.\n\n> Continues until `sdr_tx_disable` is called.",
            response_format)
    except Exception as exc:
        return fail(exc)


@server.tool(
    name="sdr_transmit_iq",
    title="Transmit an IQ file",
    description=(
        "Transmit an arbitrary IQ waveform read from a file on this machine.\n\n"
        "Formats: 'int16' interleaved little-endian I,Q (what sdr_capture_iq writes), "
        "'complex64' (what a GNU Radio file sink writes), 'wav' (2 channels as I and "
        "Q), or 'auto' to infer from the extension.\n\n"
        "With cyclic=true the buffer REPEATS FOREVER and transmission continues after "
        "this call returns - use sdr_tx_disable to stop. With cyclic=false the buffer "
        "plays once. Only transmit into a dummy load or "
        "on frequencies you are licensed to use."),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                idempotentHint=False, openWorldHint=True),
)
@_serialized
def sdr_transmit_iq(
    path: Annotated[str, Field(description="Path to the IQ file")],
    lo_hz: Annotated[int, Field(ge=70_000_000, le=6_000_000_000,
                                description="TX local oscillator in Hz")],
    sample_rate_hz: Annotated[int | None, Field(
        default=None, ge=520_000, le=61_440_000,
        description="TX sample rate; leave unset to keep the current one")] = None,
    file_format: Annotated[Literal["auto", "int16", "complex64", "wav"],
                           Field(description="Input sample format")] = "auto",
    cyclic: Annotated[bool, Field(
        description="Repeat forever (true) or play once (false)")] = False,
    scale: Annotated[float, Field(ge=0.0, le=1.0,
                                  description="Peak amplitude, 0 to 1")] = 0.5,
    tx_gain_db: Annotated[float, Field(
        ge=-89.75, le=0.0,
        description="TX attenuation in dB; 0 is full output. Must be set, "
                    "because the firmware idles at maximum attenuation.")] = -30.0,
    max_samples: Annotated[int, Field(ge=256, le=1_048_576)] = 1_048_576,
    channel: Annotated[Literal["0", "1", "both"], Field(
        description="Which transmit port: channel 0 (TX1), channel 1 (TX2), or "
                    "both, which sends the same waveform out of each.")] = "both",
    response_format: Format = "markdown",
) -> str:
    if not tx_allowed():
        return "ERROR: " + errors.tx_gate_message("transmit an IQ file")
    try:
        src = pathlib.Path(path).expanduser()
        if not src.is_file():
            raise ValueError(f"no such file: {src}")
        r = radio()
        r.check_tx_frequency(lo_hz)

        iq = _load_iq(src, file_format)
        if not iq:
            raise ValueError("no samples decoded - is file_format right?")
        truncated = len(iq) > max_samples
        if truncated:
            iq = iq[:max_samples]
        values, scaling = _to_dac(iq, scale)

        r.write(PHY, TX_LO, "powerdown", 0, output=True)
        r.write(PHY, TX_LO, "frequency", int(lo_hz), output=True)
        if sample_rate_hz is not None:
            r.write(PHY, "voltage0", "sampling_frequency", int(sample_rate_hz),
                    output=True)
        for ch in r.dds_channels():              # DDS must be silent first
            try:
                r.write(TX, ch, "scale", 0, output=True)
            except Exception:
                pass

        if cyclic:
            written = r.transmit_samples(values, cyclic, channel=channel)
            # Gain is set AFTER the buffer starts, and this order is
            # load-bearing. Starting a TX buffer fires the kernel's preenable
            # hook, which unmutes by restoring a CACHED attenuation -
            # clobbering anything written beforehand. Measured: asking for
            # -10 dB before the stream produced -60 dB on the wire. Writing it
            # afterwards lands last and wins.
            applied_gain = r.set_tx_gain(tx_gain_db, channel=channel)
        else:
            # A one-shot buffer has finished by the time transmit_samples
            # returns, so "afterwards" would raise the gain with nothing
            # playing and leave the carrier unmuted. Set it first - devkit
            # firmware (patch 0005) keeps a gain set before streaming - play
            # it out, then put the transmitter back to sleep.
            applied_gain = r.set_tx_gain(tx_gain_db, channel=channel)
            written = r.transmit_samples(values, cyclic, channel=channel)
            r.tx_disable()
        rate = r.read_int(PHY, "voltage0", "sampling_frequency", output=True)
        duration = len(iq) / rate if rate else 0.0
        log(f"TX IQ file={src.name} lo={lo_hz} samples={len(iq)} cyclic={cyclic} "
            f"scale={scale} gain={applied_gain} channel={channel}")

        payload = {"file": str(src), "lo_hz": lo_hz, "samples": len(iq),
                   "bytes_written": written, "cyclic": cyclic,
                   "sample_rate_hz": rate, "duration_s": round(duration, 4),
                   "truncated": truncated, **scaling}
        rows = [("File", f"`{src.name}`"), ("Centre", formatting.hz(lo_hz)),
                ("Samples", len(iq)), ("Sample rate", formatting.hz(rate)),
                ("Duration", f"{duration:.4f} s"),
                ("Mode", "cyclic (repeating)" if cyclic else "one shot"),
                ("Peak amplitude", f"{scale} of full scale"),
                ("TX gain", f"{applied_gain} dB"),
                ("Clipped values", scaling["clipped_values"])]
        md = "## Transmitting IQ\n\n" + formatting.table(rows)
        if cyclic:
            md += ("\n\n> **Still transmitting.** The buffer repeats until "
                   "`sdr_tx_disable` is called.")
        if truncated:
            md += (f"\n\n> File was longer than max_samples; only the first "
                   f"{max_samples} samples were sent.")
        return formatting.render(payload, md, response_format)
    except Exception as exc:
        return fail(exc)


@server.tool(
    name="sdr_transmit_waveform",
    title="Transmit a generated waveform",
    description=(
        "Synthesise and transmit a test signal without needing an IQ file: a single "
        "tone, two tones (for intermodulation testing), a linear chirp, or "
        "band-limited noise.\n\n"
        "Always cyclic, so it repeats until sdr_tx_disable is called. "
        "Only transmit into a dummy load or on frequencies you are "
        "licensed to use."),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                idempotentHint=False, openWorldHint=True),
)
@_serialized
def sdr_transmit_waveform(
    lo_hz: Annotated[int, Field(ge=70_000_000, le=6_000_000_000)],
    shape: Annotated[Literal["tone", "two_tone", "chirp", "noise"],
                     Field(description="Waveform to generate")] = "tone",
    bandwidth_hz: Annotated[float, Field(
        description="Tone offset, tone spacing, chirp width, or noise width")] = 100_000.0,
    samples: Annotated[int, Field(ge=1024, le=1_048_576)] = 65536,
    scale: Annotated[float, Field(ge=0.0, le=1.0)] = 0.25,
    tx_gain_db: Annotated[float, Field(
        ge=-89.75, le=0.0,
        description="TX attenuation in dB; 0 is full output. Must be set, "
                    "because the firmware idles at maximum attenuation.")] = -30.0,
    channel: Annotated[Literal["0", "1", "both"], Field(
        description="Which transmit port: channel 0 (TX1), channel 1 (TX2), or "
                    "both, which sends the same waveform out of each.")] = "both",
    response_format: Format = "markdown",
) -> str:
    if not tx_allowed():
        return "ERROR: " + errors.tx_gate_message(f"transmit a {shape} waveform")
    try:
        import random
        r = radio()
        r.check_tx_frequency(lo_hz)
        rate = r.read_int(PHY, "voltage0", "sampling_frequency", output=True)
        n = samples
        iq: list[complex] = []
        # A cyclic buffer must hold a whole number of cycles, or the phase
        # jumps at every wrap and the "tone" is a comb of splatter. Quantise
        # the frequency to the nearest integer cycle count and report it.
        actual_bw = bandwidth_hz
        if shape == "tone":
            cycles = max(1, round(bandwidth_hz * n / rate))
            actual_bw = cycles * rate / n
            w = 2 * math.pi * cycles / n
            iq = [complex(math.cos(w * i), math.sin(w * i)) for i in range(n)]
        elif shape == "two_tone":
            cycles = max(1, round((bandwidth_hz / 2) * n / rate))
            actual_bw = 2 * cycles * rate / n
            w1 = 2 * math.pi * cycles / n
            w2 = -w1
            iq = [0.5 * (complex(math.cos(w1 * i), math.sin(w1 * i))
                         + complex(math.cos(w2 * i), math.sin(w2 * i))) for i in range(n)]
        elif shape == "chirp":
            k = bandwidth_hz / (n / rate)
            iq = [complex(math.cos(math.pi * k * (i / rate) ** 2),
                          math.sin(math.pi * k * (i / rate) ** 2)) for i in range(n)]
        else:
            rng = random.Random(0)               # deterministic, so runs compare
            iq = [complex(rng.gauss(0, 0.3), rng.gauss(0, 0.3)) for _ in range(n)]

        values, scaling = _to_dac(iq, scale)
        r.write(PHY, TX_LO, "powerdown", 0, output=True)
        r.write(PHY, TX_LO, "frequency", int(lo_hz), output=True)
        for ch in r.dds_channels():
            try:
                r.write(TX, ch, "scale", 0, output=True)
            except Exception:
                pass
        written = r.transmit_samples(values, cyclic=True, channel=channel)
        # After the stream starts - see the note in sdr_transmit_iq.
        applied_gain = r.set_tx_gain(tx_gain_db, channel=channel)
        log(f"TX WAVEFORM {shape} lo={lo_hz} bw={actual_bw} samples={n} scale={scale} "
            f"gain={applied_gain} channel={channel}")
        payload = {"shape": shape, "lo_hz": lo_hz, "bandwidth_hz": actual_bw,
                   "requested_bandwidth_hz": bandwidth_hz,
                   "samples": n, "sample_rate_hz": rate, "bytes_written": written,
                   "cyclic": True, **scaling}
        rows = [("Shape", shape), ("Centre", formatting.hz(lo_hz)),
                ("Width / offset", formatting.hz(actual_bw)
                 + ("" if actual_bw == bandwidth_hz else
                    f" (nearest whole number of cycles to {formatting.hz(bandwidth_hz)})")),
                ("Samples", n), ("Sample rate", formatting.hz(rate)),
                ("Peak amplitude", f"{scale} of full scale"),
                ("TX gain", f"{applied_gain} dB")]
        return formatting.render(
            payload, "## Transmitting waveform\n\n" + formatting.table(rows)
            + "\n\n> **Still transmitting.** Repeats until `sdr_tx_disable` is called.",
            response_format)
    except Exception as exc:
        return fail(exc)


def _sigterm(*_):
    raise SystemExit(0)


def main() -> None:
    log("starting (stdio)")
    # Hosts commonly stop a server with SIGTERM, whose Python default is to die
    # without running finally blocks. Turn it into SystemExit so the cleanup
    # below - the only thing standing between a closed client and a cyclic
    # buffer that keeps transmitting - actually runs.
    signal.signal(signal.SIGTERM, _sigterm)
    try:
        server.run("stdio")
    finally:
        # A cyclic buffer would otherwise keep transmitting after the client
        # goes away, so silence the radio on the way out whenever this server
        # changed the transmit chain at all - not only when it thinks a
        # buffer is still active, because that flag has been wrong before.
        if _radio is not None and (_radio.tx_state.get("active")
                                   or getattr(_radio, "touched_tx", False)):
            log("shutting down after touching TX - silencing")
            try:
                _radio.tx_disable()
            except Exception as exc:
                log(f"could not silence on exit: {exc!r}")
        if _radio is not None:
            _radio.close()


if __name__ == "__main__":
    main()
