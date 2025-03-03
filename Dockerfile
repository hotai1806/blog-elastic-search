FROM python:3.11-slim

WORKDIR /app

# Install netcat for health checks
RUN apt-get update && apt-get install -y netcat-openbsd && apt-get clean

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Make the migration script executable
RUN chmod +x /app/run_migrations.sh
EXPOSE 8000

# Run migrations and then start the application
# CMD ["./run_migrations.sh"]