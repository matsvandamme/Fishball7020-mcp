"""Minimal IIOD client speaking libiio's network protocol over a raw socket.

Why this exists rather than `import iio`
----------------------------------------
libiio's network backend protocol is line-based text, and everything this
server needs from it fits in one small module. Talking it directly means the
MCP server depends on nothing but the standard library plus the MCP SDK - no
pylibiio, no matching libiio version between host and board, no C extension to
build. That matches how the rest of this repo is put together (see
firmware/scripts/gen_fir_coe.py, which is stdlib-only for the same reason).

Protocol notes, all confirmed against a live board (IIOD 0.25)
--------------------------------------------------------------
Commands are CRLF-terminated. Every reply starts with a decimal line that is
either a byte count or a NEGATIVE ERRNO. Attribute payloads are NUL-terminated
and followed by a newline.

    VERSION                                  -> version string
    PRINT                                    -> the context XML
    READ  <dev> [INPUT|OUTPUT] <ch> <attr>   -> length, then value
    READ  <dev> <attr>                       -> device-level attribute
    WRITE <dev> [INPUT|OUTPUT] <ch> <attr> <len>  then value + NUL
    OPEN  <dev> <samples> <mask> [CYCLIC]
    READBUF  <dev> <bytes>                   -> length, mask echo, then data
    WRITEBUF <dev> <bytes>                   then data
    CLOSE <dev>

THE CHANNEL MASK IS FIXED-WIDTH AND THAT IS EASY TO GET WRONG. It is a hex
string of exactly 8 characters per 32 scan channels, zero-padded. A 4-channel
device therefore needs 8 characters: "00000003" enables channels 0 and 1.
Passing "3" or "0000000000000003" both fail with -22 EINVAL and no hint as to
why. `mask_for()` builds it correctly.
"""

from __future__ import annotations

import errno
import socket
import struct
from typing import Iterable, Sequence

DEFAULT_HOST = "192.168.2.1"
DEFAULT_PORT = 30431
DEFAULT_TIMEOUT = 10.0


class IiodError(OSError):
    """An IIOD command returned a negative errno."""

    def __init__(self, code: int, command: str):
        self.code = -code if code < 0 else code
        self.command = command
        super().__init__(self.code, f"{errno.errorcode.get(self.code, self.code)}: {command}")


