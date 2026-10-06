"""Decode one image and exit so native decoder allocations are released."""

from __future__ import annotations

import sys
from contextlib import ExitStack
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.previews import PREVIEW_SIZE


def generate_image(source: Path, destination: Path) -> None:
    with ExitStack() as stack:
        opened = stack.enter_context(Image.open(source))
        opened.seek(0)
        orientation = opened.getexif().get(274, 1)
        # Resize before transpose: JPEG thumbnail() can use decoder downsampling,
        # and EXIF rotation then copies only the thumbnail, not the full image.
        size = PREVIEW_SIZE[::-1] if orientation in {5, 6, 7, 8} else PREVIEW_SIZE
        opened.thumbnail(size, Image.Resampling.LANCZOS)
        image = ImageOps.exif_transpose(opened)
        stack.callback(image.close)
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
            stack.callback(image.close)
        canvas = stack.enter_context(Image.new("RGB", PREVIEW_SIZE, "#eef2f6"))
        offset = ((PREVIEW_SIZE[0] - image.width) // 2, (PREVIEW_SIZE[1] - image.height) // 2)
        canvas.paste(image, offset, image if image.mode == "RGBA" else None)
        canvas.save(destination, "JPEG", quality=82, optimize=True)


def main() -> int:
    try:
        generate_image(Path(sys.argv[1]), Path(sys.argv[2]))
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
