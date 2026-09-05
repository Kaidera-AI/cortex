"""cortex-pdf-worker — internal PDF parsing API.

Phase 1 uses killable Poppler subprocesses for bounded text-only extraction.
Heavy MinerU/magic-pdf ships later once the lightweight path is verified.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import select
import shutil
import subprocess
import tempfile
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse

app = FastAPI(title="cortex-pdf-worker", version="0.1.0")

MAX_PDF_BYTES = 25 * 1024 * 1024
MULTIPART_OVERHEAD_BYTES = 1 * 1024 * 1024
MAX_REQUEST_BODY_BYTES = MAX_PDF_BYTES + MULTIPART_OVERHEAD_BYTES
MAX_PDF_PAGES = 1000
MAX_PDF_TEXT_BYTES = 16 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 64 * 1024
PDF_PARSE_TIMEOUT_SECONDS = 60
PDF_INFO_TIMEOUT_SECONDS = 10
MAX_PDF_INFO_BYTES = 64 * 1024
_parse_slots = asyncio.Semaphore(1)


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Bound the multipart body before Starlette parses or spools the upload."""

    def __init__(self, app, *, max_body_bytes: int):
        if max_body_bytes < 0:
            raise ValueError("max_body_bytes must be non-negative")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", [])
        content_lengths = [
            value.strip()
            for key, value in headers
            if key.lower() == b"content-length"
        ]
        has_transfer_encoding = any(
            key.lower() == b"transfer-encoding" for key, _value in headers
        )
        if len(content_lengths) > 1 or (content_lengths and has_transfer_encoding):
            response = JSONResponse({"detail": "invalid content-length"}, status_code=400)
            await response(scope, receive, send)
            return
        if content_lengths:
            declared = content_lengths[0]
            if not declared.isdigit():
                response = JSONResponse(
                    {"detail": "invalid content-length"}, status_code=400
                )
                await response(scope, receive, send)
                return
            normalized = declared.lstrip(b"0") or b"0"
            limit = str(self.max_body_bytes).encode("ascii")
            if len(normalized) > len(limit) or (
                len(normalized) == len(limit) and normalized > limit
            ):
                response = JSONResponse(
                    {"detail": "request body is too large"}, status_code=413
                )
                await response(scope, receive, send)
                return

        received = 0
        request_too_large = False
        response_started = False

        async def limited_receive():
            nonlocal received, request_too_large
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    request_too_large = True
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            # FastAPI converts receive failures during multipart parsing into a
            # generic 400. Once the byte guard trips, suppress that translated
            # response so this outer middleware can return the correct 413.
            if request_too_large:
                return
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            pass
        if request_too_large:
            if response_started:
                raise _RequestBodyTooLarge
            response = JSONResponse(
                {"detail": "request body is too large"}, status_code=413
            )
            await response(scope, receive, send)


app.add_middleware(
    RequestBodyLimitMiddleware,
    max_body_bytes=MAX_REQUEST_BODY_BYTES,
)


def _temporary_storage_writable() -> bool:
    try:
        with tempfile.TemporaryFile(dir=os.environ.get("TMPDIR")):
            return True
    except OSError:
        return False


@app.get("/health")
async def health():
    pdftotext_path = shutil.which("pdftotext")
    pdfinfo_path = shutil.which("pdfinfo")
    temporary_storage_writable = _temporary_storage_writable()
    return {
        "ok": bool(pdftotext_path) and bool(pdfinfo_path) and temporary_storage_writable,
        "pdftotext_available": bool(pdftotext_path),
        "pdftotext_path": pdftotext_path,
        "pdfinfo_available": bool(pdfinfo_path),
        "pdfinfo_path": pdfinfo_path,
        "temporary_storage_writable": temporary_storage_writable,
        "engine": "pdftotext",
    }


async def _save_bounded_upload(pdf: UploadFile, destination: Path) -> int:
    total = 0
    with destination.open("xb") as stream:
        while True:
            remaining = MAX_PDF_BYTES - total
            chunk = await pdf.read(min(UPLOAD_CHUNK_BYTES, remaining + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                raise HTTPException(413, f"PDF exceeds the {MAX_PDF_BYTES}-byte upload limit")
            stream.write(chunk)
    if total == 0:
        raise HTTPException(400, "PDF upload is empty")
    return total


def _run_bounded_command(
    command: list[str],
    *,
    output_limit: int,
    timeout: int,
    unavailable_detail: str,
    failure_status: int,
    failure_detail: str,
) -> bytes:
    """Run one local parser with capped output, timeout, kill and reap."""
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise HTTPException(503, unavailable_detail) from exc

    assert proc.stdout is not None
    chunks: list[bytes] = []
    total = 0
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HTTPException(504, "PDF extraction timed out")
            readable, _, _ = select.select([proc.stdout], [], [], min(remaining, 0.5))
            if not readable:
                continue
            chunk = os.read(proc.stdout.fileno(), UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > output_limit:
                raise HTTPException(
                    413,
                    f"parser output exceeds the {output_limit}-byte limit",
                )
            chunks.append(chunk)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HTTPException(504, "PDF extraction timed out")
        try:
            returncode = proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(504, "PDF extraction timed out") from exc
        if returncode != 0:
            raise HTTPException(failure_status, failure_detail)
        return b"".join(chunks)
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def _pdf_page_count(path: Path) -> int:
    output = _run_bounded_command(
        ["pdfinfo", str(path)],
        output_limit=MAX_PDF_INFO_BYTES,
        timeout=PDF_INFO_TIMEOUT_SECONDS,
        unavailable_detail="pdfinfo dependency is unavailable",
        failure_status=400,
        failure_detail="invalid or unreadable PDF",
    ).decode("utf-8", errors="replace")
    pages = None
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() == "pages":
            try:
                pages = int(value.strip())
            except ValueError:
                break
            break
    if pages is None or pages <= 0:
        raise HTTPException(400, "pdfinfo did not report a valid page count")
    if pages > MAX_PDF_PAGES:
        raise HTTPException(413, f"PDF exceeds the {MAX_PDF_PAGES}-page limit")
    return pages


def _run_pdftotext(path: Path) -> str:
    """Stream and cap pdftotext output instead of buffering an unbounded result."""
    return _run_bounded_command(
        ["pdftotext", str(path), "-"],
        output_limit=MAX_PDF_TEXT_BYTES,
        timeout=PDF_PARSE_TIMEOUT_SECONDS,
        unavailable_detail="pdftotext dependency is unavailable",
        failure_status=500,
        failure_detail="pdftotext failed",
    ).decode("utf-8", errors="replace")


def _parse_saved_pdf(path: Path, engine: str) -> dict:
    _pdf_page_count(path)
    text = _run_pdftotext(path)
    return {"engine": engine, "text": text, "pages": None}


@app.post("/parse-pdf")
async def parse_pdf(pdf: UploadFile = File(...), engine: Optional[str] = Form(None)):
    """Parse a bounded PDF to plain text with the killable pdftotext engine."""
    engine = engine or "pdftotext"
    if engine != "pdftotext":
        raise HTTPException(400, f"unknown engine '{engine}'; use pdftotext")

    async with _parse_slots:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as workdir:
            tmp_path = Path(workdir) / "input.pdf"
            await _save_bounded_upload(pdf, tmp_path)
            return await asyncio.to_thread(_parse_saved_pdf, tmp_path, engine)
