"""Response rendering, and the size cap that keeps results usable.

Every tool offers markdown (default, readable) and json (structured). Both go
through `render`, which truncates rather than letting a huge capture or a wide
device tree flood the caller's context.
"""

from __future__ import annotations

import json

CHARACTER_LIMIT = 25_000


def hz(value: float | int | str) -> str:
    """Human-scaled frequency: 528000 -> '528 kHz', 101044000 -> '101.044 MHz'."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    for scale, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if abs(v) >= scale:
            text = f"{v / scale:.6f}".rstrip("0").rstrip(".")
            return f"{text} {unit}"
    return f"{v:.0f} Hz"


def _truncate(text: str, note: str) -> str:
    if len(text) <= CHARACTER_LIMIT:
        return text
    keep = CHARACTER_LIMIT - len(note) - 32
    return text[:keep] + "\n\n...[truncated]...\n" + note


def render(data, markdown: str, response_format: str = "markdown") -> str:
    """Return one of the two representations, size-capped."""
    if response_format == "json":
        text = json.dumps(data, indent=2, default=str)
        return _truncate(text, "Response truncated. Request fewer items.")
    return _truncate(
        markdown,
        "Response truncated. Request fewer items, or use response_format='json'.")


def table(rows: list[tuple[str, object]]) -> str:
    """A two-column key/value markdown table."""
    if not rows:
        return "_(nothing to report)_"
    out = ["| | |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in rows]
    return "\n".join(out)


def status_markdown(s: dict) -> str:
    engaged = s["fpga_filter_engaged"]
    rows = [
        ("Board", f"{s['hw_model']} (fw {s['fw_version']})"),
        ("RX LO", hz(s["rx_lo_hz"])),
        ("Converter rate", hz(s["converter_rate_hz"])),
        ("Delivered rate", hz(s["delivered_rate_hz"])),
        ("FPGA channel filter",
         f"**engaged** (÷{s['fpga_decimation']})" if engaged else "bypassed (÷1)"),
        ("RF bandwidth", hz(s["rf_bandwidth_hz"])),
        ("Gain", f"{s['hardwaregain_db']} dB ({s['gain_control_mode']})"),
        ("RSSI", s["rssi"]),
        ("Die temperature", f"{s['temperature_c']} °C"),
        ("ENSM mode", s["ensm_mode"]),
    ]
    return ("## Radio status\n\n" + table(rows)
            + f"\n\n**Rate chain:** `{s['rx_path_rates']}`")


def peaks_markdown(peaks, floor_db: float, stats: dict, plot: str | None) -> str:
    lines = ["## Spectrum", "", f"Noise floor (median bin): **{floor_db:.1f} dBFS**", ""]
    if peaks:
        lines += ["| Frequency | Level | Above floor |", "|---|---|---|"]
        lines += [f"| {hz(f)} | {d:.1f} dBFS | {d - floor_db:.1f} dB |" for f, d in peaks]
    else:
        lines.append("_No peaks found._")
    lines += ["", "### Capture statistics", "",
              table([(k.replace("_", " "), v) for k, v in stats.items()])]
    if stats.get("clipped_samples"):
        lines += ["", f"> **{stats['clipped_samples']} samples clipped.** Reduce gain, "
                      "or switch gain_control_mode to slow_attack."]
    if plot:
        lines += ["", "```", plot, "```"]
    return "\n".join(lines)


def devices_markdown(devices: dict, context_attrs: dict) -> str:
    lines = ["## IIO context", ""]
    if context_attrs:
        lines += [table([(k, v) for k, v in sorted(context_attrs.items()) if v]), ""]
    for name, dev in sorted(devices.items()):
        scan = dev.scan_channels()
        lines.append(f"### `{name}` ({dev.id})")
        if scan:
            lines.append(f"_{len(scan)} buffered scan channels_")
        if dev.attrs:
            lines.append(f"- device attributes: `{'`, `'.join(dev.attrs)}`")
        for ch in dev.channels:
            direction = "out" if ch.output else "in "
            tag = f" scan[{ch.scan_index}]" if ch.scan_index is not None else ""
            attrs = f": `{'`, `'.join(ch.attrs)}`" if ch.attrs else ""
            lines.append(f"- `{ch.id}` ({direction}){tag}{attrs}")
        lines.append("")
    return "\n".join(lines)
