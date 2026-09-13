"""Radio operations for the Fishball7020, built on the raw IIOD client.

Devices are resolved by NAME rather than by the iio:deviceN ids, because those
ids depend on probe order and shift between firmware builds. Names
(ad9361-phy, cf-ad9361-lpc, ...) are stable.
"""

from __future__ import annotations

import os
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from . import errors
from .iiod import DEFAULT_PORT, Iiod, mask_for

PHY = "ad9361-phy"
RX = "cf-ad9361-lpc"              # the ADC capture device
TX = "cf-ad9361-dds-core-lpc"     # DAC buffer + DDS tone generators
XADC = "xadc"

RX_LO = "altvoltage0"
TX_LO = "altvoltage1"

# The AD9361's own tuning range. Anything outside this the chip will refuse.
LO_MIN_HZ = 70_000_000
LO_MAX_HZ = 6_000_000_000

# Most attenuation the AD9361 TX chain offers. The board's device tree boots it
# at only -10 dB (adi,tx-attenuation-mdB = 10000).
TX_ATTEN_MAX_DB = -89.75


def _env_uri() -> str:
    return os.environ.get("SDR_MCP_URI", "ip:192.168.2.1")


def _host_from_uri(uri: str) -> tuple[str, int]:
    if uri.startswith("ip:"):
        uri = uri[3:]
    if ":" in uri and not uri.startswith("["):
        host, _, port = uri.rpartition(":")
        return host, int(port)
    return uri, DEFAULT_PORT


def tx_allowed() -> bool:
    return os.environ.get("SDR_MCP_ALLOW_TX", "").strip() in {"1", "true", "yes", "on"}


def tx_bands() -> list[tuple[float, float]]:
    """Optional MHz ranges TX is restricted to, e.g. '2400-2483.5,868-868.6'."""
    raw = os.environ.get("SDR_MCP_TX_BANDS", "").strip()
    out = []
    for part in filter(None, (p.strip() for p in raw.split(","))):
        lo, _, hi = part.partition("-")
        try:
            out.append((float(lo) * 1e6, float(hi) * 1e6))
        except ValueError:
            continue
    return out


@dataclass
class Channel:
    id: str
    output: bool
    attrs: list[str]
    scan_index: int | None = None


@dataclass
class Device:
    id: str
    name: str
    attrs: list[str] = field(default_factory=list)
    channels: list[Channel] = field(default_factory=list)

    def scan_channels(self) -> list[Channel]:
        return [c for c in self.channels if c.scan_index is not None]


