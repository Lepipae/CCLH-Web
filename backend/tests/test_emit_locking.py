"""Emisión fuera del cerrojo global.

El GameManager serializa la partida entera con un RLock global. Medido con un
`emit` de 5 ms: una difusión a 8 jugadores retenía el cerrojo 40 ms (de 41 ms de
total) y una pasada del reaper con 49 conectados retenía 245 ms. Todo el
servidor se paraba mientras un cliente tardase en leer su búfer.

Aquí se comprueba, con un socket falso que registra y sabe ponerse lento:

1. Que NINGUNA emisión (game_update, chat, join_error, latido, respuesta de
   cartas) ocurre con el cerrojo global tomado. No es una medición de tiempo:
   el propio `emit` mira quién tiene el cerrojo en ese instante.
2. Que un cliente lento en la mesa A no bloquea a la mesa B. Es la pregunta que
   motivó el cambio.
3. Que un cliente nunca ve un estado viejo después de uno nuevo, y que a pesar
   de eso no se pierde ni una sola actualización.

Todos los tests de 1 y 2 fallan si el drenaje vuelve a meterse dentro del
`with self._lock` (comprobado revirtiéndolo).
"""
import os
import sys
import threading
import time

import pytest

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import models.game_manager as gm_module  # noqa: E402
from models.outbox import EmitBatch, RoomChannel  # noqa: E402
from models.presence import PresencePolicy  # noqa: E402

BLANCAS = [f"blanca {i}" for i in range(60)]
NEGRAS = [{"text": f"negra {i} _", "pick": 1} for i in range(20)]


class CerrojoEspia:
    """Delegación del RLock global que lleva la cuenta de si alguien lo tiene.

    No se puede auditar preguntando desde dentro: un RLock es REENTRANTE, así
    que el hilo que emite siempre puede volver a tomarlo y la pregunta se
    contesta sola. Hace falta un testigo de fuera, y este es. Como todo lo que
    usa el cerrojo pasa por aquí (el GameManager y su Outbox comparten la misma
    instancia) el contador es exacto."""

    def __init__(self, real):
        self._real = real
        self._depth = 0

    def acquire(self, blocking=True, timeout=-1):
        gained = self._real.acquire(blocking, timeout) if timeout != -1 \
            else self._real.acquire(blocking)
        if gained:
            self._depth += 1
        return gained

    def release(self):
        self._real.release()
        self._depth -= 1

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    @property
    def libre(self):
        return self._depth == 0


class SocketFalso:
    """Socket que registra todo y sabe ponerse lento a propósito.

    `bloquear` levanta la puerta, `solo_para` dice a QUIÉN se le cuelga (un
    juego de pares (evento, destinatario): el cliente lento de una mesa no
    tiene por qué estropear a las demás) y `destapar()` la cierra otra vez.
    No se destapa solo: la cierra el test en su `finally`, para que un fallo se
    lea como un fallo y no como una suite colgada."""

    def __init__(self):
        self.registro = []          # (evento, destinatario, payload)
        self.bloquear = threading.Event()
        self.liberar = threading.Event()
        self.dentro = threading.Event()
        self.solo_para = None       # None = todo el tráfico se cuelga
        self.jitter = 0.0           # retardo por emit, para cruzar dos hilos
        self.cerrojo = None         # CerrojoEspia del GameManager
        self.cerrojo_ocupado = []   # [bool] por emisión: ¿lo tenía alguien?

    def destapar(self):
        self.bloquear.clear()
        self.liberar.set()

    def emit(self, event, payload=None, to=None):
        # Desde el propio hilo que emite: si alguien tuviera el cerrojo global,
        # la partida entera estaría detenida por este cliente.
        self.cerrojo_ocupado.append(self.cerrojo is not None and not self.cerrojo.libre)
        if self.jitter:
            time.sleep(self.jitter)
        if self.bloquear.is_set() and (self.solo_para is None or (event, to) in self.solo_para):
            self.dentro.set()
            # Techo generoso: si un test se olvida de destapar, se desbloquea
            # solo y falla el aserto, en vez de colgar la suite entera.
            self.liberar.wait(30)
        self.registro.append((event, to, payload))

    def start_background_task(self, fn, *args, **kwargs):
        return None  # sin reaper ni auto-avance: los tests los disparan a mano

    def sleep(self, seconds):
        return None

    # -- utilidades de aserto ------------------------------------------------
    def eventos(self, nombre):
        return [r for r in self.registro if r[0] == nombre]

    def updates_de(self, sid):
        return [r[2] for r in self.registro if r[0] == 'game_update' and r[1] == sid]

    def limpiar(self):
        self.registro.clear()
        self.cerrojo_ocupado.clear()


