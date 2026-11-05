FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install netcat for the readiness checks in run_migrations.sh
RUN apt-get update && apt-get install -y --no-install-recommends netcat-openbsd && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Make the migration script executable
RUN chmod +x /app/run_migrations.sh
EXPOSE 8000

# Run migrations and then start the application
CMD ["sh", "-c", "./run_migrations.sh && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
