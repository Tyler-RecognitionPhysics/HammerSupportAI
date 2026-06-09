# Persistent HubSpot ticket sync + KB artifact host on Fly.io
FROM python:3.12-slim

WORKDIR /app/server

COPY demo/realtime-support-demo/server/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY knowledge_support/ /app/knowledge_support/
COPY knowledge/ /app/knowledge/
COPY wiki-support/ /app/wiki-support/
COPY demo/realtime-support-demo/server/ /app/server/
COPY demo/shared/ /app/demo/shared/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app:/app/server:/app/demo/shared
ENV SUPPORT_REPO_ROOT=/app
ENV SUPPORT_DATA_DIR=/data
ENV SUPPORT_RAW_DIR=/data/raw/support-data
ENV SUPPORT_KB_DB=/data/support_kb.sqlite
ENV SUPPORT_HUBSPOT_TICKETS_STATE_DB=/data/hubspot_tickets_sync.sqlite

EXPOSE 8782
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8782"]
