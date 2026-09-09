FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
# Put the real CSDOG animation file next to the Dockerfile and name it banner.mp4.
COPY banner.mp4 /app/banner.mp4

CMD ["python", "bot.py"]