def mask_for(channels: Iterable[int], total_channels: int) -> str:
    """Build an IIOD scan mask: 8 hex chars per 32 channels, zero-padded.

    >>> mask_for([0, 1], 4)
    '00000003'
    """
    words = max(1, (total_channels + 31) // 32)
    bits = 0
    for c in channels:
        if c < 0 or c >= total_channels:
            raise ValueError(f"channel {c} out of range for {total_channels}-channel device")
        bits |= 1 << c
    return f"{bits:0{words * 8}x}"


class Iiod:
    """One IIOD session. Not thread-safe: the protocol is a single stream."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = DEFAULT_TIMEOUT):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: socket.socket | None = None
        self._f = None

    # -- connection ---------------------------------------------------------

    def connect(self) -> None:
        if self._sock is not None:
            return
        self._sock = socket.create_connection((self.host, self.port), self.timeout)
        self._sock.settimeout(self.timeout)
        self._f = self._sock.makefile("rwb")

    def close(self) -> None:
        for obj in (self._f, self._sock):
            try:
                if obj is not None:
                    obj.close()
            except OSError:
                pass
        self._f = self._sock = None

    def __enter__(self) -> "Iiod":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- framing ------------------------------------------------------------

    def _send(self, command: str) -> None:
        self.connect()
        self._f.write((command + "\r\n").encode())
        self._f.flush()

    def _status(self, command: str) -> int:
        line = self._f.readline()
        if not line:
            raise ConnectionError(f"IIOD closed the connection during: {command}")
        n = int(line.strip())
        if n < 0:
            raise IiodError(n, command)
        return n

    def _read_exactly(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._f.read(min(65536, n - len(buf)))
            if not chunk:
                raise ConnectionError(f"IIOD sent {len(buf)} of {n} expected bytes")
            buf += chunk
        return bytes(buf)

    def _text_command(self, command: str) -> str:
        self._send(command)
        n = self._status(command)
        data = self._read_exactly(n).decode(errors="replace")
        self._f.readline()                      # trailing newline
        return data.rstrip("\x00").strip()

    # -- attributes ---------------------------------------------------------

    def version(self) -> str:
        return self._text_command("VERSION")

    def context_xml(self) -> str:
        """The full context description. Includes a trailing non-XML preamble."""
        raw = self._text_command("PRINT")
        start = raw.find("<?xml")
        end = raw.rfind("</context>")
        return raw[start:end + len("</context>")] if start >= 0 and end > start else raw

    def read_channel(self, device: str, channel: str, attr: str, output: bool = False) -> str:
        return self._text_command(
            f"READ {device} {'OUTPUT' if output else 'INPUT'} {channel} {attr}")

    def read_device(self, device: str, attr: str) -> str:
        return self._text_command(f"READ {device} {attr}")

    def write_channel(self, device: str, channel: str, attr: str, value,
                      output: bool = False) -> None:
        payload = f"{value}".encode() + b"\x00"
        command = (f"WRITE {device} {'OUTPUT' if output else 'INPUT'} "
                   f"{channel} {attr} {len(payload)}")
        self._send(command)
        self._f.write(payload)
        self._f.flush()
        self._status(command)

    def write_device(self, device: str, attr: str, value) -> None:
        payload = f"{value}".encode() + b"\x00"
        command = f"WRITE {device} {attr} {len(payload)}"
        self._send(command)
        self._f.write(payload)
        self._f.flush()
        self._status(command)

    # -- sample buffers -----------------------------------------------------

    def read_samples(self, device: str, nsamples: int, mask: str,
                     bytes_per_sample: int = 2, nchannels: int = 2) -> list[int]:
        """Capture nsamples per channel; returns a flat list of int16 values.

        Interleaving follows the scan mask in channel order, so with two
        channels enabled the result is I0, Q0, I1, Q1, ...
        """
        self._send(f"OPEN {device} {nsamples} {mask}")
        self._status(f"OPEN {device}")
        try:
            nbytes = nsamples * bytes_per_sample * nchannels
            command = f"READBUF {device} {nbytes}"
            self._send(command)
            got = self._status(command)
            self._f.readline()                  # mask echo
            data = self._read_exactly(got)
        finally:
            try:
                self._send(f"CLOSE {device}")
                self._status(f"CLOSE {device}")
            except (OSError, ValueError):
                pass                            # already reporting a real error
        return list(struct.unpack(f"<{len(data) // 2}h", data))

    def write_samples(self, device: str, values: Sequence[int], mask: str,
                      nchannels: int = 2, cyclic: bool = False) -> int:
        """Push interleaved int16 samples to a DAC buffer. Returns bytes written.

        With cyclic=True the hardware repeats the buffer until the device is
        closed, so it keeps transmitting after this call returns.
        """
        nsamples = len(values) // nchannels
        data = struct.pack(f"<{len(values)}h", *values)
        opencmd = f"OPEN {device} {nsamples} {mask}" + (" CYCLIC" if cyclic else "")
        self._send(opencmd)
        self._status(opencmd)
        command = f"WRITEBUF {device} {len(data)}"
        self._send(command)
        # IIOD acknowledges the WRITEBUF command BEFORE the payload, then
        # reports the byte count after it. Skipping this first status desyncs
        # the stream and the samples come back as the next "response line".
        self._status(command)
        self._f.write(data)
        self._f.flush()
        return self._status(command)

    def close_buffer(self, device: str) -> None:
        try:
            self._send(f"CLOSE {device}")
            self._status(f"CLOSE {device}")
        except (OSError, ValueError):
            pass
