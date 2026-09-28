"""Defensas del reaper: presencia en dos fases.

Un jugador marcado como conectado que lleva un rato sin contestar ningún latido
pasa por dos fases: `vivo -> sospechoso` y, solo si el silencio se mantiene,
`sospechoso -> purgado`. Un socket half-open que nunca emitió 'disconnect' (el
caso típico detrás de un túnel, que mantiene el socket abierto) es el que
termina purgado, y lo hace reutilizando la rutina de desconexión formal.

Los tests de la máquina en sí (relojes, plazos, perdones) están en
test_presence_machine.py. Aquí se prueba el efecto sobre la partida: quién
conserva el asiento, quién lo pierde y qué se le dice a la mesa.

La conservación de cartas NO vive aquí: la vigila la fixture `deck_audit` de
conftest.py después de cada acción, y sus propios tests están en test_deck.py.
"""
import logging
import time

from models.presence import PresencePolicy, VIVO, SOSPECHOSO, PURGADO
from test_disconnect_roles import room_clients  # noqa: F401 (fixture)


def _set_politica(manager, monkeypatch, sospecha, gracia, atributo="PRESENCE"):
    """Fija los dos umbrales de presencia para no esperar minutos reales.

    Se usa monkeypatch y no una asignación directa porque `manager` es el mismo
    GameManager de sesión que comparten todos los tests: mutarlo a pelo se
    colaba de un test al siguiente.

    `gracia=0` significa "sin margen de vuelta": la sospecha sigue necesitando
    su pasada propia (el reaper jamás suspiciona y purga a la vez) pero en la
    siguiente ya purga. Es lo que permite que estos tests no duerman.
    """
    monkeypatch.setattr(manager, atributo, PresencePolicy(sospecha, gracia))


def _dos_pasadas(manager):
    """Suspechar y purgar son pasos distintos, y por eso hacen falta dos.

    Que sea así es el comportamiento que se quiere, no un detalle del test: un
    cliente al que se le purga en la misma pasada en que el reaper se entera de
    su silencio no ha tenido ninguna oportunidad de volver.
    """
    manager.reap_empty_rooms()
    manager.reap_empty_rooms()


def _answer_heartbeats(room):
    """Deja a los jugadores de `room` como clientes que ya han contestado algún
    latido. Es el requisito para que el reaper pueda juzgarlos: sin esta prueba
    previa no hay evidencia de nada y no se purga a nadie."""
    for p in room.players.values():
        p.heartbeat_pongs = max(p.heartbeat_pongs, 1)


def _go_silent(room, seconds=None):
    """Envejece `last_seen` de todos los jugadores de `room` como si hubieran
    dejado de contestar latidos (pestaña congelada, socket medio abierto)."""
    age = time.time() - (seconds if seconds is not None else 10 ** 6)
    for p in room.players.values():
        p.last_seen = age


def _start_seq_game(manager, room_clients, room_id, names):
    """Une `names` (el primero es líder), fija turn_order='sequential' y arranca.

    Devuelve (clientes, sids, room). Con sequential el czar inicial es el
    primero que entró, así los tests son deterministas.
    """
    clients, sids = {}, {}
    for name in names:
        clients[name], sids[name] = room_clients(name, room_id)
    room = manager.rooms[room_id]
    room.options["turn_order"] = "sequential"
    clients[names[0]].emit("start_game", {"room_id": room_id})
    clients[names[0]].get_received()
    return clients, sids, room


# --- Brainstorm #3: invariante de conservación de cartas ---------------------

# --- Brainstorm #4: purga de jugadores zombi ---------------------------------

