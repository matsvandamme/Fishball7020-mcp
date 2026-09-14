# How this server is put together

## Why there is no pylibiio

libiio's network backend is a line-based text protocol, and everything needed
fits in one small module. Talking it directly means the server depends on
nothing but the standard library plus the MCP SDK — no libiio version to match
against the firmware, no C extension to build, and a client that can be read in
one sitting. `iiod.py` is that module.

## One connection, reused

IIOD is a stateful TCP session. `radio.py` holds a lazily-opened connection with
reconnect-on-failure rather than reconnecting per tool call, and `_retry`
wraps the operations that can hit a dropped socket.

## Tool conventions

Every tool is `sdr_<verb>` and takes `response_format: Format = "markdown"` —
`markdown` to read, `json` to parse. Output goes through `formatting.render`,
which enforces `CHARACTER_LIMIT`.

`ToolAnnotations` are set honestly, because a client may use them to decide what
needs confirmation:

| Kind | Annotations |
|---|---|
| Reads only | `readOnlyHint=True, destructiveHint=False` |
| Changes the radio | `idempotentHint=True`, not read-only |
| Transmits | `destructiveHint=True, openWorldHint=True` |

Validate against the radio's own `*_available` attributes rather than hardcoded
ranges, so an illegal value is refused with the legal options rather than
silently ignored or rejected with a bare errno.

## The transmit gate

The gate is **opt-out**: transmitting is permitted unless `SDR_MCP_ALLOW_TX=0`
is in the **server's** environment — not the shell the user typed in, which is a
frequent confusion, so refusals name the exact variable and `sdr_tx_status`
reports what the running server actually believes. It was opt-in until the
board's owner asked for transmit available without ceremony; the reasoning for
everything else in this section is unchanged by that.

Because the gate no longer stands between an assistant and the antenna,
`sdr_check_rf_setup` exists to answer "what is connected?" first. It is
deliberately explicit that it cannot sense the transmit port — no coupler, no
detector — and reports only what it can actually establish.

`sdr_tx_disable` is never gated and also runs on shutdown, so a crashed client
cannot leave the board transmitting a cyclic buffer. `SDR_MCP_TX_BANDS` can
restrict transmission to named ranges on top of the gate.

The startup quiesce deliberately touches **attenuation only, not the TX LO**.
Powering the synthesiser down would silently break an unrelated transmitter — a
GNU Radio sink, say — that never asked this server for anything: it would stream
into a dead LO and emit nothing. Muting the attenuators is reversible by anyone
who sets a gain, so it cannot strand another process. `sdr_tx_disable` does the
full stop, because that is an explicit request.

## Errors carry the recovery step

`errors.py` maps IIOD's negative errnos to something actionable: `-19 ENODEV` →
the device is not present, is this the right firmware; `-22 EINVAL` → the value
was rejected, read the matching `*_available`; connection refused → no IIOD at
this URI, check the USB Ethernet interface is up.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SDR_MCP_URI` | `ip:192.168.2.1` | where the board is |
| `SDR_MCP_TIMEOUT` | `10` | socket timeout, seconds |
| `SDR_MCP_CAPTURE_DIR` | `~/.cache/fishball-sdr` | where `sdr_capture_iq` writes |
| `SDR_MCP_ALLOW_TX` | unset | `1` permits transmitting |
| `SDR_MCP_TX_BANDS` | unset | restrict TX, e.g. `2400-2483.5` (MHz) |
| `SDR_MCP_NO_TX_QUIESCE` | unset | leave the transmitter exactly as found |

## Testing

`evaluation/smoke_test.py` drives the server over stdio using only the standard
library: the handshake, `tools/list`, tool schemas and annotations, that the
transmit gate refuses and names its variable, that diagnostics stay off stdout,
and — with `--live` — that every read-only tool returns real data. It never
transmits and never retunes the radio.

It exists because MCP Inspector needs Node 18 and Ubuntu 22.04 ships Node 12.

`evaluation/questions.xml` holds ten question/answer pairs, each answerable
read-only, each needing more than one call or some reasoning, and each verified
against a real board. They deliberately avoid anything that depends on the RF
environment — "which FM station is strongest here" drifts with the antenna and
the weather — and turn instead on the device tree, advertised capabilities and
arithmetic over reported rates, which are properties of the firmware.

CI runs the protocol test on Python 3.10 and 3.13, with and without numpy.
