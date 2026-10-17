FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

VOLUME /data
EXPOSE 8000
CMD ["python", "-m", "stocksvest_market", "serve"]
