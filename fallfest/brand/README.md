# Braznarkel Ranch logo

A redraw of the original ranch logo without the dog: blue golf cart, pines, cabin, campfire, Adirondack chair and the name banner.

| File | Use |
| --- | --- |
| `braznarkel-ranch-logo.svg` | Master file. Scales to any size (signs, shirts, print). |
| `braznarkel-ranch-logo.png` | 2048 × 2048 on the slate-blue background. |
| `braznarkel-ranch-logo-transparent.png` | 2048 × 2048 with a transparent background, for putting on other designs. |

The lettering is converted to shapes, so the SVG looks the same everywhere without needing the fonts installed.

`make_logo.py` regenerates the SVG. It needs Python with `fonttools` and the Alfa Slab One and Rye font files (from Google Fonts, both under the SIL Open Font License): `python3 make_logo.py <font-folder> out.svg`, where the folder holds `alfa.woff` and `rye.woff`.
