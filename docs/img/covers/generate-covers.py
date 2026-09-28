#!/usr/bin/env python3
"""Generate GitHub cover images for the VMAT SDR repositories.

2560x1280 (2x of GitHub's 1280x640 social preview), light and dark, in the
VMAT brand: #4069FF, the four-quadrant mark, Bitter for display type.
"""
import math, random, sys, pathlib

HERE = pathlib.Path(__file__).parent
BLUE = "#4069FF"

THEME = {
    "dark":  dict(bg="#0A0E14", bg2="#111823", ink="#F2F6FA", ink2="#93A3B5",
                  rule="#1E2936", logo="logo-dark.b64", grid="#16202C"),
    "light": dict(bg="#FFFFFF", bg2="#F2F5F9", ink="#0B1220", ink2="#54677E",
                  rule="#DCE3EB", logo="logo-light.b64", grid="#E8EDF3"),
}


def spectrum(seed, w, h, peaks):
    """A plausible spectrum trace: shaped noise floor plus named peaks."""
    rnd = random.Random(seed)
    n = 460
    pts = []
    for i in range(n):
        x = i / (n - 1)
        floor = 0.13 + 0.05 * math.sin(x * 5.0) + rnd.uniform(-0.022, 0.022)
        v = floor
        for cx, amp, width in peaks:
            v = max(v, floor + amp * math.exp(-((x - cx) ** 2) / (2 * width ** 2)))
        pts.append((x * w, h - v * h))
    return " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)


def cover(concept, theme, out):
    t = THEME[theme]
    logo = (HERE / t["logo"]).read_text()
    body = CONCEPTS[concept](t)
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bitter:wght@600;700;800&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:2560px;height:1280px;overflow:hidden}}
body{{background:{t['bg']};color:{t['ink']};
  font-family:Bitter,Georgia,serif;-webkit-font-smoothing:antialiased}}
.mono{{font-family:"IBM Plex Mono",monospace}}
.wrap{{position:relative;width:2560px;height:1280px}}
</style></head><body><div class="wrap">{body}</div>
<script>document.fonts.ready.then(()=>document.body.setAttribute('data-ready','1'));</script>
</body></html>"""
    out.write_text(html)


# ---------------------------------------------------------------- concepts

def _logo_img(t, size=118):
    b64 = (HERE / t["logo"]).read_text()
    return (f'<img src="data:image/png;base64,{b64}" '
            f'style="height:{size}px;width:auto;display:block">')


def devkit_signal(t):
    """Concept A - the spectrum. What the board does, with the stack named."""
    trace = spectrum(7, 2560, 420, [(0.22, .48, .012), (0.38, .28, .010),
                                    (0.52, .62, .014), (0.67, .24, .009),
                                    (0.81, .40, .011)])
    return f"""
<svg width="2560" height="1280" style="position:absolute;inset:0">
  <defs>
    <linearGradient id="fade" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{BLUE}" stop-opacity=".30"/>
      <stop offset="1" stop-color="{BLUE}" stop-opacity="0"/>
    </linearGradient>
  </defs>
  <g opacity=".5">
    {"".join(f'<line x1="0" y1="{y}" x2="2560" y2="{y}" stroke="{t["grid"]}" stroke-width="2"/>' for y in range(760, 1281, 52))}
    {"".join(f'<line x1="{x}" y1="740" x2="{x}" y2="1280" stroke="{t["grid"]}" stroke-width="2"/>' for x in range(0, 2561, 96))}
  </g>
  <polygon points="0,420 {trace} 2560,420" transform="translate(0,700)" fill="url(#fade)"/>
  <polyline points="{trace}" transform="translate(0,700)" fill="none"
            stroke="{BLUE}" stroke-width="5" stroke-linejoin="round"/>
  <rect x="0" y="1112" width="2560" height="168" fill="{t['bg']}"/>
  <line x1="0" y1="1112" x2="2560" y2="1112" stroke="{t['rule']}" stroke-width="3"/>
