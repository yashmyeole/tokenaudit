# Build from the REPO ROOT (the app prerenders reports from the captured
# runs in corpus/):  docker build -f demo/Dockerfile -t tokenaudit-demo .
FROM python:3.12-slim

WORKDIR /app

# Layer the dependency install before the source copy so code-only edits
# rebuild in seconds.
COPY demo/requirements.txt demo/requirements.txt
RUN pip install --no-cache-dir -r demo/requirements.txt

COPY . .
RUN pip install --no-cache-dir .

EXPOSE 8000
# Render/Railway inject $PORT; default to 8000 for local docker run.
CMD uvicorn demo.app:app --host 0.0.0.0 --port ${PORT:-8000}
