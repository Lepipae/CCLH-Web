# AGENTS.md — Cartas Contra Miedo y Hambre

Juego web multijugador tipo *Cards Against Humanity* (en español). Sin registro, por salas con código + enlace público via ngrok. Un juez lee carta negra, resto compite con blancas más graciosas.

## Stack

| Capa | Tech |
|---|---|
| Frontend | React 19, Vite 8, framer-motion, lucide-react, socket.io-client |
| Realtime | Socket.IO bidireccional (único canal cliente↔servidor) |
| Backend | Python 3.11, Flask 3 + Flask-SocketIO (`async_mode='threading'` + simple-websocket), Gunicorn |
| Contenedor | Docker multi-stage (`node:20-alpine` build → `python:3.11-slim` serve) |
| Túnel | ngrok sidecar + contenedor auxiliar `ngrok-url` que imprime la URL pública |
| Scraper | `requests`, BeautifulSoup, deep-translator (solo utilidades offline en `backend/DataScraping/`, nunca en caliente) |

## Arquitectura y autoridad de estado

```
Browser (React SPA en frontend/dist) ──Socket.IO──▶ Flask+SocketIO :3000
  ├── sirve frontend compilado desde frontend/dist
  ├── game_manager.py: salas, rondas, turnos, votaciones, chat, presencia
  └── DataScraping/: mazo base JSON + cartasCustom.json
```

- **El backend es autoridad total**: mazos, manos, puntos, timers. El cliente solo envía acciones y recibe snapshot completo en cada `game_update`.
- **Estado en memoria, un solo proceso.** Gunicorn OBLIGATORIO con `-w 1 --threads 100` (`Dockerfile`). Nunca subir workers: cada proceso tendría su propio estado y sus propias cookies (`SECRET_KEY` aleatoria por proceso si no hay env).
- Producción se arranca igual que el contenedor: `gunicorn -k gthread -w 1 --threads 100 --timeout 120 --bind 0.0.0.0:3000 app:app`.

## Modelo de concurrencia (leer antes de tocar `backend/models/`)

Hay **dos cerrojos, no un global**:

- `Room.lock` → estado de UNA mesa. Mesas distintas juegan en paralelo.
- `GameManager._registro_lock` → lo que no es de una mesa (dict de salas, techo de salas, mazos globales). Tomar y soltar rápido.

Reglas inviolables:

1. **Orden fijo: registro antes que sala, nunca al revés.** `GameManager._registro()` lo verifica y falla ruidosamente si se incumple (anti-deadlock).
2. Operaciones multi-sala (join que suelta sala origen, disconnect) bloquean todas las salas implicadas en **orden total por `room_id`**.
3. **Nunca hacer E/S con cerrojo tomado.** Emisión siempre vía lote `models/outbox.py`: bajo cerrojo se lee estado + serializa snapshot + se encola; el socket se escribe al salir.
4. `backend/bench_emision.py` mide cuánto bloquea un cliente lento al resto. `backend/reversion.py` verifica por reversión que cada defensa tiene su test.

## Layout

```
backend/app.py               # Flask + handlers Socket.IO + GET /metrics, sirve ../frontend/dist
backend/models/game_manager.py  # salas/rondas/votaciones/reaper/cerrojos (1578 líneas, núcleo)
backend/models/room.py       # estado de sala + serialización por jugador
backend/models/player.py     # estado de jugador
backend/models/deck.py       # mazo de sala + audit_cards() (conservación: ni perder ni duplicar)
backend/models/presence.py   # máquina vivo→sospechoso→purgado + plazos
backend/models/outbox.py     # lote de salida (encolar bajo lock, emitir fuera)
backend/models/custom_cards.py # CustomCardsMixin: alta/baja/listado/import de propias
backend/DataScraping/        # CAH-es-set-actualizado.json (base), cartasCustom.json, scrapers offline
backend/tests/               # suite pytest (ver Tests)
backend/reversion.py backend/bench_emision.py
frontend/src/App.jsx         # socket = io() (mismo origen), reconnect+rejoin, presencia, heartbeat
frontend/src/components/     # GameScreen, Card, Chat, Leaderboard, PlayersBanner, Login,
                             # LobbyOptions, CustomCards, CardWorkshop
Dockerfile docker-compose.yml .env.example start.sh
custom_cards/                # copia en host de cartasCustom.json (persiste tras rebuild)
```

## Protocolo Socket.IO

Cliente→servidor (`backend/app.py`): `join_game`, `start_game`, `update_room_options`, `play_card`, `reveal_card`, `choose_winner`, `vote_card`, `next_round`, `vote_renew`, `change_black_card`, `add_custom_card`, `get_custom_cards`, `delete_custom_card`, `import_custom_cards`, `add_room_cards`, `send_chat`, `heartbeat_pong`.

Servidor→cliente: `game_update` (snapshot completo, única fuente de verdad), `join_error`, `heartbeat_ping`, `presence_alert` (aviso a mesa), `presence_challenge` (reto con plazo al propio cliente), `custom_card_result`, `custom_cards_list`, `custom_card_deleted`, `custom_cards_imported`.

Notas:

- Handlers normalizan input con `_datos()`/`_texto()` — todo lo del socket es no confiable. Mantener ese escudo en handlers nuevos.
- `room_id` siempre `.upper()`. Reconnect: el cliente re-emite último `join_game`; el servidor reconoce asiento por **mismo nombre** (reconexión por nombre).
- Chat max 500 chars, cartas propias max 200 — validados en servidor, `maxlength` del cliente no es defensa.
- `GET /metrics` → snapshot numérico (salas, jugadores, fases presencia, salas rechazadas por techo).