</svg>
<div style="position:absolute;left:150px;top:140px;width:1750px">
  <div class="mono" style="font-size:30px;letter-spacing:.30em;text-transform:uppercase;color:{BLUE};margin-bottom:34px">
    PlutoSky&nbsp;R1 &middot; Fishball7020 &middot; 7020&#8209;SDR
  </div>
  <div style="font-weight:800;font-size:132px;line-height:1.02;letter-spacing:-.022em">
    Buildable firmware<br>for a radio that<br>ships without any.
  </div>
  <div style="font-size:42px;line-height:1.5;color:{t['ink2']};margin-top:44px;max-width:1560px">
    Rebuild every file on the SD card from source &mdash; bitstream, boot loader,
    kernel, root filesystem &mdash; and put your own HDL inside the AD9361 datapath.
  </div>
</div>
<div style="position:absolute;left:150px;bottom:52px;display:flex;align-items:center;gap:44px">
  {_logo_img(t, 92)}
  <div class="mono" style="font-size:30px;color:{t['ink2']};letter-spacing:.06em">
    Zynq&nbsp;XC7Z020 &nbsp;&middot;&nbsp; AD9361 2T2R &nbsp;&middot;&nbsp; 70&nbsp;MHz&ndash;6&nbsp;GHz
  </div>
</div>
<div class="mono" style="position:absolute;right:150px;bottom:78px;font-size:28px;color:{BLUE};letter-spacing:.06em">
  no Vitis &nbsp;&middot;&nbsp; no AMD binaries
</div>"""


def devkit_stack(t):
    """Concept B - the layers, which is the thing nobody else gives you."""
    layers = [("bitstream", "Vivado &rarr; system_top.bit"),
              ("FSBL", "AMD embeddedsw + gcc-arm-none-eabi"),
              ("U-Boot", "distro cross-compiler"),
              ("kernel", "Linux 6.12 LTS, ADI drivers"),
              ("root filesystem", "Debian 13 armhf")]
    rows = ""
    for i, (name, sub) in enumerate(layers):
        y = 330 + i * 152
        rows += f"""
<div style="position:absolute;left:1180px;top:{y}px;width:1240px;height:120px;
     background:{t['bg2']};border:3px solid {t['rule']};border-left:14px solid {BLUE};
     border-radius:14px;display:flex;align-items:center;padding:0 46px;gap:34px">
  <div style="font-weight:700;font-size:46px;min-width:430px">{name}</div>
  <div class="mono" style="font-size:27px;color:{t['ink2']}">{sub}</div>
</div>"""
    return f"""
<div style="position:absolute;left:150px;top:190px;width:900px">
  <div class="mono" style="font-size:29px;letter-spacing:.30em;text-transform:uppercase;color:{BLUE};margin-bottom:34px">
    PlutoSky&nbsp;R1 &middot; 7020&#8209;SDR
  </div>
  <div style="font-weight:800;font-size:124px;line-height:1.02;letter-spacing:-.022em">
    Every<br>layer,<br>from<br>source.
  </div>
  <div style="font-size:38px;line-height:1.5;color:{t['ink2']};margin-top:40px">
    One command rebuilds all five, and flashes them back over the network.
  </div>
</div>
{rows}
<div style="position:absolute;left:150px;bottom:92px;display:flex;align-items:center;gap:40px">
  {_logo_img(t, 100)}
  <div class="mono" style="font-size:28px;color:{t['ink2']}">Zynq&nbsp;XC7Z020 &middot; AD9361</div>
</div>"""


def mcp_tools(t):
    """MCP concept A - the radio as something an assistant can drive."""
    trace = spectrum(11, 1180, 300, [(0.30, .60, .013), (0.58, .40, .011), (0.76, .74, .010)])
    chips = ["sdr_spectrum", "sdr_scan_band", "sdr_capture_iq", "sdr_tune",
             "sdr_transmit_waveform", "sdr_board_health", "sdr_tx_status",
             "sdr_sample_gpio", "sdr_rfid_field"]
    tags = "".join(
        f'<span class="mono" style="display:inline-block;background:{t["bg2"]};'
        f'border:3px solid {t["rule"]};border-radius:999px;padding:16px 34px;'
        f'font-size:27px;color:{t["ink2"]};margin:0 16px 20px 0">{c}</span>'
        for c in chips)
    return f"""
