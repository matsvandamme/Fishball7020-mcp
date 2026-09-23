#!/usr/bin/env python3
"""Unit tests for the parts of this server that need no radio.

    python evaluation/test_units.py

The smoke test checks the protocol and the transmit gate against a running
server; this checks the arithmetic and the parsing underneath, which is where
the bugs have actually been. Several of these assert a specific defect does not
come back:

  * mask_for's zero-padding - a mask of the wrong width is rejected by IIOD
    with a bare -22 EINVAL and no hint as to why.
  * spectrum's window correction - without it a full-scale tone reads about
    6 dB low, and every dBFS figure this server reports is wrong by that much.
  * _to_dac's range - transmit full scale is the FULL 16-bit range, not the
    12 bits the receive side uses, and confusing the two costs 24 dB.
  * sample_gpio_pattern's validation - a buffer that is not a whole number of
    cycles produces a clock that stutters once per wrap, which looks like a
    hardware fault and is not.

Standard library only, and it runs with or without numpy, because dsp.py has
two code paths and CI covers both.
"""

from __future__ import annotations

import array
import math
import os
import json
import pathlib
import struct
import sys
import tempfile
import unittest
import wave
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fishball_sdr_mcp import dsp, radio as radio_mod, server, sigmf          # noqa: E402
from fishball_sdr_mcp.iiod import DEFAULT_PORT, mask_for              # noqa: E402


class TestMaskFor(unittest.TestCase):
    """The IIOD scan mask: 8 hex characters per 32 channels, zero-padded."""

    def test_known_masks(self):
        self.assertEqual(mask_for([0, 1], 4), "00000003")
        self.assertEqual(mask_for([0], 1), "00000001")
        self.assertEqual(mask_for([0, 1, 2, 3], 4), "0000000f")
        self.assertEqual(mask_for([2, 3], 4), "0000000c")

    def test_width_is_padded_per_32_channels(self):
        # "3" and "0000000000000003" both fail with -22 EINVAL on a 4-channel
        # device; only the 8-character form works.
        self.assertEqual(len(mask_for([0], 4)), 8)
        self.assertEqual(len(mask_for([0], 32)), 8)
        self.assertEqual(len(mask_for([0], 33)), 16)

    def test_out_of_range_is_refused(self):
        with self.assertRaises(ValueError):
            mask_for([4], 4)
        with self.assertRaises(ValueError):
            mask_for([-1], 4)


class TestTxChannels(unittest.TestCase):
    def test_pairs(self):
        self.assertEqual(radio_mod.tx_channels("0"), [0, 1])       # TX1 I,Q
        self.assertEqual(radio_mod.tx_channels("1"), [2, 3])       # TX2 I,Q
        self.assertEqual(radio_mod.tx_channels("both"), [0, 1, 2, 3])

    def test_both_is_the_union_of_the_two(self):
        self.assertEqual(radio_mod.tx_channels("both"),
                         radio_mod.tx_channels("0") + radio_mod.tx_channels("1"))


