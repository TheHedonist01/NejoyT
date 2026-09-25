# ==========================================
# Etapa 1: Builder (Instalación de dependencias con uv)
# ==========================================
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Desactivar compilación de bytecode innecesaria y habilitar link por copia
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Copiar manifiestos de dependencias
COPY pyproject.toml uv.lock ./

# Sincronizar dependencias en un entorno virtual aislado (.venv)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ==========================================
# Etapa 2: Runtime (Entorno de producción ligero)
# ==========================================
FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

# Instalar FFmpeg y curl para la cañería de audio y comprobaciones de salud
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copiar el entorno virtual con las dependencias precompiladas desde builder
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Copiar el código fuente y artefactos del proyecto
COPY . /app/

# Exponer el puerto 8000 (FastAPI HTTP + WebSocket)
EXPOSE 8000

# Variables de entorno por defecto
ENV PYTHONUNBUFFERED=1 \
    PORT=8000

# Healthcheck nativo consultando /health
HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

# Inicio del servidor Uvicorn en producción
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
