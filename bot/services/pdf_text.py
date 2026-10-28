"""Short-lived worker for bounded text extraction from public PDF documents."""

from __future__ import annotations

import io
import sys
from contextlib import suppress


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

        data = sys.stdin.buffer.read(1_000_001)
        if len(data) > 1_000_000:
            return 1
        reader = PdfReader(io.BytesIO(data), strict=False)
        text: list[str] = []
        remaining = 60000
        for index, page in enumerate(reader.pages):
            if index >= 20 or remaining <= 0:
                break
            chunk = (page.extract_text() or "")[:remaining]
            text.append(chunk)
            remaining -= len(chunk)
        sys.stdout.buffer.write("\n".join(text).encode("utf-8"))
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
