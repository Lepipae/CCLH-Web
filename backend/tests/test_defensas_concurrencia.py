"""Defensas de concurrencia y validación del Agente A.

Cada test caza un fallo concreto que el código ya no puede tener:

1. Descarte por destinatario (`Outbox.flush`): las versiones salen de un
   contador único del canal, así que comparar contra un máximo global
   descartaba la foto pendiente de A solo porque la de B (posterior en el
   contador, pero de OTRO jugador) hubiese salido antes. A se quedaba sin nada.
2. Drenaje en `finally` (`_tx_sala`/`_tx_registro`): si el cuerpo lanzaba, el
   lote se perdía sin emitir lo ya encolado.
3. `play_card` rechaza `bool` (`type(i) is int`) y exige las cartas exactas que
   pide la negra (`len == pick`): `True` jugaba la carta 1, y una jugada corta
   o larga quemaba cartas o dejaba rondas cojas.
4. Robo de negra sin `IndexError` (`_draw_black_card`): con el mazo de sala y el
   global vacíos se conserva la actual en vez de reventar.
5. Token de generación (`round_gen`): una tarea de auto-avance dormida no puede
   avanzar una ronda posterior que casualmente tenga el mismo estado y juez.
"""
import os
import sys

import pytest

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import models.game_manager as gm_module  # noqa: E402
from models.outbox import EmitBatch  # noqa: E402

BLANCAS = [f"blanca {i}" for i in range(60)]
NEGRAS = [{"text": f"negra {i} _", "pick": 1} for i in range(20)]


class SocketFalso:
    """Socket en proceso: registra emisiones y encola tareas en vez de correrlas."""

    def __init__(self):
        self.registro = []      # (evento, destinatario, payload)
        self.tareas = []        # (fn, args) de start_background_task

    def emit(self, event, payload=None, to=None):
        self.registro.append((event, to, payload))

    def start_background_task(self, fn, *args, **kwargs):
        self.tareas.append((fn, args))
        return None

    def sleep(self, seconds):
        return None

    def updates_de(self, sid):
        return [p for e, t, p in self.registro if e == 'game_update' and t == sid]


@pytest.fixture()
def sock():
    return SocketFalso()


@pytest.fixture()
def gm(sock):
    manager = gm_module.GameManager(sock, list(BLANCAS), list(NEGRAS))
    return manager


def sentar(gm, room_id, n):
    sids = []
    for i in range(n):
        sid = f"{room_id}-{i}"
        assert gm.join_game(sid, f"J{i}", room_id) is True
        sids.append(sid)
    return sids


def arrancar(gm, room_id):
    """Arranca con turno secuencial para que el juez sea determinista (sids[0])."""
    gm.rooms[room_id].options['turn_order'] = 'sequential'
    sids = list(gm.rooms[room_id].players)
    gm.start_game(sids[0], room_id)
    return gm.rooms[room_id].czar


# --- 1. El descarte es por destinatario, no global ---------------------------

def test_la_foto_pendiente_de_a_sobrevive_a_la_de_b(gm, sock):
    """La carrera que el `last_emitted` global perdía: el hilo de A construye su
    foto (v1) y se queda parado; el de B construye la suya (v2, posterior en el
    contador único) y drena primero. La foto de A NO es obsoleta para A (nadie
    le entregó nada posterior), así que tiene que salir igual."""
    sids = sentar(gm, "R1", 2)
    sock.registro.clear()

    canal = gm._outbox.channel("R1")
    foto_a = EmitBatch()
    foto_a.add_update("R1", canal, sids[0], canal.next_version(), {"para": "A"})
    foto_b = EmitBatch()
    foto_b.add_update("R1", canal, sids[1], canal.next_version(), {"para": "B"})

    gm._outbox.flush(foto_b)   # la carrera la gana B, con la versión más alta
    gm._outbox.flush(foto_a)   # la de A llega tarde pero es lo único que tiene A

    assert sock.updates_de(sids[0]) == [{"para": "A"}]
    assert sock.updates_de(sids[1]) == [{"para": "B"}]


