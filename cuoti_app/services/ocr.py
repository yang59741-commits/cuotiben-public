"""Local document extraction and PDF vision-OCR helpers."""

from __future__ import annotations

import base64
import math
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

MAX_LOCAL_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_OCR_TEXT_CHARS = 100_000
MAX_DOCX_XML_BYTES = 16 * 1024 * 1024
MAX_DOCX_COMPRESSION_RATIO = 100
MAX_DOCX_XML_NODES = 250_000
MAX_PDF_IMAGE_PIXELS = 20_000_000
MAX_PDF_PAGE_PIXELS = 60_000_000
MAX_PDF_PAGE_PNG_BYTES = 24 * 1024 * 1024
MAX_PDF_TASK_PNG_BYTES = 256 * 1024 * 1024
MAX_PDF_TASK_BASE64_CHARS = 360 * 1024 * 1024
PDF_TILE_COUNT = 3
PDF_TILE_OVERLAP_RATIO = 0.06
_PNG_DATA_URL_PREFIX = "data:image/png;base64,"

PDFVisionRecognizer = Callable[[list[dict[str, Any]], int, int], str]
ProgressCallback = Callable[[int, int, str], None]
CancellationCheck = Callable[[], None]


class LocalDocumentExtractionError(ValueError):
    """A stable service error that HTTP and durable-job adapters can map."""

    def __init__(self, message: str, *, status_code: int, code: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _docx_invalid(message: str = "DOCX 文件无效或已损坏"):
    return LocalDocumentExtractionError(
        message,
        status_code=400,
        code="docx_invalid",
    )


def _docx_expansion_too_large():
    return LocalDocumentExtractionError(
        "DOCX 解压内容超过安全上限，请缩小文件后重试",
        status_code=413,
        code="docx_expansion_too_large",
    )


class _LimitedReader:
    """Expose at most a fixed number of decompressed bytes to a parser."""

    def __init__(self, stream, maximum_bytes: int):
        self._stream = stream
        self._maximum_bytes = maximum_bytes
        self._consumed = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self._maximum_bytes - self._consumed
        requested = remaining + 1 if size is None or size < 0 else min(size, remaining + 1)
        chunk = self._stream.read(requested)
        self._consumed += len(chunk)
        if self._consumed > self._maximum_bytes:
            raise _docx_expansion_too_large()
        return chunk


def _extract_docx_xml(stream) -> str:
    paragraph_tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
    text_tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
    paragraphs: list[str] = []
    paragraph_text: list[str] | None = None
    node_count = 0
    text_chars = 0
    for event, element in ElementTree.iterparse(stream, events=("start", "end")):
        if event == "start":
            node_count += 1
            if node_count > MAX_DOCX_XML_NODES:
                raise _docx_expansion_too_large()
            if element.tag == paragraph_tag:
                paragraph_text = []
            continue
        if element.tag == text_tag and paragraph_text is not None:
            value = element.text or ""
            text_chars += len(value)
            if text_chars > MAX_OCR_TEXT_CHARS:
                raise _docx_expansion_too_large()
            paragraph_text.append(value)
        elif element.tag == paragraph_tag and paragraph_text is not None:
            if text_chars + len(paragraphs) > MAX_OCR_TEXT_CHARS:
                raise _docx_expansion_too_large()
            paragraphs.append("".join(paragraph_text))
            paragraph_text = None
        element.clear()
    return "\n".join(paragraphs)


def extract_docx_text(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            documents = [
                info
                for info in archive.infolist()
                if info.filename == "word/document.xml"
            ]
            if len(documents) != 1:
                raise _docx_invalid("DOCX 缺少唯一的主文档 XML")
            document = documents[0]
            if document.flag_bits & 0x1:
                raise _docx_invalid("DOCX 主文档已加密，无法读取")
            if document.file_size < 0 or document.compress_size < 0:
                raise _docx_invalid()
            if document.file_size > MAX_DOCX_XML_BYTES:
                raise _docx_expansion_too_large()
            if document.file_size and (
                document.compress_size == 0
                or document.file_size / document.compress_size
                > MAX_DOCX_COMPRESSION_RATIO
            ):
                raise _docx_expansion_too_large()
            with archive.open(document, "r") as xml_stream:
                return _extract_docx_xml(
                    _LimitedReader(xml_stream, MAX_DOCX_XML_BYTES)
                )
    except LocalDocumentExtractionError:
        raise
    except (
        ElementTree.ParseError,
        EOFError,
        NotImplementedError,
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        raise _docx_invalid() from exc


def _png_data_url(raw: bytes) -> str:
    return _PNG_DATA_URL_PREFIX + base64.b64encode(raw).decode("ascii")


def _pdf_render_budget_exceeded():
    return LocalDocumentExtractionError(
        "PDF 页面渲染超过安全上限，请缩小页面或拆分文件后重试",
        status_code=413,
        code="pdf_render_budget_exceeded",
    )


@dataclass
class _PDFRenderBudget:
    png_bytes: int = 0
    base64_chars: int = 0


def _expected_image_pixels(rect, scale: float) -> int:
    coordinates = (
        float(rect.x0),
        float(rect.y0),
        float(rect.x1),
        float(rect.y1),
        float(rect.width),
        float(rect.height),
    )
    if not all(math.isfinite(value) for value in coordinates):
        raise _pdf_render_budget_exceeded()
    if rect.width <= 0 or rect.height <= 0:
        raise LocalDocumentExtractionError(
            "PDF 页面尺寸无效，无法渲染",
            status_code=400,
            code="pdf_render_failed",
        )
    width = max(1, math.ceil(float(rect.width) * scale))
    height = max(1, math.ceil(float(rect.height) * scale))
    pixels = width * height
    if pixels > MAX_PDF_IMAGE_PIXELS:
        raise _pdf_render_budget_exceeded()
    return pixels


def _page_render_specs(page_rect, pymupdf):
    _expected_image_pixels(page_rect, 1.0)
    tile_height = float(page_rect.height) / PDF_TILE_COUNT
    overlap = tile_height * PDF_TILE_OVERLAP_RATIO
    specs = [(1.25, None, page_rect)]
    for index in range(PDF_TILE_COUNT):
        y0 = max(float(page_rect.y0), float(page_rect.y0) + index * tile_height - overlap)
        y1 = min(
            float(page_rect.y1),
            float(page_rect.y0) + (index + 1) * tile_height + overlap,
        )
        clip = pymupdf.Rect(page_rect.x0, y0, page_rect.x1, y1)
        specs.append((2.0, clip, clip))
    page_pixels = sum(
        _expected_image_pixels(pixel_rect, scale)
        for scale, _clip, pixel_rect in specs
    )
    if page_pixels > MAX_PDF_PAGE_PIXELS:
        raise _pdf_render_budget_exceeded()
    return specs


def _render_page_images(
    page, *, budget: _PDFRenderBudget | None = None
) -> list[str]:
    """Render one PDF page as an overview followed by readable vertical tiles."""

    import pymupdf

    if budget is None:
        budget = _PDFRenderBudget()
    page_rect = page.rect
    specs = _page_render_specs(page_rect, pymupdf)
    images: list[str] = []
    page_png_bytes = 0
    page_pixels = 0
    for scale, clip, _pixel_rect in specs:
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale),
            colorspace=pymupdf.csRGB,
            alpha=False,
            **({"clip": clip} if clip is not None else {}),
        )
        actual_pixels = int(pixmap.width) * int(pixmap.height)
        page_pixels += actual_pixels
        if (
            actual_pixels > MAX_PDF_IMAGE_PIXELS
            or page_pixels > MAX_PDF_PAGE_PIXELS
        ):
            del pixmap
            raise _pdf_render_budget_exceeded()
        raw = pixmap.tobytes("png")
        del pixmap
        page_png_bytes += len(raw)
        next_png_bytes = budget.png_bytes + len(raw)
        encoded_chars = len(_PNG_DATA_URL_PREFIX) + 4 * ((len(raw) + 2) // 3)
        next_base64_chars = budget.base64_chars + encoded_chars
        if (
            page_png_bytes > MAX_PDF_PAGE_PNG_BYTES
            or next_png_bytes > MAX_PDF_TASK_PNG_BYTES
            or next_base64_chars > MAX_PDF_TASK_BASE64_CHARS
        ):
            del raw
            raise _pdf_render_budget_exceeded()
        images.append(_png_data_url(raw))
        budget.png_bytes = next_png_bytes
        budget.base64_chars = next_base64_chars
        del raw
    return images


def iter_pdf_page_images(path: str | Path) -> Iterator[tuple[int, int, list[str]]]:
    """Yield locally rendered page images without ever uploading the source PDF."""

    try:
        import pymupdf
    except ImportError as exc:
        raise LocalDocumentExtractionError(
            "缺少 PDF 渲染组件，无法启动视觉识别",
            status_code=503,
            code="pdf_renderer_configuration",
        ) from exc

    try:
        document = pymupdf.open(path)
    except Exception as exc:
        raise LocalDocumentExtractionError(
            "PDF 文件无效或已损坏",
            status_code=400,
            code="invalid_pdf",
        ) from exc
    try:
        if document.needs_pass:
            raise LocalDocumentExtractionError(
                "PDF 受密码保护，请解密后重试",
                status_code=400,
                code="pdf_password_protected",
            )
        page_count = int(document.page_count)
        if page_count <= 0:
            raise LocalDocumentExtractionError(
                "PDF 中没有可识别的页面",
                status_code=400,
                code="pdf_empty",
            )
        if page_count > MAX_PDF_PAGES:
            raise LocalDocumentExtractionError(
                f"PDF 不能超过 {MAX_PDF_PAGES} 页，请拆分后重试",
                status_code=413,
                code="pdf_too_many_pages",
            )
        budget = _PDFRenderBudget()
        for page_index in range(page_count):
            try:
                images = _render_page_images(
                    document.load_page(page_index), budget=budget
                )
            except LocalDocumentExtractionError:
                raise
            except Exception as exc:
                raise LocalDocumentExtractionError(
                    f"PDF 第 {page_index + 1} 页无法渲染",
                    status_code=400,
                    code="pdf_render_failed",
                ) from exc
            yield page_index + 1, page_count, images
            del images
    finally:
        document.close()


def build_pdf_vision_messages(
    images: list[str], *, page_number: int, page_count: int
) -> list[dict[str, Any]]:
    """Build a DeepSeek-compatible user message for one rendered PDF page."""

    prompt = (
        f"请逐字识别这份数学文档的第 {page_number}/{page_count} 页。"
        "第一张图是整页概览，后面三张图是从上到下、带少量重叠的清晰分块；"
        "它们属于同一页，请结合概览判断版面并只转录一次。"
        "保留题号、中文、英文、公式、矩阵、分段函数、上下标和证明结构；"
        "数学内容使用规范 LaTeX，段落和列表保持原顺序。"
        "不要解题，不要总结，不要臆造看不清的内容，不要输出 Markdown 代码围栏，"
        "只输出可直接编辑的识别正文。"
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    labels = ("整页概览", "页面上部", "页面中部", "页面下部")
    for label, data_url in zip(labels, images, strict=True):
        content.append({"type": "text", "text": label})
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": data_url, "detail": "original"},
            }
        )
    return [{"role": "user", "content": content}]


def _normalize_ocr_page(text: object) -> str:
    normalized = str(text or "").strip()
    if normalized.startswith("```") and normalized.endswith("```"):
        lines = normalized.splitlines()
        if len(lines) >= 2:
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines.pop()
            normalized = "\n".join(lines).strip()
    return normalized


def extract_pdf_text(
    path: str | Path,
    *,
    recognizer: PDFVisionRecognizer,
    progress: ProgressCallback | None = None,
    raise_if_cancelled: CancellationCheck | None = None,
) -> dict[str, str]:
    pages: list[str] = []
    text_chars = 0
    page_images = iter_pdf_page_images(path)
    try:
        for page_number, page_count, images in page_images:
            if raise_if_cancelled is not None:
                raise_if_cancelled()
            if progress is not None:
                progress(
                    page_number,
                    page_count + 1,
                    f"正在用 DeepSeek 视觉识别第 {page_number}/{page_count} 页",
                )
            messages = build_pdf_vision_messages(
                images, page_number=page_number, page_count=page_count
            )
            try:
                text = _normalize_ocr_page(
                    recognizer(messages, page_number, page_count)
                )
            finally:
                del messages
                del images
            if not text:
                raise LocalDocumentExtractionError(
                    f"DeepSeek 未返回第 {page_number} 页的识别内容",
                    status_code=502,
                    code="vision_ocr_empty",
                )
            text_chars += len(text) + (2 if pages else 0)
            if text_chars > MAX_OCR_TEXT_CHARS:
                raise LocalDocumentExtractionError(
                    "PDF 识别结果超过 10 万字符，请拆分文件后重试",
                    status_code=413,
                    code="ocr_output_too_large",
                )
            pages.append(text)
    finally:
        close = getattr(page_images, "close", None)
        if callable(close):
            close()
    return {
        "text": "\n\n".join(pages),
        "method": "deepseek_vision",
    }


def extract_local_document(
    path: str | Path,
    *,
    filename: str | None = None,
    maximum_bytes: int = MAX_LOCAL_DOCUMENT_BYTES,
    pdf_recognizer: PDFVisionRecognizer | None = None,
    progress: ProgressCallback | None = None,
    raise_if_cancelled: CancellationCheck | None = None,
) -> dict[str, str]:
    """Validate and extract one already-local supported document."""

    source = Path(path)
    display_name = filename or source.name
    suffix = Path(display_name).suffix.lower()
    if suffix not in {".pdf", ".docx", ".tex", ".txt"}:
        raise LocalDocumentExtractionError(
            "仅支持 PDF/DOCX/TeX/TXT 文件",
            status_code=400,
            code="file_type",
        )
    if source.stat().st_size > maximum_bytes:
        raise LocalDocumentExtractionError(
            "上传文件不能超过 20 MB",
            status_code=413,
            code="file_too_large",
        )
    if suffix == ".pdf":
        if pdf_recognizer is None:
            raise LocalDocumentExtractionError(
                "PDF 视觉识别服务未配置",
                status_code=503,
                code="vision_ocr_configuration",
            )
        return extract_pdf_text(
            source,
            recognizer=pdf_recognizer,
            progress=progress,
            raise_if_cancelled=raise_if_cancelled,
        )
    try:
        if suffix in {".tex", ".txt"}:
            text = source.read_text(encoding="utf-8", errors="replace")
            return {"text": text, "method": "direct"}
        return {"text": extract_docx_text(str(source)), "method": "docx"}
    except LocalDocumentExtractionError:
        raise
    except Exception as exc:
        raise LocalDocumentExtractionError(
            "文件处理失败，请检查文件格式或稍后重试",
            status_code=500,
            code="file_processing_failed",
        ) from exc
