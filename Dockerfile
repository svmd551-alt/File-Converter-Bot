FROM python:3.11-slim

# ffmpeg is required for convert / rename / portrait features
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gcc build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY media_bot.py .

ENV PYTHONUNBUFFERED=1
CMD ["python", "media_bot.py"]
