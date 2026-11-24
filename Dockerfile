FROM python:3.12-slim

WORKDIR /app

# Install minimal runtime utilities (curl for healthchecks, sqlite3)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Fix CRLF line endings for Linux execution
RUN sed -i 's/\r$//' docker/entrypoint.sh scripts/*.sh 2>/dev/null || true && \
    chmod +x docker/entrypoint.sh scripts/*.sh 2>/dev/null || true

ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["python", "-m", "app.bot.main"]
