# Booking service: exactly ONE running task (sole SQLite writer).
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SETUHAUL_SOURCE_DB=/app/data/setuhaul_freight_operations.db \
    SETUHAUL_DB_PATH=/data/setuhaul.db
COPY booking/requirements.txt booking/requirements.txt
RUN pip install --no-cache-dir -r booking/requirements.txt
COPY booking/ booking/
COPY data/setuhaul_freight_operations.db data/
# /data is a host/EBS volume so the working DB survives task restarts
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"
# --workers 1 on purpose: one process owns the write path
CMD ["uvicorn", "booking.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
