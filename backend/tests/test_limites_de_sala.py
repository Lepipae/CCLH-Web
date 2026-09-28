"""Límites de recursos por sala: cuántas hay y cuánto viven.

`join_game` acepta cualquier código de sala que le envíen. Eso convierte la
creación de salas en una vía de agotamiento de memoria (y de tiempo del reaper,
que las recorre todas bajo el cerrojo global), así que hay dos brakes:

- **Techo de salas por proceso** (`MAX_ROOMS`): crear la sala N+1 falla con un
  `join_error` legible, sin llegar a reservar memoria.
- **Plazo de vacía según la sala haya empezado o no** (`ROOM_LIFECYCLE` vs
  `ROOM_LIFECYCLE_NUEVA`): un lobby que se creó y se quedó vacío no tiene nada
  que conservar y se va en ~20 s; una sala con partida sí conserva marcador y
  turnos, y se queda el plazo largo.

Los dos son configurables por entorno y un valor inválido no impide arrancar.
"""
import time

import pytest

from models.presence import PresencePolicy, SOSPECHOSO
from test_disconnect_roles import make_client, room_clients  # noqa: F401 (fixture)


@pytest.fixture()
def intento(app_module):
    """`join_game` como lo haría un cliente real, sin la fixture `room_clients`.

    Esa fixture exige que el jugador acabe en la sala, que es justo lo que NO
    pasa cuando el servidor rechaza la creación: aquí se necesita un cliente
    capable de quedarse en el login con un `join_error` en la mano."""
    creados = []

    def _join(room_id, nombre):
        cliente = make_client(app_module)
        creados.append(cliente)
        cliente.emit("join_game", {"name": nombre, "room_id": room_id})
        return cliente

    yield _join
    for c in creados:
        try:
            c.disconnect()
        except Exception:
            pass


def _politicas(manager, monkeypatch, sospecha, gracia):
    """Fija a la vez las dos políticas de sala, con el mismo plazo."""
    politica = PresencePolicy(sospecha, gracia)
    monkeypatch.setattr(manager, 'ROOM_LIFECYCLE', politica)
    monkeypatch.setattr(manager, 'ROOM_LIFECYCLE_NUEVA', politica)


def _sala_con_partida(manager, room_clients, room_id, nombres=("Ana", "Beto")):
    """Crea una sala y la ARRANCA, para que `ha_empezado` quede puesto."""
    sids = {}
    for nombre in nombres:
        _, sids[nombre] = room_clients(nombre, room_id)
    room = manager.rooms[room_id]
    room.options['turn_order'] = 'sequential'
    manager.start_game(sids[nombres[0]], room_id)
    assert room.ha_empezado is True
    return room, sids


def _vaciar(manager, room_clients, room, sids):
    """Deja la sala vacía y con el reloj muy atrasado (sin dormir: el reloj se
    envejece a mano). Acepta un dict de sids o uno suelto."""
    for sid in (sids.values() if isinstance(sids, dict) else [sids]):
        manager.disconnect(sid)
    assert room.empty_since is not None
    room.empty_since = time.time() - 10 ** 6   # vacía desde hace días


# --- Techo de salas ----------------------------------------------------------

class TestTechoDeSalas:
    def test_no_se_pueden_crear_mas_salas_que_el_techo(self, manager, room_clients,
                                                       monkeypatch, intento):
        monkeypatch.setattr(manager, 'MAX_ROOMS', 3)
        for i in range(3):
            room_clients(f"J{i}", f"S{i}")

        cliente = intento("S999", "Intruso")
        recibidos = cliente.get_received()

        errores = [e['args'][0] for e in recibidos if e['name'] == 'join_error']
        assert len(errores) == 1, "el rechazo tiene que llegar como join_error"
        assert "lleno" in errores[0]['message']
        # Y lo importante: la sala no existe. El rechazo no deja nada a medias.
        assert "S999" not in manager.rooms
        assert len(manager.rooms) == 3

    def test_el_techo_no_cierra_las_salas_que_ya_existen(self, manager, room_clients, monkeypatch):
        """Entrar en una sala que ya hay no crea nada, así que no puede ser el
        motivo de un rechazo: si lo fuera, un servidor lleno dejaría de dejar
        jugar en las mesas que ya están en marcha."""
        monkeypatch.setattr(manager, 'MAX_ROOMS', 2)
        for i in range(2):
            room_clients(f"J{i}", f"S{i}")

        cliente, sid = room_clients("Tarde", "S0")
        assert manager.rooms['S0'].players[sid].is_connected
        assert [e for e in cliente.get_received() if e['name'] == 'join_error'] == []

    def test_el_techo_se_libera_al_purgar(self, manager, room_clients, monkeypatch,
                                           intento):
        """Un servidor lleno se vacía solo: el reaper es la válvula de escape, y
        por eso el rechazo no es un callejón sin salida."""
        monkeypatch.setattr(manager, 'MAX_ROOMS', 1)
        _politicas(manager, monkeypatch, 0.05, 0)
        _, sids = room_clients("Ana", "LLENA")
        _vaciar(manager, room_clients, manager.rooms['LLENA'], sids)

        cliente = intento("OTRA", "Intruso")
        assert [e for e in cliente.get_received() if e['name'] == 'join_error']

        manager.reap_empty_rooms()
        manager.reap_empty_rooms()
        assert "LLENA" not in manager.rooms

        _, sid = room_clients("Tarde", "OTRA")
        assert manager.rooms['OTRA'].players[sid].is_connected
        assert manager.salas_rechazadas == 1

    def test_el_rechazo_se_cuenta(self, manager, room_clients, monkeypatch, intento):
        """Es un contador de ataque, no de juego: si sube solo, alguien está
        probando códigos de sala."""
        monkeypatch.setattr(manager, 'MAX_ROOMS', 1)
        room_clients("Ana", "S0")
        antes = manager.salas_rechazadas

        for i in range(5):
            intento(f"NO{i}", f"Bot{i}")

        assert manager.salas_rechazadas == antes + 5
        assert len(manager.rooms) == 1

    def test_el_rechazo_queda_registrado_en_el_log(self, manager, room_clients,
                                                   monkeypatch, caplog, intento):
        import logging
        monkeypatch.setattr(manager, 'MAX_ROOMS', 1)
        room_clients("Ana", "S0")

        with caplog.at_level(logging.WARNING, logger="models.game_manager"):
            intento("S9999", "Intruso")

        assert any("MAX_ROOMS" in r.getMessage() for r in caplog.records)

    def test_el_techo_no_numérico_no_impide_arrancar(self):
        """Como el resto de umbrales: un despliegue con la variable mal puesta
        tiene que arrancar igual, solo que con el techo nominal."""
        import os
        import importlib
        import models.game_manager as gm_module

        os.environ['MAX_ROOMS'] = 'no soy un número'
        try:
            importlib.reload(gm_module)
            assert gm_module.GameManager.MAX_ROOMS == 200
        finally:
            del os.environ['MAX_ROOMS']
            importlib.reload(gm_module)


