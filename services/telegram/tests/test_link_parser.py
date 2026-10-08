from app.link_parser import parse_telegram_links


def test_parse_public_channel_link():
    links = parse_telegram_links("https://t.me/CosSSDZH/17530")
    assert len(links) == 1
    assert links[0].channel == "CosSSDZH"
    assert links[0].message_ids == [17530]
    assert not links[0].is_single
    assert not links[0].is_private
    assert links[0].topic_id is None


def test_parse_single_query_param():
    links = parse_telegram_links("https://t.me/CosSSDZH/17530?single")
    assert len(links) == 1
    assert links[0].is_single
    assert links[0].message_ids == [17530]


def test_parse_private_channel_link():
    links = parse_telegram_links("https://t.me/c/1234567890/17530")
    assert len(links) == 1
    assert links[0].channel == -1001234567890
    assert links[0].is_private
    assert links[0].message_ids == [17530]


def test_parse_topic_link():
    links = parse_telegram_links("https://t.me/CosSSDZH/42/17530")
    assert len(links) == 1
    assert links[0].channel == "CosSSDZH"
    assert links[0].topic_id == 42
    assert links[0].message_ids == [17530]


def test_parse_range_link():
    links = parse_telegram_links("https://t.me/CosSSDZH/100-105")
    assert len(links) == 1
    assert links[0].message_ids == [100, 101, 102, 103, 104, 105]


def test_parse_multiple_and_deduplicate():
    text = """
    First: https://t.me/CosSSDZH/17530
    Duplicate: https://t.me/CosSSDZH/17530
    Second: https://t.me/CosSSDZH/17531
    """
    links = parse_telegram_links(text)
    assert len(links) == 2
    assert links[0].message_ids == [17530]
    assert links[1].message_ids == [17531]


def test_parse_punctuation_stripping():
    text = "请下载链接：https://t.me/CosSSDZH/17530，谢谢！"
    links = parse_telegram_links(text)
    assert len(links) == 1
    assert links[0].channel == "CosSSDZH"
    assert links[0].message_ids == [17530]


def test_parse_scheme_link():
    links = parse_telegram_links("tg://resolve?domain=CosSSDZH&post=17530")
    assert len(links) == 1
    assert links[0].channel == "CosSSDZH"
    assert links[0].message_ids == [17530]


def test_parse_empty_and_non_link():
    assert parse_telegram_links("") == []
    assert parse_telegram_links("hello world") == []
    assert parse_telegram_links("https://t.me/share/url?url=xxx") == []
