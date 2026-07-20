FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PORT=8080

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY run_demo.py README.md ./

USER app
EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn soutui.api:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
