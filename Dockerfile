FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# tgcrypto needs a compiler on some platforms; remove it afterwards to keep the image small
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc-dev ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# All state lives in PostgreSQL (DATABASE_URL), including the Telegram session
# and saved images, so no volume is needed.
CMD ["python", "main.py"]
