"""Strict validation and cookie-free CDN downloading for XHS direct manifests."""
from __future__ import annotations

import ipaddress
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from .config import sanitize

ALLOWED_XHS_MEDIA_SUFFIXES = (".xhscdn.com", ".xhscdn.net", ".xiaohongshu.com")
ALLOWED_XHS_NOTE_HOSTS = frozenset({"www.xiaohongshu.com", "xiaohongshu.com"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".webm"})
MAX_DIRECT_NOTES_PER_BATCH = 100
MAX_MEDIA_PER_NOTE = 50
MAX_MEDIA_BYTES = int(os.getenv("NAS_XHS_MAX_MEDIA_BYTES", str(512 * 1024 * 1024)))
DOWNLOAD_TIMEOUT = 45
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_NOTE_ID_RE = re.compile(r"^[a-f\d]{24}$", re.IGNORECASE)


def _is_allowed_media_host(host: str) -> bool:
    clean = host.lower().strip(".")
    return any(
        clean == suffix.lstrip(".") or clean.endswith(suffix)
        for suffix in ALLOWED_XHS_MEDIA_SUFFIXES
    )


def _safe_folder_segment(value: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "").strip())
    cleaned = cleaned.strip(" ._")
    return (cleaned or fallback)[:60]


def validate_xhs_note_url(raw_url: str, note_id: str) -> str:
    """Validate and normalize a Xiaohongshu note page URL."""
    text = str(raw_url or "").strip()
    if not text:
        return f"https://www.xiaohongshu.com/explore/{note_id}"
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or host not in ALLOWED_XHS_NOTE_HOSTS
        or parsed.port not in (None, 443)
    ):
        raise ValueError("小红书作品链接域名不受支持")
    clean_path = parsed.path or f"/explore/{note_id}"
    return urlunparse(("https", host, clean_path, "", parsed.query[:512], ""))


def validate_xhs_media_url(raw_url: str, media_type: str) -> str:
    """Validate a Xiaohongshu CDN media URL and upgrade http:// to https://."""
    text = str(raw_url or "").strip()
    if not text or len(text) > 2048:
        raise ValueError("小红书媒体直链无效")
    parsed = urlparse(text)
    if parsed.scheme == "http":
        parsed = parsed._replace(scheme="https")
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
    if not _is_allowed_media_host(host):
        raise ValueError("小红书媒体直链域名不在官方 CDN 白名单内")
    if media_type not in {"image", "video"}:
        raise ValueError("不支持的小红书媒体类型")
    return urlunparse(parsed)


