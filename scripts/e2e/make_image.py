"""Write one generated test image (S17b e2e): `make_image.py <w> <h> <r,g,b> <format> <out> [fill]`.

A flat background of `<r,g,b>` with a centred dark square (the "product"); `format` is a Pillow
format name (JPEG, PNG, TIFF, WEBP) or `corrupt` (JPEG magic bytes and nothing decodable). `fill`
is the square's side as a share of the shorter side (default 0.5); 0.94 fills the frame enough for
the Amazon main photo's fill-ratio check (85 % of the area) and keeps the white border band the
background check reads. Run with `uv run --frozen python` so Pillow comes from the lock. No binary
fixture is committed.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) not in (5, 6):
        sys.stderr.write("usage: make_image.py <w> <h> <r,g,b> <format> <out> [fill]\n")
        return 2
    w, h = int(argv[0]), int(argv[1])
    rgb = tuple(int(part) for part in argv[2].split(","))
    fmt, out = argv[3].upper(), Path(argv[4])
    fill = float(argv[5]) if len(argv) == 6 else 0.5
    if not 0 < fill <= 1:
        sys.stderr.write("make_image.py: fill must be in (0, 1]\n")
        return 2
    if fmt == "CORRUPT":
        out.write_bytes(b"\xff\xd8\xff" + b"\x00" * 512)
        return 0
    from PIL import Image

    img = Image.new("RGB", (w, h), rgb)  # type: ignore[arg-type]
    side = max(1, int(min(w, h) * fill))
    img.paste((40, 60, 90), ((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
    img.save(out, format=fmt)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
