FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8090

CMD ["sh", "-c", "uvicorn app.secured:app --host ${EVIDENCE_VIEWER_HOST:-0.0.0.0} --port ${EVIDENCE_VIEWER_PORT:-8090}"]
