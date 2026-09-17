"""Turn low-level failures into messages that say what to do next.

IIOD reports failures as bare negative errnos. `-22` on its own tells an agent
nothing actionable, so each one is mapped to the specific recovery step for
this board.
"""

from __future__ import annotations

import errno
import socket

from .iiod import IiodError

# Filled in by radio.py so connection errors can name the URI actually in use.
_URI_HINT = "ip:192.168.2.1"


def set_uri_hint(uri: str) -> None:
    global _URI_HINT
    _URI_HINT = uri


_ERRNO_ADVICE = {
    errno.EINVAL: (
        "the value was rejected as out of range or malformed. Read the matching "
        "'*_available' attribute (e.g. sampling_frequency_available) for the legal "
        "range before retrying."),
    errno.ENODEV: (
        "no such IIO device. Run sdr_list_devices to see what this firmware "
        "actually exposes - device names differ between firmware builds."),
    errno.ENOENT: (
        "no such channel or attribute. Run sdr_list_devices to see the exact "
        "names; they are case-sensitive and channel ids look like 'voltage0'."),
    errno.EBUSY: (
        "the device is busy - something else holds its buffer. Another capture "
        "or a running GNU Radio flowgraph is the usual cause."),
    errno.EBADF: (
        "the buffer is not open. Capture tools open and close it themselves, so "
        "this usually means a previous capture died partway."),
    errno.EAGAIN: (
        "the device had no data ready in time. Try fewer samples, or check that "
        "the sample rate is set to something the link can sustain."),
    errno.EPERM: "the operation was refused by the device.",
    errno.ETIMEDOUT: (
        "the board did not finish the transfer in time. For a capture this "
        "usually means too many samples in one go - the client library chunks "
        "long captures, so a bare ETIMEDOUT here points at a slow or "
        "congested link rather than the request size."),
    errno.ENOMEM: (
        "the board could not allocate a buffer that large. Ask for fewer "
        "samples."),
}


def describe(exc: BaseException) -> str:
    """A single actionable sentence for any failure this server can raise."""
    if isinstance(exc, IiodError):
        name = errno.errorcode.get(exc.code, str(exc.code))
        advice = _ERRNO_ADVICE.get(exc.code, "the device rejected the request.")
        return f"Radio rejected '{exc.command}' ({name}): {advice}"

    # Only NETWORK failures are "cannot reach the radio". A missing capture
    # directory or an unreadable IQ file is also an OSError, and telling the
    # caller to check the USB cable for those sends them the wrong way.
    if isinstance(exc, (FileNotFoundError, IsADirectoryError, NotADirectoryError,
                        PermissionError)):
        return f"File problem: {exc}"
    if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout, socket.gaierror,
                        OSError)) and not isinstance(exc, IiodError):
        return (
            f"Cannot reach the radio at {_URI_HINT}. Check the board is powered and "
            f"enumerated, that the USB Ethernet interface is up, and that "
            f"'ping 192.168.2.1' answers. Set SDR_MCP_URI to use a different address. "
            f"({type(exc).__name__}: {exc})")

    if isinstance(exc, ValueError):
        return f"Invalid request: {exc}"

    return f"{type(exc).__name__}: {exc}"


class TxRefused(RuntimeError):
    """Raised when a transmit tool is called without the gate enabled."""


def tx_gate_message(what: str) -> str:
    return (
        f"Refusing to {what}: transmitting has been turned off for this server. "
        f"SDR_MCP_ALLOW_TX is set to 0 in its environment.\n\n"
        f"To allow it, remove that variable or set it to 1, and restart the "
        f"server. Transmitting is permitted by default; it is off here because "
        f"somebody chose to close it.\n\n"
        f"sdr_tx_disable and sdr_tx_status remain available regardless.")
