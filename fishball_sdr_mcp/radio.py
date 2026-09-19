"""Radio operations for the Fishball7020, built on the raw IIOD client.

Devices are resolved by NAME rather than by the iio:deviceN ids, because those
ids depend on probe order and shift between firmware builds. Names
(ad9361-phy, cf-ad9361-lpc, ...) are stable.
"""

from __future__ import annotations

import math
import os
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from . import errors
from .iiod import DEFAULT_PORT, Iiod, IiodError, mask_for

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
# How hard the setup probe transmits. About -41 dBm at the port with the PA
# fitted: loud enough to find a cable through a 50 dB pad, quiet enough to be
# meaningless into an antenna and 44 dB under what the receive port survives.
PROBE_ATTEN_DB = 60.0


def _env_uri() -> str:
    return os.environ.get("SDR_MCP_URI", "ip:192.168.2.1")


def _host_from_uri(uri: str) -> tuple[str, int]:
    if uri.startswith("ip:"):
        uri = uri[3:]
    if ":" in uri and not uri.startswith("["):
        host, _, port = uri.rpartition(":")
        return host, int(port)
    return uri, DEFAULT_PORT


def find_boards(extra: list[str] | None = None) -> list[dict]:
    """Which addresses actually answer IIOD?

    The server defaults to the USB gadget's 192.168.2.1. A board on Ethernet
    with DHCP is somewhere else, and the only symptom is a connection error
    that says nothing about where to look. Try the obvious places and report
    what answered, so the fix is "set SDR_MCP_URI to this" rather than a hunt.
    """
    import socket
    import subprocess

    candidates: list[str] = ["192.168.2.1"]
    current, _ = _host_from_uri(_env_uri())
    if current not in candidates:
        candidates.insert(0, current)
    for name in ("pluto.local", "fishball.local"):
        try:
            candidates.append(socket.gethostbyname(name))
        except OSError:
            pass
    try:                                  # hosts this machine has recently talked to
        out = subprocess.run(["ip", "neigh"], capture_output=True, text=True,
                             timeout=5).stdout
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0].count(".") == 3:
                candidates.append(parts[0])
    except Exception:
        pass
    if extra:
        candidates.extend(extra)

    seen: list[str] = []
    for host in candidates:
        if host not in seen:
            seen.append(host)
    # Bounded: a LAN with dozens of stale ARP neighbours must not turn this
    # into a minutes-long crawl. The default and mDNS names come first, so the
    # cap only ever drops distant strangers.
    seen = seen[:16]
    try:
        current_ip = socket.gethostbyname(current)
    except OSError:
        current_ip = current

    def probe(host: str) -> dict | None:
        try:
            with Iiod(host, DEFAULT_PORT, timeout=1.5) as c:
                xml = c.context_xml()
            model = ""
            for attr in ET.fromstring(xml).iter("context-attribute"):
                if attr.get("name") == "hw_model":
                    model = attr.get("value", "")
                    break
            return {"uri": f"ip:{host}", "hw_model": model or "unknown",
                    "current": host in (current, current_ip)}
        except Exception:
            return None

    from concurrent.futures import ThreadPoolExecutor, as_completed
    found = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(probe, h): h for h in seen}
        try:
            for fut in as_completed(futures, timeout=8.0):
                r = fut.result()
                if r:
                    found.append(r)
        except Exception:
            pass                        # deadline hit: report what answered
    found.sort(key=lambda b: (not b["current"], b["uri"]))
    return found


