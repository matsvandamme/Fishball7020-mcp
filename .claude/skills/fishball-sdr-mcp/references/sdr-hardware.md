# What the radio does that surprises people

Enough to write correct tools against. The companion repository
`fishball7020-fpga-devkit` carries the full reference; this is the subset a
server author keeps needing, with the figures measured on real hardware.

## The receive port is the fragile end, and this board has a PA

The AD9361's RX input is rated to about **+2.5 dBm**. This board ships in a
variant carrying a Mini-Circuits **PGA-102+** — 17.7 dB of gain at 50 MHz
falling to 10.4 dB at 6 GHz, P1dB +17.5 dBm — and measures **+19 dBm** flat out,
consistent to 0.7 dB across six runs.

That is **16 dB above what its own receiver survives**. A TX→RX loopback with no
attenuator destroys the board. Fit at least 20 dB; 40–50 dB is comfortable.
Documentation and refusals should say this rather than quote the +7 dBm figure
that applies to a bare AD9361 — most Pluto advice on the internet does, and it
is wrong here by 10–18 dB.

## Receive is 12-bit, transmit is 16-bit

Received samples are 12-bit sign-extended into `int16`, so **full scale is
±2047** and levels in dBFS are referred to that. Transmit takes the **full
16-bit range**. Scaling transmit samples to ±2047, as the receive side does,
emits 24 dB low. Measured over a cable: digital amplitudes of 8191 and 32767
produced +12.7 dB and +24.8 dB relative to 2047, with no rise in distortion.

## Gain in dB is not proportional to gain

`hardwaregain` is an **index with a dB-shaped name**. The AD9361 maps one index
onto a whole chain — LNA, mixer, TIA, LPF, digital — and the nominal dB value is
a label, not the delivered gain. At several indices the LNA/mixer state changes
and the real gain steps by up to 10 dB while the label steps by 1.

Transitions sit at commanded **5, 17, 27, ~31–37, 52, and every step above 63**.
**38–51 dB is the widest transition-free window** in every band; a gain sweep
that needs to be linear has to stay inside it. Fitting a line across the whole
range gives 0.65 dB/dB for a perfectly healthy front end.

The legal range also moves with frequency, because the chip swaps gain table per
band: `[-1, 73]` below 1.3 GHz, `[-3, 71]` to 4 GHz, `[-10, 62]` above. **Read
`hardwaregain_available` after every retune** and clamp to it — writing outside
returns `-22 EINVAL`, which is how a frequency sweep silently loses points.

Crossing 4 GHz also shifts delivered gain by several dB at the same commanded
value, and by a different amount per channel (4.6 dB on one, 7.4 on the other).
A calibration made below 4 GHz is wrong above it.

## IIOD protocol gotchas

All confirmed against a live board running IIOD 0.25.

- **The channel mask is fixed-width**: exactly 8 hex characters per 32 scan
  channels. `00000003` enables channels 0 and 1; both `3` and
  `0000000000000003` fail with `-22 EINVAL` and no hint.
- **`WRITEBUF` is acknowledged twice**, before and after the payload. Skip the
  first status and the stream desyncs, samples arriving as the next response.
- **`VERSION` answers with a bare line**, not a length-prefixed payload.
- **Large transfers time out on the board, not the client.** 1,048,576 samples
  succeeds, 4,194,304 fails with `-110 ETIMEDOUT`, and raising the client
  timeout does not help. Chunk at 262,144 and loop `READBUF` on one open buffer.

## Behaviour worth knowing

**Two applications cannot hold the board at once.** Opening it in SDRangel or
similar reconfigures the composite USB device, the Ethernet gadget disappears,
and `ip:192.168.2.1` stops answering until that application closes.

**TX gain ordering no longer matters** on current devkit firmware. Older builds
restored a cached attenuation when a DMA buffer opened, overwriting a gain set
beforehand — asking for −10 dB put −60 dB on the wire. `patches/0005` in the
devkit restores the cache only if nothing has been set since the mute, so both
orders work. This server sets gain after the stream regardless, which is correct
either way.

**Something on the board may be changing your gain.** `/mnt/jffs2` is persistent
and `/mnt/jffs2/autorun.sh` runs at every boot, so a helper script there survives
reflashing and appears nowhere in the firmware source. A common one applies a
fixed gain a second or two after any stream starts, silently overriding whatever
this server set. If levels do not match what was asked for, look there first.

**Empty serials.** Firmware built before September 2026 reported an empty
`hw_serial`, and tools that identify Plutos by serial — SDRangel among them —
refuse the board. This server connects by URI and is unaffected, but
`sdr_get_status` reports the serial so the problem is visible.
