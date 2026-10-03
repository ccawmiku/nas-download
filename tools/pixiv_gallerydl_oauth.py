#!/usr/bin/env python3
import runpy
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "_src" / "pixiv-auto-download-nas-main" / "pixiv_gallerydl_oauth.py"


if __name__ == "__main__":
    runpy.run_path(str(SCRIPT), run_name="__main__")