class TestZombieReaper:
    def test_player_that_stopped_answering_heartbeats_is_purged(self, manager, room_clients, monkeypatch):
        """Un cliente que ya había contestado latidos y luego se calla (pestaña
        congelada, socket medio abierto) acaba purgado, pero no en la primera
        pasada: primero tiene que pasar por la fase sospechosa."""
        clients, sids, room = _start_seq_game(manager, room_clients, "Z1", ["Alice", "Bob", "Carol"])
        _set_politica(manager, monkeypatch, 0.05, 0)
        _answer_heartbeats(room)
        _go_silent(room, seconds=0.2)  # sin sleeps: la ranciedad se fija a mano

        manager.reap_empty_rooms()
        # Primera pasada: sospecha y AVISA. Nadie pierde su asiento.
        assert all(p.presence.state == SOSPECHOSO for p in room.players.values())
        assert all(p.is_connected for p in room.players.values())

        _dos_pasadas(manager)
        manager.reap_empty_rooms()

        assert room.players[sids["Bob"]].is_connected is False
        assert room.players[sids["Carol"]].is_connected is False
        # El proceso es idéntico a una desconexión formal: los tres se fueron
        # en el mismo barrido y la sala queda sin conectados.
        assert all(p.is_connected is False for p in room.players.values())
        assert all(p.presence.state == PURGADO for p in room.players.values())
        # Con la sala sin conectados, arranca el reloj de sala vacía
        assert room.empty_since is not None

    def test_active_player_is_never_purged(self, manager, room_clients, monkeypatch):
        clients, sids, room = _start_seq_game(manager, room_clients, "Z2", ["Alice", "Bob", "Carol"])
        _set_politica(manager, monkeypatch, 300, 180)  # plazos reales: nadie es sospechoso
        _answer_heartbeats(room)

        # Actividad reciente para todos (la difusión NO cuenta como señal de vida)
        manager.play_card(sids["Bob"], "Z2", 0)
        for p in room.players.values():
            p.last_seen = time.time()  # refresco explícito de los tres

        _dos_pasadas(manager)

        assert all(p.is_connected for p in room.players.values())
        assert all(p.presence.state == VIVO for p in room.players.values())
        assert room.empty_since is None

    def test_zombie_purge_reuses_formal_disconnect_routine(self, manager, room_clients, monkeypatch):
        """La purga delega en _release_from_room, la misma rutina de la
        desconexión formal: los relevés de líder y juez son idénticos."""
        clients, sids, room = _start_seq_game(manager, room_clients, "Z3", ["Alice", "Bob", "Carol"])
        _set_politica(manager, monkeypatch, 0.05, 0)
        _answer_heartbeats(room)
        # Alice (czar y líder) se queda rancia; refrescar a los demás ANTES de purgar
        room.players[sids["Bob"]].last_seen = time.time()
        room.players[sids["Carol"]].last_seen = time.time()
        room.players[sids["Alice"]].last_seen = time.time() - 9999

        _dos_pasadas(manager)

        # La sala continúa con jugadores reales conectados y roles reasignados
        assert room.players[sids["Bob"]].is_connected
        assert room.players[sids["Carol"]].is_connected
        assert room.players[sids["Alice"]].is_connected is False
        assert room.leader in (sids["Bob"], sids["Carol"])
        assert room.czar in (sids["Bob"], sids["Carol"])
        assert room.empty_since is None  # no es una sala vacía

    def test_disconnected_seat_is_never_re_purged(self, manager, room_clients, caplog):
        """Un asiento ya marcado is_connected=False (remove_player) no es
        candidato: no debe volverse a pasar por la rutina de limpieza."""
        clients, sids, room = _start_seq_game(manager, room_clients, "Z4", ["Alice", "Bob", "Carol"])
        manager.disconnect(sids["Bob"])
        room.players[sids["Bob"]].last_seen = time.time() - 10**6  # rancio de hace días

        with caplog.at_level(logging.INFO, logger="models.game_manager"):
            _dos_pasadas(manager)

        purged = [r for r in caplog.records if "pasa a 'purgado'" in r.getMessage()]
        assert purged == []  # Bob ya estaba desconectado: no se toca

    def test_reconnected_player_gets_fresh_last_seen(self, manager, room_clients, monkeypatch):
        """Al reconectar, el reloj del asiento se renueva: un objeto viejo
        con marca rancio no puede ser purgado justo al volver."""
        clients, sids, room = _start_seq_game(manager, room_clients, "Z5", ["Alice", "Bob", "Carol"])
        manager.disconnect(sids["Bob"])
        room.players[sids["Bob"]].last_seen = time.time() - 10**6

        _, bob2 = room_clients("Bob", "Z5")   # reconexión con el mismo nombre

        assert room.players[bob2].last_seen > time.time() - 60  # fresco
        assert room.players[bob2].is_connected
        _set_politica(manager, monkeypatch, 300, 180)
        _dos_pasadas(manager)
        assert room.players[bob2].is_connected  # sobrevive al barrido

    def test_zombie_purge_is_logged(self, manager, room_clients, monkeypatch, caplog):
        clients, sids, room = _start_seq_game(manager, room_clients, "Z6", ["Alice", "Bob"])
        _set_politica(manager, monkeypatch, 0.05, 0)
        _answer_heartbeats(room)
        _go_silent(room, seconds=0.2)

        with caplog.at_level(logging.INFO, logger="models.game_manager"):
            _dos_pasadas(manager)

        purgas = [r for r in caplog.records if "pasa a 'purgado'" in r.getMessage()]
        assert len(purgas) == 2
        assert any("Alice" in r.getMessage() for r in purgas)
        # Y también quedó registrado el paso por la fase intermedia, con el
        # plazo de gracia en el propio mensaje.
        gracias = [r for r in caplog.records if "pasa a 'sospechoso'" in r.getMessage()]
        assert len(gracias) == 2
        assert all("asiento se libera en" in r.getMessage() for r in gracias)


# --- Regresión: la señal de vida debe venir del cliente, no de las difusiones --

