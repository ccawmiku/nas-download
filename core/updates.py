from __future__ import annotations
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

# Installed revisions are explicit; compare release versions and actual source commits.
SOURCES = {
    "f2": {
        "repo": "Johnserf-Seed/f2",
        "installed": "0.0.1.7 · 5828ea7",
        "version": "0.0.1.7",
        "baseline": "7dab3e2ffffaa2535834d28fca99dbc2e89fa9d3",
        "fork": "ccawmiku/f2",
        "fork_commit": "5828ea7edb52e8148cbd8dff2c778cf305152fc9",
    },
    "xhs": {
        "repo": "JoeanAmier/XHS-Downloader",
        "installed": "2.8-nas.2",
        "version": "2.8",
        "baseline": "3261312721f0b37c705ba6515885bc7f34349f2f",
        "fork": "ccawmiku/XHS-Downloader",
        "fork_commit": "5336169fe5d2f3a6dcb171309a14d273b917286d",
    },
    "yt-dlp": {
        "repo": "yt-dlp/yt-dlp",
        "installed": "2026.8.19",
        "version": "2026.8.19",
    },
    "gallery-dl": {
        "repo": "mikf/gallery-dl",
        "installed": "1.32.13",
        "version": "1.32.13",
    },
}
LOCK = Lock()


def normalized_version(value):
    return tuple(
        int(part) if part.isdigit() else part.lower()
        for part in value.lstrip("v").split(".")
    )


def github(path):
    request = urllib.request.Request(
        "https://api.github.com/repos/" + path,
        headers={"User-Agent": "nas-download", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.load(response)


def check_one(key, spec, previous):
    item = {
        "key": key,
        "installed": spec["installed"],
        "url": "https://github.com/" + spec["repo"],
        "status": "unknown",
    }
    try:
        data = github(spec["repo"] + "/releases/latest")
        item.update(
            candidate=data["tag_name"],
            published_at=data.get("published_at"),
            url=data["html_url"],
            status="review"
            if normalized_version(data["tag_name"])
            != normalized_version(spec["version"])
            else "current",
        )
        if spec.get("baseline"):
            latest = github(spec["repo"] + "/commits?per_page=1")[0]["sha"]
            item["upstream_commit"] = latest[:7]
            branch_changed = latest != spec["baseline"]
            fork_latest = github(spec["fork"] + "/commits?per_page=1")[0]["sha"]
            fork_changed = fork_latest != spec["fork_commit"]
            item["fork_commit"] = fork_latest[:7]
            if branch_changed or fork_changed:
                item["status"] = "review"
            parts = []
            if branch_changed:
                parts.append("上游分支有变化")
            if fork_changed:
                parts.append("修复分支有变化")
            parts.append("升级时保留现有修复补丁")
            item["note"] = " · ".join(parts)
    except Exception as error:
        old = next(
            (entry for entry in previous.get("items", []) if entry["key"] == key), {}
        )
        item.update(
            candidate=item.get("candidate", old.get("candidate", "")),
            status="unavailable",
            note=type(error).__name__,
        )
    return item


def check_updates(store, force=False):
    with LOCK:
        previous = store.get("upstreams", {})
        if not force and time.time() - previous.get("checked_at", 0) < 21600:
            return previous
        with ThreadPoolExecutor(max_workers=4) as executor:
            items = list(
                executor.map(
                    lambda pair: check_one(pair[0], pair[1], previous), SOURCES.items()
                )
            )
        result = {"checked_at": time.time(), "items": items}
        store.set("upstreams", result)
        return result
