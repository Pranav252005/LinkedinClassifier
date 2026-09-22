# Portable backend image — works on Fly.io, Railway, Cloud Run, or any host
# that runs containers. Netlify does not run containers or Python.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first so the layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

# The schema is created on startup; no migration step is needed.
EXPOSE 8000
CMD ["sh", "-c", "cd backend && uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
