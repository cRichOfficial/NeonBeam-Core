FROM python:3.11-slim

WORKDIR /app

# Install dependencies if present
COPY requirements.txt* ./
RUN if [ -f "requirements.txt" ]; then pip install --no-cache-dir -r requirements.txt; fi

COPY . .

# Create log directory (COMM_LOG_FILE defaults to /app/logs/neonbeam_core.log)
RUN mkdir -p /app/logs

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