<div style="position:absolute;left:150px;top:150px;width:1460px">
  <div class="mono" style="font-size:30px;letter-spacing:.30em;text-transform:uppercase;color:{BLUE};margin-bottom:34px">
    Model Context Protocol &middot; 21 tools
  </div>
  <div style="font-weight:800;font-size:138px;line-height:1.02;letter-spacing:-.022em">
    Give an assistant<br>a real radio.
  </div>
  <div style="font-size:42px;line-height:1.5;color:{t['ink2']};margin-top:40px;max-width:1380px">
    Tune, scan a band, measure a spectrum, capture IQ as SigMF and transmit &mdash;
    on a PlutoSky&nbsp;R1, over libiio, from a conversation.
  </div>
  <div style="margin-top:56px;max-width:1420px">{tags}</div>
</div>
<svg width="1180" height="300" style="position:absolute;right:150px;top:420px">
  <polyline points="{trace}" fill="none" stroke="{BLUE}" stroke-width="5" stroke-linejoin="round"/>
</svg>
<div style="position:absolute;left:150px;bottom:92px;display:flex;align-items:center;gap:40px">
  {_logo_img(t, 100)}
  <div class="mono" style="font-size:28px;color:{t['ink2']}">Zynq&nbsp;XC7Z020 &middot; AD9361 &middot; 70&nbsp;MHz&ndash;6&nbsp;GHz</div>
</div>"""


def mcp_prompt(t):
    """MCP concept B - what using it actually looks like."""
    trace = spectrum(23, 1120, 340, [(0.33, .66, .012), (0.62, .88, .009), (0.79, .34, .013)])
    return f"""
<div style="position:absolute;left:150px;top:170px;width:1290px">
  <div class="mono" style="font-size:30px;letter-spacing:.30em;text-transform:uppercase;color:{BLUE};margin-bottom:36px">
    MCP server &middot; PlutoSky&nbsp;R1
  </div>
  <div style="font-weight:800;font-size:150px;line-height:1.0;letter-spacing:-.024em">
    Ask the<br>spectrum<br>a question.
  </div>
  <div style="font-size:42px;line-height:1.5;color:{t['ink2']};margin-top:46px">
    21 tools that tune, scan, measure, capture IQ as SigMF and transmit &mdash;
    driving real hardware over libiio.
  </div>
</div>
<div style="position:absolute;right:150px;top:250px;width:1000px">
  <div style="background:{t['bg2']};border:3px solid {t['rule']};border-radius:20px;
       padding:40px 46px;margin-bottom:34px">
    <div class="mono" style="font-size:26px;color:{BLUE};margin-bottom:18px">you</div>
    <div style="font-size:40px;line-height:1.35">What is on the air around 433&nbsp;MHz?</div>
  </div>
  <div style="background:{t['bg2']};border:3px solid {t['rule']};border-radius:20px;
       padding:40px 46px 30px">
    <div class="mono" style="font-size:26px;color:{t['ink2']};margin-bottom:22px">sdr_scan_band &rarr;</div>
    <svg width="900" height="340" style="display:block">
      <polyline points="{trace}" fill="none" stroke="{BLUE}" stroke-width="5" stroke-linejoin="round"/>
    </svg>
  </div>
</div>
<div style="position:absolute;left:150px;bottom:64px;display:flex;align-items:center;gap:40px">
  {_logo_img(t, 92)}
  <div class="mono" style="font-size:28px;color:{t['ink2']}">Zynq&nbsp;XC7Z020 &middot; AD9361 &middot; SigMF</div>
</div>"""


CONCEPTS = {
    "devkit-signal": devkit_signal,
    "devkit-stack": devkit_stack,
    "mcp-tools": mcp_tools,
    "mcp-prompt": mcp_prompt,
}

if __name__ == "__main__":
    for name in CONCEPTS:
        for theme in ("dark", "light"):
            out = HERE / f"{name}-{theme}.html"
            cover(name, theme, out)
            print(f"  wrote {out.name}")
