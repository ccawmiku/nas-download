"""Source-local stopping, independent of upstream progress/log wording."""

import json

from .client import Client
from core.config import THRESHOLDS


class KnownRun:
    def __init__(self, threshold):
        self.threshold = int(threshold)
        self.seen = set()
        self.consecutive = 0

    def observe(self, identifier, known):
        identifier = str(identifier)
        if identifier in self.seen:
            return False
        self.seen.add(identifier)
        self.consecutive = self.consecutive + 1 if known else 0
        return self.consecutive >= self.threshold


def lookup(platform, identifiers, client=None):
    identifiers = list(dict.fromkeys(str(i) for i in identifiers))
    result = {}
    client = client or Client()
    for offset in range(0, len(identifiers), 100):
        result.update(
            client.request(
                "/internal/records/lookup",
                {
                    "platform": platform,
                    "source_ids": identifiers[offset : offset + 100],
                },
            )["records"]
        )
    return result


def completed(value):
    return bool(
        value
        and value["state"] == "complete"
        and value.get("files")
        and value.get("metadata", {}).get("legacy_status", "done") == "done"
    )


def legacy_completed(platform, store, identifier):
    getter = store.get_tweet if platform == "x" else store.get_artwork
    value = getter(identifier)
    if not value or value["status"] != "done":
        return False
    try:
        return bool(json.loads(value["files_json"] or "[]"))
    except (ValueError, KeyError, IndexError):
        return False


def threshold(platform, config):
    key, default = THRESHOLDS[platform]
    return max(1, int(config.get(key) or default))


def summary(source, reason, guard, last_id=""):
    return {
        "source": source,
        "stop_reason": reason,
        "known_consecutive": guard.consecutive,
        "known_threshold": guard.threshold,
        "last_id": last_id,
    }
