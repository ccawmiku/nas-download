"""Strict validation and cookie-free CDN downloading for X direct manifests."""
from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from .config import sanitize

ALLOWED_MEDIA_HOSTS = frozenset({"pbs.twimg.com", "video.twimg.com"})
ALLOWED_TWEET_HOSTS = frozenset(
    {"x.com", "www.x.com", "twitter.com", "mobile.twitter.com"}
)
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".webm"})
MAX_DIRECT_TWEETS_PER_BATCH = 500
MAX_MEDIA_PER_TWEET = 20
MAX_TOTAL_MEDIA_PER_BATCH = 2000
MAX_MEDIA_BYTES = int(os.getenv("NAS_X_MAX_MEDIA_BYTES", str(512 * 1024 * 1024)))
DOWNLOAD_TIMEOUT = (10, 60)

_TWEET_ID_RE = re.compile(r"^\d{1,30}$")
_SAFE_AUTHOR_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_author(value: str) -> str:
    cleaned = _SAFE_AUTHOR_RE.sub("_", str(value or "").strip()).strip("_")
    return (cleaned or "unknown")[:64]


def _safe_media_key(value: str, fallback: str) -> str:
    cleaned = _SAFE_AUTHOR_RE.sub("_", str(value or "").strip()).strip("_")
    return (cleaned or fallback)[:64]


def validate_tweet_url(raw_url: str, tweet_id: str, author: str = "") -> str:
    text = str(raw_url or "").strip()
    if not text:
        handle = _safe_author(author)
        return (
            f"https://x.com/{handle}/status/{tweet_id}"
            if handle != "unknown"
            else f"https://x.com/i/web/status/{tweet_id}"
        )
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or host not in ALLOWED_TWEET_HOSTS
    ):
        raise ValueError("推文链接域名不受支持")
    if parsed.port not in (None, 443):
        raise ValueError("推文链接端口不受支持")
    clean_path = parsed.path or f"/i/web/status/{tweet_id}"
    return f"https://{host}{clean_path}"


def validate_media_url(raw_url: str, media_type: str) -> str:
    text = str(raw_url or "").strip()
    if not text or len(text) > 2048:
        raise ValueError("媒体直链无效")
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("仅允许使用 HTTPS 官方 CDN 直链")
    if parsed.port not in (None, 443):
        raise ValueError("CDN 链接仅允许标准 443 端口")
    try:
        ipaddress.ip_address(host)
        raise ValueError("禁止直接使用 IP 地址作为 CDN 主机")
    except ValueError as error:
        if "禁止直接使用 IP" in str(error):
            raise
    if host not in ALLOWED_MEDIA_HOSTS:
        raise ValueError("媒体直链域名不在官方 CDN 白名单内")
    if media_type == "photo" and host != "pbs.twimg.com":
        raise ValueError("图片直链必须来自 pbs.twimg.com")
    if media_type in {"video", "animated_gif"} and host != "video.twimg.com":
        raise ValueError("视频或动图直链必须来自 video.twimg.com")
    return text