class TestHeartbeatIsTheOnlyLivenessSignal:
    """El umbral de zombis se alimentaba de `send_room_update`, que renovaba
    `last_seen` de todo el mundo cada vez que el servidor emitía. Eso medía
    'ruido de sala', no vida: una sala callada mataba a todos a la vez y un solo
    jugador hablando resucitaba a los muertos."""

    def test_reaper_emits_heartbeat_ping_to_connected_players(self, manager, room_clients):
        clients, sids, room = _start_seq_game(manager, room_clients, "H1", ["Alice", "Bob"])
        for c in clients.values():
            c.get_received()  # descartar el game_update del arranque

        manager.reap_empty_rooms()

        for name, c in clients.items():
            pings = [e for e in c.get_received() if e["name"] == "heartbeat_ping"]
            assert pings, f"{name} no recibió heartbeat_ping"
            assert pings[0]["args"][0]["room_id"] == "H1"

    def test_pong_event_refreshes_last_seen(self, manager, room_clients):
        clients, sids, room = _start_seq_game(manager, room_clients, "H2", ["Alice", "Bob"])
        room.players[sids["Alice"]].last_seen = time.time() - 10**6
        assert room.players[sids["Alice"]].heartbeat_pongs == 0

        clients["Alice"].emit("heartbeat_pong")

        alice = room.players[sids["Alice"]]
        assert alice.last_seen > time.time() - 60
        assert alice.heartbeat_pongs == 1  # ya se le puede juzgar

    def test_join_alone_does_not_prove_heartbeat_support(self, manager, room_clients):
        """Entrar no demuestra que el cliente sepa contestar un latido: por eso
        un build antiguo en caché, que jamás contestará, no acaba purgado."""
        clients, sids, room = _start_seq_game(manager, room_clients, "H3", ["Alice", "Bob"])
        assert all(p.heartbeat_pongs == 0 for p in room.players.values())

    def test_server_broadcast_does_not_count_as_liveness(self, manager, room_clients, monkeypatch):
        """REGRESIÓN: la difusión del servidor ya no debe resucitar a nadie. Los
        tres contestaron un latido y luego se quedaron congelados; Bob juega una
        carta y eso emite a toda la sala. Los tres deben seguir como
        candidatos, primero a sospechosos y después, si siguen callados,
        purgados."""
        clients, sids, room = _start_seq_game(manager, room_clients, "H4", ["Alice", "Bob", "Carol"])
        for c in clients.values():
            c.emit("heartbeat_pong")  # respondieron al latido: ahora son juzgables
        _set_politica(manager, monkeypatch, 300, 0)  # sospecha pronta, sin margen
        _go_silent(room)  # los tres se quedaron congelados

        manager.play_card(sids["Bob"], "H4", 0)  # difusión a los tres

        manager.reap_empty_rooms()
        assert all(p.presence.state == SOSPECHOSO for p in room.players.values()), \
            "una difusión del servidor no puede contar como señal de vida"
        assert all(p.is_connected for p in room.players.values())

        _dos_pasadas(manager)
        assert all(p.is_connected is False for p in room.players.values())

    def test_quiet_room_with_live_clients_is_not_purged(self, manager, room_clients, monkeypatch):
        """REGRESIÓN (el bug reportado): un lobby en 'waiting' donde todo el mundo
        está vivo pero nadie pulsa nada durante minutos. Antes el reaper los
        expulsaba a todos y la sala terminaba borrada. Ahora, contestando
        latidos, siguen dentro."""
        clients = {name: room_clients(name, "H5")[0] for name in ["Ana", "Beto", "Caro"]}
        room = manager.rooms["H5"]
        assert room.state == "waiting"

        _set_politica(manager, monkeypatch, 1, 180)  # cualquier segundo cuenta
        for _ in range(3):  # tres pasadas del reaper, contestando entremedias
            # El tiempo pasa de verdad: el umbral llega con 10 min de silencio
            _go_silent(room, seconds=600)
            for c in clients.values():
                c.emit("heartbeat_pong")  # pero el cliente contesta el ping
            manager.reap_empty_rooms()

        assert all(p.is_connected for p in room.players.values())
        assert all(p.presence.state == VIVO for p in room.players.values())
        assert room.empty_since is None
        assert "H5" in manager.rooms

    def test_player_that_never_answered_is_never_purged(self, manager, room_clients, monkeypatch):
        """Sin ninguna prueba previa de latido no hay evidencia de nada: el
        reaper se abstiene y deja pasar al zombie hasta que engine.io lo
        desconecte por su cuenta."""
        clients, sids, room = _start_seq_game(manager, room_clients, "H6", ["Alice", "Bob", "Carol"])
        _set_politica(manager, monkeypatch, 0.05, 0)
        _go_silent(room, seconds=10**6)  # rancios de días, pero nunca contestaron

        _dos_pasadas(manager)

        assert all(p.is_connected for p in room.players.values())
        assert all(p.presence.state == VIVO for p in room.players.values())
        assert room.empty_since is None