class Radio:
    """A reconnecting IIOD session plus the operations the MCP tools expose."""

    def __init__(self, uri: str | None = None, timeout: float | None = None):
        self.uri = uri or _env_uri()
        self.host, self.port = _host_from_uri(self.uri)
        self.timeout = timeout or float(os.environ.get("SDR_MCP_TIMEOUT", "10"))
        self._client: Iiod | None = None
        self._devices: dict[str, Device] | None = None
        self._context_attrs: dict[str, str] = {}
        self._lock = threading.Lock()
        # Set only by this server's own TX tools, so sdr_tx_status can describe
        # what it started. The hardware has no way to report "cyclic buffer".
        self.tx_state: dict = {"active": False}
        errors.set_uri_hint(self.uri)

    # -- connection ---------------------------------------------------------

    def _connect(self) -> Iiod:
        if self._client is None:
            self._client = Iiod(self.host, self.port, self.timeout)
            self._client.connect()
        return self._client

    def _reset(self) -> None:
        if self._client is not None:
            self._client.close()
        self._client = None
        self._devices = None

    def _retry(self, fn, *a, **kw):
        """Run fn, and on a transport failure reconnect once and retry.

        IIOD sessions get dropped by USB re-enumeration, which is routine with
        this board; a dropped socket should not surface as a tool failure.
        """
        with self._lock:
            try:
                return fn(self._connect(), *a, **kw)
            except (ConnectionError, TimeoutError, BrokenPipeError, ValueError):
                self._reset()
                return fn(self._connect(), *a, **kw)

    def close(self) -> None:
        with self._lock:
            self._reset()

    # -- discovery ----------------------------------------------------------

    def devices(self, refresh: bool = False) -> dict[str, Device]:
        if self._devices is not None and not refresh:
            return self._devices
        xml = self._retry(lambda c: c.context_xml())
        root = ET.fromstring(xml)
        self._context_attrs = {
            a.get("name"): a.get("value") for a in root.findall("context-attribute")}
        found: dict[str, Device] = {}
        for dev in root.findall("device"):
            d = Device(id=dev.get("id"), name=dev.get("name") or dev.get("id"),
                       attrs=[a.get("name") for a in dev.findall("attribute")])
            for ch in dev.findall("channel"):
                scan = ch.find("scan-element")
                d.channels.append(Channel(
                    id=ch.get("id"),
                    output=(ch.get("type") == "output"),
                    attrs=[a.get("name") for a in ch.findall("attribute")],
                    scan_index=int(scan.get("index")) if scan is not None else None))
            found[d.name] = d
        self._devices = found
        return found

    def context_attrs(self) -> dict[str, str]:
        self.devices()
        return self._context_attrs

    def device_id(self, name: str) -> str:
        devs = self.devices()
        if name not in devs:
            raise ValueError(
                f"device '{name}' not present. Found: {', '.join(sorted(devs))}. "
                f"Run sdr_list_devices for the full tree.")
        return devs[name].id

    # -- attributes ---------------------------------------------------------

    def read(self, device: str, channel: str, attr: str, output: bool = False) -> str:
        did = self.device_id(device)
        return self._retry(lambda c: c.read_channel(did, channel, attr, output))

    def read_dev(self, device: str, attr: str) -> str:
        did = self.device_id(device)
        return self._retry(lambda c: c.read_device(did, attr))

    def write(self, device: str, channel: str, attr: str, value,
              output: bool = False) -> None:
        did = self.device_id(device)
        self._retry(lambda c: c.write_channel(did, channel, attr, value, output))

    def read_int(self, device: str, channel: str, attr: str, output: bool = False) -> int:
        return int(float(self.read(device, channel, attr, output)))

    # -- status -------------------------------------------------------------

    def rx_lo(self) -> int:
        return self.read_int(PHY, RX_LO, "frequency", output=True)

    def converter_rate(self) -> int:
        return self.read_int(PHY, "voltage0", "sampling_frequency")

    def delivered_rate(self) -> int:
        """The rate samples actually arrive at, after any FPGA decimation."""
        return self.read_int(RX, "voltage0", "sampling_frequency")

    def decimation_factor(self) -> int:
        """1 when the FPGA channel filter is bypassed, 8 when it is engaged.

        There is no attribute that says 'the filter is on'. The AXI ADC driver
        only offers factors 1 and 8, and engaging 8 is exactly what sets
        GP_CONTROL bit 0 and switches the bypass mux, so the ratio of the two
        reported rates IS the filter state.
        """
        delivered = self.delivered_rate()
        if delivered <= 0:
            return 1
        return max(1, round(self.converter_rate() / delivered))

    def status(self) -> dict:
        ctx = self.context_attrs()
        converter = self.converter_rate()
        delivered = self.delivered_rate()
        factor = max(1, round(converter / delivered)) if delivered else 1
        return {
            "hw_model": ctx.get("hw_model", "unknown"),
            "fw_version": ctx.get("fw_version", "unknown"),
            "uri": self.uri,
            "rx_lo_hz": self.rx_lo(),
            "converter_rate_hz": converter,
            "delivered_rate_hz": delivered,
            "fpga_decimation": factor,
            "fpga_filter_engaged": factor > 1,
            "rf_bandwidth_hz": self.read_int(PHY, "voltage0", "rf_bandwidth"),
            "gain_control_mode": self.read(PHY, "voltage0", "gain_control_mode"),
            "hardwaregain_db": self.read(PHY, "voltage0", "hardwaregain").split()[0],
            "rssi": self.read(PHY, "voltage0", "rssi"),
            "temperature_c": round(self.read_int(PHY, "temp0", "input") / 1000.0, 1),
            "ensm_mode": self.read_dev(PHY, "ensm_mode"),
            "rx_path_rates": self.read_dev(PHY, "rx_path_rates"),
        }

    def board_health(self) -> list[dict]:
        out = []
        dev = self.devices().get(XADC)
        if dev is None:
            return out
        for ch in dev.channels:
            if "raw" not in ch.attrs:
                continue
            entry: dict = {"channel": ch.id}
            try:
                raw = float(self.read(XADC, ch.id, "raw"))
                scale = float(self.read(XADC, ch.id, "scale")) if "scale" in ch.attrs else 1.0
                offset = float(self.read(XADC, ch.id, "offset")) if "offset" in ch.attrs else 0.0
                value = (raw + offset) * scale
                entry["value"] = round(value / 1000.0, 3)
                entry["unit"] = "degC" if ch.id.startswith("temp") else "V"
                if "label" in ch.attrs:
                    entry["label"] = self.read(XADC, ch.id, "label")
            except Exception as exc:                # one bad rail must not hide the rest
                entry["error"] = errors.describe(exc)
            out.append(entry)
        return out

    # -- control ------------------------------------------------------------

    def tune(self, hz: int) -> int:
        if not (LO_MIN_HZ <= hz <= LO_MAX_HZ):
            raise ValueError(
                f"{hz/1e6:.3f} MHz is outside the AD9361's {LO_MIN_HZ/1e6:.0f}-"
                f"{LO_MAX_HZ/1e9:.0f} GHz tuning range.")
        self.write(PHY, RX_LO, "frequency", int(hz), output=True)
        return self.rx_lo()

    def configure_rx(self, sample_rate_hz: int | None = None,
                     bandwidth_hz: int | None = None,
                     gain_mode: str | None = None,
                     gain_db: float | None = None) -> dict:
        if gain_mode is not None:
            allowed = self.read(PHY, "voltage0", "gain_control_mode_available").split()
            if gain_mode not in allowed:
                raise ValueError(
                    f"gain_control_mode '{gain_mode}' not supported. Choose one of: "
                    f"{', '.join(allowed)}.")
            self.write(PHY, "voltage0", "gain_control_mode", gain_mode)
        if sample_rate_hz is not None:
            self.write(PHY, "voltage0", "sampling_frequency", int(sample_rate_hz))
        if bandwidth_hz is not None:
            self.write(PHY, "voltage0", "rf_bandwidth", int(bandwidth_hz))
        if gain_db is not None:
            mode = self.read(PHY, "voltage0", "gain_control_mode")
            if mode != "manual":
                raise ValueError(
                    f"hardwaregain only applies in manual gain mode, but the mode is "
                    f"'{mode}'. Set gain_mode='manual' in the same call.")
            self.write(PHY, "voltage0", "hardwaregain", gain_db)
        return {
            "converter_rate_hz": self.converter_rate(),
            "rf_bandwidth_hz": self.read_int(PHY, "voltage0", "rf_bandwidth"),
            "gain_control_mode": self.read(PHY, "voltage0", "gain_control_mode"),
            "hardwaregain_db": self.read(PHY, "voltage0", "hardwaregain").split()[0],
        }

    def set_fpga_filter(self, engaged: bool) -> dict:
        """Engage or bypass the FPGA channel filter by choosing the ADC rate."""
        converter = self.converter_rate()
        target = converter // 8 if engaged else converter
        self.write(RX, "voltage0", "sampling_frequency", target)
        delivered = self.delivered_rate()
        factor = max(1, round(converter / delivered)) if delivered else 1
        return {
            "requested": "engaged" if engaged else "bypassed",
            "converter_rate_hz": converter,
            "delivered_rate_hz": delivered,
            "fpga_decimation": factor,
            "fpga_filter_engaged": factor > 1,
        }

    # -- capture ------------------------------------------------------------

    def capture(self, nsamples: int, channel_pair: int = 0) -> list[int]:
        """Capture interleaved int16 I/Q for one RX channel pair (0 or 1)."""
        dev = self.devices().get(RX)
        if dev is None:
            raise ValueError(f"'{RX}' not present; this firmware cannot capture.")
        total = len(dev.scan_channels())
        first = channel_pair * 2
        if first + 1 >= total:
            raise ValueError(
                f"channel pair {channel_pair} needs scan channels {first} and "
                f"{first+1}, but the device has {total}.")
        mask = mask_for([first, first + 1], total)
        did = dev.id
        return self._retry(
            lambda c: c.read_samples(did, nsamples, mask, nchannels=2))

    # -- transmit -----------------------------------------------------------

    def dds_channels(self) -> list[str]:
        dev = self.devices().get(TX)
        if dev is None:
            return []
        return sorted(c.id for c in dev.channels if c.id.startswith("altvoltage"))

    def tx_disable(self) -> dict:
        """Silence everything: DDS tones off, any buffer closed, TX LO down.

        Deliberately tolerant - it is the off switch, so it does as much as it
        can and reports what failed rather than aborting on the first error.
        """
        stopped, failed = [], []
        try:
            did = self.device_id(TX)
            self._retry(lambda c: (c.close_buffer(did), None)[1])
            stopped.append("buffer closed")
        except Exception as exc:
            failed.append(f"buffer: {errors.describe(exc)}")
        for ch in self.dds_channels():
            for attr, value in (("raw", 0), ("scale", 0)):
                try:
                    self.write(TX, ch, attr, value, output=True)
                except Exception:
                    pass                        # not every DDS channel has both
            stopped.append(f"{ch} silenced")
        # Maximum attenuation, then stop the synthesiser. Order matters: turn
        # the signal down before turning the oscillator off, not after.
        for ch in ("voltage0", "voltage1"):
            try:
                self.write(PHY, ch, "hardwaregain", TX_ATTEN_MAX_DB, output=True)
                stopped.append(f"TX {ch} attenuated to {TX_ATTEN_MAX_DB} dB")
            except Exception as exc:
                failed.append(f"TX {ch} attenuation: {errors.describe(exc)}")
        try:
            self.write(PHY, TX_LO, "powerdown", 1, output=True)
            stopped.append("TX LO powered down")
        except Exception as exc:
            failed.append(f"TX LO: {errors.describe(exc)}")
        self.tx_state = {"active": False, "stopped_at": time.time()}
        return {"stopped": stopped, "failed": failed}

    def quiesce_tx_if_not_allowed(self) -> dict | None:
        """Silence the transmitter at startup unless transmitting is enabled.

        The board boots into ENSM 'fdd' with the TX synthesiser running and only
        10 dB of attenuation, so the transmit chain is live from power-on even
        with no data in the DAC DMA. Measured on hardware, quiescing it changes
        received channel power by 0.08 dB - i.e. not at all - so there is no
        reason to leave it running when this server is not allowed to transmit.

        Set SDR_MCP_NO_TX_QUIESCE=1 to leave the radio exactly as found.
        """
        if tx_allowed() or os.environ.get("SDR_MCP_NO_TX_QUIESCE", "").strip() in {
                "1", "true", "yes", "on"}:
            return None
        # Attenuation only - deliberately NOT the TX LO. Powering the
        # synthesiser down here would silently break an unrelated transmitter
        # (a GNU Radio sink, say) that never asked this server for anything:
        # it would stream happily into a dead LO and emit nothing. Muting the
        # attenuators is reversible by anyone who sets a gain, so it cannot
        # strand another process. sdr_tx_disable still does the full stop,
        # because that is an explicit request to stop.
        stopped, failed = [], []
        for ch in ("voltage0", "voltage1"):
            try:
                self.write(PHY, ch, "hardwaregain", TX_ATTEN_MAX_DB, output=True)
                stopped.append(f"TX {ch} attenuated to {TX_ATTEN_MAX_DB} dB")
            except Exception as exc:
                failed.append(f"TX {ch}: {errors.describe(exc)}")
        return {"stopped": stopped, "failed": failed} if stopped else None

    def tx_status(self) -> dict:
        info: dict = {"allowed_by_env": tx_allowed(), "started_by_this_server": self.tx_state}
        bands = tx_bands()
        if bands:
            info["restricted_to_bands_mhz"] = [
                [round(lo / 1e6, 4), round(hi / 1e6, 4)] for lo, hi in bands]
        try:
            info["tx_lo_hz"] = self.read_int(PHY, TX_LO, "frequency", output=True)
            info["tx_lo_powerdown"] = self.read(PHY, TX_LO, "powerdown", output=True)
            info["tx_sample_rate_hz"] = self.read_int(PHY, "voltage0",
                                                      "sampling_frequency", output=True)
            info["tx_rf_bandwidth_hz"] = self.read_int(PHY, "voltage0",
                                                       "rf_bandwidth", output=True)
            dds = {}
            for ch in self.dds_channels():
                try:
                    dds[ch] = {
                        "frequency_hz": self.read(TX, ch, "frequency", output=True),
                        "scale": self.read(TX, ch, "scale", output=True),
                    }
                except Exception:
                    continue
            info["dds"] = dds
        except Exception as exc:
            info["error"] = errors.describe(exc)
        return info

    def set_tx_gain(self, db: float) -> float:
        """Set TX attenuation on both channels; returns what the chip took.

        Necessary, not optional. With the TX-mute firmware the chip sits at
        maximum attenuation until something asks to transmit, so a transmit
        tool that never sets a gain emits nothing at all.
        """
        if not (TX_ATTEN_MAX_DB <= db <= 0.0):
            raise ValueError(
                f"TX gain must be between {TX_ATTEN_MAX_DB} and 0 dB (0 = full "
                f"output). Got {db}.")
        for ch in ("voltage0", "voltage1"):
            self.write(PHY, ch, "hardwaregain", db, output=True)
        return float(self.read(PHY, "voltage0", "hardwaregain", output=True).split()[0])

    def check_tx_frequency(self, hz: float) -> None:
        bands = tx_bands()
        if not bands:
            return
        if not any(lo <= hz <= hi for lo, hi in bands):
            pretty = ", ".join(f"{lo/1e6:g}-{hi/1e6:g} MHz" for lo, hi in bands)
            raise ValueError(
                f"{hz/1e6:.4f} MHz is outside the bands this server is allowed to "
                f"transmit in ({pretty}). Change SDR_MCP_TX_BANDS to permit it.")

    def transmit_samples(self, values: list[int], cyclic: bool) -> int:
        did = self.device_id(TX)
        # A cyclic buffer left running makes the next OPEN fail with EBUSY, so
        # replace rather than refuse: transmitting again is a perfectly
        # reasonable thing to ask for, and the error was unhelpful.
        self._retry(lambda c: (c.close_buffer(did), None)[1])
        dev = self.devices()[TX]
        total = len(dev.scan_channels())
        mask = mask_for([0, 1], total)
        written = self._retry(
            lambda c: c.write_samples(did, values, mask, nchannels=2, cyclic=cyclic))
        self.tx_state = {
            "active": True, "cyclic": cyclic,
            "samples": len(values) // 2, "started_at": time.time(),
        }
        if not cyclic:
            self._retry(lambda c: (c.close_buffer(did), None)[1])
            self.tx_state["active"] = False
        return written
