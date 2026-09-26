# What the radio does that surprises people

Enough to write correct tools against. The companion repository
`fishball7020-fpga-devkit` carries the full reference; this is the subset a
server author keeps needing, with the figures measured on real hardware.

## The receive port is the fragile end, and this board has a PA

The AD9361's RX input is rated to about **+2.5 dBm**. This board ships in a
variant carrying a Mini-Circuits **PGA-102+** — 17.7 dB of gain at 50 MHz
falling to 10.4 dB at 6 GHz, P1dB +17.5 dBm — and should be taken to reach
about **+19 dBm** flat out. That is the devkit self-test's estimate, scaled up
from a quiet measurement and capped at the PA's compression point; nobody has
metered it. Never write "+19 dBm measured".

That is **16 dB above what its own receiver survives**. A TX→RX loopback with no
attenuator destroys the board. Fit at least 20 dB. Bigger pads are equally
safe but measure worse: the board's own TX->RX leak equals a 33-60 dB pad on
channel 0 above 1 GHz, so a 50 dB loop there measures the leak as much as the
cable. Recommend exactly 20 dB for measurement.
Documentation and refusals should say this rather than quote the +7 dBm figure
that applies to a bare AD9361 — most Pluto advice on the internet does, and it
is wrong here by 10–18 dB.

## Receive is 12-bit, transmit is 16-bit

Received samples are 12-bit sign-extended into `int16`, so **full scale is
±2047** and levels in dBFS are referred to that. Transmit takes the **full
16-bit range**. Scaling transmit samples to ±2047, as the receive side does,
emits 24 dB low. Measured over a cable: digital amplitudes of 8191 and 32767
produced +12.7 dB and +24.8 dB relative to 2047, with no rise in distortion.

**The DAC is nevertheless 12 bits.** It takes the top 12 of your 16-bit word
(`dma_data[15:4]`) and discards the bottom four, which is why the full range
matters. Those four discarded bits are what `sdr_sample_gpio` puts on header
pins: they cost no analog performance because nothing downstream ever read
them. A consequence worth knowing — two transmit samples differing only in
their low nibble produce the *identical* DAC code, so a pattern authored there
is invisible in the RF.

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
- **`-16 EBUSY` on a buffer is a stale session ON THE BOARD.** A client that is
  killed rather than closed leaves its session open, holding the DMA. The board
  showed three live connections on port 30431 while the host showed none, and
  every later transmit allocation was refused indefinitely. Restarting the
  client, or the host, changes nothing; `killall iiod` on the board clears it,
  and so does a reboot. Do not read a size limit into it - a 4 MB transmit
  buffer allocates fine as a fresh process's first request while a 1 MB one is
  refused as the same process's second. An hour went into a non-existent
  "1 MB ceiling" because the large sizes were tested second.
- **`-104 ECONNRESET` while the digital loopback is engaged.** Allocating a
  large transmit buffer with `loopback` set made IIOD reset the session, and
  the reset then left the DMA allocated, producing the `-16` cascade above.
  Memory was not the cause: 963 MB free and 260 of 262 MB CMA free.

## Transmitting is latency-critical; receiving is not

Anything this server does that streams to the DAC has a deadline. Receiving
tolerates a slow link - samples pile up on the board and some are lost, which
libiio reports as an overflow. **Transmitting does not**: the converter has to
be fed in real time, so a late buffer means it runs dry, and on this firmware
**patch 0015 mutes the transmitter after 250 ms of starvation** and switches
the data source to the internal DDS. The failure mode is the nasty one - the
client still looks like it is transmitting and the receiver sees exactly zero.

Measured over a wireless host link: transmit and receive together at 4 MS/s
produced bursts of 20-40 underflows in 45 s, each beside `Unable to push
buffer: Connection timed out`, while transmit alone at the same rate and
buffer produced none. It is intermittent - the same configuration ran clean an
hour later - so it is channel contention rather than a throughput limit.

The defence is buffer **duration**, `buffer / sample_rate`, because that is the
length of stall the DAC can ride out. Lowering the rate helps twice (more
slack, less traffic); enlarging the buffer helps once. A one-shot or cyclic
buffer sidesteps the whole problem, which is why `sdr_tx_tone` and
`sdr_transmit_waveform` are safer here than a continuous stream would be.

## The internal digital loopback: exercise transmit, radiate nothing

`loopback` is a debugfs attribute on `ad9361-phy`: 0 off, 1 digital TX->RX,
2 digital RX->TX. With it at 1 the transmit samples reach the receiver inside
the chip, past the mixers and the power amplifier, so **nothing is radiated**.
That makes it the right way to check a transmit-and-receive path before making
a licensing decision on the operator's behalf. In the devkit it is
`./devkit loopback on|off`.

Three things to know before relying on it:

- It does **not** translate frequency, so a transmit LO offset and a receive
  LO offset do not cancel - they must be set equal.
- The transmit attenuator is analogue and therefore does not apply. The
  loopback level is set by the digital scale alone.
- A board left in loopback is deaf to its antennas and looks broken for no
  visible reason. It survives everything short of a reboot, so put it back.

It also proves nothing about the analogue radio. A clean result through
loopback means the DSP is right and says nothing about the mixers, the
amplifier, the baluns or the antennas.

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


## The RF-setup probe, and why it once lied

`sdr_check_rf_setup` transmits a short tone and looks for it on the receiver.
It listens at the RECEIVE LO, so the transmitter must be put there too and
switched on. An earlier version did neither: it went out on whatever TX LO the
last tool left (2.4 GHz after boot) while the receiver listened at 88 MHz
after a band scan, and its own cleanup powered the LO down for every probe
after the first. Both produced a confident "quiet - consistent with a load or
nothing attached" with a cable fitted. The probe now programs and restores the
TX LO and powerdown state, checks the band list, and reads its attenuation
back before capturing.

**It was also deaf, until 2026-09-19.** `probe_loopback` handed the raw
interleaved I/Q integers to the spectrum as if they were complex samples: the
tone landed at the wrong frequency and the probe measured noise (the "4-7 dB
with no cable" once recorded here). It now converts the capture, and takes a
reference with the transmitter silent so only power the probe ADDS counts -
an unconnected port picks up broadcast FM, which read as a 25.6 dB "return"
at 87.8 MHz.

Fixed, the probe hears the board's own TX->RX leak. Measured over 60
frequencies, 70 MHz - 6 GHz:

| | below 3 GHz | 3 - 6 GHz |
|---|---|---|
| nothing attached (channel 0 / 1) | up to 42 dB | up to 48 dB |
| 20 dB-pad loop, channel 0 | 59 - 69 dB | 47.5 - 60 dB |

The verdict threshold is the leak ceiling + 8 dB (50 / 56 dB). Above 3 GHz a
20 dB loop overlaps the bare leak, so returns in between are reported as
"uncertain". A bare cable returns ~20 dB more than a 20 dB loop and is never
ambiguous, and that is the case that destroys receivers.

**Open issue: the passive half of `sdr_check_rf_setup` can call an open port
an antenna.** Its "ambient" figure is the strongest spectral peak above the
median floor, and anything over 15 dB reads as "antenna on RX". Measured
2026-09-19 with RX2A open: at 866.3 MHz the AGC had the receiver at 73 dB,
where a spur at +8.72 MHz stands 30 dB above the floor on BOTH channels -
including channel 0, which had a padded cable on it, so it is the board's,
not the air's. At 5.56 GHz the strongest peak is DC/LO leakage at 0 Hz. Until
the check excludes DC and known internal spurs (or measures at a pinned,
lower gain), treat "antenna on RX" as "signals present", not as proof of an
antenna.