@pytest.fixture()
def sock():
    return SocketFalso()


@pytest.fixture()
def gm(sock, test_paths):
    """GameManager propio, con mazos pequeños y sin tareas de fondo.

    Pide `test_paths` para que las cartas custom de estos tests se escriban en
    el directorio temporal y no en el archivo real de la aplicación."""
    manager = gm_module.GameManager(sock, list(BLANCAS), list(NEGRAS))
    espia = CerrojoEspia(manager._lock)
    manager._lock = espia
    manager._outbox._lock = espia
    sock.cerrojo = espia
    return manager


def sentar(gm, room_id, n, con=False):
    """Sienta n jugadores en la sala y devuelve sus sids."""
    sids = []
    for i in range(n):
        sid = f"{room_id}-{i}"
        gm.join_game(sid, f"J{i}", room_id)
        if con:
            gm.heartbeat(sid)
        sids.append(sid)
    return sids


def arrancar(gm, room_id):
    """Arranca la sala con el juez FIJO (el primero en llegar) y lo devuelve.

    El turno 'random' por defecto haría depender cada test del azar: jugar una
    carta es ilegal para el juez, y la mitad de las veces lo sería."""
    sids = list(gm.rooms[room_id].players)
    gm.rooms[room_id].options['turn_order'] = 'sequential'
    gm.start_game(sids[0], room_id)
    return gm.rooms[room_id].czar


def en_hilo(fn):
    hilo = threading.Thread(target=fn)
    hilo.start()
    return hilo


def termino(hilo, segundos=2):
    """True si el hilo acabó dentro del plazo."""
    hilo.join(segundos)
    return not hilo.is_alive()


# --- 1. Ninguna E/S con el cerrojo global tomado ----------------------------

def test_game_update_se_emite_sin_el_cerrojo_global(gm, sock):
    """La difusión es el caso caro (una emisión por jugador y acción): si alguna
    ocurre con el cerrojo tomado, ese cliente para al servidor entero."""
    sids = sentar(gm, "R1", 4)
    arrancar(gm, "R1")
    sock.limpiar()

    gm.play_card(sids[1], "R1", 0)
    gm.send_chat(sids[2], "R1", "hola")

    assert sock.cerrojo_ocupado == [False] * len(sock.registro)
    assert len(sock.updates_de(sids[0])) >= 1


def test_todos_los_canales_de_salida_se_emiten_sin_el_cerrojo(gm, sock):
    """No solo el game_update: chat de sistema, chat de jugador, join_error,
    latido del reaper, avisos de presencia y respuesta de la interfaz de cartas."""
    sids = sentar(gm, "R1", 3, con=True)
    arrancar(gm, "R1")
    sock.limpiar()

    gm.send_chat(sids[0], "R1", "hola")             # chat de jugador
    gm.send_chat_system("R1", "mensaje del sistema")
    gm.add_room_cards("R1", "carta a, carta b")     # cartas al mazo + chat
    gm.join_game("intruso", "J0", "R1")            # join_error (nombre repetido)
    # Un jugador al que se le corta el silencio: el reaper lo sospecha y le
    # manda el reto. Los avisos de presencia también salen fuera del cerrojo.
    gm.PRESENCE = PresencePolicy(0.05, 180)
    sala = gm.rooms["R1"]
    for p in sala.players.values():
        p.heartbeat_pongs = 1
        p.last_seen = time.time() - 3600
    gm.reap_empty_rooms()
    gm.add_custom_card("white", "Blanca muy original de verdad", 1, lambda r: None)

    eventos = [r[0] for r in sock.registro]
    assert "chat_message" in eventos
    assert "join_error" in eventos
    assert "heartbeat_ping" in eventos
    assert "presence_alert" in eventos
    assert "presence_challenge" in eventos
    assert len(sock.cerrojo_ocupado) == len(sock.registro)
    assert sock.cerrojo_ocupado and not any(sock.cerrojo_ocupado)


def test_la_respuesta_a_cartas_custom_no_responde_dentro_del_cerrojo(gm, sock):
    """El `respond_cb` de la interfaz de cartas escribe en el socket del
    cliente que pregunta: también sale fuera del cerrojo."""
    respuestas = []
    ocupado = []

    def cb(respuesta):
        # Se mide desde dentro del callback, que es justo cuando se respondería.
        ocupado.append(not gm._lock.libre)
        respuestas.append(respuesta)

    gm.add_custom_card("white", "Carta para medir el cerrojo", 1, cb)
    assert respuestas and respuestas[0]['success'] is True
    assert ocupado == [False]