def normalize_direct_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("请求体格式无效")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= MAX_DIRECT_TWEETS_PER_BATCH:
        raise ValueError(f"每次最多提交 1–{MAX_DIRECT_TWEETS_PER_BATCH} 条推文清单")

    grouped: dict[str, dict[str, Any]] = {}
    total_media = 0

    for entry in raw_items:
        if not isinstance(entry, dict):
            raise ValueError("推文条目格式无效")
        tweet_id = str(entry.get("tweet_id") or entry.get("id") or "").strip()
        if not _TWEET_ID_RE.match(tweet_id):
            raise ValueError("推文 ID 格式无效")
        author = _safe_author(str(entry.get("author") or ""))
        tweet_url = validate_tweet_url(str(entry.get("url") or ""), tweet_id, author)
        title = sanitize(str(entry.get("title") or entry.get("text") or ""))[:500]
        owner_role = str(entry.get("owner_role") or "direct").strip().lower()
        if owner_role not in {"direct", "outer", "quoted", "retweeted"}:
            owner_role = "direct"
        quoted_from = str(entry.get("quoted_from") or "").strip()
        if quoted_from and not _TWEET_ID_RE.match(quoted_from):
            raise ValueError("引用来源推文 ID 无效")
        unresolvable = str(entry.get("unresolvable_reason") or "").strip()[:80]

        raw_media = entry.get("media")
        if raw_media is None:
            raw_media = []
        if not isinstance(raw_media, list) or len(raw_media) > MAX_MEDIA_PER_TWEET:
            raise ValueError(f"单条推文最多包含 {MAX_MEDIA_PER_TWEET} 个媒体")

        normalized_media: list[dict[str, str]] = []
        for idx, media in enumerate(raw_media, start=1):
            if not isinstance(media, dict):
                raise ValueError("媒体条目格式无效")
            mtype = str(media.get("type") or "").strip().lower()
            if mtype not in {"photo", "video", "animated_gif"}:
                raise ValueError("未知媒体类型")
            murl = validate_media_url(str(media.get("url") or ""), mtype)
            mkey = _safe_media_key(str(media.get("media_key") or media.get("mediaKey") or ""), f"m{idx}")
            normalized_media.append({"media_key": mkey, "type": mtype, "url": murl})

        total_media += len(normalized_media)
        if total_media > MAX_TOTAL_MEDIA_PER_BATCH:
            raise ValueError(f"单批总媒体数量超出 {MAX_TOTAL_MEDIA_PER_BATCH} 限制")
        if not normalized_media and not unresolvable:
            raise ValueError("推文条目既无有效媒体直链也无异常说明")

        existing = grouped.get(tweet_id)
        if existing is None:
            grouped[tweet_id] = {
                "tweet_id": tweet_id,
                "author": author,
                "url": tweet_url,
                "title": title,
                "owner_role": owner_role,
                "quoted_from": quoted_from,
                "media": normalized_media,
                "unresolvable_reason": unresolvable,
            }
        else:
            seen_urls = {m["url"] for m in existing["media"]}
            for item in normalized_media:
                if item["url"] not in seen_urls:
                    if len(existing["media"]) >= MAX_MEDIA_PER_TWEET:
                        break
                    existing["media"].append(item)
                    seen_urls.add(item["url"])
            if not existing["title"] and title:
                existing["title"] = title
            if existing["author"] == "unknown" and author != "unknown":
                existing["author"] = author
            if existing["media"]:
                existing["unresolvable_reason"] = ""

    return list(grouped.values())


def _target_extension(url: str, media_type: str, content_type: str) -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if media_type == "photo":
        if "png" in ctype or "format=png" in url or url.lower().endswith(".png"):
            return ".png"
        if "webp" in ctype or "format=webp" in url or url.lower().endswith(".webp"):
            return ".webp"
        return ".jpg"
    return ".mp4"


def _expected_content_type(media_type: str, content_type: str) -> bool:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if media_type == "photo":
        return ctype.startswith("image/")
    return ctype.startswith("video/") or ctype == "application/octet-stream"


def download_cdn_file(
    url: str,
    media_type: str,
    target_dir: Path,
    stem: str,
    *,
    session: requests.Session | None = None,
    max_bytes: int = MAX_MEDIA_BYTES,
    timeout: tuple[int, int] = DOWNLOAD_TIMEOUT,
    user_agent: str = "",
) -> Path:
    validate_media_url(url, media_type)
    headers = {"User-Agent": user_agent} if user_agent else {}
    own_session = session is None
    client = session or requests.Session()
    tmp_path: Path | None = None
    try:
        with client.get(
            url,
            headers=headers,
            timeout=timeout,
            stream=True,
            allow_redirects=False,
        ) as response:
            if 300 <= response.status_code < 400:
                raise RuntimeError(f"拒绝官方 CDN 外的重定向 ({response.status_code})")
            if response.status_code != 200:
                raise RuntimeError(f"CDN 返回 HTTP {response.status_code}")
            ctype = response.headers.get("content-type", "")
            if not _expected_content_type(media_type, ctype):
                raise RuntimeError(f"CDN 响应类型不符: {ctype or 'unknown'}")
            raw_len = response.headers.get("content-length")
            if raw_len:
                try:
                    if int(raw_len) > max_bytes:
                        raise RuntimeError("CDN 文件超出单文件体积上限")
                except ValueError:
                    pass
            ext = _target_extension(url, media_type, ctype)
            target_dir.mkdir(parents=True, exist_ok=True)
            final_path = target_dir / f"{stem}{ext}"
            if final_path.is_file() and final_path.stat().st_size > 0:
                return final_path
            tmp_path = final_path.with_suffix(final_path.suffix + ".part")
            written = 0
            for attempt in range(3):
                target_dir.mkdir(parents=True, exist_ok=True)
                try:
                    file_handle = tmp_path.open("wb")
                    break
                except FileNotFoundError:
                    if attempt == 2:
                        raise
            with file_handle as handle:
                for chunk in response.iter_content(chunk_size=262144):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        raise RuntimeError("CDN 下载流超出单文件体积上限")
                    handle.write(chunk)
            if written <= 0 or not tmp_path.is_file() or tmp_path.stat().st_size <= 0:
                raise RuntimeError("CDN 下载内容为空")
            tmp_path.replace(final_path)
            return final_path
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        if own_session:
            client.close()


