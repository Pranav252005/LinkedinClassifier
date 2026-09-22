"""Turn an uploaded resume (PDF or image) into plain text.

PDFs with a real text layer are read locally with pypdf — free and instant.
Images, and PDFs that turn out to be scans, go to a vision model on OpenRouter.
"""

from __future__ import annotations

import base64
import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from openrouter import OpenRouterError, read_image

IMAGE_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/webp": "webp",
    "image/gif": "gif",
}

# Below this, a "text layer" is almost certainly just page furniture on a scan.
MIN_TEXT_LAYER_CHARS = 180
MAX_RESUME_CHARS = 20_000

OCR_INSTRUCTION = (
    "This image is someone's resume. Transcribe all of its text as plain text, "
    "preserving section headings and the order things appear in. Do not summarize, "
    "add commentary, or invent details. Output only the transcription."
)


class ResumeError(RuntimeError):
    pass


def _clean(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    blank = 0
    for line in lines:
        if line.strip():
            blank = 0
            out.append(line)
        else:
            blank += 1
            if blank < 2:
                out.append("")
    return "\n".join(out).strip()[:MAX_RESUME_CHARS]


def extract_pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:  # pypdf raises several types here
                raise ResumeError("That PDF is password-protected.") from exc
        pages = [(page.extract_text() or "") for page in reader.pages]
    except ResumeError:
        raise
    except (PdfReadError, ValueError, OSError) as exc:
        raise ResumeError(f"Could not read that PDF: {exc}") from exc
    return _clean("\n\n".join(pages))


async def extract(data: bytes, content_type: str, filename: str = "") -> tuple[str, str]:
    """Return (resume_text, how_it_was_read)."""
    content_type = (content_type or "").split(";")[0].strip().lower()
    name = filename.lower()

    if content_type == "application/pdf" or name.endswith(".pdf"):
        text = extract_pdf_text(data)
        if len(text) >= MIN_TEXT_LAYER_CHARS:
            return text, "pdf-text-layer"
        raise ResumeError(
            "That PDF has no readable text layer — it looks like a scan. "
            "Export a text PDF, or upload it as a PNG/JPG image instead."
        )

    if content_type in IMAGE_TYPES or name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        subtype = IMAGE_TYPES.get(content_type, "png")
        data_url = f"data:image/{subtype};base64," + base64.b64encode(data).decode("ascii")
        try:
            text = _clean(await read_image(data_url, OCR_INSTRUCTION))
        except OpenRouterError as exc:
            raise ResumeError(f"Could not read that image: {exc}") from exc
        if len(text) < 40:
            raise ResumeError("Couldn't make out any text in that image — try a sharper scan.")
        return text, "vision-ocr"

    raise ResumeError(f"Unsupported file type '{content_type or 'unknown'}'. Upload a PDF, PNG, or JPG.")