# --- Regresión: un sid tiene un solo asiento ----------------------------------

class TestSeatIsUniquePerSid:
    """Migrar de sala dejaba un Player fantasma 'conectado' en la anterior. El
    reaper lo daba por zombi y, al purgarlo con _disconnect_locked (que recorre
    todas las salas), le cortaba la sesion en la sala nueva."""

    def _move_to(self, client, name, room_id):
        """El MISMO socket (mismo sid) entra en otra sala: lo que pasa cuando un
        jugador cambia de sala sin recargar la pagina."""
        client.emit("join_game", {"name": name, "room_id": room_id})

    def test_migration_leaves_no_ghost_seat(self, manager, room_clients):
        ana, sid_ana = room_clients("Ana", "S1")
        room_clients("Beto", "S1")
        old_room = manager.rooms["S1"]

        self._move_to(ana, "Ana", "S2")

        assert old_room.players[sid_ana].is_connected is False
        assert manager.rooms["S2"].players[sid_ana].is_connected is True

    def test_purge_does_not_evict_player_from_current_room(self, manager, room_clients, monkeypatch):
        """REGRESION: el sintoma que ve el jugador. Ana se muda a S2, su asiento
        viejo en S1 se queda rancia y el reaper lo purga; antes eso le cortaba
        la sesion en S2, que era donde estaba jugando de verdad."""
        ana, sid_ana = room_clients("Ana", "S1")
        room_clients("Beto", "S1")
        self._move_to(ana, "Ana", "S2")
        room_clients("Caro", "S2")
        new_room = manager.rooms["S2"]

        # Aunque algo dejara un asiento rancia de Ana en S1, purgarlo no puede
        # tocar su asiento de S2: la purga va por sala, no por sid.
        ghost = manager.rooms["S1"].players[sid_ana]
        ghost.is_connected = True
        ghost.heartbeat_pongs = 1
        ghost.last_seen = time.time() - 10**6
        _set_politica(manager, monkeypatch, 0.05, 0)

        _dos_pasadas(manager)

        assert ghost.is_connected is False                   # purgado de S1
        assert new_room.players[sid_ana].is_connected is True  # intacto en S2
        assert len(new_room.get_active_players()) == 2

    def test_migration_heals_abandoned_room(self, manager, room_clients):
        """La sala abandonada no se queda con un lider zombi: reutiliza la misma
        rutina que una desconexion formal."""
        ana, sid_ana = room_clients("Ana", "S1")
        _, sid_beto = room_clients("Beto", "S1")
        old_room = manager.rooms["S1"]
        assert old_room.leader == sid_ana

        self._move_to(ana, "Ana", "S2")

        assert old_room.leader == sid_beto  # relevo de liderazgo
        assert old_room.players[sid_ana].is_connected is False

    def test_abandoned_room_becomes_empty_and_expires(self, manager, room_clients, monkeypatch):
        """Si el ultimo jugador se va, la sala queda vacia y el TTL de limpieza
        sigue contando desde la migracion."""
        ana, _ = room_clients("Ana", "S1")
        old_room = manager.rooms["S1"]

        self._move_to(ana, "Ana", "S2")
        assert old_room.empty_since is not None

        # La sala NUNCA llegó a empezar, así que la política que le toca es la
        # corta de las salas nuevas, no la de una partida en curso.
        _set_politica(manager, monkeypatch, 300, 0,
                      atributo="ROOM_LIFECYCLE_NUEVA")
        old_room.empty_since = time.time() - manager.ROOM_LIFECYCLE_NUEVA.purge_after - 1
        _dos_pasadas(manager)
        assert "S1" not in manager.rooms

    def test_rejoin_same_room_keeps_leadership(self, manager, room_clients):
        """Reconectar en la MISMA sala no debe pasar por la liberacion: es un
        asiento vivo y conserva su rol."""
        ana, sid_ana = room_clients("Ana", "S1")
        room_clients("Beto", "S1")
        room = manager.rooms["S1"]
        assert room.leader == sid_ana

        # Mismo sid, misma sala: es una reconexion en toda regla
        self._move_to(ana, "Ana", "S1")

        assert room.players[sid_ana].is_connected is True
        assert room.leader == sid_ana  # Ana sigue siendo lider
        assert len(room.players) == 2

    def test_room_of_tracks_seat(self, manager, room_clients):
        ana, sid_ana = room_clients("Ana", "S1")
        assert manager.room_of(sid_ana) == "S1"
        self._move_to(ana, "Ana", "S2")
        assert manager.room_of(sid_ana) == "S2"  # se movio con ella