def tx_allowed() -> bool:
    """Is transmitting permitted?

    Enabled by default; set SDR_MCP_ALLOW_TX=0 to turn it off. This was an
    opt-IN gate, and is deliberately no longer one - the board's owner asked
    for transmit available without ceremony.

    What has NOT changed, because the reasons for it have not: sdr_tx_disable
    and sdr_tx_status are never gated, every transmit call is logged, and
    SDR_MCP_TX_BANDS still restricts frequencies if it is set. Transmitting on
    licensed spectrum is still the operator's responsibility, and this board
    reaches about +19 dBm.
    """
    raw = os.environ.get("SDR_MCP_ALLOW_TX", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


def tx_channels(which: str = "both") -> list[int]:
    """Transmit scan-channel indices for a channel selection.

    The DAC device exposes four scan channels: TX1 I, TX1 Q, TX2 I, TX2 Q. A
    single channel is a consecutive I/Q pair; "both" is all four, which sends
    the same waveform out of both ports.
    """
    return {"0": [0, 1], "1": [2, 3], "both": [0, 1, 2, 3]}[str(which)]


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

    def write_dev(self, device: str, attr: str, value) -> None:
        did = self.device_id(device)
        self._retry(lambda c: c.write_device(did, attr, value))

    def read_int(self, device: str, channel: str, attr: str, output: bool = False) -> int:
        return int(float(self.read(device, channel, attr, output)))

    # -- sample-locked GPIO -------------------------------------------------

    def sample_gpio(self, enable: bool | None = None) -> dict:
        """Read or set the sample-locked GPIO outputs.

        The four bits the 12-bit DAC discards from every transmit sample are
        routed to header pins JP5 7/9/11/13, so their edges are locked to the
        RF sample that carried them. The enable is a device attribute added by
        the devkit's patch 0007; firmware without it has no such attribute,
        which is worth saying plainly rather than reporting a bare IIO error.
        """
        try:
            if enable is not None:
                self.write_dev(TX, "tx_sample_gpio_en", 1 if enable else 0)
            state = self.read_dev(TX, "tx_sample_gpio_en").strip()
        except IiodError as exc:
            # ENOENT/EINVAL from IIOD is "no such attribute". A dropped
            # connection is an OSError and must NOT be reported as old firmware.
            raise ValueError(
                "This firmware has no tx_sample_gpio_en attribute, so it predates "
                "the sample-locked GPIO feature (devkit patches 0006/0007). Rebuild "
                "and reflash from the devkit to get it."
            ) from exc
        return {
            "enabled": state not in ("0", ""),
            "pins": {f"sample_gpio[{i}]": {"header_net": f"3V3_IO{i + 1}",
                                           "jp5_pin": 7 + 2 * i,
                                           "fpga_ball": b,
                                           "linux_gpio": 978 + i}
                     for i, b in enumerate(("V10", "U9", "U10", "T9"))},
        }

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

    # The DDS exposes eight generators: two per I/Q path per channel.
    #   altvoltage0 TX1_I_F1   1 TX1_I_F2   2 TX1_Q_F1   3 TX1_Q_F2
    #             4 TX2_I_F1   5 TX2_I_F2   6 TX2_Q_F1   7 TX2_Q_F2
    # A single complex tone needs the F1 generator on BOTH I and Q of a
    # channel, 90 degrees apart - which is the driver's own default phasing.
    # Driving the two generators of the I path instead, as this once did,
    # produces a real signal with both sidebands rather than one tone.
    DDS_F1 = {"0": [(0, 2)], "1": [(4, 6)], "both": [(0, 2), (4, 6)]}
    DDS_F2 = {"0": [1, 3], "1": [5, 7], "both": [1, 3, 5, 7]}

    def dds_tone(self, which: str, offset_hz: float, scale: float) -> list[str]:
        """Set up a complex tone on the selected channel(s). Returns the
        channel ids driven."""
        if offset_hz < 0:
            raise ValueError(
                "negative tone offsets are not supported by the DDS phasing used "
                "here; choose an LO below the wanted frequency instead.")
        self.touched_tx = True
        avail = self.dds_channels()
        driven = []
        for i_idx, q_idx in self.DDS_F1[str(which)]:
            for idx, phase in ((i_idx, 90000), (q_idx, 0)):
                ch = f"altvoltage{idx}"
                if ch not in avail:
                    continue
                self.write(TX, ch, "frequency", int(abs(offset_hz)), output=True)
                self.write(TX, ch, "phase", phase, output=True)
                self.write(TX, ch, "scale", scale, output=True)
                driven.append(ch)
        # Silence the second generator of every driven path, or it adds a tone.
        for idx in self.DDS_F2[str(which)]:
            ch = f"altvoltage{idx}"
            if ch in avail:
                try:
                    self.write(TX, ch, "scale", 0, output=True)
                except Exception:
                    pass
        return driven

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

    def probe_loopback(self, channel_pair: int = 0, rx_pair: int | None = None) -> float:
        """Transmit a brief minimum-power tone; return how far above the noise
        floor it comes back, in dB.

        Deliberately quiet: PROBE_ATTEN_DB of attenuation puts about -41 dBm at
        the port even on a PA-equipped board, which is negligible into an
        antenna and some 44 dB below what the receiver can survive. Maximum
        attenuation was tried first and is too quiet to be useful - the return
        through a 20 dB pad came back only 6-10 dB above the floor, which is
        not distinguishable from leakage. Restores the transmitter afterwards
        whatever happens.
        """
        from . import dsp
        rate = self.delivered_rate()
        offset = rate / 8.0
        n = 4096
        k = round(offset * n / rate)
        values = []
        for i in range(n):
            ph = 2 * math.pi * k * i / n
            values += [int(round(16384 * math.cos(ph))), int(round(16384 * math.sin(ph)))]
        rxp = channel_pair if rx_pair is None else rx_pair
        before = [self.read(PHY, ch, "hardwaregain", output=True).split()[0]
                  for ch in ("voltage0", "voltage1")]
        # The probe listens on the RECEIVE LO, so the transmitter has to be put
        # there too - and switched on. Neither happened in an earlier version:
        # the probe went out on whatever TX LO the last tool left (2.4 GHz after
        # boot) while the receiver listened at, say, 88 MHz after a band scan,
        # and tx_disable() in the cleanup had powered the LO down for every
        # probe after the first. Both produced a confident "quiet" verdict with
        # a cable fitted, on the one check meant to protect the receiver.
        rx_lo = self.rx_lo()
        self.check_tx_frequency(rx_lo)
        tx_lo_before = self.read(PHY, TX_LO, "frequency", output=True).strip()
        tx_pd_before = self.read(PHY, TX_LO, "powerdown", output=True).strip()
        # Pin the receive gain. Without this the probe reads whatever gain was
        # left behind - or an AGC adapting under it - and the result says more
        # about the last tool that ran than about the cabling. 40 dB is inside
        # the AD9361's transition-free window, so it means what it says.
        rx_mode = self.read(PHY, f"voltage{rxp}", "gain_control_mode")
        rx_gain = self.read(PHY, f"voltage{rxp}", "hardwaregain").split()[0]
        target = rx_lo + k * rate / n
        window = max(rate / 512.0, 2000.0)

        def level_at_target() -> tuple[float, float]:
            # capture() returns interleaved int16 (I, Q, I, Q, ...). Handed to
            # the spectrum as-is, it was analysed as a real signal: the tone
            # landed at the wrong frequency and the probe read noise, so it
            # could not see a loopback at all. Always convert.
            iq = dsp.interleaved_to_complex(self.capture(16384, rxp))
            freqs, mags = dsp.spectrum(iq, rate, rx_lo)
            floor = dsp.noise_floor_db(mags)
            best = max((m for f, m in zip(freqs, mags) if abs(f - target) < window),
                       default=floor)
            return best, floor

        try:
            self.write(PHY, f"voltage{rxp}", "gain_control_mode", "manual")
            self.write(PHY, f"voltage{rxp}", "hardwaregain", 40)
            # Reference, transmitter still silent: whatever already sits at the
            # probe frequency. An open receive port picks up broadcast FM
            # strongly enough that, without this, a station at 87.8 MHz read
            # as a 25.6 dB "return" with nothing attached.
            time.sleep(0.05)
            before_db, _ = level_at_target()
            self.write(PHY, TX_LO, "powerdown", 0, output=True)
            self.write(PHY, TX_LO, "frequency", int(rx_lo), output=True)
            self.transmit_samples(values, cyclic=True, channel=str(channel_pair))
            # AFTER the stream starts: on this firmware starting a buffer
            # restores a cached attenuation, so a gain written before it can be
            # overwritten. Read it back rather than trust it.
            self.set_tx_gain(-PROBE_ATTEN_DB, channel=str(channel_pair))
            applied = self.read(PHY, f"voltage{channel_pair}", "hardwaregain",
                                output=True).split()[0]
            if abs(float(applied) + PROBE_ATTEN_DB) > 0.5:
                raise RuntimeError(
                    f"probe attenuation did not take: asked -{PROBE_ATTEN_DB} dB, "
                    f"chip reports {applied} dB - refusing to probe louder than "
                    f"intended.")
            time.sleep(0.15)
            after_db, floor = level_at_target()
            # Only the power the probe ADDED counts: subtract the reference in
            # linear power, so a station under the tone cannot pass for it.
            added = 10 ** (after_db / 10) - 10 ** (before_db / 10)
            if added <= 10 ** (floor / 10):
                return 0.0
            return 10 * math.log10(added) - floor
        finally:
            try:
                self.tx_disable()
                for ch, val in zip(("voltage0", "voltage1"), before):
                    self.write(PHY, ch, "hardwaregain", val, output=True)
                self.write(PHY, TX_LO, "frequency", tx_lo_before, output=True)
                self.write(PHY, TX_LO, "powerdown", tx_pd_before, output=True)
                self.write(PHY, f"voltage{rxp}", "gain_control_mode", rx_mode)
                if rx_mode == "manual":
                    self.write(PHY, f"voltage{rxp}", "hardwaregain", rx_gain)
            except Exception:
                pass

    def stop_buffer(self) -> None:
        """Close any running sample buffer and mark it inactive."""
        did = self.device_id(TX)
        self._retry(lambda c: (c.close_buffer(did), None)[1])
        if self.tx_state.get("kind") != "dds":
            self.tx_state = {"active": False}

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
            try:
                info["sample_gpio_enabled"] = (
                    self.read_dev(TX, "tx_sample_gpio_en").strip() not in ("0", ""))
            except Exception:
                pass          # firmware without the feature simply omits the row
        except Exception as exc:
            info["error"] = errors.describe(exc)
        return info

    def sample_gpio_pattern(self, divider: int, frame_every: int | None,
                            nsamples: int) -> dict:
        """Stream a cyclic buffer that makes the header pins tick.

        The pins carry whatever is in the low nibble of the samples, so a clock
        is not a mode you select - it is a pattern you author. Bit 0 toggles
        every `divider` samples; bit 1, if asked, pulses one sample every
        `frame_every`. The DAC never sees any of it: the top 12 bits are zero
        throughout, so this runs with the transmitter at maximum attenuation
        and nothing meaningful leaves the port.
        """
        if divider < 1:
            raise ValueError("divider must be at least 1 sample")
        if nsamples % (2 * divider):
            raise ValueError(
                f"buffer of {nsamples} samples is not a whole number of "
                f"{2 * divider}-sample cycles, so the pattern would glitch "
                f"where the cyclic buffer wraps. Pick a multiple.")
        if frame_every is not None:
            if frame_every < 1:
                raise ValueError("frame_every must be at least 1 sample")
            if nsamples % frame_every:
                raise ValueError(
                    f"buffer of {nsamples} samples is not a whole number of "
                    f"{frame_every}-sample frames; the marker would jitter at "
                    f"the wrap.")

        values: list[int] = []
        for n in range(nsamples):
            nib = 1 if (n // divider) % 2 == 0 else 0
            if frame_every is not None and n % frame_every == 0:
                nib |= 2
            values += [nib, 0]          # I carries the nibble, Q stays zero

        self.set_tx_gain(TX_ATTEN_MAX_DB, channel="0")
        self.sample_gpio(True)
        self.transmit_samples(values, cyclic=True, channel="0")
        # Again, AFTER the stream starts: on devkit firmware starting a buffer
        # restores a cached attenuation, which can be anything a previous tool
        # left. The DAC data is zero, but the LO is up while a buffer runs, and
        # LO leakage scales with attenuation - so pin it and read it back.
        self.set_tx_gain(TX_ATTEN_MAX_DB, channel="0")
        applied = self.read(PHY, "voltage0", "hardwaregain", output=True).split()[0]
        if abs(float(applied) - TX_ATTEN_MAX_DB) > 0.5:
            self.tx_disable()
            raise RuntimeError(
                f"could not hold TX0 at maximum attenuation (chip reports "
                f"{applied} dB) - stopped rather than run the pattern louder than "
                f"intended.")

        rate = self.read_int(PHY, "voltage0", "sampling_frequency", output=True)
        return {
            "sample_rate_hz": rate,
            "clock_hz": rate / (2.0 * divider),
            "frame_hz": (rate / frame_every) if frame_every else None,
            "divider": divider,
            "frame_every": frame_every,
            "buffer_samples": nsamples,
            "tx_attenuation_db": TX_ATTEN_MAX_DB,
        }

    def set_tx_gain(self, db: float, channel: str = "both") -> float:
        """Set TX attenuation on the selected channel(s); returns what the chip took.

        Only the ports asked for: raising TX2's gain because the caller wanted
        TX1 puts full LO leakage out of a port the caller was told is idle.

        Necessary, not optional. With the TX-mute firmware the chip sits at
        maximum attenuation until something asks to transmit, so a transmit
        tool that never sets a gain emits nothing at all.
        """
        if not (TX_ATTEN_MAX_DB <= db <= 0.0):
            raise ValueError(
                f"TX gain must be between {TX_ATTEN_MAX_DB} and 0 dB (0 = full "
                f"output). Got {db}.")
        self.touched_tx = True
        ports = {"0": ("voltage0",), "1": ("voltage1",)}.get(str(channel),
                                                             ("voltage0", "voltage1"))
        for ch in ports:
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

    def transmit_samples(self, values: list[int], cyclic: bool,
                         channel: str = "both") -> int:
        did = self.device_id(TX)
        # A cyclic buffer left running makes the next OPEN fail with EBUSY, so
        # replace rather than refuse: transmitting again is a perfectly
        # reasonable thing to ask for, and the error was unhelpful.
        self._retry(lambda c: (c.close_buffer(did), None)[1])
        dev = self.devices()[TX]
        total = len(dev.scan_channels())
        chans = [c for c in tx_channels(channel) if c < total]
        if len(chans) < 2:
            raise ValueError(
                f"channel {channel!r} needs scan channels this device does not "
                f"have (it has {total}).")
        mask = mask_for(chans, total)
        # "both" duplicates each I/Q pair, so the same waveform leaves both
        # ports. The DMA interleaves in scan-channel order, so the sample
        # stream has to be widened to match the mask.
        if len(chans) == 4:
            it = iter(values)
            values = [v for i, q in zip(it, it) for v in (i, q, i, q)]
        self.touched_tx = True
        written = self._retry(
            lambda c: c.write_samples(did, values, mask,
                                      nchannels=len(chans), cyclic=cyclic))
        self.tx_state = {
            "active": True, "cyclic": cyclic,
            "samples": len(values) // 2, "started_at": time.time(),
        }
        if not cyclic:
            # A one-shot buffer is torn down by CLOSE. Closing straight after
            # WRITEBUF truncated the waveform - it had barely started - so wait
            # for it to play out first. Bounded, so a huge buffer at a low rate
            # cannot hold the tool for minutes.
            try:
                rate = self.read_int(PHY, "voltage0", "sampling_frequency", output=True)
                time.sleep(min(len(values) / max(len(chans), 2) / rate + 0.05, 10.0))
            except Exception:
                time.sleep(0.5)
            self._retry(lambda c: (c.close_buffer(did), None)[1])
            self.tx_state["active"] = False
        return written