# --- 2. Un cliente lento no bloquea a los demás -----------------------------

def test_una_mesa_atascada_no_bloquea_a_otra(gm, sock):
    """La pregunta que motivó el cambio: la mesa A tiene un socket colgado
    escribiendo; la mesa B tiene que poder seguir jugando mientras tanto."""
    sids_a = sentar(gm, "A", 8, con=True)
    arrancar(gm, "A")
    sock.limpiar()

    sock.solo_para = {('chat_message', 'A')}
    sock.bloquear.set()
    atascada = en_hilo(lambda: gm.send_chat(sids_a[0], "A", "hola"))
    try:
        assert sock.dentro.wait(5), "el emit atascado ni siquiera entró"
        assert gm._lock.libre, "con el emit en curso el cerrojo sigue tomado"

        juego = {}

        def mesa_b():
            sids = sentar(gm, "B", 3, con=True)
            arrancar(gm, "B")
            gm.play_card(sids[1], "B", 0)
            juego['sids'] = sids

        rival = en_hilo(mesa_b)
        assert termino(rival), \
            "otra mesa quedó esperando a que A terminara de escribir en su socket"
        assert gm.rooms["B"].state == 'playing'
        assert gm.rooms["B"].players[juego['sids'][1]].played_card is not None
    finally:
        sock.destapar()
        atascada.join(10)
    assert not atascada.is_alive()


def test_el_reaper_colgado_no_bloquea_a_la_partida(gm, sock):
    """El reaper recorre TODAS las salas y emite un latido a cada jugador
    conectado: con 49 conectados eran 245 ms de cerrojo global por su cuenta."""
    for i in range(12):
        sentar(gm, f"L{i}", 4, con=True)
    sock.limpiar()
    # Solo se atascan los latidos de las mesas L: las de la partida nueva salen
    # por su socket, como en la vida real.
    sock.solo_para = {('heartbeat_ping', f"L{i}-{j}") for i in range(12) for j in range(4)}
    sock.bloquear.set()

    atascado = en_hilo(gm.reap_empty_rooms)
    try:
        assert sock.dentro.wait(5), "el reaper ni siquiera entró en el emit"
        assert gm._lock.libre, "con el reaper emitiendo el cerrojo sigue tomado"

        def partida():
            sids = sentar(gm, "VIVA", 2, con=True)
            arrancar(gm, "VIVA")
            gm.vote_renew(sids[0], "VIVA")

        rival = en_hilo(partida)
        assert termino(rival), "la partida se quedó esperando al reaper"
        assert gm.rooms["VIVA"].state == 'playing'
    finally:
        sock.destapar()
        atascado.join(30)
    assert not atascado.is_alive()
    # Los 48 latidos que había al empezar la pasada salieron todos: diferirlos
    # no es perderlos. (Los de VIVA se unieron cuando el reaper ya había
    # recorrido las salas, así que a esa pasada no le tocaban.)
    assert len(sock.eventos('heartbeat_ping')) == 12 * 4


# --- 3. Orden de entrega: nada de estados rancios ---------------------------

def test_una_instantanea_obsoleta_se_descarta(gm, sock):
    """La carrera entre dos hilos de la misma sala. Gana la que toma antes el
    cerrojo del canal, y la perdedora lleva un estado más antiguo: si saliera
    después, el cliente vería como una jugada se deshecha sola.

    Con el cerrojo global esto no hacía falta (él solo ordenaba todo). Ahora sí:
    es el fallo que introduciría un 'solo mueve el emit fuera del lock' ingenuo.
    """
    sids = sentar(gm, "R1", 2, con=True)
    arrancar(gm, "R1")
    sock.limpiar()

    canal = gm._outbox.channel("R1")
    vieja = EmitBatch()
    vieja.add_update("R1", canal, sids[0], canal.next_version(), {"hand": ["vieja"]})
    nueva = EmitBatch()
    nueva.add_update("R1", canal, sids[0], canal.next_version(), {"hand": ["nueva"]})

    gm._outbox.flush(nueva)     # la carrera la gana la foto más nueva
    gm._outbox.flush(vieja)      # la vieja llega tarde y se descarta
    assert sock.updates_de(sids[0]) == [{"hand": ["nueva"]}]


