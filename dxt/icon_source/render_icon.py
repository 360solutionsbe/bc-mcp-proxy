"""Render the vgs-bc-mcp extension icon deterministically.

The icon is a derivation of the Vangelder Solutions V5 favicon (navy square,
white Archivo "V", green rule) placed on a stack of flat teal tiles that
nods to the Business Central colour family without reproducing any Microsoft
artwork (the Business Central logo is a Microsoft trademark and may not be
used as a product icon).

Usage:
    python dxt/icon_source/render_icon.py <path-to-Archivo-Variable.ttf>

Archivo is distributed under the SIL Open Font License; the font file itself
is not part of this repository. Outputs dxt/icon.png (512x512) and
dxt/icon-256.png (256x256).
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Vangelder Solutions V5 brand tokens.
NAVY = (10, 17, 36, 255)
GREEN_ON_DARK = (52, 210, 125, 255)
WHITE = (255, 255, 255, 255)
# Flat tones from the Dynamics 365 blue->teal family (colours only, no artwork).
TILES = ((12, 116, 161, 255), (24, 160, 196, 255), (38, 207, 232, 255))

N = 1024  # master canvas; downsampled with Lanczos for the shipped sizes.


def _font(path: str, size: int, weight: int = 700) -> ImageFont.FreeTypeFont:
  font = ImageFont.truetype(path, size)
  font.set_variation_by_axes([weight])
  return font


def _text_centered(draw: ImageDraw.ImageDraw, text: str, font, cx: float, cy: float, fill) -> None:
  left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
  width, height = right - left, bottom - top
  draw.text((cx - width / 2 - left, cy - height / 2 - top), text, font=font, fill=fill)


def render(font_path: str) -> Image.Image:
  image = Image.new("RGBA", (N, N), NAVY)
  draw = ImageDraw.Draw(image)
  for offset, colour in zip((0.00, 0.05, 0.10), TILES):
    x0, y0 = N * (0.22 + offset), N * (0.20 + offset)
    draw.rounded_rectangle([x0, y0, x0 + N * 0.46, y0 + N * 0.46], radius=int(N * 0.06), fill=colour)
  _text_centered(draw, "V", _font(font_path, 400), N * 0.50, N * 0.48, WHITE)
  draw.rectangle([N * 0.33, N * 0.80, N * 0.67, N * 0.80 + int(N * 0.022)], fill=GREEN_ON_DARK)
  return image


def main(argv: list[str]) -> int:
  if len(argv) != 2:
    print(__doc__, file=sys.stderr)
    return 2
  out_dir = Path(__file__).resolve().parent.parent
  master = render(argv[1])
  for size, name in ((512, "icon.png"), (256, "icon-256.png")):
    master.resize((size, size), Image.LANCZOS).save(out_dir / name, optimize=True)
    print(f"wrote {out_dir / name}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main(sys.argv))
