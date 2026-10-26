from bot.respond import fits_popup, html_to_plain
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


async def test_popups_are_bound_to_their_chat(storage):
    popup_id = await storage.popups.add(ALLOWED_CHAT, "secret")
    assert await storage.popups.get(popup_id, ALLOWED_CHAT) == "secret"
    assert await storage.popups.get(popup_id, FOREIGN_CHAT) is None
    assert await storage.popups.get(popup_id + 1, ALLOWED_CHAT) is None
