# Booking service: exactly ONE running task (it is the only service that writes capacity).
# No data is baked in and nothing is stored on the host: the snapshot is read from S3
# into memory on first start and loaded into RDS Postgres.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY booking/requirements.txt booking/requirements.txt
RUN pip install --no-cache-dir -r booking/requirements.txt
COPY booking/ booking/
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["uvicorn", "booking.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
