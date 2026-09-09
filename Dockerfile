FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
# Положи этот файл рядом с Dockerfile перед деплоем.
COPY musordrop_animation_green-screen_sound_on.mp4 /app/banner.mp4

ENV PYTHONUNBUFFERED=1
ENV FFMPEG_TIMEOUT=600

CMD ["python", "bot.py"]
