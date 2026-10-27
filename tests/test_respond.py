from bot.respond import fits_popup, html_to_plain
from bot.services.formatting import split_message
from tests.conftest import ALLOWED_CHAT, FOREIGN_CHAT


def test_html_to_plain():
    text = '✅ <a href="https://x.example/1">#1 a &lt;b&gt;</a> → <b>alice</b>'
    assert html_to_plain(text) == "✅ #1 a <b> → alice"


def test_popup_limit_counts_plain_utf16_length():
    assert fits_popup("<b>" + "a" * 200 + "</b>")
    assert not fits_popup("a" * 201)
    assert fits_popup("😀" * 100)  # emoji take two UTF-16 units each
    assert not fits_popup("😀" * 101)
    assert not fits_popup("")


def test_message_chunks_preserve_long_links_and_never_split_utf16_or_html_entities():
    link = '<a href="https://hack.example/' + "a" * 3900 + '">Сайт</a>'
    assert split_message(link) == [link]
    chunks = split_message("<b>" + "😀" * 2500 + "&amp;</b>")
    assert len(chunks) == 1
    assert len(html_to_plain(chunks[0]).encode("utf-16-le")) // 2 <= 3900
    assert chunks[0].endswith("…") and "<b>" not in chunks[0]


async def test_popups_are_bound_to_their_chat(storage):
    popup_id = await storage.popups.add(ALLOWED_CHAT, "secret")
    assert await storage.popups.get(popup_id, ALLOWED_CHAT) == "secret"
    assert await storage.popups.get(popup_id, FOREIGN_CHAT) is None
    assert await storage.popups.get(popup_id + 1, ALLOWED_CHAT) is None
