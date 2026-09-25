#!/usr/bin/env bash
set -e

echo "==================================================="
echo "  NejoyT - Subtitulado Simultáneo Nerdearla 2026"
echo "  Despliegue Rápido en Contenedor Docker"
echo "==================================================="
echo ""

if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        echo "[INFO] Creando archivo .env a partir de .env.example..."
        cp .env.example .env
        echo "[AVISO] Por favor verifica o coloca tu GEMINI_API_KEY en el archivo .env si deseas usar ASR en la nube."
        echo ""
    fi
fi

echo "[1/2] Compilando imagen y levantando contenedor..."
docker compose up --build -d

echo ""
echo "[2/2] Contenedor NejoyT iniciado con éxito."
echo ""
echo "  - Panel de Control:       http://localhost:8000/admin"
echo "  - Interfaz de Audiencia:  http://localhost:8000/"
echo "  - Comprobación de Salud:  http://localhost:8000/health"
echo ""
echo "Para ver los logs en tiempo real ejecuta:"
echo "  docker compose logs -f nejoyt"
