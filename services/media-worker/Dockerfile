FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY requirements.pool.txt ./requirements.pool.txt
RUN pip install --no-cache-dir -r requirements.pool.txt

COPY app/ ./app/
COPY main.py ./main.py

CMD ["python", "main.py"]
