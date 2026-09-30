"""Emisión fuera de los cerrojos de juego.

El GameManager serializa la partida con un cerrojo POR SALA y otro de REGISTRO
(lo que no es de una mesa). Antes había un único RLock global para todo el
proceso: medido con un `emit` de 5 ms, una difusión a 8 jugadores retenía el
cerrojo 40 ms (de 41 ms de total) y una pasada del reaper con 49 conectados
retenía 245 ms. Todo el servidor se paraba mientras un cliente tardase en leer
su búfer.

Aquí se comprueba, con un socket falso que registra y sabe ponerse lento:

1. Que NINGUNA emisión (game_update, chat, join_error, latido, respuesta de
   cartas) ocurre con un cerrojo de juego tomado: ni el de la sala de la que
   sale el mensaje, ni el de ninguna otra, ni el del registro. No es una
   medición de tiempo: el propio `emit` mira quién tiene cada cerrojo en ese
   instante, a través de los espías que la fixture cuelga de todas las salas.
2. Que un cliente lento en la mesa A no bloquea a la mesa B. Es la pregunta que
   motivó el cambio.
3. Que un cliente nunca ve un estado viejo después de uno nuevo, y que a pesar
   de eso no se pierde ni una sola actualización.
4. Que el orden de cerrojos (registro antes que sala, nunca al revés) se
   upholda, y que dos hilos cambiando de sala el uno al otro no se MUEREN.

Todos los tests de 1 y 2 fallan si el drenaje vuelve a meterse dentro del
cerrojo (comprobado revirtiéndolo).
"""
import os
import sys
import threading
import time
from contextlib import contextmanager

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
    """Delegación de un cerrojo de juego que lleva la cuenta de si alguien lo
    tiene. Ahora hay dos (el de cada sala y el del registro) y los dos se
    espían: "no se emite con el cerrojo tomado" tiene que significar ninguno de
    los dos, no solo el que quede en pie.

    No se puede auditar preguntando desde dentro: un RLock es REENTRANTE, así
    que el hilo que emite siempre puede volver a tomarlo y la pregunta se
    contesta sola. Hace falta un testigo de fuera, y este es. Como todo lo que
    usa un cerrojo de juego pasa por aquí, el contador es exacto."""

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
        self.registro_cerrojo = None    # CerrojoEspia del cerrojo de registro
        self._espias = {}               # room_id -> CerrojoEspia de esa sala
        self._candado = threading.Lock()
        self.cerrojo_ocupado = []   # [bool] por emisión: ¿había alguno tomado?

    def espiar_sala(self, room_id, espia):
        """La fixture cuelga aquí el espía de cada sala que se crea."""
        with self._candado:
            self._espias[room_id] = espia

    def espias(self):
        with self._candado:
            return list(self._espias.values())

    def espia_de(self, room_id):
        with self._candado:
            return self._espias.get(room_id)

    def ocupado(self):
        """¿Alguien tiene un cerrojo de juego en este instante?"""
        return (self.registro_cerrojo is not None and not self.registro_cerrojo.libre) \
            or any(not e.libre for e in self.espias())

    def destapar(self):
        self.bloquear.clear()
        self.liberar.set()

    def emit(self, event, payload=None, to=None):
        # Desde el propio hilo que emite: si alguien tuviera un cerrojo de
        # juego, esa partida (o el registro entero) estaría detenida por este
        # cliente.
        self.cerrojo_ocupado.append(self.ocupado())
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
def gm(sock, test_paths, monkeypatch):
    """GameManager propio, con mazos pequeños y sin tareas de fondo.

    Pide `test_paths` para que las cartas custom de estos tests se escriban en
    el directorio temporal y no en el archivo real de la aplicación.

    Y con la vista puesta en los cerrojos: el registro recibe un espía, y cada
    sala que se cree recibe el suyo al construirse (parcheando la clase `Room`
    que usa el GameManager). Así "ninguna E/S ocurre con un cerrojo de juego
    tomado" se puede comprobar contra los dos sitios donde vive estado, en vez
    de contra un cerrojo global que ya no existe."""
    manager = gm_module.GameManager(sock, list(BLANCAS), list(NEGRAS))
    registro = CerrojoEspia(manager._registro_lock)
    manager._registro_lock = registro
    sock.registro_cerrojo = registro

    class RoomConEspia(gm_module.Room):
        def __init__(self, room_id, *args, **kwargs):
            super().__init__(room_id, *args, **kwargs)
            self.lock = CerrojoEspia(self.lock)
            sock.espiar_sala(room_id, self.lock)

    monkeypatch.setattr(gm_module, "Room", RoomConEspia)
    return manager


