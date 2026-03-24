FROM python:3.12-slim

WORKDIR /app

# Install deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py .

# State persisted to /data via volume mount
VOLUME /data

ENV PYTHONUNBUFFERED=1

CMD ["python", "monitor.py"]
