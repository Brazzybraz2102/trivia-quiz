# Braznarkel Ranch logo

The ranch wordmark: "BRAZNARKEL" in a bold Western slab over "RANCH", on a dark banner with a sky-blue edge.

| File | Use |
| --- | --- |
| `braznarkel-ranch-logo.svg` | Master file. Scales to any size (signs, shirts, print). |
| `braznarkel-ranch-logo.png` | 3120 × 1104 on the slate-blue background. |
| `braznarkel-ranch-logo-transparent.png` | 2760 × 744 with a transparent background, for putting on other designs. |

The lettering is converted to shapes, so the SVG looks the same everywhere without needing the fonts installed.

`make_logo.py` regenerates the SVG. It needs Python with `fonttools` and the Alfa Slab One and Rye font files (from Google Fonts, both under the SIL Open Font License): `python3 make_logo.py <font-folder> out.svg`, where the folder holds `alfa.woff` and `rye.woff`.