# --- 2. El lote se drena aunque el cuerpo lance -------------------------------

def test_tx_sala_drena_aunque_el_cuerpo_lance(gm, sock):
    sids = sentar(gm, "R1", 2)
    sock.registro.clear()

    class Bomba(Exception):
        pass

    with pytest.raises(Bomba):
        with gm._tx_sala("R1") as room:
            gm.send_room_update("R1", _room=room)
            raise Bomba()

    # Lo encolado antes de la excepción salió igual al salir de la transacción.
    assert len(sock.updates_de(sids[0])) == 1
    assert sock.updates_de(sids[0])[0] == gm.rooms["R1"].to_dict(for_sid=sids[0])
    # Y el cerrojo de la sala no quedó tomado por el camino.
    assert not room.lock.locked()


def test_tx_registro_drena_aunque_el_cuerpo_lance(gm, sock):
    sock.registro.clear()

    class Bomba(Exception):
        pass

    with pytest.raises(Bomba):
        with gm._tx_registro():
            gm._outbox.add_direct("R9", 'join_error', {'message': 'hola'}, to="s1")
            raise Bomba()

    assert sock.registro == [('join_error', 's1', {'message': 'hola'})]


# --- 3. Índices booleanos y recuento exacto de cartas -------------------------

def test_bool_no_juega_ninguna_carta(gm, sock):
    """`True == 1` en Python: con `isinstance(i, int)` el `True` del JSON jugaba
    la carta 1. Con `type(i) is int` la jugada se descarta entera."""
    sids = sentar(gm, "R1", 3)
    arrancar(gm, "R1")
    jugador = [s for s in sids if s != gm.rooms["R1"].czar][0]
    mano = list(gm.rooms["R1"].players[jugador].hand)

    gm.play_card(jugador, "R1", True)
    gm.play_card(jugador, "R1", [False])

    p = gm.rooms["R1"].players[jugador]
    assert p.played_card is None
    assert list(p.hand) == mano
    assert gm.rooms["R1"].state == 'playing'


def test_jugar_menos_cartas_que_el_pick_no_hace_nada(gm, sock):
    sids = sentar(gm, "R1", 3)
    arrancar(gm, "R1")
    room = gm.rooms["R1"]
    room.black_card = {"text": "Doble hueco _ y _", "pick": 2}
    jugador = [s for s in sids if s != room.czar][0]
    mano = list(room.players[jugador].hand)

    gm.play_card(jugador, "R1", 0)          # una sola carta para un pick 2
    assert room.players[jugador].played_card is None
    assert list(room.players[jugador].hand) == mano

    gm.play_card(jugador, "R1", [0, 1])     # las dos que pide la negra: sí juega
    assert room.players[jugador].played_card is not None
    assert len(room.players[jugador].hand) == len(mano) - 2


def test_jugar_mas_cartas_que_el_pick_no_hace_nada(gm, sock):
    sids = sentar(gm, "R1", 3)
    arrancar(gm, "R1")
    room = gm.rooms["R1"]
    assert room.black_card["pick"] == 1
    jugador = [s for s in sids if s != room.czar][0]
    mano = list(room.players[jugador].hand)

    gm.play_card(jugador, "R1", [0, 1])     # dos cartas para un pick 1
    assert room.players[jugador].played_card is None
    assert list(room.players[jugador].hand) == mano


# --- 4. Sin negras no hay IndexError ------------------------------------------

def test_avanzar_sin_negras_conserva_la_actual(gm, sock):
    """Con la sala y el global vacíos, avanzar de ronda conserva la negra que
    había en vez de reventar en `random.choice([])`."""
    sids = sentar(gm, "R1", 2)
    arrancar(gm, "R1")
    room = gm.rooms["R1"]
    actual = room.black_card
    room.available_blacks = []
    gm.global_black_cards = []

    gm.advance_to_next_round("R1")          # antes: IndexError aquí

    assert room.black_card == actual
    assert room.state == 'playing'