class TestTxGate(unittest.TestCase):
    """Opt-OUT: permitted unless explicitly switched off."""

    def test_default_permits(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(radio_mod.tx_allowed())

    def test_off_values_forbid(self):
        for value in ("0", "false", "no", "off", "OFF", " No "):
            with mock.patch.dict(os.environ, {"SDR_MCP_ALLOW_TX": value}):
                self.assertFalse(radio_mod.tx_allowed(), value)

    def test_anything_else_permits(self):
        for value in ("1", "yes", "true", ""):
            with mock.patch.dict(os.environ, {"SDR_MCP_ALLOW_TX": value}):
                self.assertTrue(radio_mod.tx_allowed(), value)


class TestTxBands(unittest.TestCase):
    def test_unset_is_unrestricted(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(radio_mod.tx_bands(), [])

    def test_parses_mhz_ranges(self):
        with mock.patch.dict(os.environ, {"SDR_MCP_TX_BANDS": "2400-2483.5,868-868.6"}):
            self.assertEqual(radio_mod.tx_bands(),
                             [(2400e6, 2483.5e6), (868e6, 868.6e6)])

    def test_whitespace_and_empties_tolerated(self):
        with mock.patch.dict(os.environ, {"SDR_MCP_TX_BANDS": " 2400-2483.5 , , "}):
            self.assertEqual(radio_mod.tx_bands(), [(2400e6, 2483.5e6)])

    def test_malformed_entries_are_skipped_not_fatal(self):
        with mock.patch.dict(os.environ, {"SDR_MCP_TX_BANDS": "rubbish,2400-2483.5"}):
            self.assertEqual(radio_mod.tx_bands(), [(2400e6, 2483.5e6)])

    def test_check_tx_frequency_enforces_them(self):
        r = radio_mod.Radio(uri="ip:127.0.0.1")          # no connection is made
        with mock.patch.dict(os.environ, {"SDR_MCP_TX_BANDS": "2400-2483.5"}):
            r.check_tx_frequency(2_450_000_000)          # in band: no exception
            with self.assertRaises(ValueError):
                r.check_tx_frequency(100_000_000)
        with mock.patch.dict(os.environ, {}, clear=True):
            r.check_tx_frequency(100_000_000)            # unrestricted


class TestRfidField(unittest.TestCase):
    """The bib reader's own web server answers this one, not the radio: the
    reader owns the board while it works, and two things driving one board
    is how you get a reader that stops reading."""

    def test_says_what_to_do_when_no_reader_is_running(self):
        out = server.sdr_rfid_field(url="http://127.0.0.1:1", response_format="markdown")
        self.assertIn("no reader answered", out)
        self.assertIn("host/gui.py", out)

    def test_renders_the_field(self):
        import http.server
        import json
        import threading
        state = {"mode": "live", "reads_per_s": 128.0,
                 "counts": {"epc_ok": 500, "epc_bad": 0, "queries": 500, "acks": 500},
                 "signal": {"q": 2, "verdict": {"level": "good", "says": "Reading 2 bibs cleanly."}},
                 "bibs": {"A": {"epc": "20B8" + "0" * 28, "reads_seen": 300,
                                "dbm": -55.0, "tid": "E280689420004027"}}}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(state).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            out = server.sdr_rfid_field(url=f"http://127.0.0.1:{srv.server_port}")
        finally:
            srv.shutdown()
        self.assertIn("Reading 2 bibs cleanly.", out)
        self.assertIn("20B8", out)
        self.assertIn("-55.0 dBm", out)
        self.assertIn("4", out)               # four slots a round


class TestHostFromUri(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(radio_mod._host_from_uri("ip:192.168.2.1"),
                         ("192.168.2.1", DEFAULT_PORT))
        self.assertEqual(radio_mod._host_from_uri("192.168.2.1"),
                         ("192.168.2.1", DEFAULT_PORT))
        self.assertEqual(radio_mod._host_from_uri("ip:pluto.local:1234"),
                         ("pluto.local", 1234))


class TestInterleaved(unittest.TestCase):
    def test_pairs(self):
        self.assertEqual(dsp.interleaved_to_complex([1, 2, 3, 4]),
                         [complex(1, 2), complex(3, 4)])

    def test_odd_trailing_value_is_dropped_not_fatal(self):
        self.assertEqual(dsp.interleaved_to_complex([1, 2, 3]), [complex(1, 2)])

    def test_empty(self):
        self.assertEqual(dsp.interleaved_to_complex([]), [])


class TestSpectrum(unittest.TestCase):
    """A tone of known amplitude and frequency must read back as itself."""

    RATE = 1_000_000.0
    CENTER = 100_000_000.0
    N = 1024
    FULL_SCALE = 2047.0          # the AD9361 is 12 bits on receive

    def _tone(self, bin_index: int, amplitude: float):
        return [amplitude * complex(math.cos(2 * math.pi * bin_index * i / self.N),
                                    math.sin(2 * math.pi * bin_index * i / self.N))
                for i in range(self.N)]

    def test_full_scale_tone_reads_0_dbfs(self):
        # This is the window's coherent-gain correction. Without it a Hann
        # window loses 6.02 dB and every level this server reports is wrong.
        freqs, mags = dsp.spectrum(self._tone(64, self.FULL_SCALE),
                                   self.RATE, self.CENTER)
        self.assertAlmostEqual(max(mags), 0.0, delta=0.5)

    def test_tone_lands_on_the_right_frequency(self):
        freqs, mags = dsp.spectrum(self._tone(64, self.FULL_SCALE),
                                   self.RATE, self.CENTER)
        peak = freqs[mags.index(max(mags))]
        self.assertAlmostEqual(peak, self.CENTER + 64 * self.RATE / self.N,
                               delta=self.RATE / self.N)

    def test_negative_offset_lands_below_centre(self):
        freqs, mags = dsp.spectrum(self._tone(self.N - 64, self.FULL_SCALE),
                                   self.RATE, self.CENTER)
        peak = freqs[mags.index(max(mags))]
        self.assertAlmostEqual(peak, self.CENTER - 64 * self.RATE / self.N,
                               delta=self.RATE / self.N)

    def test_half_scale_tone_is_6_db_down(self):
        _, mags = dsp.spectrum(self._tone(64, self.FULL_SCALE / 2),
                               self.RATE, self.CENTER)
        self.assertAlmostEqual(max(mags), -6.02, delta=0.5)

    def test_output_is_sorted_by_frequency(self):
        freqs, _ = dsp.spectrum(self._tone(64, self.FULL_SCALE),
                                self.RATE, self.CENTER)
        self.assertEqual(freqs, sorted(freqs))

    def test_non_power_of_two_input_uses_the_largest_power_of_two(self):
        freqs, mags = dsp.spectrum(self._tone(64, self.FULL_SCALE)[:1000],
                                   self.RATE, self.CENTER)
        self.assertEqual(len(freqs), 512)
        self.assertEqual(len(mags), 512)

    @unittest.skipUnless(dsp.HAVE_NUMPY, "numpy not installed")
    def test_numpy_and_pure_python_paths_agree(self):
        """The two transforms must agree wherever a real signal could be.

        Only down to a floor, deliberately. Bins in the numerical dust - this
        tone's skirts reach -210 dBFS - differ between the two FFTs by float64
        rounding amplified by the log, and comparing those is testing the
        arithmetic of the C library rather than anything about this server. A
        real capture's noise floor is around -95 dBFS, so -150 is already far
        below anything that is ever reported.
        """
        samples = self._tone(64, self.FULL_SCALE)
        f_np, m_np = dsp.spectrum(samples, self.RATE, self.CENTER)
        with mock.patch.object(dsp, "HAVE_NUMPY", False):
            f_py, m_py = dsp.spectrum(samples, self.RATE, self.CENTER)

        self.assertEqual(len(f_np), len(f_py))
        for a, b in zip(f_np, f_py):
            self.assertAlmostEqual(a, b, delta=1e-3)

        compared = 0
        for a, b in zip(m_np, m_py):
            if max(a, b) < -150.0:
                continue
            self.assertAlmostEqual(a, b, delta=1e-6)
            compared += 1
        self.assertGreater(compared, 0, "no bins were above the comparison floor")

        # And the thing that actually matters: same peak, same place.
        self.assertAlmostEqual(max(m_np), max(m_py), delta=1e-9)
        self.assertEqual(f_np[m_np.index(max(m_np))], f_py[m_py.index(max(m_py))])


class TestNoiseFloor(unittest.TestCase):
    def test_median_odd(self):
        self.assertEqual(dsp.noise_floor_db([-90.0, -10.0, -80.0]), -80.0)

    def test_median_even(self):
        self.assertEqual(dsp.noise_floor_db([-90.0, -80.0, -70.0, -60.0]), -75.0)

    def test_one_loud_signal_does_not_move_it(self):
        floor = dsp.noise_floor_db([-90.0] * 100 + [0.0])
        self.assertAlmostEqual(floor, -90.0)

    def test_empty_is_nan_not_a_crash(self):
        self.assertTrue(math.isnan(dsp.noise_floor_db([])))


class TestFindPeaks(unittest.TestCase):
    def test_strongest_first(self):
        freqs = [0.0, 1e6, 2e6, 3e6]
        mags = [-50.0, -10.0, -30.0, -20.0]
        peaks = dsp.find_peaks(freqs, mags, limit=2, min_separation_hz=1.0)
        self.assertEqual([f for f, _ in peaks], [1e6, 3e6])

    def test_separation_thins_a_single_wide_signal(self):
        # Without thinning, one strong carrier fills the whole list with its
        # own skirts and nothing else is ever reported.
        # One carrier at 50 kHz with Gaussian skirts, spanning 100 kHz total.
        # A separation wider than the whole span must leave exactly one peak.
        freqs = [i * 1e3 for i in range(100)]
        mags = [-100.0 + 50.0 * math.exp(-((i - 50) ** 2) / 8.0) for i in range(100)]
        self.assertEqual(len(dsp.find_peaks(freqs, mags, limit=5,
                                            min_separation_hz=200e3)), 1)
        # ...and the one it keeps is the carrier itself, not a skirt bin.
        self.assertAlmostEqual(dsp.find_peaks(freqs, mags, limit=5,
                                              min_separation_hz=200e3)[0][0], 50e3)

    def test_limit_is_respected(self):
        freqs = [i * 1e6 for i in range(20)]
        mags = [-float(i) for i in range(20)]
        self.assertEqual(len(dsp.find_peaks(freqs, mags, limit=3,
                                            min_separation_hz=1.0)), 3)


class TestIqStatistics(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(dsp.iq_statistics([]), {"samples": 0})

    def test_full_scale_tone(self):
        n = 1024
        s = [2047.0 * complex(math.cos(2 * math.pi * 8 * i / n),
                              math.sin(2 * math.pi * 8 * i / n)) for i in range(n)]
        st = dsp.iq_statistics(s)
        self.assertEqual(st["samples"], n)
        self.assertAlmostEqual(st["rms"], 2047.0, delta=1.0)     # constant envelope
        self.assertAlmostEqual(st["rms_dbfs"], 0.0, delta=0.1)
        self.assertAlmostEqual(st["dc_offset_i"], 0.0, delta=0.1)

    def test_dc_offset_is_reported_and_excluded_from_rms(self):
        st = dsp.iq_statistics([complex(100.0, -50.0)] * 64)
        self.assertAlmostEqual(st["dc_offset_i"], 100.0, delta=0.01)
        self.assertAlmostEqual(st["dc_offset_q"], -50.0, delta=0.01)
        self.assertAlmostEqual(st["rms"], 0.0, delta=0.01)       # pure DC, no AC power

    def test_clipping_is_counted(self):
        st = dsp.iq_statistics([complex(2047.0, 0.0)] * 10 + [complex(1.0, 1.0)] * 10)
        self.assertEqual(st["clipped_samples"], 10)


class TestAsciiSpectrum(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(dsp.ascii_spectrum([], []), "(no data)")

    def test_output_is_bounded(self):
        freqs = [i * 1e3 for i in range(4096)]
        mags = [-80.0 + (i % 17) for i in range(4096)]
        out = dsp.ascii_spectrum(freqs, mags, width=64, height=12)
        lines = out.splitlines()
        self.assertLessEqual(len(lines), 16)
        self.assertTrue(all(len(line) < 200 for line in lines))


class TestToDac(unittest.TestCase):
    """Transmit full scale is the FULL 16-bit range, not the receive side's 12."""

    def test_scales_to_16_bit_full_scale(self):
        iq = [complex(1.0, 0.0), complex(-1.0, 0.0), complex(0.0, 1.0)]
        values, info = _to_dac(iq, 1.0)
        self.assertEqual(max(values), int(server.DAC_FULL_SCALE))
        self.assertEqual(info["clipped_values"], 0)

    def test_scale_halves_the_amplitude(self):
        iq = [complex(1.0, 0.0)]
        values, _ = _to_dac(iq, 0.5)
        self.assertAlmostEqual(max(values), server.DAC_FULL_SCALE / 2, delta=1)

    def test_everything_stays_in_range(self):
        iq = [complex(math.cos(i), math.sin(i)) for i in range(256)]
        values, _ = _to_dac(iq, 1.0)
        self.assertTrue(all(server.DAC_MIN <= v <= server.DAC_FULL_SCALE
                            for v in values))

    def test_all_zeros_is_refused_with_a_reason(self):
        with self.assertRaises(ValueError):
            _to_dac([complex(0, 0)] * 8, 1.0)


class TestLoadIq(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_int16_round_trip(self):
        p = self.path / "c.iq16"
        p.write_bytes(struct.pack("<4h", 1, 2, 3, 4))
        self.assertEqual(_load_iq(p, "int16"), [complex(1, 2), complex(3, 4)])

    def test_auto_detects_by_suffix(self):
        p = self.path / "c.iq16"
        p.write_bytes(struct.pack("<4h", 1, 2, 3, 4))
        self.assertEqual(_load_iq(p, "auto"), [complex(1, 2), complex(3, 4)])

    def test_complex64_round_trip(self):
        p = self.path / "c.cf32"
        a = array.array("f", [1.5, -2.5, 3.5, -4.5])
        if sys.byteorder != "little":
            a.byteswap()
        p.write_bytes(a.tobytes())
        self.assertEqual(_load_iq(p, "auto"), [complex(1.5, -2.5), complex(3.5, -4.5)])

    def test_wav_round_trip(self):
        p = self.path / "c.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(struct.pack("<4h", 10, 20, 30, 40))
        self.assertEqual(_load_iq(p, "auto"), [complex(10, 20), complex(30, 40)])

    def test_mono_wav_is_refused_with_a_reason(self):
        p = self.path / "mono.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(struct.pack("<2h", 1, 2))
        with self.assertRaises(ValueError):
            _load_iq(p, "auto")

    def test_unknown_format_is_refused(self):
        p = self.path / "c.iq16"
        p.write_bytes(b"\x00\x00")
        with self.assertRaises(ValueError):
            _load_iq(p, "nonsense")

    def test_odd_trailing_bytes_do_not_crash(self):
        p = self.path / "odd.iq16"
        p.write_bytes(struct.pack("<4h", 1, 2, 3, 4) + b"\x07")
        self.assertEqual(_load_iq(p, "int16"), [complex(1, 2), complex(3, 4)])


class TestSigmfSidecar(unittest.TestCase):
    """The sidecar is the only place a capture's settings survive the session."""

    class FakeRadio:
        """Answers what the board would, and refuses one attribute on purpose."""
        def delivered_rate(self): return 3_000_000
        def converter_rate(self): return 24_000_000
        def rx_lo(self): return 900_000_000
        def read(self, dev, chan, attr, output=False):
            return {"hardwaregain": "71.000000 dB", "gain_control_mode": "slow_attack",
                    "rf_bandwidth": "18000000", "rssi": "120.75 dB"}[attr]

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_meta_path_pairs_with_data(self):
        self.assertEqual(sigmf.meta_path(pathlib.Path("a/b.sigmf-data")).name,
                         "b.sigmf-meta")

    def test_meta_path_handles_a_legacy_iq16(self):
        self.assertEqual(sigmf.meta_path(pathlib.Path("a/b.iq16")).name, "b.sigmf-meta")

    def test_describe_reads_the_board_not_the_request(self):
        g = sigmf.describe(self.FakeRadio(), 0, 1024)["global"]
        self.assertEqual(g["core:datatype"], "ci16_le")
        self.assertEqual(g["core:sample_rate"], 3_000_000)
        self.assertEqual(g["fishball:hardwaregain_db"], 71.0)
        self.assertEqual(g["fishball:rssi_db_below_fs"], 120.75)
        # delivered != converter, so the fabric decimator must be reported engaged
        self.assertEqual(g["fishball:fabric_decimator"], "engaged")

    def test_full_scale_is_the_twelve_bit_one(self):
        g = sigmf.describe(self.FakeRadio(), 0, 1024)["global"]
        self.assertEqual(g["fishball:full_scale"], 2047)
        self.assertIn("NOT 32768", g["fishball:scaling_note"])

    def test_channel_pair_one_is_rx2(self):
        g = sigmf.describe(self.FakeRadio(), 1, 1024)["global"]
        self.assertTrue(g["core:hw"].endswith("RX2"))

    def test_statistics_are_carried_with_dsp_key_names(self):
        stats = dsp.iq_statistics([complex(2047, 0), complex(-3, 4)])
        g = sigmf.describe(self.FakeRadio(), 0, 2, stats)["global"]
        self.assertEqual(g["fishball:clipped_samples"], 1)
        self.assertIn("clipping_warning", " ".join(g))

    def test_an_unreadable_attribute_is_absent_not_wrong(self):
        class Mute(self.FakeRadio):
            def read(self, *a, **k): raise RuntimeError("no")
        g = sigmf.describe(Mute(), 0, 1024)["global"]
        self.assertNotIn("fishball:hardwaregain_db", g)
        self.assertEqual(g["core:sample_rate"], 3_000_000)   # the rest still lands

    def test_written_sidecar_is_valid_json_beside_the_data(self):
        data = self.path / "c.sigmf-data"
        data.write_bytes(b"\x00" * 8)
        meta = sigmf.write(data, sigmf.describe(self.FakeRadio(), 0, 2))
        self.assertEqual(meta.parent, data.parent)
        self.assertEqual(json.loads(meta.read_text())["global"]["core:version"], "1.0.0")


class TestPruneTakesTheSidecar(unittest.TestCase):
    """Pruning the data and leaving the metadata describes a file that is gone."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.prev = os.environ.get("SDR_MCP_CAPTURE_DIR")
        os.environ["SDR_MCP_CAPTURE_DIR"] = self.dir.name

    def tearDown(self):
        if self.prev is None:
            os.environ.pop("SDR_MCP_CAPTURE_DIR", None)
        else:
            os.environ["SDR_MCP_CAPTURE_DIR"] = self.prev
        self.dir.cleanup()

    def test_sidecars_go_with_their_data(self):
        d = pathlib.Path(self.dir.name)
        for i in range(3):
            (d / f"c{i}.sigmf-data").write_bytes(b"\x00" * 8)
            (d / f"c{i}.sigmf-meta").write_text("{}")
            os.utime(d / f"c{i}.sigmf-data", (1000 + i, 1000 + i))
        self.assertEqual(server.prune_captures(keep=1), 2)
        left = sorted(p.name for p in d.iterdir())
        self.assertEqual(left, ["c2.sigmf-data", "c2.sigmf-meta"])


class TestSampleGpioPatternValidation(unittest.TestCase):
    """Validation runs before any I/O, so it is testable without a board.

    A cyclic buffer that is not a whole number of cycles produces a clock that
    stutters once per wrap - a fault that looks like hardware and is not.
    """

    def setUp(self):
        self.r = radio_mod.Radio(uri="ip:127.0.0.1")     # never connects

    def test_divider_must_be_at_least_one(self):
        with self.assertRaises(ValueError):
            self.r.sample_gpio_pattern(0, None, 8192)

    def test_buffer_must_hold_whole_cycles(self):
        with self.assertRaises(ValueError) as cm:
            self.r.sample_gpio_pattern(3, None, 8192)     # 8192 % 6 != 0
        self.assertIn("glitch", str(cm.exception))

    def test_buffer_must_hold_whole_frames(self):
        with self.assertRaises(ValueError):
            self.r.sample_gpio_pattern(2, 3000, 8192)     # 8192 % 3000 != 0

    def test_frame_every_must_be_positive(self):
        with self.assertRaises(ValueError):
            self.r.sample_gpio_pattern(2, 0, 8192)


class TestDescribeErrors(unittest.TestCase):
    """A file problem must not be reported as "cannot reach the radio"."""

    def test_file_errors_say_file(self):
        from fishball_sdr_mcp import errors
        for exc in (FileNotFoundError("no such file"), PermissionError("denied"),
                    IsADirectoryError("is a dir")):
            self.assertIn("File problem", errors.describe(exc))

    def test_network_errors_say_unreachable(self):
        from fishball_sdr_mcp import errors
        self.assertIn("Cannot reach the radio",
                      errors.describe(ConnectionRefusedError("refused")))

    def test_value_errors_are_invalid_requests(self):
        from fishball_sdr_mcp import errors
        self.assertIn("Invalid request", errors.describe(ValueError("bad")))


# _to_dac and _load_iq are module-private helpers in server.py; bind them once
# here rather than reaching through the module in every test.
_to_dac = server._to_dac
_load_iq = server._load_iq


if __name__ == "__main__":
    unittest.main(verbosity=2)
