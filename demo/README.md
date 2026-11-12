# Hosted demo

Live at https://tokenaudit-demo.onrender.com (free instance; the first load
after a quiet spell can take a minute).

A one-page FastAPI app: drop a trace, get the diagnosis report back.
Uploads are parsed in memory with `tokenaudit.ingest.ingest_text` and never
written to disk, stored, or logged; the two sample reports are prerendered
at startup from files already public in this repo. The demo is not part of
the pip package — the library keeps zero runtime dependencies.

## Run locally

    pip install -e . -r demo/requirements.txt
    uvicorn demo.app:app --reload
    # open http://127.0.0.1:8000

Or with Docker (from the repo root — the image needs `corpus/` for the
real-capture sample):

    docker build -f demo/Dockerfile -t tokenaudit-demo .
    docker run --rm -p 8000:8000 tokenaudit-demo

## Deploy on Render (free tier)

The repo root has a `render.yaml`, so the quickest path is render.com → New →
**Blueprint** → connect `yashmyeole/tokenaudit` → Apply. To set it up by hand
instead:

1. render.com → New → **Web Service** → connect `yashmyeole/tokenaudit`.
2. Language: **Docker**; Dockerfile path: `demo/Dockerfile`;
   root directory: leave at repo root.
3. Instance type: **Free**. Create the service.
4. First build takes a few minutes; the URL looks like
   `https://tokenaudit-demo.onrender.com`.

Free instances sleep after ~15 minutes idle, so the first visit after a
quiet spell takes ~30–60 s to wake — fine for a portfolio demo, and worth
a one-line note next to the link. Railway and Fly.io both deploy the same
Dockerfile if you prefer them (both currently want a card on file).

## Endpoints

- `GET /` — the page
- `GET /api/sample/drift` — prerendered report for the bundled synthetic
  prefix-drift fixture (labeled as synthetic)
- `GET /api/sample/tradingagents` — prerendered report for the three real
  captured TradingAgents runs
- `POST /api/diagnose` — multipart upload (`traces`, up to 20 files /
  15 MB total), returns the self-contained HTML report; 422 with the
  loud refusal message for unrecognized formats