def normalize_xhs_direct_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate and normalize browser-captured Xiaohongshu note media manifests."""
    if not isinstance(payload, dict):
        raise ValueError("请求体格式无效")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= MAX_DIRECT_NOTES_PER_BATCH:
        raise ValueError(f"每次最多提交 1–{MAX_DIRECT_NOTES_PER_BATCH} 条小红书直推清单")

    grouped: dict[str, dict[str, Any]] = {}
    for entry in raw_items:
        if not isinstance(entry, dict):
            raise ValueError("作品条目格式无效")
        note_id = str(entry.get("note_id") or entry.get("id") or "").strip().lower()
        if not _NOTE_ID_RE.match(note_id):
            raise ValueError("小红书作品 ID 格式无效")
        note_url = validate_xhs_note_url(str(entry.get("url") or ""), note_id)
        title = sanitize(str(entry.get("title") or ""))[:500]
        author = sanitize(str(entry.get("author") or ""))[:80]
        category = str(entry.get("category") or "").strip().lower()
        if category not in {"", "liked", "saved"}:
            raise ValueError("保存分类无效")
        raw_media = entry.get("media")
        if not isinstance(raw_media, list) or not 1 <= len(raw_media) <= MAX_MEDIA_PER_NOTE:
            raise ValueError("每条作品需包含 1–50 个媒体直链")

        media_list: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for item in raw_media:
            if not isinstance(item, dict):
                raise ValueError("媒体条目格式无效")
            mtype = str(item.get("type") or "").strip().lower()
            if mtype in {"photo", "img"}:
                mtype = "image"
            if mtype not in {"image", "video"}:
                raise ValueError("小红书直推仅支持 image 或 video 媒体")
            clean_url = validate_xhs_media_url(str(item.get("url") or ""), mtype)
            if clean_url in seen_urls:
                continue
            seen_urls.add(clean_url)
            media_list.append({"type": mtype, "url": clean_url})
        if not media_list:
            raise ValueError("作品中缺少有效媒体直链")

        if note_id not in grouped:
            grouped[note_id] = {
                "note_id": note_id,
                "url": note_url,
                "title": title,
                "author": author,
                "category": category,
                "media": media_list,
            }
    return list(grouped.values())


def _target_extension(url: str, media_type: str, content_type: str) -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    suffix = Path(urlparse(url).path).suffix.lower()
    if media_type == "video":
        if "quicktime" in ctype or suffix == ".mov":
            return ".mov"
        if suffix in VIDEO_EXTENSIONS:
            return suffix
        return ".mp4"
    if "png" in ctype or suffix == ".png":
        return ".png"
    if "webp" in ctype or suffix == ".webp":
        return ".webp"
    if "gif" in ctype or suffix == ".gif":
        return ".gif"
    if "avif" in ctype or suffix == ".avif":
        return ".avif"
    if suffix in IMAGE_EXTENSIONS:
        return suffix
    return ".jpg"


def _expected_content_type(media_type: str, content_type: str) -> bool:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if not ctype:
        return True
    if media_type == "image":
        return ctype.startswith("image/") or ctype == "application/octet-stream"
    return ctype.startswith("video/") or ctype == "application/octet-stream"


def _write_stream_chunks(chunks, target_dir: Path, stem: str, ext: str, max_bytes: int) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    final_path = target_dir / f"{stem}{ext}"
    if final_path.is_file() and final_path.stat().st_size > 0:
        return final_path
    tmp_path = final_path.with_suffix(final_path.suffix + ".part")
    written = 0
    try:
        with tmp_path.open("wb") as handle:
            for chunk in chunks:
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise RuntimeError("CDN 下载流超出单文件体积上限")
                handle.write(chunk)
        if written <= 0:
            raise RuntimeError("CDN 下载内容为空")
        tmp_path.replace(final_path)
        return final_path
    finally:
        tmp_path.unlink(missing_ok=True)


def _stream_with_httpx(
    start_url: str,
    media_type: str,
    headers: dict[str, str],
    target_dir: Path,
    stem: str,
    max_bytes: int,
) -> Path:
    import httpx

    url = validate_xhs_media_url(start_url, media_type)
    with httpx.Client(follow_redirects=False, timeout=DOWNLOAD_TIMEOUT) as client:
        for _ in range(4):
            with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code in {301, 302, 303, 307, 308}:
                    loc = resp.headers.get("location") or ""
                    if not loc:
                        raise RuntimeError("CDN 重定向缺少目标地址")
                    url = validate_xhs_media_url(urljoin(url, loc), media_type)
                    continue
                if resp.status_code != 200:
                    raise RuntimeError(f"CDN 返回 HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "")
                if not _expected_content_type(media_type, ctype):
                    raise RuntimeError(f"CDN 响应类型不符: {ctype}")
                raw_len = resp.headers.get("content-length")
                if raw_len and raw_len.isdigit() and int(raw_len) > max_bytes:
                    raise RuntimeError("CDN 文件超出单文件体积上限")
                ext = _target_extension(url, media_type, ctype)
                return _write_stream_chunks(
                    resp.iter_bytes(chunk_size=262144), target_dir, stem, ext, max_bytes
                )
    raise RuntimeError("CDN 重定向次数过多")


def _stream_with_urllib(
    start_url: str,
    media_type: str,
    headers: dict[str, str],
    target_dir: Path,
    stem: str,
    max_bytes: int,
) -> Path:
    class _ValidatedRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            safe_url = validate_xhs_media_url(newurl, media_type)
            return super().redirect_request(req, fp, code, msg, hdrs, safe_url)

    def _iter_resp(resp):
        while True:
            chunk = resp.read(262144)
            if not chunk:
                break
            yield chunk

    opener = urllib.request.build_opener(_ValidatedRedirect)
    req = urllib.request.Request(
        validate_xhs_media_url(start_url, media_type),
        headers=headers,
        method="GET",
    )
    with opener.open(req, timeout=DOWNLOAD_TIMEOUT) as resp:
        ctype = resp.headers.get("content-type", "")
        if not _expected_content_type(media_type, ctype):
            raise RuntimeError(f"CDN 响应类型不符: {ctype}")
        ext = _target_extension(resp.geturl() or start_url, media_type, ctype)
        return _write_stream_chunks(_iter_resp(resp), target_dir, stem, ext, max_bytes)


def download_xhs_direct_item(
    entry: dict[str, Any],
    dest_dir: Path,
    *,
    max_bytes: int = MAX_MEDIA_BYTES,
) -> tuple[str, list[str], str]:
    """Download all validated CDN media files for one Xiaohongshu note."""
    note_id = str(entry.get("note_id") or "").strip().lower()
    if not _NOTE_ID_RE.match(note_id):
        return "failed", [], "作品 ID 无效"
    folder_label = _safe_folder_segment(str(entry.get("title") or ""), note_id)
    note_dir = dest_dir / f"{note_id}_{folder_label}"
    headers = {
        "User-Agent": BROWSER_UA,
        "Referer": "https://www.xiaohongshu.com/",
        "Accept": "*/*",
    }
    try:
        import httpx  # noqa: F401
        streamer = _stream_with_httpx
    except Exception:
        streamer = _stream_with_urllib

    saved_files: list[str] = []
    media_list = entry.get("media") or []
    for idx, media in enumerate(media_list, start=1):
        mtype = str(media.get("type") or "image").lower()
        url = str(media.get("url") or "")
        stem = f"{note_id}_{idx:02d}"
        try:
            saved = streamer(url, mtype, headers, note_dir, stem, max_bytes)
            saved_files.append(str(saved))
        except (ValueError, urllib.error.HTTPError, urllib.error.URLError, Exception) as exc:
            return "failed", saved_files, str(exc) or "小红书媒体直链下载失败"
    if not saved_files:
        return "failed", [], "未下载到任何小红书媒体文件"
    return "done", saved_files, ""
