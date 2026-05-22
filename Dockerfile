# Sistema Padaria O Pão — container de produção
# Build: docker compose build
# Run:   docker compose up -d
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Dependências de sistema mínimas (psycopg2-binary, Pillow, qrcode)
# postgresql-client traz pg_dump pro job de backup diario (app/services/backup.py)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    postgresql-client \
    libjpeg62-turbo \
    libpng16-16 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instala dependências primeiro (camada cacheada)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia código depois (camada que muda mais)
COPY . .

# Cria usuário não-root para rodar a aplicação
RUN useradd --create-home --shell /bin/bash padaria && \
    chown -R padaria:padaria /app
USER padaria

# Healthcheck para o docker compose saber se está vivo
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:5000/health || exit 1

EXPOSE 5000

# Gunicorn em produção — 2 workers gthread (ajusta conforme CPU disponível)
CMD ["gunicorn", "run:app", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "2", \
     "--threads", "4", \
     "--worker-class", "gthread", \
     "--timeout", "120", \
     "--keep-alive", "5", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
