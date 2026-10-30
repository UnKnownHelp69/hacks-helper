"""Short-lived worker for bounded text extraction from public PDF documents."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

MAX_PDF_BYTES = 2_000_000
PDF_TIMEOUT_SECONDS = 60
OCR_UNAVAILABLE = 2


def ocr_page(document: Path, page_number: int) -> str:
    """Render one bounded image at a time; all children share the worker's limits."""
    image = document.with_name("page")
    subprocess.run(
        [
            "pdftoppm",
            "-f",
            str(page_number),
            "-l",
            str(page_number),
            "-singlefile",
            "-scale-to",
            "2200",
            "-png",
            str(document),
            str(image),
        ],
        check=True,
        timeout=10,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        result = subprocess.run(
            ["tesseract", str(image.with_suffix(".png")), "stdout", "-l", "rus+eng"],
            check=True,
            timeout=10,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return result.stdout.decode("utf-8")
    finally:
        image.with_suffix(".png").unlink(missing_ok=True)


def ocr_available() -> bool:
    # The parent must be able to terminate the renderer and OCR together on cancellation.
    if os.name != "posix":
        return False
    if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
        return False
    result = subprocess.run(
        ["tesseract", "--list-langs"],
        check=True,
        timeout=5,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return {"rus", "eng"} <= set(result.stdout.decode("utf-8").splitlines())


def main() -> int:
    try:
        # Unix also bounds heap and CPU; the parent enforces wall time on all OSes.
        try:
            import resource
        except ImportError:
            pass
        else:
            with suppress(OSError, ValueError):
                resource.setrlimit(resource.RLIMIT_DATA, (256 * 1024 * 1024,) * 2)
            with suppress(OSError, ValueError):
                resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
        from pypdf import Configuration, PdfReader, apply_configuration

        apply_configuration(
            Configuration(
                maximum_declared_stream_length=10_000_000,
                array_based_stream_maximum_output_length=10_000_000,
                zlib_maximum_output_length=10_000_000,
                lzw_maximum_output_length=10_000_000,
                run_length_maximum_output_length=10_000_000,
                stream_decoding_work_maximum_length=20_000_000,
                array_maximum_element_count=10000,
                dictionary_maximum_element_count=10000,
            )
        )

        data = sys.stdin.buffer.read(MAX_PDF_BYTES + 1)
        if len(data) > MAX_PDF_BYTES:
            return 1
        reader = PdfReader(io.BytesIO(data), strict=False)
        text: list[str] = []
        remaining = 60000
        document = None
        for index, page in enumerate(reader.pages):
            if index >= 20 or remaining <= 0:
                break
            chunk = page.extract_text() or ""
            if not chunk.strip() and page.get("/Resources", {}).get("/XObject"):
                if document is None:
                    if not ocr_available():
                        return OCR_UNAVAILABLE
                    document = Path(sys.argv[1]) / "document.pdf"
                    document.write_bytes(data)
                chunk = ocr_page(document, index + 1)
            chunk = chunk[:remaining]
            text.append(chunk)
            remaining -= len(chunk)
        sys.stdout.buffer.write("\n".join(text).encode("utf-8"))
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
