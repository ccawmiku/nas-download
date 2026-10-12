"""Validation shared by the standalone console and Instagram executor."""

import math


def validate_cookies(text):
    cookies = {}
    for line in text.splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            raise ValueError("请粘贴 Netscape Cookies 文件内容，每条 Cookie 使用制表符分隔")
        if parts[0].lstrip(".") in {"instagram.com", "www.instagram.com"}:
            cookies[parts[5]] = parts[6]
    if not cookies.get("sessionid") or not cookies.get("ds_user_id", "").isdigit():
        raise ValueError("Instagram Cookies 须包含 sessionid 和数字 ds_user_id")
    return cookies["ds_user_id"]


def validate_settings(value):
    for name in ("request", "download"):
        low = value.get("sleep_" + name + "_min", 30 if name == "request" else 5)
        high = value.get("sleep_" + name + "_max", 60 if name == "request" else 10)
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (low, high)):
            raise ValueError("请求与下载间隔须为有效数字")
        if not 1 <= low <= high <= 3600:
            raise ValueError("请求与下载间隔须为 1–3600 秒，最小值不能大于最大值")
    attempts = value.get("max_download_attempts", 3)
    if type(attempts) is not int or not 1 <= attempts <= 100:
        raise ValueError("自动下载尝试上限须为 1–100 的整数")
    if type(value.get("retry_failed", True)) is not bool:
        raise ValueError("自动重试开关无效")
    browser = value.get("browser", "chrome")
    if not isinstance(browser, str) or not browser or len(browser) > 128 or "\n" in browser:
        raise ValueError("请填写导出 Cookies 时使用的浏览器类型")
