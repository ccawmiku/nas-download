from __future__ import annotations

import json
import hashlib
import os
import subprocess
import time
from pathlib import Path

from PIL import Image, ImageFile

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".ts"}


class MediaError(RuntimeError):
    pass


def execute(command, timeout=60):
    result = subprocess.run(command, capture_output=True, timeout=timeout)
    if result.returncode:
        raise MediaError(result.stderr.decode("utf-8", "replace")[-1500:])
    return result.stdout


def probe(path: Path, binary="ffprobe"):
    data = json.loads(
        execute(
            [
                binary,
                "-v",
                "error",
                "-show_error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ]
        )
    )
    streams = data.get("streams", [])
    video = next(
        (
            stream
            for stream in streams
            if stream.get("codec_type") == "video"
            and not stream.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    if (
        data.get("error")
        or not video
        or min(int(video.get("width", 0)), int(video.get("height", 0))) <= 0
    ):
        raise MediaError("视频封装或视频流无效")
    duration = float(
        data.get("format", {}).get("duration") or video.get("duration") or 0
    )
    if duration <= 0 or not path.is_file() or path.stat().st_size <= 0:
        raise MediaError("视频时长或文件大小无效")
    return {
        "width": video["width"],
        "height": video["height"],
        "codec": video["codec_name"],
        "pix_fmt": video.get("pix_fmt", ""),
        "duration": duration,
        "fps": video.get("avg_frame_rate", ""),
        "audio": [s for s in streams if s.get("codec_type") == "audio"],
        "color": {
            k: video[k]
            for k in ("color_space", "color_transfer", "color_primaries")
            if k in video
        },
    }


def validate_video(path, info, ffmpeg="ffmpeg", ffprobe="ffprobe", reference=None):
    if reference:
        from fractions import Fraction

        if (info["width"], info["height"]) != (reference["width"], reference["height"]):
            raise MediaError("转换改变了分辨率")
        if abs(info["duration"] - reference["duration"]) > max(
            0.25, reference["duration"] * 0.002
        ):
            raise MediaError("转换视频时长不匹配")
        if len(info["audio"]) != len(reference["audio"]):
            raise MediaError("转换丢失音频")
        if info.get("fps") and reference.get("fps") and Fraction(reference["fps"]) > 0:
            if (
                abs(float(Fraction(info["fps"]) / Fraction(reference["fps"])) - 1)
                > 0.005
            ):
                raise MediaError("转换改变了帧率")
    # Inspect packet accessibility near both ends, without traversing the full file.
    for position in sorted({0, max(0, info["duration"] - 1)}):
        packet = json.loads(
            execute(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-read_intervals",
                    f"{position}%+0.5",
                    "-select_streams",
                    "v:0",
                    "-show_packets",
                    "-show_entries",
                    "packet=pts_time,size",
                    "-of",
                    "json",
                    str(path),
                ]
            )
        )
        if not packet.get("packets"):
            raise MediaError("视频首尾片段不可读取")
        execute(
            [
                ffmpeg,
                "-hide_banner",
                "-v",
                "error",
                "-xerror",
                "-ss",
                str(position),
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-frames:v",
                "2",
                "-threads",
                "1",
                "-f",
                "null",
                "-",
            ],
            timeout=45,
        )


def png_candidate(source: Path, candidate: Path):
    with Image.open(source) as image:
        if getattr(image, "is_animated", False):
            return False, "动画 PNG 保留原件"
        if "A" in image.getbands() or "transparency" in image.info:
            if image.convert("RGBA").getchannel("A").getextrema()[0] < 255:
                return False, "透明 PNG 保留原件"
        original_size = image.size
        image.load()
        # Do not transpose EXIF: stored dimensions and orientation remain intact.
        kwargs = {"quality": 95, "subsampling": 0, "optimize": True}
        for key in ("icc_profile", "exif"):
            if image.info.get(key):
                kwargs[key] = image.info[key]
        old_block = ImageFile.MAXBLOCK
        try:
            ImageFile.MAXBLOCK = max(
                old_block, 1024 * 1024, image.width * image.height * 4
            )
            image.convert("RGB").save(candidate, "JPEG", **kwargs)
        finally:
            ImageFile.MAXBLOCK = old_block
    with Image.open(candidate) as check:
        check.load()
        if check.size != original_size:
            raise MediaError("JPEG 尺寸不一致")
    if candidate.stat().st_size >= source.stat().st_size:
        candidate.unlink()
        return False, "JPEG 不更小，保留 PNG"
    return True, "PNG 已转换为 JPEG · 质量 95"


def hevc_command(
    source, candidate, info, ffmpeg="ffmpeg", device="/dev/dri/renderD128"
):
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "warning",
        "-y",
        "-init_hw_device",
        f"vaapi=va:{device}",
        "-init_hw_device",
        "qsv=qs@va",
        "-filter_hw_device",
        "qs",
        "-hwaccel",
        "qsv",
        "-hwaccel_output_format",
        "qsv",
        "-readrate",
        "0.5",
        "-noautorotate",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        "-c:v",
        "hevc_qsv",
        "-preset",
        "slow",
        "-global_quality",
        "18",
        "-async_depth",
        "1",
        "-low_power",
        "0",
        "-threads",
        "1",
        "-fps_mode",
        "passthrough",
        "-c:a",
        "copy",
    ]
    if candidate.suffix == ".mp4":
        command += ["-tag:v", "hvc1", "-movflags", "+faststart"]
    for key, option in (
        ("color_space", "colorspace"),
        ("color_transfer", "color_trc"),
        ("color_primaries", "color_primaries"),
    ):
        value = info["color"].get(key)
        if value and value not in {"unknown", "reserved"}:
            command += ["-" + option, value]
    return command + [str(candidate)]


def convert(source, ffmpeg="ffmpeg", ffprobe="ffprobe", retries=1):
    source = Path(source)
    if not source.is_file() or source.stat().st_size == 0:
        raise MediaError("原文件不存在或为空")
    info = None
    if source.suffix.lower() in VIDEO_EXTENSIONS:
        info = probe(source, ffprobe)
        validate_video(source, info, ffmpeg, ffprobe)
        if info["codec"] == "hevc":
            return source, "已是 HEVC，保留原件", info
    elif source.suffix.lower() != ".png":
        return source, "无需转换", {}
    else:
        with Image.open(source) as image:
            image.verify()
    errors = []
    # Candidate extension is selected for audio compatibility without audio transcoding.
    extension = (
        ".jpg"
        if info is None
        else (
            ".mp4"
            if all(s.get("codec_name") in {"aac", "mp3", "alac"} for s in info["audio"])
            else ".mkv"
        )
    )
    candidate = source.with_name("." + source.stem + ".converted" + extension)
    for attempt in range(retries + 1):
        try:
            candidate.unlink(missing_ok=True)
            if info is None:
                used, reason = png_candidate(source, candidate)
                return (candidate if used else source), reason, {}
            command = hevc_command(source, candidate, info, ffmpeg)
            if os.name != "nt":
                command = ["nice", "-n", "19", "ionice", "-c", "3"] + command
            execute(command, timeout=max(600, int(info["duration"] * 12)))
            output_info = probe(candidate, ffprobe)
            validate_video(candidate, output_info, ffmpeg, ffprobe, info)
            if output_info["codec"] != "hevc":
                raise MediaError("成品不是 HEVC")
            if candidate.stat().st_size >= source.stat().st_size:
                candidate.unlink()
                return source, "HEVC 不更小，保留原视频", info
            return candidate, "HEVC 硬件转换完成", output_info
        except Exception as error:
            candidate.unlink(missing_ok=True)
            errors.append(str(error))
            if attempt < retries:
                time.sleep(2)
    return (
        source,
        "转换重试仍失败，原件入库",
        {**(info or {}), "processing_error": errors[-1]},
    )


def publish(source: Path, chosen: Path, target: Path, journal=None):
    from .config import read_json, write_json
    import secrets

    target = target.with_suffix(chosen.suffix) if chosen != source else target
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = read_json(journal) if journal else {}
    if previous.get("destination") and previous.get("sha256"):
        saved = Path(previous["destination"])
        if saved.is_file() and saved.stat().st_size == previous.get("bytes"):
            with saved.open("rb") as stream:
                if (
                    hashlib.file_digest(stream, "sha256").hexdigest()
                    == previous["sha256"]
                ):
                    if previous.get("temporary"):
                        Path(previous["temporary"]).unlink(missing_ok=True)
                    return saved
    stage = target.with_name(
        "." + target.name + "." + secrets.token_hex(8) + ".publishing"
    )
    try:
        digest = hashlib.sha256()
        with chosen.open("rb") as incoming, stage.open("xb") as outgoing:
            while chunk := incoming.read(1024 * 1024):
                digest.update(chunk)
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if stage.stat().st_size != chosen.stat().st_size:
            raise MediaError("入库大小不一致")
        original, index = target, 0
        while True:
            if index:
                target = original.with_name(
                    f"{original.stem} ({index}){original.suffix}"
                )
            if journal:
                write_json(
                    journal,
                    {
                        **previous,
                        "destination": str(target),
                        "temporary": str(stage),
                        "sha256": digest.hexdigest(),
                        "bytes": stage.stat().st_size,
                    },
                )
            try:
                # Link makes a complete file visible atomically, and never replaces another file.
                os.link(stage, target)
                break
            except FileExistsError:
                index += 1
        stage.unlink()
        if os.name != "nt":
            descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return target
    finally:
        stage.unlink(missing_ok=True)