def _convert_gif_mp4(source: Path, target: Path, log: Any = None) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(source),
            "-vf",
            "fps=15,scale=iw:-1:flags=lanczos",
            "-loop",
            "0",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
        if log:
            log.write(f"GIF 转换失败，保留 MP4：{target.name}")
        target.unlink(missing_ok=True)
        return False
    source.unlink(missing_ok=True)
    return True


def download_direct_item(
    entry: dict[str, Any],
    staging_root: Path,
    config: dict[str, Any],
    log: Any = None,
    *,
    session: requests.Session | None = None,
    max_bytes: int = MAX_MEDIA_BYTES,
) -> tuple[str, list[str], str]:
    tweet_id = str(entry["tweet_id"])
    unresolvable = str(entry.get("unresolvable_reason") or "").strip()
    media_list = list(entry.get("media") or [])
    if not media_list:
        return (
            "failed",
            [],
            f"direct_unresolvable:{unresolvable or 'empty_media'}",
        )

    author = _safe_author(entry.get("author") or "")
    images_dir = staging_root / "images"
    videos_dir = staging_root / "videos"
    images_dir.mkdir(parents=True, exist_ok=True)
    videos_dir.mkdir(parents=True, exist_ok=True)

    convert_gif = bool(config.get("media", {}).get("convert_gif", True))
    user_agent = str(config.get("default_user_agent") or "")
    own_session = session is None
    client = session or requests.Session()
    downloaded_files: list[str] = []
    errors: list[str] = []

    try:
        for index, media in enumerate(media_list, start=1):
            mtype = str(media["type"])
            mkey = _safe_media_key(media.get("media_key") or "", f"m{index}")
            stem = f"{tweet_id}_{author}_{index}_{mkey}"[:180]
            out_dir = images_dir if mtype in {"photo", "animated_gif"} else videos_dir
            if mtype == "animated_gif":
                gif_stem = f"{tweet_id}_{author}_gif_{index}_{mkey}"[:180]
                stem = gif_stem
            try:
                saved = download_cdn_file(
                    media["url"],
                    mtype,
                    out_dir,
                    stem,
                    session=client,
                    max_bytes=max_bytes,
                    user_agent=user_agent,
                )
                if mtype == "animated_gif" and convert_gif and saved.suffix.lower() == ".mp4":
                    gif_path = out_dir / f"{stem}.gif"
                    if _convert_gif_mp4(saved, gif_path, log):
                        saved = gif_path
                downloaded_files.append(str(saved))
                if log:
                    log.write(f"CDN 直链下载成功：{saved.name}")
            except Exception as error:
                errors.append(sanitize(str(error)))
                if log:
                    log.write(f"CDN 直链下载失败（{tweet_id} #{index}）：{sanitize(error)}")
    finally:
        if own_session:
            client.close()

    if len(downloaded_files) < len(media_list) or not downloaded_files:
        detail = "; ".join(dict.fromkeys(e for e in errors if e)) or "部分媒体下载未完成"
        return "failed", downloaded_files, f"CDN 直链下载未完成 ({len(downloaded_files)}/{len(media_list)}): {detail}"
    return "done", downloaded_files, ""
