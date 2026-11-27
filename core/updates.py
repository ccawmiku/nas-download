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
        "installed": "v0.0.1.8-pw3 · f6be8c0",
        "version": "0.0.1.7",
        "branch": "v0.0.1.8-pw3",
        "baseline": "f6be8c0ffba9a127075bbeafe4838716650b6325",
    },
    "xhs": {
        "repo": "JoeanAmier/XHS-Downloader",
        "installed": "master · e4fc8a6",
        "version": "2.8",
        "baseline": "e4fc8a61630bbe59c5002e1461302ff90eb1172b",
    },
    "yt-dlp": {
        "repo": "yt-dlp/yt-dlp",
        "installed": "2026.10.26",
        "version": "2026.10.26",
    },
    "gallery-dl": {
        "repo": "mikf/gallery-dl",
        "installed": "1.32.19",
        "version": "1.32.19",
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
            commit_query = (
                f"{spec['repo']}/commits?sha={spec['branch']}&per_page=1"
                if spec.get("branch")
                else f"{spec['repo']}/commits?per_page=1"
            )
            commits = github(commit_query)
            latest = commits[0]["sha"] if commits else spec["baseline"]
            item["upstream_commit"] = latest[:7]
            branch_changed = latest != spec["baseline"]
            fork_changed = False
            if spec.get("fork"):
                fork_commits = github(spec["fork"] + "/commits?per_page=1")
                fork_latest = (
                    fork_commits[0]["sha"] if fork_commits else spec["fork_commit"]
                )
                fork_changed = fork_latest != spec["fork_commit"]
                item["fork_commit"] = fork_latest[:7]
            if branch_changed or fork_changed:
                item["status"] = "review"
            parts = []
            if branch_changed:
                parts.append("上游分支有变化")
            if fork_changed:
                parts.append("修复分支有变化")
            if spec.get("fork"):
                parts.append("升级时保留现有修复补丁")
            elif not branch_changed:
                parts.append("已与上游分支同步")
            if parts:
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
