FROM python:3.12-slim-bookworm

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000
ENV STORAGE_DIR=/data
EXPOSE 8000

CMD ["sh", "-c", "mkdir -p \"$STORAGE_DIR\" && uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
