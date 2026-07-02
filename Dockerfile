FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/release_analytics.sqlite3 \
    RUN_INTERVAL_SECONDS=21600 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_PORT=8501

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY collector.py dashboard.py db.py import_history.py docker-entrypoint.sh ./

VOLUME ["/data"]
EXPOSE 8501

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["web"]
