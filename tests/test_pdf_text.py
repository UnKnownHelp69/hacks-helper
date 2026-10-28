import io

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from bot.errors import UserError
from bot.services.hack_import import pdf_text


async def test_pdf_text_is_extracted_by_bounded_worker():
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=400)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 40 200 Td (Hackathon team: 2 to 5 people) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    output = io.BytesIO()
    writer.write(output)
    assert "Hackathon team: 2 to 5 people" in await pdf_text(output.getvalue())


async def test_invalid_pdf_does_not_crash_bot():
    with pytest.raises(UserError, match="PDF"):
        await pdf_text(b"invalid PDF data")


async def test_pdf_worker_is_killed_and_reaped_on_timeout(monkeypatch):
    from unittest.mock import AsyncMock, Mock

    process = Mock(returncode=None)
    process.communicate = AsyncMock(side_effect=TimeoutError)
    process.wait = AsyncMock(return_value=-9)
    process.kill = Mock()
    monkeypatch.setattr(
        "bot.services.hack_import.asyncio.create_subprocess_exec", AsyncMock(return_value=process)
    )
    with pytest.raises(UserError, match="слишком сложный"):
        await pdf_text(b"%PDF")
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()