# --- Los dos plazos de sala vacía --------------------------------------------

class TestPlazoSegunLaSalaHayaEmpezado:
    def test_una_sala_nueva_se_purga_mas_rapido(self, manager, room_clients, monkeypatch):
        """La vía barata de gastar memoria es crear y salir. Esa sala no tiene
        ronda, ni manos, ni marcador: no tiene que esperar el plazo largo."""
        _, sids = room_clients("Ana", "NUEVA")
        room = manager.rooms['NUEVA']
        assert room.ha_empezado is False
        _vaciar(manager, room_clients, room, sids)

        # Solo se toca la política CORTA; la larga queda en un plazo que no
        # se cumpliría ni en un milenio. Si el reaper usara la larga para esta
        # sala, el test falla: la gracia que se le enseñó al cliente es la que
        # decide, no la que le gustaría.
        monkeypatch.setattr(manager, 'ROOM_LIFECYCLE_NUEVA', PresencePolicy(0.05, 0))
        monkeypatch.setattr(manager, 'ROOM_LIFECYCLE', PresencePolicy(0.05, 10_000))
        manager.reap_empty_rooms()
        assert room.presence.state == SOSPECHOSO
        assert room.presence.grace == 0
        manager.reap_empty_rooms()
        assert "NUEVA" not in manager.rooms

    def test_una_sala_con_partida_usa_el_plazo_largo(self, manager, room_clients, monkeypatch):
        """La misma sala, pero con una partida detrás: conserva marcador y
        turnos, así que aguanta el plazo largo aunque la política corta ya haya
        vencido de sobra."""
        room, sids = _sala_con_partida(manager, room_clients, "PARTIDA")
        for p in room.players.values():
            p.points = 3
        _vaciar(manager, room_clients, room, sids)

        # Corta vencida de sobra, larga de sobra: solo la larga decide.
        monkeypatch.setattr(manager, 'ROOM_LIFECYCLE_NUEVA', PresencePolicy(0.05, 0))
        monkeypatch.setattr(manager, 'ROOM_LIFECYCLE', PresencePolicy(0.05, 900))
        manager.reap_empty_rooms()
        manager.reap_empty_rooms()
        manager.reap_empty_rooms()
        assert "PARTIDA" in manager.rooms
        assert room.presence.state == SOSPECHOSO
        assert room.presence.grace == 900

        # Pasa el tiempo y se va como cualquier sala. Se adelanta el plazo (y no
        # se reconfigura la política) porque el plazo con el que se lechó es una
        # promesa: cambiar la política con la sala ya en gracia no la acorta.
        room.presence.deadline = time.time() - 1
        manager.reap_empty_rooms()
        assert "PARTIDA" not in manager.rooms

    def test_ha_empezado_no_se_deshace_al_volver_a_waiting(self, manager, room_clients, monkeypatch):
        """`state` no vale para esto: al quedarse sin jugadores la sala vuelve a
        'waiting' por el revert, y eso no borra que hubo una partida. Si el
        plazo dependiera del estado, una partida real se iría con el plazo
        corto en cuanto se vaciara."""
        room, sids = _sala_con_partida(manager, room_clients, "VUELTA")
        for sid in sids.values():
            manager.disconnect(sid)

        assert room.state == 'waiting'   # el revert ya la dejó así
        assert room.ha_empezado is True

    def test_el_plazo_se_elige_al_acusar_y_no_se_cambia_despues(
            self, manager, room_clients, monkeypatch):
        """La promesa que se le enseñó al cliente no se toca por el camino: si se
        abriera la sospecha con el plazo corto y la sala empezara después, la
        purga seguiría midiendo al plazo con el que se acusó, que es lo que
        dijo el log."""
        _, sids = room_clients("Ana", "MIDE")
        room = manager.rooms['MIDE']
        _vaciar(manager, room_clients, room, sids)

        _politicas(manager, monkeypatch, 0.05, 0)   # accused y purgada de golpe
        manager.reap_empty_rooms()
        assert room.presence.state == SOSPECHOSO
        assert room.presence.grace == 0

        _politicas(manager, monkeypatch, 0.05, 900)  # la larga ya no manda
        manager.reap_empty_rooms()
        assert "MIDE" not in manager.rooms
