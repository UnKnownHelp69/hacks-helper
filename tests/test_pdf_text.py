import asyncio
import io
import os
import signal
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from bot.errors import UserError
from bot.services.hack_import import MAX_PDF_BYTES, pdf_text


@pytest.mark.parametrize("padding", [0, 1_344_069])
async def test_pdf_text_is_extracted_by_bounded_worker(padding):
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
    if padding:
        attachment = DecodedStreamObject()
        attachment.set_data(b"x" * padding)
        writer._add_object(attachment)
    output = io.BytesIO()
    writer.write(output)
    assert len(output.getvalue()) < MAX_PDF_BYTES
    assert "Hackathon team: 2 to 5 people" in await pdf_text(output.getvalue())


async def test_pdf_worker_rejects_documents_over_size_limit():
    with pytest.raises(UserError, match="PDF"):
        await pdf_text(b"%PDF" + b" " * MAX_PDF_BYTES)


async def test_invalid_pdf_does_not_crash_bot():
    with pytest.raises(UserError, match="PDF"):
        await pdf_text(b"invalid PDF data")


@pytest.mark.parametrize("cancelled", [False, True])
async def test_pdf_workers_are_killed_and_reaped_on_timeout_or_cancellation(monkeypatch, cancelled):
    from unittest.mock import AsyncMock, Mock

    process = Mock(returncode=None, pid=12345)
    process.communicate = AsyncMock(
        side_effect=asyncio.CancelledError if cancelled else TimeoutError
    )
    process.wait = AsyncMock(return_value=-9)
    process.kill = Mock()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr("bot.services.hack_import.asyncio.create_subprocess_exec", spawn)
    kill_group = Mock()
    monkeypatch.setattr("bot.services.hack_import.os.killpg", kill_group, raising=False)
    with pytest.raises(asyncio.CancelledError if cancelled else UserError):
        await pdf_text(b"%PDF")
    if os.name == "posix":
        kill_group.assert_called_once_with(process.pid, signal.SIGKILL)
    else:
        process.kill.assert_called_once()
    process.wait.assert_awaited_once()
    assert not Path(spawn.call_args.args[3]).exists()


def scanned_pdf():
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=400)
    image = DecodedStreamObject()
    image.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(1),
            NameObject("/Height"): NumberObject(1),
            NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
            NameObject("/BitsPerComponent"): NumberObject(8),
        }
    )
    image.set_data(b"\xff\xff\xff")
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): writer._add_object(image)})}
    )
    content = DecodedStreamObject()
    content.set_data(b"q 400 0 0 400 0 0 cm /Im0 Do Q")
    page[NameObject("/Contents")] = writer._add_object(content)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


async def test_scan_reports_missing_ocr_instead_of_silently_losing_document(monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(UserError, match=r"PDF-скан.*Poppler.*Tesseract"):
        await pdf_text(scanned_pdf())


@pytest.mark.skipif(os.name != "posix", reason="Fake OCR commands use POSIX scripts")
@pytest.mark.parametrize("languages", ["rus eng", "eng"])
async def test_scan_worker_uses_ocr_only_with_both_languages(tmp_path, monkeypatch, languages):
    renderer = tmp_path / "pdftoppm"
    renderer.write_text('#!/bin/sh\nfor last; do :; done\nprintf image > "$last.png"\n')
    ocr = tmp_path / "tesseract"
    ocr.write_text(
        '#!/bin/sh\nif [ "$1" = "--list-langs" ]; then\n'
        f"printf '%s\\n' {languages}\nelse\n"
        "printf 'Положение о Лиге ЦТФ. Общие положения'\nfi\n"
    )
    renderer.chmod(0o700)
    ocr.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    if languages == "eng":
        with pytest.raises(UserError, match="PDF-скан"):
            await pdf_text(scanned_pdf())
    else:
        assert "Лиге ЦТФ" in await pdf_text(scanned_pdf())


def test_ocr_renderer_bounds_resolution_and_execution_time(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from bot.services.pdf_text import ocr_page

    run = Mock(return_value=Mock(stdout="Распознанный текст".encode()))
    monkeypatch.setattr("bot.services.pdf_text.subprocess.run", run)
    assert ocr_page(tmp_path / "document.pdf", 3) == "Распознанный текст"
    render, ocr = run.call_args_list
    assert render.args[0][1:8] == ["-f", "3", "-l", "3", "-singlefile", "-scale-to", "2200"]
    assert render.kwargs["timeout"] == ocr.kwargs["timeout"] == 10
    assert ocr.args[0][-2:] == ["-l", "rus+eng"]
    assert not Path(tmp_path / "page.png").exists()
