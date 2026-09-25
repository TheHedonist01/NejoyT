@echo off
chcp 65001 > nul
echo ===================================================
echo   NejoyT - Subtitulado Simultáneo Nerdearla 2026
echo   Despliegue Rápido en Contenedor Docker
echo ===================================================
echo.

if not exist .env (
    if exist .env.example (
        echo [INFO] Creando archivo .env a partir de .env.example...
        copy .env.example .env > nul
        echo [AVISO] Por favor verifica o coloca tu GEMINI_API_KEY en el archivo .env si deseas usar ASR en la nube.
        echo.
    )
)

echo [1/2] Compilando imagen y levantando contenedor...
docker compose up --build -d

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Falló la compilación o inicio con Docker Compose.
    echo Verifica que Docker Desktop o el daemon de Docker esté iniciado.
    pause
    exit /b %errorlevel%
)

echo.
echo [2/2] Contenedor NejoyT iniciado con éxito.
echo.
echo   - Panel de Control:       http://localhost:8000/admin
echo   - Interfaz de Audiencia:  http://localhost:8000/
echo   - Comprobación de Salud:  http://localhost:8000/health
echo.
echo Para ver los logs en tiempo real ejecuta:
echo   docker compose logs -f nejoyt
echo.
pause
