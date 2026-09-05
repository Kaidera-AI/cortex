import importlib.util
import inspect
from pathlib import Path
import subprocess
import sys

import pytest


WORKER_PATH = Path(__file__).resolve().parents[1] / "worker.py"


def load_worker(name: str):
    spec = importlib.util.spec_from_file_location(name, WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Upload:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.offset = 0
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        chunk = self.payload[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


def test_request_limit_reserves_only_finite_multipart_overhead():
    module = load_worker("pdf_worker_wire_limit_test")

    assert module.MULTIPART_OVERHEAD_BYTES == 1 * 1024 * 1024
    assert module.MAX_REQUEST_BODY_BYTES == (
        module.MAX_PDF_BYTES + module.MULTIPART_OVERHEAD_BYTES
    )


def test_request_limit_middleware_stack_builds_with_starlette_keyword_contract():
    module = load_worker("pdf_worker_middleware_stack_test")

    module.app.build_middleware_stack()
    first = list(
        inspect.signature(module.RequestBodyLimitMiddleware.__init__).parameters
    )[1]
    assert first == "app"


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [b"33", b"9" * 5000])
async def test_declared_multipart_body_above_wire_limit_fails_before_body_read(declared):
    module = load_worker("pdf_worker_declared_body_limit_test")
    called = False
    sent = []

    async def downstream(_scope, _receive, _send):
        nonlocal called
        called = True

    async def receive():
        pytest.fail("an oversized declared body must not be read")

    async def send(message):
        sent.append(message)

    middleware = module.RequestBodyLimitMiddleware(downstream, max_body_bytes=32)
    await middleware(
        {
            "type": "http",
            "headers": [
                (b"content-type", b"multipart/form-data; boundary=attack"),
                (b"content-length", declared),
            ],
        },
        receive,
        send,
    )

    assert called is False
    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 413


@pytest.mark.asyncio
async def test_chunked_multipart_body_above_wire_limit_fails_closed_during_parse():
    module = load_worker("pdf_worker_chunked_body_limit_test")
    boundary = b"kaidera-pdf-boundary"
    body = b"".join(
        [
            b"--" + boundary + b"\r\n",
            b'Content-Disposition: form-data; name="pdf"; filename="attack.pdf"\r\n',
            b"Content-Type: application/pdf\r\n\r\n",
            b"%PDF-1.7\n" + (b"x" * 64),
            b"\r\n--" + boundary + b"--\r\n",
        ]
    )
    chunks = [body[index : index + 16] for index in range(0, len(body), 16)]
    messages = iter(
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    )
    sent = []

    async def receive():
        try:
            return next(messages)
        except StopIteration:
            return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    middleware = module.RequestBodyLimitMiddleware(module.app, max_body_bytes=96)
    await middleware(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/parse-pdf",
            "raw_path": b"/parse-pdf",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"content-type", b"multipart/form-data; boundary=" + boundary),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("pdf-worker", 9004),
        },
        receive,
        send,
    )

    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 413


@pytest.mark.asyncio
async def test_upload_is_streamed_and_rejected_at_the_explicit_byte_limit(tmp_path):
    module = load_worker("pdf_worker_bounded_upload_test")
    module.MAX_PDF_BYTES = 10
    module.UPLOAD_CHUNK_BYTES = 4
    upload = Upload(b"x" * 11)

    with pytest.raises(module.HTTPException) as exc_info:
        await module._save_bounded_upload(upload, tmp_path / "input.pdf")

    assert exc_info.value.status_code == 413
    assert upload.read_sizes == [4, 4, 3]
    assert all(size > 0 for size in upload.read_sizes)
    assert (tmp_path / "input.pdf").stat().st_size == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["remote-parser", "pypdf"])
async def test_unknown_or_unbounded_engine_is_rejected_before_reading_upload(engine):
    module = load_worker("pdf_worker_engine_allowlist_test")
    upload = Upload(b"not read")

    with pytest.raises(module.HTTPException) as exc_info:
        await module.parse_pdf(upload, engine)

    assert exc_info.value.status_code == 400
    assert upload.read_sizes == []


def test_pdfinfo_page_count_is_bounded(monkeypatch, tmp_path):
    module = load_worker("pdf_worker_page_limit_test")
    module.MAX_PDF_PAGES = 2
    monkeypatch.setattr(module, "_run_bounded_command", lambda *_args, **_kwargs: b"Pages: 3\n")

    with pytest.raises(module.HTTPException) as exc_info:
        module._pdf_page_count(tmp_path / "input.pdf")

    assert exc_info.value.status_code == 413
    assert "2-page limit" in exc_info.value.detail


def test_pdftotext_output_is_streamed_and_bounded(monkeypatch, tmp_path):
    module = load_worker("pdf_worker_pdftotext_output_limit_test")
    module.MAX_PDF_TEXT_BYTES = 16
    real_popen = subprocess.Popen

    def noisy_process(*_args, **kwargs):
        return real_popen(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 1024)"],
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
        )

    monkeypatch.setattr(module.subprocess, "Popen", noisy_process)

    with pytest.raises(module.HTTPException) as exc_info:
        module._run_pdftotext(tmp_path / "input.pdf")

    assert exc_info.value.status_code == 413
    assert "output" in exc_info.value.detail


def test_pdftotext_timeout_kills_and_reaps_the_parser(monkeypatch, tmp_path):
    module = load_worker("pdf_worker_pdftotext_timeout_test")
    module.PDF_PARSE_TIMEOUT_SECONDS = 0

    class Output:
        def close(self):
            pass

    class HungProcess:
        stdout = Output()

        def __init__(self):
            self.killed = False
            self.waited = False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            self.waited = True
            return -9

    process = HungProcess()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: process)

    with pytest.raises(module.HTTPException) as exc_info:
        module._run_pdftotext(tmp_path / "input.pdf")

    assert exc_info.value.status_code == 504
    assert process.killed is True
    assert process.waited is True


@pytest.mark.asyncio
async def test_health_reports_real_dependency_and_temp_failures(monkeypatch):
    module = load_worker("pdf_worker_dependency_health_test")
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(module, "_temporary_storage_writable", lambda: False)

    result = await module.health()

    assert result["ok"] is False
    assert result["pdftotext_available"] is False
    assert result["pdfinfo_available"] is False
    assert result["temporary_storage_writable"] is False
