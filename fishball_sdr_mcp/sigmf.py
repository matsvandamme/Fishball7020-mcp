"""SigMF sidecars for captures.

A capture returned through MCP has a problem the CLI does not: its metadata
arrives in a chat reply, and the chat is over long before the file is. Whoever
opens that capture months later - a different agent, or a person - gets bytes
with no sample rate, no centre frequency and no gain, and cannot scroll back.

SigMF (the Signal Metadata Format) fixes it with a JSON file beside the data.
The samples are untouched, so anything that read the raw file still reads it,
and the recording now explains itself.

Everything here is written from values READ BACK off the board, never from the
arguments that were requested: the AD9361 quantises gain to its own table,
rf_bandwidth snaps to what the filter design supports, and sampling_frequency
lands on what the clock tree can produce.
"""

from __future__ import annotations

import datetime
import json
import pathlib

DATA_SUFFIX = ".sigmf-data"
META_SUFFIX = ".sigmf-meta"

# The AD9361's converters are 12-bit, delivered sign-extended into an int16, so
# receive full scale is +-2047 and NOT +-32767. SigMF's ci16_le describes the
# container and has no field for this, so it is recorded explicitly below.
# Divide by 32768 instead and every absolute level is 24 dB low - uniformly, so
# nothing looks wrong: spectra keep their shape and every SNR figure is
# unchanged, because those are ratios and the error cancels.
RX_FULL_SCALE = 2047


def meta_path(data_path: pathlib.Path) -> pathlib.Path:
    """The sidecar that belongs to a capture, whatever the data file is called."""
    if data_path.name.endswith(DATA_SUFFIX):
        return data_path.with_name(data_path.name[: -len(DATA_SUFFIX)] + META_SUFFIX)
    return data_path.with_suffix(META_SUFFIX)


def describe(radio, channel_pair: int, samples: int, stats: dict | None = None,
             description: str | None = None) -> dict:
    """Build the SigMF document for a capture that has just been taken.

    `radio` is a live Radio; every field is read from it now rather than
    remembered from the request. Anything the board declines to answer is left
    out rather than guessed - a missing field is honest, a wrong one is not.
    """
    def maybe(fn, *a):
        try:
            return fn(*a)
        except Exception:                      # a value we cannot read is simply absent
            return None

    rx = "voltage%d" % channel_pair            # gain lives on ad9361-phy, one per RX
    delivered = maybe(radio.delivered_rate)
    converter = maybe(radio.converter_rate)
    centre = maybe(radio.rx_lo)

    g = {
        "core:datatype": "ci16_le",
        "core:sample_rate": delivered or converter,
        "core:version": "1.0.0",
        "core:hw": "Fishball7020 / PlutoSky (Zynq XC7Z020 + AD9361), RX%d"
                   % (channel_pair + 1),
        "core:recorder": "fishball-sdr-mcp",
        "core:description": description or (
            "MCP capture, %d samples from RX%d" % (samples, channel_pair + 1)),
        "fishball:full_scale": RX_FULL_SCALE,
        "fishball:scaling_note":
            "Samples are signed 12-bit sign-extended into int16. Divide by %d for "
            "full scale, NOT 32768." % RX_FULL_SCALE,
        "fishball:converter_rate_hz": converter,
        "fishball:fabric_decimator":
            ("engaged" if (delivered and converter and delivered != converter)
             else "bypassed"),
        "fishball:hardwaregain_db":
            maybe(lambda: float(radio.read("ad9361-phy", rx, "hardwaregain").split()[0])),
        "fishball:gain_control_mode":
            maybe(lambda: radio.read("ad9361-phy", rx, "gain_control_mode")),
        "fishball:rf_bandwidth_hz":
            maybe(lambda: int(radio.read("ad9361-phy", rx, "rf_bandwidth").split()[0])),
        "fishball:rssi_db_below_fs":
            maybe(lambda: float(radio.read("ad9361-phy", rx, "rssi").split()[0])),
    }
    # the statistics the capture already computed, rather than recomputing them.
    # These key names are dsp.iq_statistics' own - keep them in step with it.
    for key in ("samples", "rms_dbfs", "peak_dbfs",
                "dc_offset_i", "dc_offset_q", "clipped_samples"):
        if stats and key in stats:
            g["fishball:" + key] = stats[key]

    # Clipping is worth saying twice: a clipped capture generates harmonics that
    # were never on the air, and analysing one wastes an afternoon.
    if stats and stats.get("clipped_samples"):
        g["fishball:clipping_warning"] = (
            "%s samples reached the +-%d rail. Reduce gain and capture again."
            % (stats["clipped_samples"], RX_FULL_SCALE))

    return {
        "global": {k: v for k, v in g.items() if v is not None},
        "captures": [{
            "core:sample_start": 0,
            "core:frequency": centre,
            "core:datetime": datetime.datetime.now(
                datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }],
        "annotations": [],
    }


def write(data_path: pathlib.Path, doc: dict) -> pathlib.Path:
    """Write the sidecar beside the capture. Returns its path."""
    path = meta_path(data_path)
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return path