@contextmanager
def transaccion(gm):
    """Un lote de salida a mano, como el que abren `_tx_sala`/`_tx_registro`.

    Los tests lo necesitan para montar el caso "la misma sala se difunde dos
    veces dentro de una sola transacción" sin meter una operación de juego por
    medio. No toma ningún cerrojo: se lo pone quien llama, como en producción."""
    with gm._outbox.lote() as lote:
        yield lote
    gm._outbox.drenar(lote)


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


# --- 1. Ninguna E/S con un cerrojo de juego tomado ---------------------------

def test_game_update_se_emite_sin_cerrojo_de_juego(gm, sock):
    """La difusión es el caso caro (una emisión por jugador y acción): si alguna
    ocurre con un cerrojo tomado, ese cliente para al mesa —y, si es el del
    registro, a todas las demás."""
    sids = sentar(gm, "R1", 4)
    arrancar(gm, "R1")
    sock.limpiar()

    gm.play_card(sids[1], "R1", 0)
    gm.send_chat(sids[2], "R1", "hola")

    assert sock.cerrojo_ocupado == [False] * len(sock.registro)
    assert len(sock.updates_de(sids[0])) >= 1


def test_todos_los_canales_de_salida_se_emiten_sin_cerrojo(gm, sock):
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


def test_el_rechazo_por_servidor_lleno_no_emite_con_el_registro(gm, sock):
    """El `join_error` del techo de salas es la única emisión que se decide dentro
    del REGISTRO, y por eso es la que más fácil se le escaparía a alguien: emitirla
    con el registro tomado convierte "un cliente lento bloquea el registro" en
    "un cliente lento bloquea la entrada a todas las salas del proceso"."""
    gm.MAX_ROOMS = 0
    sock.limpiar()

    assert gm.join_game("s1", "Ana", "LLENA") is False
    assert [r[0] for r in sock.registro] == ["join_error"]
    assert sock.cerrojo_ocupado == [False]
    assert "LLENA" not in gm.rooms


