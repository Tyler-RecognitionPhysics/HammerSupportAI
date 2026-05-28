# Telephony + Hammer Office (Playwright) on Fly.io
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app/server

COPY demo/realtime-sales-demo/server/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY demo/realtime-sales-demo/server/ ./
# pen-challenge + hammer browser prompt sources (read by voice_instructions.py)
COPY demo/realtime-sales-demo/web/src/pen-challenge-instructions.ts /app/demo/realtime-sales-demo/web/src/
COPY demo/realtime-sales-demo/web/src/hammer-sales-instructions.ts /app/demo/realtime-sales-demo/web/src/
COPY demo/realtime-sales-demo/web/src/pen-challenge-close.ts /app/demo/realtime-sales-demo/web/src/
COPY demo/realtime-sales-demo/web/src/voice-anti-narration.ts /app/demo/realtime-sales-demo/web/src/
COPY demo/realtime-sales-demo/web/src/voice-contact-readback.ts /app/demo/realtime-sales-demo/web/src/
COPY demo/shared/ /app/demo/shared/
COPY wiki/ /app/wiki/
COPY knowledge/ /app/knowledge/
COPY raw/hammer-data/ /app/raw/hammer-data/
RUN python /app/knowledge/scripts/sync_sqlite.py --db /app/knowledge/data/company_kb.sqlite

ENV PYTHONUNBUFFERED=1
ENV REALTIME_SALES_REPO_ROOT=/app
ENV REALTIME_SALES_WIKI_DIR=/app/wiki
ENV REALTIME_SALES_TELEPHONY=1
ENV REALTIME_SALES_PRODUCTION=1
ENV HAMMER_OFFICE_USE_PLAYWRIGHT=1
ENV HAMMER_OFFICE_HEADLESS=1
ENV HAMMER_OFFICE_INSTANT=1

EXPOSE 8780
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8780"]
