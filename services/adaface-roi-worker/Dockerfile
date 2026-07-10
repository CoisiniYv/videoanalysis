FROM ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt
RUN python -m pip install --no-cache-dir --no-deps tensorrt-cu12==10.9.0.34

COPY app /app/app
COPY main.py /app/main.py

ENTRYPOINT []
HEALTHCHECK --interval=5s --timeout=3s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/metrics', timeout=2).read()"
CMD ["python", "/app/main.py"]
