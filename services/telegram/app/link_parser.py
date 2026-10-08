from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence


@dataclass
class ParsedTelegramLink:
    raw_url: str
    channel: str | int  # Public username (str) or private channel ID (int, -100...)
    message_ids: list[int]
    is_single: bool
    is_private: bool
    topic_id: int | None = None

    def display_name(self) -> str:
        if self.is_private:
            return f"私有频道 {abs(self.channel) - 1000000000000 if isinstance(self.channel, int) and abs(self.channel) > 1000000000000 else self.channel}"
        return f"@{self.channel}" if isinstance(self.channel, str) else str(self.channel)


# Matches:
# 1) https://t.me/username/123
# 2) https://t.me/username/123?single
# 3) https://t.me/username/12/123 (with topic_id)
# 4) https://t.me/username/123-128 (range)
# 5) https://t.me/c/1234567890/123 (private channel)
# 6) https://t.me/c/1234567890/12/123 (private channel with topic)
# 7) https://t.me/b/botname/123 (bot message)
# 8) t.me/... (without https://)
# 9) tg://resolve?domain=username&post=123
TG_HTTP_LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:c/(\d+)|b/([A-Za-z0-9_]+)|([A-Za-z0-9_]+))"
    r"(?:/(\d+))?/(\d+)(?:-(\d+))?(?:\?([^\s]+))?",
    re.IGNORECASE,
)

TG_SCHEME_LINK_RE = re.compile(
    r"tg://resolve\?domain=([A-Za-z0-9_]+)&post=(\d+)(?:&single)?",
    re.IGNORECASE,
)


def parse_telegram_links(text: str, max_range_span: int = 100) -> list[ParsedTelegramLink]:
    if not text or not text.strip():
        return []

    results: list[ParsedTelegramLink] = []
    seen: set[tuple[str | int, int]] = set()

    # 1. Parse standard http/https links
    for match in TG_HTTP_LINK_RE.finditer(text):
        raw_match = match.group(0).rstrip(".,;:!?)>\"'，。；！？）》」』")
        private_id, bot_name, public_name, part1, part2, range_end, query = match.groups()

        # Channel identification
        if private_id:
            channel: str | int = int(f"-100{private_id}")
            is_private = True
        elif bot_name:
            channel = bot_name
            is_private = False
        elif public_name:
            # Exclude non-channel special paths like share, joinchat, addstickers, etc.
            if public_name.lower() in {"share", "joinchat", "addstickers", "addtheme", "setlanguage", "invoice"}:
                continue
            channel = public_name
            is_private = False
        else:
            continue

        # Message ID and topic handling
        # If both part1 and part2 are present, part1 is topic_id, part2 is msg_id
        # If only part2 is present (part1 is None), part2 is msg_id
        if part1 is not None and part2 is not None:
            topic_id: int | None = int(part1)
            start_msg_id = int(part2)
        else:
            topic_id = None
            start_msg_id = int(part2)

        # Range handling
        if range_end:
            end_msg_id = int(range_end)
            if end_msg_id < start_msg_id:
                start_msg_id, end_msg_id = end_msg_id, start_msg_id
            span = min(end_msg_id - start_msg_id + 1, max_range_span)
            msg_ids = [start_msg_id + i for i in range(span)]
        else:
            msg_ids = [start_msg_id]

        is_single = bool(query and "single" in query.lower())

        # Filter out duplicates
        unique_ids = [mid for mid in msg_ids if (channel, mid) not in seen]
        for mid in unique_ids:
            seen.add((channel, mid))

        if unique_ids:
            results.append(
                ParsedTelegramLink(
                    raw_url=raw_match,
                    channel=channel,
                    message_ids=unique_ids,
                    is_single=is_single,
                    is_private=is_private,
                    topic_id=topic_id,
                )
            )

    # 2. Parse tg:// scheme links
    for match in TG_SCHEME_LINK_RE.finditer(text):
        raw_match = match.group(0)
        domain, post_id = match.groups()
        mid = int(post_id)
        if (domain, mid) not in seen:
            seen.add((domain, mid))
            is_single = "single" in raw_match.lower()
            results.append(
                ParsedTelegramLink(
                    raw_url=raw_match,
                    channel=domain,
                    message_ids=[mid],
                    is_single=is_single,
                    is_private=False,
                    topic_id=None,
                )
            )

    return results