## Presencia y reaper

- Dos fases (`models/presence.py`): `vivo --(silencio ≥ suspect_after)--> sospechoso --(gracia)--> purgado`. Defaults `PRESENCE_SUSPECT=120s + GRACE=180s` (=300s total heredado), configurables por env (`PRESENCE_SUSPECT_SECONDS`, `PRESENCE_GRACE_SECONDS`, `ROOM_*`). Valor inválido → default, nunca impedir arranque.
- Reaper de fondo: emite `heartbeat_ping`, espera `heartbeat_pong`, avanza máquina, purga jugadores/salas vacías. `disconnect` no siempre llega tras ngrok/half-open — no asumirlo.
- Frontend reconstruye presencia desde `game_update` + aplica `presence_alert` encima; avisos expiran (`MAX_AVISOS=4`, `VIDA_AVISO_MS=9000`).

## Cartas y persistencia

- Base: `backend/DataScraping/CAH-es-set-actualizado.json` (`whiteCards: [text]`, `blackCards: [{text, pick}]`).
- Custom globales: Taller (CardWorkshop) → `cartasCustom.json`, validación anti-duplicados aprox. en cliente+servidor. Import/export `.json` mismo formato: inválidas/duplicadas se omiten individualmente, resto entra al mazo global y salas vivas al instante.
- Sincronización al arranque (`cargar_cartas()` en `app.py`): merge externa `custom_cards/cartasCustom.json` + interna `DataScraping/cartasCustom.json` + cualquier otro `.json` en `custom_cards/`, escribe en ambos sitios.
- Por sala: cartas blancas temporales (coma-separadas) en lobby, no persisten.
- Env overrides (usados por tests): `INTERNAL_CUSTOM_PATH`, `EXTERNAL_CARDS_DIR`. Volúmenes compose: `./backend/DataScraping:/app/backend/DataScraping`, `./custom_cards:/app/custom_cards`.
- Reglas de validación (ver `custom_cards.py` + `tests/conftest.py::BASE_SET`): blancas no vacías/no-numéricas/≤200 chars; negras requieren `_` hueco, `pick` numérico en rango, no vacías.

## Comandos

```bash
# Docker (vía recomendada)
cp .env.example .env   # poner NGROK_AUTHTOKEN real
docker compose up -d --build     # OBLIGATORIO --build tras cambiar código (sirve copia compilada)
docker logs cclh_ngrok_url       # URL pública https://xxxx.ngrok-free.app
docker compose down

# Local sin Docker
cd frontend && npm install && npm run build && cd ..
cd backend && pip install -r requirements.txt && python app.py  # :3000
# o producción local: gunicorn -k gthread -w 1 --threads 100 --timeout 120 --bind 0.0.0.0:3000 app:app

# Frontend dev hot-reload: npm run dev — pero socket.io va contra mismo origen,
# así que o se desarrolla contra el build o se añade proxy en vite.config.js (hoy no hay).

# Tests backend
pip install -r backend/requirements.txt -r backend/requirements-dev.txt
python3 -m pytest backend/tests -q        # desde raíz (lo que corre el CI)
# Frontend
cd frontend && npm run lint               # oxlint
```

## Tests (backend/tests/)

- Fixtures en `conftest.py`: `INTERNAL_CUSTOM_PATH`+`EXTERNAL_CARDS_DIR` en tempdir (nunca tocan datos reales), import único de `app`, `manager` con estado limpio por test, `client` = `socketio.test_client` en proceso (sin red/puertos).
- `deck_audit` (autouse): engancha `send_room_update` y corre `room.audit_cards()` tras cada acción + al final; falla si hay carta perdida/duplicada. **Toda acción nueva debe pasar por `send_room_update` para quedar auditada.**
- Archivos: `test_custom_card_crud`, `test_export_deck`, `test_import_custom_cards`, `test_deck`, `test_presence_machine`, `test_reaper_defenses`, `test_disconnect_roles`, `test_emit_locking`, `test_fuzz_rules`, `test_limites_de_sala`, `test_metrics`.
- Al añadir una defensa, añadir su test (lo verifica `reversion.py`). Al tocar emisión/locks, correr `bench_emision.py`.

## Convenciones para agentes

- Comentarios del repo en español, largos y explicativos (el porqué, no el qué). Mantener ese estilo; no borrar contexto histórico de cerrojos/presencia/outbox.
- `.env`, `backend/DataScraping/cartasCustom.json`, `start.sh`, `ngrok*` están en `.gitignore` — no commitear secretos ni cartas locales.
- Tras cambio de código: `docker compose up -d --build` (reiniciar no basta). Tras cambio de frontend: `npm run build` antes de probar contra backend.
- No añadir workers/hilos fuera de `Room.lock`/`_registro_lock`, no emitir dentro del lock (usar Outbox), respetar orden registro→sala.
- Frontend: estado de verdad siempre de `game_update`; no duplicar lógica de juego en cliente. Validar también en servidor.
- Troubleshooting: sin enlace público → `NGROK_AUTHTOKEN` + `docker logs cclh_ngrok` + `:4040`; puerto 3000 ocupado → remapear en compose; jugador congelado → recargar y reentrar con mismo nombre/sala.
