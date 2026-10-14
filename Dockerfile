FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# tgcrypto needs a compiler on some platforms; remove it afterwards to keep the image small
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc-dev ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Keep these on a volume so settings, thumbnails, covers and the Telegram
# session survive restarts:  docker run -v botdata:/app/data ...
# (see README "النشر"). Mount/copy bot_database.db and *.session into /app.
CMD ["python", "main.py"]
