"""Saved-only adapter for gallery-dl 1.32.16; keep private upstream APIs here.

The core catalog owns completion. A local manifest owns partially downloaded
files, so interrupted carousels can resume without trusting arbitrary filenames.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from core.config import CONFIG_PATHS, read_json, write_json
from core.instagram import validate_cookies, validate_settings
from .client import Client, prepare, record, submit
from .stopping import KnownRun, lookup, summary, threshold


class SourceError(RuntimeError):
    """An upstream/authentication error must stop the whole run."""


def failure_reason(error):
    """Only controlled messages enter records/logs; never signed URLs or cookies."""
    if isinstance(error, SourceError):
        return str(error)
    if isinstance(error, requests.HTTPError):
        code = error.response.status_code if error.response is not None else 0
        return f"媒体请求失败（HTTP {code}），文件保留供重试"
    if isinstance(error, (requests.Timeout, requests.ConnectionError)):
        return "媒体下载网络异常或超时，文件保留供重试"
    if isinstance(error, FileExistsError):
        return "归档文件已存在但未通过归属校验，未覆盖原文件"
    safe = {"媒体响应类型不匹配", "媒体文件为空或下载不完整", "收藏媒体不完整，保留失败记录供重试", "收藏图片格式不受支持，请更新下载器", "媒体实际格式不受支持"}
    if isinstance(error, ValueError) and str(error) in safe:
        return str(error)
    return "媒体下载或文件校验失败，已下载部分保留供重试"


def media_url(url):
    host = urlsplit(url).hostname or ""
    if urlsplit(url).scheme != "https" or not any(
        host.endswith("." + domain) for domain in ("cdninstagram.com", "fbcdn.net")
    ):
        raise SourceError("Instagram 返回了无效的媒体地址，本次停止")
    return url


class GallerySource:
    def __init__(self, settings):
        from gallery_dl import config, extractor, version

        if version.__version__ != "1.32.16":
            raise SourceError("Instagram 适配器需要 gallery-dl 1.32.16")
        self.account = validate_cookies(Path(settings["cookie_file"]).read_text(encoding="utf-8-sig"))
        config.clear()
        for key, value in {
            "cookies": settings["cookie_file"], "browser": settings.get("browser", "chrome"),
            "sleep-request": [settings.get("sleep_request_min", 30), settings.get("sleep_request_max", 60)],
            "retries": 0, "timeout": 30, "proxy": settings.get("proxy") or None,
            "audio": False, "videos": True, "warn-images": False, "warn-videos": False,
        }.items():
            config.set(("extractor", "instagram"), key, value)
        self.extractor = extractor.find(f"https://www.instagram.com/id:{self.account}/saved/all-posts/")
        self.extractor.initialize()
        if not self.extractor.cookies_check(("sessionid", "ds_user_id")):
            raise SourceError("Instagram Cookies 已过期或无效，请重新导出")
        self.extractor.login()
        self.extractor.videos_dash = False
        # Download requests never carry the Instagram login cookies.
        self.session = requests.Session()
        self.session.headers["User-Agent"] = self.extractor.session.headers["User-Agent"]
        self.session.proxies.update(self.extractor.session.proxies)

    def call(self, endpoint, **kwargs):
        try:
            data = self.extractor.api._call(endpoint, **kwargs)
        except Exception as exc:
            raise SourceError("Instagram 请求失败（登录异常、限流或网络错误），本次停止；请检查账号后再运行") from exc
        if not isinstance(data, dict) or data.get("status") != "ok" or any(
            data.get(key) for key in ("challenge", "challenge_required", "checkpoint_url", "feedback_required", "login_required")
        ):
            raise SourceError("Instagram 返回登录或风控异常，本次停止")
        return data

    def pages(self):
        cursor, cursors, seen = "", set(), set()
        while True:
            data = self.call("/v1/feed/saved/posts/", params={"count": 50, "max_id": cursor})
            if type(data.get("more_available")) is not bool or not isinstance(data.get("items"), list):
                raise SourceError("Instagram 收藏分页不完整，未更新同步边界")
            items = []
            for wrapper in data["items"]:
                post = wrapper.get("media") if isinstance(wrapper, dict) else None
                if not isinstance(post, dict) or not str(post.get("pk", "")).isdigit():
                    raise SourceError("Instagram 收藏缺少作品 ID，未更新同步边界")
                items.append(post)
            more = data["more_available"]
            next_cursor = data.get("next_max_id")
            ids = {str(p["pk"]) for p in items}
            if more and (not ids - seen or not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor or next_cursor in cursors):
                raise SourceError("Instagram 收藏分页重复或缺少游标，未更新同步边界")
            seen.update(ids)
            yield items, more
            if not more:
                return
            cursors.add(next_cursor)
            cursor = next_cursor

    def refresh(self, identifier):
        data = self.call(f"/v1/media/{identifier}/info/")
        items = data.get("items")
        if not isinstance(items, list) or len(items) != 1 or str(items[0].get("pk")) != identifier:
            raise SourceError("Instagram 重试详情不完整，本次停止")
        return items[0]

    def media(self, post):
        try:
            data = self.extractor._parse_post(post)
            files = [item for item in data["_files"] if not item.get("audio_url")]
            expected = len(post.get("carousel_media") or [post])
            if len(files) != expected or post.get("carousel_media_count", expected) != expected:
                raise ValueError("incomplete carousel")
            result = []
            for item in files:
                identifier = str(item["media_id"])
                if not identifier.isdigit():
                    raise ValueError("invalid media id")
                video = bool(item.get("video_url"))
                url = media_url(item["video_url"] if video else item["display_url"])
                extension = "mp4" if video else Path(urlsplit(url).path).suffix.lstrip(".").lower()
                if extension not in {"mp4", "jpg", "jpeg", "png", "webp", "heic", "heif"}:
                    raise ValueError("unsupported extension")
                result.append({"id": identifier, "url": url, "kind": "videos" if video else "images", "name": f'{post["pk"]}_{identifier}.{extension}'})
            if len({item["id"] for item in result}) != expected:
                raise ValueError("duplicate media id")
            return result
        except SourceError:
            raise
        except Exception as exc:
            if isinstance(exc, ValueError) and str(exc) == "unsupported extension":
                raise ValueError("收藏图片格式不受支持，请更新下载器") from exc
            raise ValueError("收藏媒体不完整，保留失败记录供重试") from exc

    def download(self, item, target, journal):
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name("." + target.name + ".part")
        try:
            with self.session.get(item["url"], stream=True, timeout=(15, 60), allow_redirects=False) as response:
                if response.status_code in {401, 403, 429}:
                    raise SourceError("Instagram 媒体访问被拒绝或限流，本次停止")
                response.raise_for_status()
                prefix = "video/" if item["kind"] == "videos" else "image/"
                if not response.headers.get("Content-Type", "").lower().startswith(prefix):
                    raise ValueError("媒体响应类型不匹配")
                digest, size = hashlib.sha256(), 0
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(1024 * 256):
                        if chunk:
                            output.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                length = response.headers.get("Content-Length")
                if not size or (length and not response.headers.get("Content-Encoding") and size != int(length)):
                    raise ValueError("媒体文件为空或下载不完整")
                if item["kind"] == "videos":
                    from core.media import probe, validate_video
                    validate_video(temporary, probe(temporary))
                else:
                    from PIL import Image
                    if target.suffix.lower() in {".heic", ".heif"}:
                        from pillow_heif import register_heif_opener
                        register_heif_opener(decode_threads=1, thumbnails=False)
                    with Image.open(temporary) as image:
                        extensions = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "HEIF": ".heic"}
                        extension = extensions.get(image.format)
                        if not extension:
                            raise ValueError("媒体实际格式不受支持")
                        # Instagram CDN can return JPEG bytes for a .heic URL.
                        # Publish with the decoded format, without re-encoding.
                        target = target.with_suffix(extension)
                        if image.format == "HEIF":
                            # HEIF's verify() does not decode image data. Load the
                            # primary image to reject truncated/corrupt payloads.
                            image.load()
                        image.verify()
                entry = {"source": str(target), "size": size, "sha256": digest.hexdigest()}
                # Journal ownership before publication, closing the crash window
                # between creating the final file and saving its resume manifest.
                if target.exists():
                    raise FileExistsError("归档文件已存在，拒绝覆盖")
                journal(entry)
                # Publish only if the destination is absent; never overwrite user files.
                os.link(temporary, target)
                return entry
        finally:
            temporary.unlink(missing_ok=True)


def owned(entry):
    path = Path(entry.get("source", ""))
    if not path.is_file() or not entry.get("size") or path.stat().st_size != entry["size"]:
        return False
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest() == entry.get("sha256")


def complete(value, expected):
    if not value or value["state"] != "complete" or value.get("metadata", {}).get("download_incomplete", True):
        return False
    metadata = value.get("metadata", {})
    if set(metadata.get("media_ids", [])) != set(expected):
        return False
    files = value.get("files", [])
    checks = metadata.get("file_checks", {})
    return len(files) == len(expected) and all(
        owned(checks[f]) if f in checks else Path(f).is_file() and Path(f).stat().st_size > 0
        for f in files
    )


class SavedJob:
    def __init__(self, job, settings, source, client=None):
        self.job, self.settings, self.source = job, settings, source
        self.client = client or Client()
        self.key = "saved-" + source.account
        self.manifests = Path(settings["state_dir"]) / "posts"
        root, self.staged = prepare("instagram", Path(settings["image_dir"]).parent, job["id"])
        self.roots = {"images": root / "images", "videos": root / "videos"} if self.staged else {"images": Path(settings["image_dir"]), "videos": Path(settings["video_dir"])}
        self.stats = {"discovered": 0, "downloaded": 0, "skipped": 0, "failed": 0, "processing": 0}
        self.attempted = set()

    def process(self, post, previous=None, manual=False):
        identifier = str(post["pk"])
        self.attempted.add(identifier)
        manifest_path = self.manifests / (identifier + ".json")
        manifest = read_json(manifest_path, {"media": {}})
        metadata = {**(previous or {}).get("metadata", {}), "account_id": self.source.account,
                    "download_incomplete": True, "attempts": int((previous or {}).get("metadata", {}).get("attempts", 0)) + 1}
        files = []
        record("instagram", identifier, "failed", files, metadata)
        pending = False
        checks = {}
        try:
            media = self.source.media(post)
            metadata["media_ids"] = [item["id"] for item in media]
            metadata["url"] = f'https://www.instagram.com/p/{post.get("code", "")}/'
            for item in media:
                entry = manifest["media"].get(item["id"], {})
                target = Path(self.settings["image_dir" if item["kind"] == "images" else "video_dir"]) / item["name"]
                # Reuse completed catalog outputs, including transcoded extensions.
                old_checks = (previous or {}).get("metadata", {}).get("file_checks", {})
                existing = next((f for f in (previous or {}).get("files", []) if Path(f).stem == Path(item["name"]).stem and Path(f).is_file() and Path(f).stat().st_size > 0 and (f not in old_checks or owned(old_checks[f]))), None)
                if existing:
                    target = target.with_suffix(Path(existing).suffix)
                    files.append(existing)
                    if Path(existing).parent != target.parent:
                        submit("instagram", existing, target, {"source_id": identifier, "metadata": metadata})
                        pending = True
                    elif existing in old_checks:
                        checks[existing] = old_checks[existing]
                    continue
                if not owned(entry):
                    time.sleep(random.uniform(self.settings.get("sleep_download_min", 5), self.settings.get("sleep_download_max", 10)))
                    def journal(value):
                        manifest["media"][item["id"]] = value
                        write_json(manifest_path, manifest)
                    entry = self.source.download(item, self.roots[item["kind"]] / item["name"], journal)
                path = Path(entry["source"])
                target = target.with_suffix(path.suffix)
                if path != target:
                    submit("instagram", path, target, {"source_id": identifier, "metadata": metadata})
                    pending = True
                else:
                    checks[str(path)] = entry
                files.append(str(path))
                metadata["file_checks"] = checks
                record("instagram", identifier, "failed", files, metadata)
            metadata["download_incomplete"] = False
            metadata.pop("error", None)
            record("instagram", identifier, "processing" if pending else "complete", files, metadata)
            self.stats["processing" if pending else "downloaded"] += 1
        except Exception as exc:
            metadata["error"] = failure_reason(exc)
            record("instagram", identifier, "failed", files, metadata)
            self.stats["failed"] += 1
            self.client.log("instagram", f"收藏 {identifier}：{metadata['error']}", self.job["id"], "error")
            if isinstance(exc, SourceError):
                raise
            print(json.dumps({"phase": "download_failed", "source_id": identifier, "error": metadata["error"]}), flush=True)

    def retry(self, manual=False, urls=None):
        page = 1
        candidates = []
        # Snapshot first: mutating record states during offset pagination skips rows.
        while True:
            values = self.client.request(f"/internal/failed/instagram?page={page}")["items"]
            candidates.extend(values)
            if len(values) < 100:
                break
            page += 1
        for value in candidates:
            meta = value.get("metadata", {})
            identifier = str(value["source_id"])
            if meta.get("account_id") != self.source.account or not identifier.isdigit():
                continue
            if urls is not None and meta.get("url") not in urls:
                continue
            if meta.get("download_incomplete") is False:
                # Download is finished; conversion failures belong to the workspace
                # retry action and must not cause fresh Instagram requests.
                continue
            if not manual and meta.get("attempts", 0) >= self.settings.get("max_download_attempts", 3):
                continue
            self.process(self.source.refresh(identifier), value, manual)

    def sync(self):
        checkpoint = self.client.request("/internal/checkpoint/instagram/" + self.key).get("head", "")
        guard = KnownRun(threshold("instagram", self.settings))
        head, last, reason, bounded, incomplete = "", "", "end", False, bool(self.stats["failed"] or self.stats["processing"])
        for items, more in self.source.pages():
            values = lookup("instagram", [str(p["pk"]) for p in items], self.client)
            for post in items:
                identifier = str(post["pk"])
                if identifier in guard.seen:
                    continue
                head, last = head or identifier, identifier
                self.stats["discovered"] += 1
                expected = [str(item["pk"]) for item in post.get("carousel_media") or [post]]
                previous = values.get(identifier)
                catalog_complete = post.get("carousel_media_count", len(expected)) == len(expected) and complete(previous, expected)
                known = catalog_complete and identifier not in self.attempted
                if known:
                    self.stats["skipped"] += 1
                elif identifier in self.attempted and catalog_complete:
                    self.stats["skipped"] += 1
                elif previous and previous["state"] == "deleted":
                    self.stats["skipped"] += 1
                elif identifier in self.attempted or (previous and previous["state"] == "processing") or (previous and previous["state"] == "failed" and not self.settings.get("retry_failed", True)) or (previous and previous.get("metadata", {}).get("attempts", 0) >= self.settings.get("max_download_attempts", 3)):
                    incomplete = True
                    self.stats["skipped"] += 1
                    if identifier not in self.attempted:
                        self.stats["processing" if previous["state"] == "processing" else "failed"] += 1
                else:
                    self.process(post, previous)
                    incomplete |= bool(self.stats["failed"] or self.stats["processing"])
                reached = guard.observe(identifier, known)
                if known and identifier == checkpoint:
                    reason, bounded = "checkpoint", True
                elif reached:
                    reason, bounded = "known_limit", True
                print(json.dumps({**self.stats, **summary(self.key, reason, guard, last), "phase": "syncing"}), flush=True)
                if bounded:
                    break
            if bounded or not more:
                bounded = True
                break
        if not bounded:
            raise SourceError("Instagram 未到达可靠的停止边界")
        self.stats.update(summary(self.key, reason, guard, last))
        self.stats["checkpoint_updated"] = not incomplete
        if not incomplete:
            self.client.request("/internal/checkpoint/instagram/" + self.key, {"head": head or "__empty__"})
        return self.stats


def run(job):
    settings = read_json(CONFIG_PATHS["instagram"])
    validate_settings(settings)
    try:
        source = GallerySource(settings)
        if job["kind"] == "test":
            items, _ = next(source.pages())
            return {"phase": "connected", "discovered": len(items), "failed": 0}
        runner = SavedJob(job, settings, source)
        if job["kind"] == "retry" or settings.get("retry_failed", True):
            urls = job.get("payload", {}).get("urls") if job["kind"] == "retry" else None
            if urls is not None and (not isinstance(urls, list) or any(not isinstance(url, str) for url in urls)):
                raise ValueError("invalid retry selection")
            runner.retry(job["kind"] == "retry", urls)
        return runner.stats if job["kind"] == "retry" else runner.sync()
    except SourceError as exc:
        # Never print upstream exception text: it can contain cookies or signed URLs.
        raise RuntimeError(str(exc)) from None
    except Exception:
        raise RuntimeError("Instagram 执行失败，未更新同步边界；请检查配置、Cookies 与网络") from None
