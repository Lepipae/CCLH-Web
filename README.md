# Cartas Contra Miedo y Hambre

Juego web multijugador de tipo *Cards Against Humanity*: un jugador actúa como juez, lee una carta negra con una frase incompleta y el resto compite presentando la combinación de cartas blancas más graciosa. Funciona en escritorio y móvil, está pensado para jugarse en la misma sala compartiendo un enlace público y no requiere registro ni instalación para los jugadores.

## Características

- Salas privadas por código, con invitación mediante enlace directo.
- Partidas de 2 a N jugadores; quien se une a mitad de partida entra como espectador y juega la ronda siguiente.
- Reconexión automática por nombre: si un jugador se cae, puede volver a entrar con el mismo nombre y recupera su estado.
- Cartas negras con selección múltiple (`pick` > 1): el jugador presenta varias cartas blancas ordenadas.
- Votación de cartas favoritas entre jugadores (el juez no puede votar y nadie puede votarse a sí mismo).
- Renovación de mano por votación con umbral configurable, y cambio de carta negra por parte del juez.
- Chat integrado. En móvil se muestra como panel deslizable con contador de mensajes no leídos; en escritorio, como panel lateral.
- Taller de cartas personalizadas globales y cartas custom exclusivas por sala.
- Interfaz responsive: en móvil, banner compacto con los 3 mejores jugadores y área de juego a pantalla completa.

## Tecnologías

| Capa | Tecnología |
| --- | --- |
| Frontend | React 19, Vite, framer-motion, lucide-react |
| Comunicación | Socket.IO (eventos en tiempo real bidireccionales) |
| Backend | Python 3.11, Flask, Flask-SocketIO, eventlet |
| Contenedor | Docker multi-stage (node:20-alpine para el build, python:3.11-slim para servir) |
| Túnel público | ngrok (contenedor sidecar) |
| Scraper de cartas | requests, BeautifulSoup, deep-translator (opcional, solo para regenerar el mazo) |

## Arquitectura

```
Navegador (React, SPA compilada)
        │  Socket.IO (WebSocket)
        ▼
Flask + Flask-SocketIO (puerto 3000)
  ├── Sirve el frontend compilado desde frontend/dist
  ├── game_manager.py: salas, rondas, turnos, votaciones, chat
  └── DataScraping/: mazo base en JSON + cartas custom persistidas
```

El backend es la autoridad de todo el estado de la partida (mazos, manos, puntuaciones, temporizadores de ronda). El cliente solo envía acciones (`jugar carta`, `revelar`, `elegir ganador`, `votar`, `chat`) y recibe el estado completo en cada evento `game_update`.

## Requisitos

- Docker y Docker Compose (vía recomendada), o bien Node.js 20+ y Python 3.11+ para desarrollo local.
- Una cuenta gratuita de ngrok si se quiere jugar fuera de la red local (el authtoken se configura en `.env`).

## Puesta en marcha con Docker

Es la vía recomendada: compila el frontend, arranca el backend y publica un enlace público mediante ngrok.

1. Configurar el token de ngrok:

   ```bash
   cp .env.example .env
   # Editar .env y poner el authtoken real de https://dashboard.ngrok.com
   ```

2. Construir y levantar:

   ```bash
   docker compose up -d --build
   ```

3. Obtener el enlace público para compartir con los jugadores:

   ```bash
   docker logs cclh_ngrok_url
   ```

   El contenedor auxiliar `ngrok-url` imprime la URL con el formato `https://xxxx.ngrok-free.app`. También puede consultarse en `http://localhost:4040`, el panel local de ngrok.

4. Jugar: abre la URL, elige un nombre y crea una sala con un código (por ejemplo `CASA1`). Los demás jugadores se unen con el mismo código o directamente con el enlace `https://xxxx.ngrok-free.app/?room=CASA1`. El líder de la sala configura las opciones y pulsa *Iniciar Partida* (mínimo 2 jugadores).

Para aplicar cambios de código hay que reconstruir la imagen, no basta con reiniciar:

```bash
docker compose up -d --build
```

Para detenerlo:

```bash
docker compose down
```

### Volúmenes

El `docker-compose.yml` monta dos rutas del host para que los datos persistan aunque se recree el contenedor:

| Ruta en el host | Uso |
| --- | --- |
| `./backend/DataScraping` | Mazo base JSON y `cartasCustom.json` interno |
| `./custom_cards` | Copia sincronizada de las cartas personalizadas, accesible desde fuera del contenedor |

## Puesta en marcha sin Docker

