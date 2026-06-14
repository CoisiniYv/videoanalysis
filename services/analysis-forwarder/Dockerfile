FROM ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1

WORKDIR /app
COPY app /app/app

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "app.main"]