def test_cambiar_la_negra_sin_negras_no_hace_nada(gm, sock):
    sids = sentar(gm, "R1", 2)
    czar = arrancar(gm, "R1")
    room = gm.rooms["R1"]
    actual = room.black_card
    room.available_blacks = []
    gm.global_black_cards = []

    gm.change_black_card(czar, "R1")        # antes: IndexError aquí

    assert room.black_card == actual


def test_empezar_sin_negras_no_arranca_a_medias(gm, sock):
    """Sin ninguna negra en el sistema no se puede jugar: el arranque se aborta
    ANTES de repartir o de tocar el estado, en vez de reventar a mitad."""
    sids = sentar(gm, "R1", 2)
    gm.global_black_cards = []
    gm.rooms["R1"].available_blacks = []

    gm.start_game(sids[0], "R1")            # antes: IndexError aquí

    assert gm.rooms["R1"].state == 'waiting'
    assert gm.rooms["R1"].black_card is None


def test_la_sala_agotada_reusa_las_descartadas(gm, sock):
    """Caso normal de agotamiento: la sala gastó las suyas pero el global tiene.
    Rebaraja desde el global (las usadas vuelven) en vez de fallar."""
    sids = sentar(gm, "R1", 2)
    arrancar(gm, "R1")
    room = gm.rooms["R1"]
    room.available_blacks = []

    gm.advance_to_next_round("R1")

    assert room.black_card is not None
    assert len(room.available_blacks) == len(gm.global_black_cards) - 1


# --- 5. El auto-avance viejo no toca la ronda nueva ----------------------------

def test_auto_avance_de_ronda_vieja_no_avanza(gm, sock):
    """Dos jugadores en secuencial (s0 -> s1 -> s0): la tarea dormida de la ronda
    0 comparte juez y estado con el recuento de la ronda 2, pero su generación
    ya no vale y no puede avanzar nada. Solo la tarea vigente avanza."""
    sids = sentar(gm, "R1", 2)
    s0, s1 = sids[0], sids[1]
    assert arrancar(gm, "R1") == s0
    room = gm.rooms["R1"]

    # Ronda 0 (juez s0): gana s1, se programa la tarea con gen0.
    gm.play_card(s1, "R1", 0)
    assert room.state == 'judging'
    gm.choose_winner(s0, "R1", room.played_cards[0]['id'])
    assert room.state == 'round_end'
    tarea_vieja = sock.tareas[-1]
    assert tarea_vieja[1][0] == "R1" and tarea_vieja[1][1] == s0
    gen0 = tarea_vieja[1][2]

    # Ronda 1 (juez s1): se avanza a mano y se cierra.
    gm.force_next_round(s0, "R1")
    assert room.state == 'playing' and room.czar == s1
    gm.play_card(s0, "R1", 0)
    gm.choose_winner(s1, "R1", room.played_cards[0]['id'])
    assert room.state == 'round_end'

    # Ronda 2 (juez s0 otra vez): se cierra con el MISMO juez que la tarea vieja.
    gm.force_next_round(s1, "R1")
    assert room.state == 'playing' and room.czar == s0
    negra_r2 = room.black_card
    gm.play_card(s1, "R1", 0)
    gm.choose_winner(s0, "R1", room.played_cards[0]['id'])
    assert room.state == 'round_end' and room.czar == s0
    assert room.round_gen != gen0

    # La tarea de la ronda 0 despierta tarde: mismo estado y mismo juez, pero
    # otra generación. No toca nada.
    fn_vieja, args_viejos = tarea_vieja
    fn_vieja(*args_viejos)
    assert room.state == 'round_end'
    assert room.black_card == negra_r2

    # La tarea vigente sí avanza.
    fn_ahora, args_ahora = sock.tareas[-1]
    fn_ahora(*args_ahora)
    assert room.state == 'playing'