def test_la_respuesta_a_cartas_custom_no_responde_dentro_del_cerrojo(gm, sock):
    """El `respond_cb` de la interfaz de cartas escribe en el socket del
    cliente que pregunta: también sale fuera del cerrojo."""
    respuestas = []
    ocupado = []

    def cb(respuesta):
        # Se mide desde dentro del callback, que es justo cuando se respondería.
        ocupado.append(sock.ocupado())
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
        assert not sock.ocupado(), "con el emit en curso hay un cerrojo tomado"
        assert sock.espia_de("A").libre, "el cerrojo de la mesa A sigue tomado"

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
    conectado: con 49 conectados eran 245 ms de cerrojo global por su cuenta.
    Ahora recorre una mesa por vez, así que el atasco además es por mesa."""
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
        assert not sock.ocupado(), "con el reaper emitiendo hay un cerrojo tomado"

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

    with transaccion(gm):
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

    with transaccion(gm):
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


# --- 6. El orden de los cerrojos: registro antes que sala, nunca al reves ----

def test_tomar_el_registro_con_una_sala_en_la_mano_falla(gm):
    """El deadlock clásico es este: un hilo con la sala A esperando el registro, y
    otro con el registro esperando la sala A. El orden de cerrojos lo impide, y
    `_registro()` no se fía de la buena fe: si alguien lo incumple, revienta en
    el sitio donde se introdujo el error. Un deadlock aquí es el servidor parado
    entero, y ningún test lo detectaría a tiempo."""

    sala = gm.rooms['A'] = gm_module.Room('A', 's1', list(BLANCAS), list(NEGRAS))
    with gm._sala(sala):
        with pytest.raises(RuntimeError, match="orden de cerrojos"):
            with gm._registro():
                pass
    # Y al revés (registro -> sala) sí se puede, que es el orden bueno.
    with gm._registro():
        with gm._sala(sala):
            pass


def test_el_registro_se_puede_tomar_entre_mesas(gm):
    """La otra mitad de la regla: tomar el registro y, dentro, el cerrojo de una
    sala, es lo que hacen las cartas propias (importar un set las inyecta en
    todas las mesas vivas). Tiene que funcionar, y sobre todo tiene que poder
    repetirse mesa a mesa sin quedarse colgado a mitad."""

    def importar():
        with gm._registro():
            for sala in sorted(gm.rooms.values(), key=lambda r: r.room_id):
                with gm._sala(sala):
                    sala.deck.inject(['blanca de importación'])

    for i in range(4):
        sentar(gm, f"M{i}", 2, con=True)

    hilo = en_hilo(importar)
    assert termino(hilo, 5), "importar cartas se quedó esperando a una mesa"
    for i in range(4):
        # El mazo CRECE (no se reparte): la carta nueva está disponible en las
        # cuatro mesas y no le ha quitado nada a nadie de la mano.
        mazo = gm.rooms[f"M{i}"].deck
        assert mazo.size == len(BLANCAS) + 1
        assert list(mazo.pool()).count('blanca de importación') == 1


# --- 7. Dos hilos, dos salas, ningun deadlock --------------------------------

def test_cambiarse_de_sala_en_las_direcciones_opuestas_no_se_muere(gm, sock):
    """La carrera que el orden total de `_tx_salas` elimina: dos clientes que se
    cambian el uno al otro de sala a la vez toman las mismas dos mesas. Si cada
    uno cogiera la suya primero, se quedarían esperando el uno al otro para
    siempre. Ordenando por `room_id` los dos toman el mismo orden y no hay ciclo.
    """
    a = [gm.join_game(f"A-{i}", f"Ana{i}", "A") for i in range(2)]
    b = [gm.join_game(f"B-{i}", f"Beto{i}", "B") for i in range(2)]
    assert all(a) and all(b)
    sock.limpiar()

    fallos = []

    def migrar(sid, nombre, destino):
        try:
            assert gm.join_game(sid, nombre, destino)
        except Exception as e:          # noqa: BLE001 - el test lo reporta
            fallos.append(e)

    hilos = [en_hilo(lambda: migrar("A-0", "Ana0", "B")),
             en_hilo(lambda: migrar("B-0", "Beto0", "A"))]
    for hilo in hilos:
        assert termino(hilo, 10), "cambiarse de sala se quedó esperando a la otra mesa"

    assert not fallos, f"la migración lanzó {fallos}"
    # Cada uno está en la sala a la que se mudó, y en la que dejó el asiento
    # DESCONECTADO (se conserva a propósito: guarda la mano y el marcador). Lo
    # que no puede pasar es que siga ahí como 'conectado': el reaper lo daría
    # por zombi y, al purgarlo, le cortaría la sesión en la sala nueva.
    assert gm.rooms["B"].asiento("A-0") is not None
    assert gm.rooms["A"].asiento("B-0") is not None
    assert gm.rooms["A"].players["A-0"].is_connected is False
    assert gm.rooms["B"].players["B-0"].is_connected is False
    assert gm.rooms["A"].asiento("A-0") is None
    assert gm.rooms["B"].asiento("B-0") is None
