import subprocess

import pytest
from PIL import Image, ImageChops, ImageOps

from app.previews import PREVIEW_SIZE, PreviewError, PreviewGenerator


@pytest.mark.parametrize("orientation", range(1, 9))
def test_preview_preserves_exif_orientation(tmp_path, orientation):
    source = tmp_path / "oriented.jpg"
    with Image.new("RGB", (800, 480), "red") as image:
        image.paste("green", (400, 0, 800, 240))
        image.paste("blue", (0, 240, 400, 480))
        image.paste("yellow", (400, 240, 800, 480))
        exif = Image.Exif()
        exif[274] = orientation
        image.save(source, exif=exif, quality=95)

    output = PreviewGenerator(tmp_path / "previews").generate(source, "images")
    with Image.open(source) as opened, Image.open(output) as actual:
        expected = ImageOps.exif_transpose(opened)
        expected.thumbnail(PREVIEW_SIZE, Image.Resampling.LANCZOS)
        with Image.new("RGB", PREVIEW_SIZE, "#eef2f6") as canvas:
            canvas.paste(expected, ((320 - expected.width) // 2, (180 - expected.height) // 2))
            # Compare a downsampled image to tolerate JPEG edge ringing.
            diff = ImageChops.difference(actual.resize((32, 18)), canvas.resize((32, 18)))
            assert sum(sum(pixel) for pixel in diff.get_flattened_data()) / (32 * 18 * 3) < 5
        expected.close()
        assert actual.size == PREVIEW_SIZE


@pytest.mark.parametrize("mode,extension", [("RGBA", "png"), ("P", "gif")])
def test_preview_transparency_and_first_frame(tmp_path, mode, extension):
    source = tmp_path / f"transparent.{extension}"
    with Image.new("RGBA", (320, 180), (255, 0, 0, 0)) as image:
        image.paste((255, 0, 0, 255), (120, 60, 200, 120))
        if mode == "P":
            with image.convert("P") as palette, Image.new("RGB", image.size, "blue") as second:
                palette.save(source, transparency=palette.getpixel((10, 10)),
                             save_all=True, append_images=[second], duration=100)
        else:
            image.save(source)
    output = PreviewGenerator(tmp_path / "cache").generate(source, "images")
    with Image.open(output) as actual:
        assert all(abs(a - b) < 6 for a, b in zip(actual.getpixel((10, 10)), (238, 242, 246)))
        assert actual.getpixel((160, 90))[0] > 240


def test_cache_hit_does_not_start_decoder(tmp_path, monkeypatch):
    source = tmp_path / "cached.png"
    with Image.new("RGB", (100, 100), "blue") as image:
        image.save(source)
    generator = PreviewGenerator(tmp_path / "cache")
    first = generator.generate(source, "images")

    def fail(*args, **kwargs):
        pytest.fail("a cache hit must not launch a decoder")

    monkeypatch.setattr(subprocess, "run", fail)
    assert generator.generate(source, "images") == first


def test_corrupt_image_does_not_leave_temporary_file(tmp_path):
    source = tmp_path / "broken.jpg"
    source.write_bytes(b"not an image")
    cache = tmp_path / "cache"
    with pytest.raises(PreviewError):
        PreviewGenerator(cache).generate(source, "images")
    assert list(cache.iterdir()) == []


def test_timeout_cleans_up_temporary_file(tmp_path, monkeypatch):
    source = tmp_path / "image.png"
    source.write_bytes(b"placeholder")
    cache = tmp_path / "cache"

    def timeout(command, **kwargs):
        from pathlib import Path

        Path(command[-1]).write_bytes(b"partial")
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(PreviewError, match="超时"):
        PreviewGenerator(cache).generate(source, "images")
    assert list(cache.iterdir()) == []
