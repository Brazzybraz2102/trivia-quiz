# Builds the Braznarkel Ranch wordmark logo as a self-contained SVG.
# The full scene is assembled first because the wordmark reuses its defs and banner.
import sys
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.boundsPen import BoundsPen

FD = sys.argv[1]; OUT = sys.argv[2]

def text_paths(font_file, text, x0, x1, baseline, cap_h, spacing=0.0, edge_boost=0.0):
    """Lay `text` out to fill x0..x1, caps `cap_h` tall on `baseline`.
    edge_boost > 0 makes letters taller toward the ends, like the original sign."""
    f = TTFont(font_file); gs = f.getGlyphSet(); cmap = f.getBestCmap()
    upm = f["head"].unitsPerEm
    bp = BoundsPen(gs); gs[cmap[ord("H")]].draw(bp); capH = bp.bounds[3]
    names = [cmap[ord(c)] for c in text]
    adv = [gs[n].width for n in names]
    sp = spacing * upm
    total = sum(adv) + sp * (len(text) - 1)
    sx = (x1 - x0) / total
    sy = cap_h / capH
    out, x = [], x0
    cx, hw = (x0 + x1) / 2, (x1 - x0) / 2
    for n, a in zip(names, adv):
        mid = x + a * sx / 2
        k = 1 + edge_boost * ((mid - cx) / hw) ** 2
        pen = SVGPathPen(gs)
        tp = TransformPen(pen, (sx, 0, 0, -sy * k, x, baseline))
        gs[n].draw(tp)
        out.append(pen.getCommands())
        x += (a + sp) * sx
    return " ".join(out)

big = text_paths(f"{FD}/alfa.woff", "BRAZNARKEL", 100, 924, 872, 96, spacing=0.0, edge_boost=0.28)
small = text_paths(f"{FD}/rye.woff", "RANCH", 380, 644, 946, 42, spacing=0.16)

def stones(cx, cy, rx, ry, n):
    import math
    s = []
    for i in range(n):
        a = math.pi * (0.05 + 0.9 * i / (n - 1))  # front arc of the ring
        x, y = cx - rx * math.cos(a), cy + ry * math.sin(a)
        s.append(f'<ellipse cx="{x:.0f}" cy="{y:.0f}" rx="21" ry="15" fill="#ece8de" stroke="#4b4f55" stroke-width="4"/>')
    return "\n    ".join(s)

def pine(x, base, h, w, fill, tiers=5):
    pts = []
    for i in range(tiers):
        t0 = base - h * i / tiers
        ww = w * (1 - i / (tiers + 0.6))
        pts.append(f'<path d="M{x - ww / 2:.0f} {t0:.0f} L{x:.0f} {t0 - h / tiers * 1.7:.0f} L{x + ww / 2:.0f} {t0:.0f} Z" fill="{fill}"/>')
    pts.append(f'<rect x="{x - 9:.0f}" y="{base - 4:.0f}" width="18" height="40" fill="{fill}"/>')
    return "\n    ".join(pts)

svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="1024" height="1024">
  <title>Braznarkel Ranch</title>
  <defs>
    <radialGradient id="sky" cx=".5" cy=".42" r=".62"><stop offset="0" stop-color="#e3f4fc"/><stop offset=".7" stop-color="#a9dbf2"/><stop offset="1" stop-color="#7cc4e6"/></radialGradient>
    <linearGradient id="cream" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffffff"/><stop offset=".55" stop-color="#f6efdf"/><stop offset="1" stop-color="#d9c6a0"/></linearGradient>
    <linearGradient id="cartBlue" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5fb7f0"/><stop offset=".5" stop-color="#2f8fd6"/><stop offset="1" stop-color="#1d67a6"/></linearGradient>
    <linearGradient id="canopy" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f8dcae"/><stop offset="1" stop-color="#e2ad6c"/></linearGradient>
    <linearGradient id="wood" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#e9a25a"/><stop offset="1" stop-color="#c47a35"/></linearGradient>
    <radialGradient id="flame" cx=".5" cy=".8" r=".8"><stop offset="0" stop-color="#fff1a8"/><stop offset=".45" stop-color="#ffb02e"/><stop offset="1" stop-color="#f0541e"/></radialGradient>
    <radialGradient id="glow"><stop offset="0" stop-color="#ffb347" stop-opacity=".55"/><stop offset="1" stop-color="#ffb347" stop-opacity="0"/></radialGradient>
    <clipPath id="skyClip"><circle cx="512" cy="410" r="318"/></clipPath>
    <filter id="sticker" x="-10%" y="-10%" width="120%" height="120%">
      <feMorphology in="SourceAlpha" operator="dilate" radius="11" result="fat"/>
      <feFlood flood-color="#ffffff"/><feComposite in2="fat" operator="in" result="white"/>
      <feMorphology in="SourceAlpha" operator="dilate" radius="16" result="fatter"/>
      <feFlood flood-color="#0f1b26" flood-opacity=".55"/><feComposite in2="fatter" operator="in" result="shadow"/>
      <feOffset in="shadow" dy="6" result="shadowOff"/>
      <feMerge><feMergeNode in="shadowOff"/><feMergeNode in="white"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
  </defs>

  <g filter="url(#sticker)">
    <!-- sky and distant pines -->
    <circle cx="512" cy="410" r="318" fill="url(#sky)"/>
    <g clip-path="url(#skyClip)" opacity=".75">
      {pine(330, 560, 300, 120, "#8fb9cf", 5)}
      {pine(420, 540, 220, 90, "#9cc4d8", 4)}
      {pine(690, 520, 270, 110, "#8fb9cf", 5)}
    </g>

    <!-- big pines -->
    {pine(175, 640, 470, 210, "#1d2f40", 6)}
    {pine(840, 640, 500, 220, "#1d2f40", 6)}

    <!-- cabin -->
    <g>
      <path d="M640 520 V400 L760 318 L880 400 V520 Z" fill="#2d4459"/>
      <path d="M620 410 L760 304 L900 410 L884 422 L760 330 L636 422 Z" fill="#162534"/>
      <rect x="822" y="322" width="26" height="54" fill="#162534"/>
      <rect x="732" y="378" width="56" height="44" fill="#a8d0e4" stroke="#d9e5ec" stroke-width="5"/>
      <path d="M760 378 V422 M732 400 H788" stroke="#d9e5ec" stroke-width="4"/>
      <path d="M640 452 H880" stroke="#d9e5ec" stroke-width="5"/>
      <path d="M660 452 V520 M690 452 V520 M720 452 V520 M750 452 V520 M780 452 V520 M810 452 V520 M840 452 V520 M870 452 V520" stroke="#c4d3dc" stroke-width="4"/>
      <path d="M640 486 H880" stroke="#c4d3dc" stroke-width="4"/>
    </g>

    <!-- grass mound -->
    <path d="M96 690 Q120 610 300 598 L760 590 Q920 600 932 680 Q900 735 512 742 Q130 738 96 690 Z" fill="#5aa83c"/>
    <path d="M110 700 Q512 760 920 690 Q900 735 512 742 Q130 738 110 700 Z" fill="#3f8a2b"/>
    <path d="M150 640 l6 -18 l6 18 M240 618 l6 -16 l6 16 M820 612 l6 -16 l6 16 M880 640 l6 -18 l6 18 M600 600 l5 -14 l5 14" stroke="#3f8a2b" stroke-width="4" fill="none" stroke-linecap="round"/>

    <!-- golf cart, facing left -->
    <g stroke-linejoin="round">
      <!-- rear wheel -->
      <circle cx="690" cy="598" r="64" fill="#1b1b1b"/>
      <circle cx="690" cy="598" r="34" fill="#9aa4ad" stroke="#5b636a" stroke-width="5"/>
      <circle cx="690" cy="598" r="11" fill="#5b636a"/>
      <!-- struts and roof -->
      <path d="M262 208 L222 448" stroke="#2c2f33" stroke-width="13" stroke-linecap="round"/>
      <path d="M702 214 L690 452 M742 210 L746 452" stroke="#2c2f33" stroke-width="12" stroke-linecap="round"/>
      <path d="M250 214 L398 214 L380 438 L226 438 Z" fill="#d7eef9" fill-opacity=".55" stroke="#2c2f33" stroke-width="6"/>
      <path d="M240 330 H390" stroke="#2c2f33" stroke-width="5"/>
      <path d="M244 150 L742 158 L770 196 L224 190 Z" fill="url(#canopy)" stroke="#5a3d22" stroke-width="6"/>
      <path d="M224 190 L770 196 L766 220 L230 214 Z" fill="#4a3423" stroke="#2c1d12" stroke-width="5"/>
      <!-- seat back -->
      <path d="M596 446 L612 330 Q616 312 638 312 L694 316 Q712 318 710 336 L700 446 Z" fill="#ecbb80" stroke="#7a4f27" stroke-width="6"/>
      <path d="M622 340 L690 344" stroke="#d29a5c" stroke-width="5"/>
      <!-- steering -->
      <path d="M392 470 L446 352" stroke="#2c2f33" stroke-width="14" stroke-linecap="round"/>
      <ellipse cx="452" cy="344" rx="52" ry="22" transform="rotate(-24 452 344)" fill="none" stroke="#202326" stroke-width="13"/>
      <!-- body -->
      <path d="M148 548 Q146 470 236 458 L478 450 Q520 452 530 480 L536 556 Q400 580 182 584 Q150 578 148 548 Z" fill="url(#cartBlue)" stroke="#123f66" stroke-width="7"/>
      <path d="M440 462 L790 462 Q808 464 808 484 L804 556 Q790 572 760 572 L520 572 Z" fill="url(#cartBlue)" stroke="#123f66" stroke-width="7"/>
      <path d="M418 452 L716 452 L728 478 L410 480 Z" fill="#ecbb80" stroke="#7a4f27" stroke-width="6"/>
      <path d="M236 470 Q330 462 470 462" stroke="#9fd6fa" stroke-width="7" fill="none" stroke-linecap="round" opacity=".8"/>
      <!-- grille and lights -->
      <path d="M162 516 L300 512 L302 566 L176 570 Q162 556 162 516 Z" fill="#3a4148" stroke="#123f66" stroke-width="5"/>
      <path d="M176 528 H296 M176 542 H298 M178 556 H298" stroke="#7b858e" stroke-width="5"/>
      <rect x="168" y="482" width="44" height="22" rx="5" fill="#f5a03a" stroke="#123f66" stroke-width="5"/>
      <rect x="300" y="478" width="44" height="22" rx="5" fill="#f5a03a" stroke="#123f66" stroke-width="5"/>
      <!-- running board -->
      <path d="M350 574 L640 568 L646 590 L356 596 Z" fill="#cfd6db" stroke="#4d565e" stroke-width="5"/>
      <!-- front wheel -->
      <circle cx="292" cy="610" r="80" fill="#1b1b1b"/>
      <path d="M292 530 v16 M292 674 v16 M212 610 h16 M356 610 h16 M236 554 l11 11 M337 655 l11 11 M348 554 l-11 11 M247 655 l-11 11" stroke="#3a3a3a" stroke-width="7"/>
      <circle cx="292" cy="610" r="44" fill="#a4adb5" stroke="#5b636a" stroke-width="6"/>
      <circle cx="292" cy="610" r="14" fill="#5b636a"/>
    </g>

    <!-- campfire -->
    <ellipse cx="520" cy="660" rx="140" ry="60" fill="url(#glow)"/>
    <ellipse cx="520" cy="672" rx="92" ry="26" fill="#2b2a28"/>
    <path d="M520 666 C470 650 476 610 494 586 C492 606 504 612 510 600 C506 572 520 548 540 530 C536 560 560 570 556 600 C566 592 568 580 566 568 C590 600 584 652 520 666 Z" fill="url(#flame)" stroke="#c2410c" stroke-width="4"/>
    <path d="M520 662 C500 652 502 630 512 618 C514 630 522 632 526 620 C536 634 542 654 520 662 Z" fill="#fff3b0"/>
    {stones(520, 668, 98, 30, 9)}

    <!-- Adirondack chair -->
    <g stroke="#5a3418" stroke-width="5" stroke-linejoin="round">
      <path d="M716 560 L744 712" stroke-width="16" stroke="#8a5124" stroke-linecap="round"/>
      <path d="M864 556 L884 700" stroke-width="16" stroke="#8a5124" stroke-linecap="round"/>
      <path d="M760 440 Q770 412 792 414 L806 590 L774 596 Z" fill="url(#wood)"/>
      <path d="M800 420 Q812 396 834 402 L836 594 L806 596 Z" fill="url(#wood)"/>
      <path d="M840 410 Q856 392 874 404 L862 592 L836 594 Z" fill="url(#wood)"/>
      <path d="M876 418 Q894 410 904 428 L884 590 L862 592 Z" fill="url(#wood)"/>
      <path d="M690 596 L896 586 L904 612 L700 624 Z" fill="#d48a43"/>
      <path d="M676 548 L842 536 L846 556 L680 570 Z" fill="#e39a52"/>
      <path d="M696 568 L704 690" stroke-width="16" stroke="#8a5124" stroke-linecap="round"/>
      <path d="M842 556 L850 690" stroke-width="16" stroke="#8a5124" stroke-linecap="round"/>
    </g>
  </g>

  <!-- name banner -->
  <path d="M64 742 L960 742 L948 902 L766 902 L746 966 L278 966 L258 902 L76 902 Z" fill="#13212f" stroke="#8fd3f5" stroke-width="9" stroke-linejoin="round"/>
  <path d="{big}" fill="url(#cream)" stroke="#0b1520" stroke-width="12" stroke-linejoin="round" paint-order="stroke"/>
  <path d="{small}" fill="#ffffff" stroke="#0b1520" stroke-width="6" paint-order="stroke"/>
</svg>
'''

# Wordmark only: the name banner on its own, no illustration.
banner = svg[svg.index("  <!-- name banner -->"):svg.index("</svg>")]
defs = svg[svg.index("  <defs>"):svg.index("  </defs>") + len("  </defs>")]
word = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="52 730 920 248" width="920" height="248">
  <title>Braznarkel Ranch</title>
{defs}
{banner}</svg>
'''
open(OUT, "w").write(word)
print("wrote", OUT, len(word))
