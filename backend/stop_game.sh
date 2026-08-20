#!/bin/bash

echo "=========================================="
echo "🛑 Deteniendo Cartas Contra Miedo y Hambre"
echo "=========================================="

# Buscar y matar app.py específicamente
APP_PIDS=$(pgrep -f "python3 app.py")
if [ -n "$APP_PIDS" ]; then
    echo "[*] Deteniendo servidor Python (app.py)..."
    kill -9 $APP_PIDS
else
    echo "[✓] El servidor Python ya estaba detenido."
fi

# Buscar y matar el proceso de ngrok
NGROK_PIDS=$(pgrep -f "ngrok http 3000")
if [ -n "$NGROK_PIDS" ]; then
    echo "[*] Cerrando túnel de ngrok..."
    kill -9 $NGROK_PIDS
else
    echo "[✓] El túnel de ngrok ya estaba cerrado."
fi

# Asegurarnos de liberar el puerto por si algo se quedó colgado (fuser no mata todo python)
fuser -k 3000/tcp 2>/dev/null

echo "=========================================="
echo "✅ Todo apagado correctamente."
echo "=========================================="