```bash
# 1. Compilar el frontend
cd frontend
npm install
npm run build
cd ..

# 2. Arrancar el backend (sirve frontend/dist en el puerto 3000)
cd backend
pip install -r requirements.txt
python app.py
```

La aplicación quedará disponible en `http://localhost:3000`. Existe también `start.sh`, que automatiza estos pasos y arranca ngrok si está instalado localmente.

Para desarrollar el frontend con recarga en caliente (`npm run dev` en Vite) es necesario que el backend esté accesible desde el puerto del servidor de desarrollo; la configuración actual conecta Socket.IO contra el mismo origen, por lo que la vía simple es desarrollar contra el build compilado o añadir un proxy en `vite.config.js`.

## Opciones de sala

Las configura el líder antes de iniciar la partida:

| Opción | Descripción | Valores |
| --- | --- | --- |
| Cartas en la mano | Tamaño de mano con el que se juega cada ronda | 5 a 20 |
| Votos para renovar (%) | Porcentaje de jugadores activos necesario para renovar todas las manos | 10 a 100 |
| Orden de turnos (juez) | Cómo se asigna el juez en cada ronda | Ganador anterior, aleatorio o por orden de llegada |

## Cartas personalizadas

- **Taller de cartas**: accesible desde la pantalla inicial. Permite crear cartas blancas y negras que se guardan de forma persistente en `cartasCustom.json` y están disponibles en todas las salas y partidas. Las cartas se validan contra el mazo existente para evitar duplicados prácticamente idénticos.
- **Importar y exportar el mazo**: el taller admite subir un archivo `.json` con el mismo formato que los sets base (`whiteCards` como texto, `blackCards` como `{text, pick}`). El archivo se valida carta a carta en cliente y servidor: las inválidas y duplicadas se omiten individualmente y el resto se incorpora al mazo global y a las salas activas al instante. El botón «Exportar mazo» descarga el mazo personalizado completo como `cartasCustom.json`, listo para compartir o volver a importar en otra instancia.
- **Cartas por sala**: durante la sala de espera, cualquier jugador puede añadir cartas blancas temporales separadas por comas. Solo viven en esa sala y no se guardan en el mazo global.
- El archivo `cartasCustom.json` se sincroniza automáticamente entre el directorio interno del backend y la carpeta `custom_cards/` del host, de modo que las cartas creadas desde la web sobreviven a reconstrucciones del contenedor.

## Datos de cartas

El mazo base se carga al arrancar desde `backend/DataScraping/CAH-es-set-actualizado.json` (formato `whiteCards` / `blackCards`, con `pick` en las negras). En `backend/DataScraping/` hay scripts opcionales para regenerar o ampliar el mazo:

```bash
python backend/DataScraping/scraper_cartas.py
```

Son utilidades de una sola ejecución; no forman parte del servicio ni se ejecutan dentro del contenedor en caliente.

## Estructura del proyecto

```
backend/
  app.py               # Servidor Flask + rutas Socket.IO
  models/
    game_manager.py    # Lógica de salas, rondas y votaciones
    room.py            # Estado de sala y serialización por jugador
    player.py          # Estado de jugador
  DataScraping/        # Mazo base JSON, cartas custom y scrapers
frontend/
  src/
    App.jsx            # Conexión Socket.IO y navegación de pantallas
    components/        # GameScreen, Card, Chat, Leaderboard, PlayersBanner, ...
  index.html           # Entrada de Vite
custom_cards/          # Copia en el host de las cartas personalizadas
Dockerfile             # Build multi-stage (React -> Python)
docker-compose.yml     # app + ngrok + auxiliar de URL pública
```

## Resolución de problemas

- **No aparece el enlace público**: revisa que `NGROK_AUTHTOKEN` esté bien puesto en `.env` y consulta los logs con `docker logs cclh_ngrok`. El panel de diagnóstico de ngrok está en `http://localhost:4040`.
- **He cambiado código y no se ve reflejado**: el contenedor sirve una copia compilada dentro de la imagen. Ejecuta de nuevo `docker compose up -d --build`.
- **El puerto 3000 está ocupado**: detén el proceso que lo usa o cambia el mapeo en `docker-compose.yml` (por ejemplo `"3001:3000"`); el túnel de ngrok siempre apunta al puerto interno.
- **El backend indica que no encuentra el archivo de cartas**: comprueba que `backend/DataScraping/CAH-es-set-actualizado.json` existe en el host; al estar montado como volumen, el contenedor lo lee directamente.
- **Un jugador se quedó congelado tras perder la conexión**: basta con recargar la página y volver a entrar con el mismo nombre en la misma sala; recuperará su mano y puntuación.
