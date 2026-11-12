"""The hosted demo: drag a trace in, get the diagnosis back. Nothing stored.

Privacy is a design property here, not a promise: uploads are handled with
:func:`tokenaudit.ingest.ingest_text`, which parses the bytes in memory —
there is no temp file, no persistence layer to configure away, and the app
never logs request bodies. The two sample reports are prerendered at
startup from files already public in this repo.

The demo is deliberately not part of the ``tokenaudit`` package (the
library keeps zero runtime dependencies); it is a separate FastAPI app
deployed from ``demo/Dockerfile``.

Run locally:  pip install -e . -r demo/requirements.txt
              uvicorn demo.app:app --reload
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from importlib.resources import files
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from tokenaudit.detect import diagnose, diagnose_across
from tokenaudit.ingest import UnknownTraceFormatError, ingest_text
from tokenaudit.report import render_html
from tokenaudit.rulebook import load_rulebook

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # per request, checked before parsing
MAX_FILES = 20

_DEMO_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DEMO_DIR.parent
_CAPTURED = _REPO_ROOT / "corpus" / "captured" / "runs" / "tradingagents"

app = FastAPI(title="tokenaudit demo", docs_url=None, redoc_url=None, openapi_url=None)


def _diagnose_to_html(named_texts: list[tuple[str, str]]) -> str:
    """Ingest + diagnose in memory and render the self-contained report."""
    rulebook = load_rulebook()
    results = [ingest_text(text, name) for name, text in named_texts]
    per_result = [(result, diagnose(result)) for result in results]
    across = diagnose_across(results) if sum(len(r.sessions) for r in results) > 1 else None
    return render_html(per_result, rulebook, across)


def _prerender_samples() -> dict[str, str]:
    samples: dict[str, str] = {}
    drift = files("tokenaudit.examples").joinpath("sample.trace.json")
    samples["drift"] = _diagnose_to_html(
        [("sample.trace.json (synthetic prefix-drift demo)", drift.read_text(encoding="utf-8"))]
    )
    if _CAPTURED.is_dir():
        captured = [
            (str(p.relative_to(_REPO_ROOT)), p.read_text(encoding="utf-8"))
            for p in sorted(_CAPTURED.rglob("*.jsonl"))
        ]
        if captured:
            samples["tradingagents"] = _diagnose_to_html(captured)
    return samples


_SAMPLES = _prerender_samples()


@app.middleware("http")
async def security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(_DEMO_DIR / "static" / "index.html", media_type="text/html")


@app.get("/api/sample/{name}")
async def sample(name: str) -> HTMLResponse:
    if name not in _SAMPLES:
        raise HTTPException(404, f"no sample named {name!r}")
    return HTMLResponse(_SAMPLES[name])


@app.post("/api/diagnose")
async def diagnose_upload(traces: list[UploadFile]) -> HTMLResponse:
    if not traces:
        raise HTTPException(400, "no files uploaded")
    if len(traces) > MAX_FILES:
        raise HTTPException(413, f"too many files (max {MAX_FILES})")
    named_texts: list[tuple[str, str]] = []
    total = 0
    for upload in traces:
        data = await upload.read()
        total += len(data)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"upload exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(400, f"{upload.filename}: not UTF-8 text ({exc})") from exc
        named_texts.append((upload.filename or "upload", text))
    try:
        return HTMLResponse(_diagnose_to_html(named_texts))
    except UnknownTraceFormatError as exc:
        raise HTTPException(422, str(exc)) from exc
