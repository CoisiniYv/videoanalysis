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
CMD ["python", "/app/main.py"]
