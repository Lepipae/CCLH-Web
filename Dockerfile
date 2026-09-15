# Stage 1: Compilar Frontend (React)
FROM node:20-alpine AS frontend-builder
WORKDIR /app/frontend

COPY frontend/package*.json ./
RUN npm install

COPY frontend/ ./
RUN npm run build

# Stage 2: Backend (Python)
FROM python:3.11-slim
WORKDIR /app/backend

# Instalar dependencias de Python
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código del backend
COPY backend/ /app/backend/

# Copiar el build compilado del frontend al directorio donde app.py lo espera (../frontend/dist)
COPY --from=frontend-builder /app/frontend/dist /app/frontend/dist

EXPOSE 3000

ENV PYTHONUNBUFFERED=1

# Servidor de producción: Gunicorn con worker 'gthread' (hilos) que empareja con
# async_mode='threading' de app.py; simple-websocket da soporte de WebSocket.
# Un solo proceso worker es obligatorio con Flask-SocketIO (el estado de partida
# vive en memoria); la concurrencia se logra con los hilos. --timeout cubre las
# conexiones WebSocket largas del polling/WebSocket de Socket.IO.
CMD ["gunicorn", "-k", "gthread", "-w", "1", "--threads", "100", "--timeout", "120", "--graceful-timeout", "30", "--bind", "0.0.0.0:3000", "app:app"]