def test_el_ultimo_estado_de_cada_jugador_es_el_real(gm, sock):
    """Varios hilos jugando a la vez con un socket de duración irregular, para
    que las difusiones se crucen de verdad: el último `game_update` que ve cada
    jugador tiene que ser el estado final de la sala, nunca uno rancio.

    La regla que lo garantiza (descartar la instantánea vieja) la comprueba
    `test_una_instantanea_obsoleta_se_descarta` de forma determinista; aquí lo
    que se vigila es el efecto de extremo a extremo con la carrera de verdad."""
    sids = sentar(gm, "R1", 4, con=True)
    arrancar(gm, "R1")
    sock.limpiar()
    sock.jitter = 0.003

    for _ in range(3):
        hilos = [en_hilo(lambda s=s: gm.play_card(s, "R1", 0)) for s in sids[1:]]
        for hilo in hilos:
            assert termino(hilo, 10), "una jugada se quedó colgada"
        for sid in sids:
            assert sock.updates_de(sid)[-1] == gm.rooms["R1"].to_dict(for_sid=sid), \
                f"el jugador {sid} se quedó con un estado viejo"


def test_no_se_pierde_ninguna_actualizacion(gm, sock):
    """Descartar lo obsoleto no puede acabar siendo perder actualizaciones: al
    final todos han recibido el estado final y nadie se ha quedado sin nada."""
    sids = sentar(gm, "R1", 4, con=True)
    arrancar(gm, "R1")
    sock.limpiar()

    for i, sid in enumerate(sids[1:], start=1):
        gm.play_card(sid, "R1", 0)
        gm.send_chat(sid, "R1", f"mensaje {i}")

    for sid in sids:
        updates = sock.updates_de(sid)
        assert updates, f"{sid} no recibió ni una actualización"
        assert updates[-1] == gm.rooms["R1"].to_dict(for_sid=sid)
    assert len(sock.eventos('chat_message')) == 3


# --- 4. Coalescencia: difundir dos veces la misma sala es difundir una -------

def test_dos_difusiones_en_la_misma_transaccion_solo_salen_una(gm, sock):
    """Unirse con el juez ya marchado sanea la mesa y la difunde, y luego la
    unión vuelve a difundirla: la misma foto en dos pasos de una sola
    transacción. Sale una."""
    sids = sentar(gm, "R1", 2, con=True)
    arrancar(gm, "R1")
    sock.limpiar()

    with gm._outbox.transaction():
        gm.send_room_update("R1")
        gm.send_room_update("R1")
        gm.send_room_update("R1")

    for sid in sids:
        assert len(sock.updates_de(sid)) == 1


def test_la_coalescencia_solo_descarta_lo_que_ya_no_vale(gm, sock):
    """Contrapeso del anterior: si entre una difusión y otra pasa algo de
    verdad (se va un jugador), las dos fotos son estados distintos y solo
    puede salir la última, porque en cuanto existe la nueva la otra está
    obsoleta. Quien se fue no recibe la foto en la que ya no estaba."""
    sids = sentar(gm, "R1", 3, con=True)
    arrancar(gm, "R1")
    sock.limpiar()

    with gm._outbox.transaction():
        gm.send_room_update("R1")            # los tres, con el estado inicial
        gm.disconnect(sids[2])               # uno se va: el estado ya no es el
    assert len(sock.updates_de(sids[0])) == 1
    assert sock.updates_de(sids[0])[-1] == gm.rooms["R1"].to_dict(for_sid=sids[0])
    assert sock.updates_de(sids[2]) == []


# --- 5. La sala borrada limpia su estado de emisión -----------------------

def test_borrar_una_sala_libera_su_canal(gm, sock):
    """Los canales de salida son memoria: una sala que se va (el reaper limpia
    las vacías) no puede dejar su cerrojo colgando para siempre."""
    sentar(gm, "R1", 2, con=True)
    gm.send_room_update("R1")
    assert "R1" in gm._outbox._channels

    # La sala vacía pasa por las dos fases antes de purgarse, como un jugador.
    # Nunca llegó a empezar, así que le toca la política corta de sala nueva.
    gm.ROOM_LIFECYCLE_NUEVA = PresencePolicy(0.05, 0)
    gm.rooms["R1"].empty_since = time.time() - 1
    gm.reap_empty_rooms()
    gm.reap_empty_rooms()

    assert "R1" not in gm.rooms
    assert "R1" not in gm._outbox._channels
    # Un snapshot construido antes del borrado, si llega tarde, se descarta
    # en vez de mandarle mensajes a una sala que ya no existe.
    canal = RoomChannel()
    canal.closed = True
    loteria = EmitBatch()
    loteria.add_update("R1", canal, "fantasma", canal.next_version(), {"hand": []})
    gm._outbox.flush(loteria)
    assert sock.registro == [] or sock.registro[-1][1] != "fantasma"
