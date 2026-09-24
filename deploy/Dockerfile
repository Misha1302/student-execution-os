# Student Execution OS API + web client, session (login) mode.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    SEOS_DATABASE=/data/student-execution-os.db \
    SEOS_HOST=0.0.0.0 \
    SEOS_PORT=8765

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src

RUN useradd --system --uid 10001 seos && mkdir -p /data && chown seos /data
USER seos
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/v1/health').status==200 else 1)"

CMD ["python", "-m", "student_execution_os.web.server"]
