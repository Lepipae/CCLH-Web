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
    latido, que es lo que hace que se les juzgue con la política normal y no con
    la más generosa de quien no ha dado ninguna prueba."""
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
        """Entrar no demuestra que el cliente sepa contestar un latido, y por eso
        un build antiguo en caché se juzga con la política más generosa. Lo que
        ya no hace es que eso lo deje sin juicio: ver
        `test_sin_prueba_acaba_purgado_tras_su_gracia`."""
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

    def test_sin_prueba_acaba_purgado_tras_su_gracia(self, manager, room_clients, monkeypatch):
        """REGRESIÓN: sin ninguna prueba de latido ya no hay salto eterno.

        El transporte de un cliente con el JS de aplicación muerto sigue vivo,
        así que el `disconnect` de engine.io no llega nunca y el reaper se lo
        comía como si fuera a purgarse solo. Medido contra un servidor real: 25
        de estos llenaron el techo de salas y, tras 100 s y siete pasadas,
        seguían allí, sin un solo aviso. Ahora se les juzga con
        PRESENCE_SIN_PRUEBA y, vencido su plazo, salen por la puerta de los
        vivos: aviso, reto, y solo entonces el asiento.
        """
        clients, sids, room = _start_seq_game(manager, room_clients, "H6", ["Alice", "Bob", "Carol"])
        _set_politica(manager, monkeypatch, 10**9, 10**9)
        _set_politica(manager, monkeypatch, 0.05, 0, "PRESENCE_SIN_PRUEBA")
        _go_silent(room, seconds=10**6)  # rancios de días, pero nunca contestaron

        manager.reap_empty_rooms()
        assert all(p.presence.state == SOSPECHOSO for p in room.players.values())
        for nombre, c in clients.items():
            retos = [e for e in c.get_received() if e['name'] == 'presence_challenge']
            assert retos, f"{nombre} no recibió el reto antes de que le purguen"
            assert retos[0]['args'][0]['grace'] == 0

        _dos_pasadas(manager)

        assert not any(p.is_connected for p in room.players.values())
        assert all(p.presence.state == PURGADO for p in room.players.values())
        # Y la sala, ahora sí vacía, arranca su propio reloj de silencio.
        assert room.empty_since is not None

    def test_sin_prueba_respeta_la_politica_normal(self, manager, room_clients, monkeypatch):
        """La otra mitad: con la política de falta de prueba amplia, estos mismos
        clientes NO se tocan. El margen se aplica, no se ignora."""
        clients, sids, room = _start_seq_game(manager, room_clients, "H6B", ["Alice", "Bob", "Carol"])
        _set_politica(manager, monkeypatch, 0.01, 0)
        _set_politica(manager, monkeypatch, 10**9, 10**9, "PRESENCE_SIN_PRUEBA")
        _go_silent(room, seconds=10**6)

        _dos_pasadas(manager)

        assert all(p.is_connected for p in room.players.values())
        assert all(p.presence.state == VIVO for p in room.players.values())
        assert room.empty_since is None

    def test_un_mudo_deja_libre_el_techo_de_salas(self, manager, room_clients, monkeypatch):
        """El final de la historia: la sala del cliente mudo se acaba purgando,
        que es lo que hace que el techo de salas sea un tope y no una
        irreversible. Cada cliente mudo se sienta en una sala propia."""
        clients, sids = {}, {}
        for i in range(3):
            c, sid = room_clients(f"Silencio{i}", f"LEAK{i}")
            clients[f"Silencio{i}"] = c
            sids[f"Silencio{i}"] = sid
        _set_politica(manager, monkeypatch, 0.05, 0, "PRESENCE_SIN_PRUEBA")
        _set_politica(manager, monkeypatch, 0.05, 0, "ROOM_LIFECYCLE_NUEVA")
        for i in range(3):
            _go_silent(manager.rooms[f"LEAK{i}"], seconds=10**6)

        # Pasadas para suspicionar y purgar a los mudos: sus asientos se
        # liberan y la sala se queda vacía.
        for _ in range(2):
            manager.reap_empty_rooms()
        assert not any(p.is_connected
                       for i in range(3)
                       for p in manager.rooms[f"LEAK{i}"].players.values())

        # Y ahora la sala, ya vacía, con su reloj muy atrasado (sin dormir: el
        # reloj se envejece a mano, igual que en _vaciar). Dos pasadas más la
        # borran: sospechar y purgar nunca ocurren en la misma.
        for i in range(3):
            manager.rooms[f"LEAK{i}"].empty_since = time.time() - 10 ** 6
        for _ in range(3):
            manager.reap_empty_rooms()

        assert not [r for r in manager.rooms if r.startswith("LEAK")], (
            "las salas de los clientes mudos no se purgaron: el techo de salas "
            "vuelve a ser una irreversible")


# --- La sala de Socket.IO no es la sala del GameManager -----------------------

class _ServidorFalso:
    """Del servidor de Socket.IO solo se falsea `leave_room`, que es lo que se
    quiere vigilar: el GameManager lo llama a través de `socketio.server` porque
    el `leave_room` de flask_socketio necesita un contexto de petición y el
    reaper corre en su propio hilo. Todo lo demás (las emisiones de verdad) se
    sigue delegando en el servidor real."""

    def __init__(self, real):
        self._real = real
        self.salidas = []

    def leave_room(self, sid, room, namespace=None):
        self.salidas.append((sid, room))

    def __getattr__(self, nombre):
        return getattr(self._real, nombre)


def _servidor_falso(monkeypatch, manager):
    servidor = _ServidorFalso(manager.socketio.server)
    monkeypatch.setattr(manager.socketio, "server", servidor)
    return servidor


class TestSalaPurgadaSaleDeSocketIO:
    """`app.py` mete a cada cliente en una sala de Socket.IO con su codigo de
    sala, y solo se sale de ella al cambiar de sala (o al desconectarse, que lo
    hace la propia libreria). La sala del GameManager y esa NO son la misma
    cosa: un cliente purgado por el reaper sigue conectado, y sin sacarlo de
    ahi se seguia enterando del chat de la SIGUIENTE mesa que usara ese
    codigo."""

    def test_la_sala_purgada_saca_a_sus_clientes(self, manager, room_clients, monkeypatch):
        clients, sids, room = _start_seq_game(manager, room_clients, "OUT1", ["Ana", "Beto", "Caro"])
        # El parche va DESPUÉS de crear los clientes: `socketio.test_client`
        # necesita el servidor de verdad para montar su propio socket.
        servidor = _servidor_falso(monkeypatch, manager)
        _answer_heartbeats(room)      # PresencePolicy, no la de "sin prueba"
        _go_silent(room)              # y llevan días sin contestar: son zombis

        _set_politica(manager, monkeypatch, 0.05, 0)                  # PRESENCE
        _set_politica(manager, monkeypatch, 0.05, 0, "ROOM_LIFECYCLE")
        _dos_pasadas(manager)                      # los tres son zombis
        assert all(not p.is_connected for p in room.players.values())
        # La sala, ya vacía, tiene que morir también para que salga el leave_room
        room.empty_since = time.time() - 10 ** 6
        _dos_pasadas(manager)

        assert "OUT1" not in manager.rooms
        assert sorted(servidor.salidas) == sorted(
            (sid, "OUT1") for sid in sids.values()), (
            "los clientes de la sala purgada se quedan dentro de la sala de "
            "Socket.IO y oiran el chat de la proxima mesa con ese codigo")

    def test_una_sala_que_no_se_purga_no_toca_a_nadie(self, manager, room_clients, monkeypatch):
        """Contrapeso: `leave_room` es de las salas que se van, no uno por cada
        foto de la sala."""
        _start_seq_game(manager, room_clients, "OUT2", ["Ana", "Beto", "Caro"])
        servidor = _servidor_falso(monkeypatch, manager)

        manager.reap_empty_rooms()
        manager.reap_empty_rooms()

        assert "OUT2" in manager.rooms
        assert servidor.salidas == []


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


# --- La purga deja el asiento sin autoridad ----------------------------------

class TestAsientoPurgadoNoManda:
    """Un asiento purgado o desconectado sigue en `room.players` a propósito
    (su mano y su marcador se conservan, y la auditoría de cartas cuenta con
    ellos dentro), así que "estar en players" no puede ser lo que autoriza a
    jugar: la puerta es `Room.asiento(sid)`.

    Antes de esto un cliente purgado seguía jugando cartas y chateando con su
    socket abierto. La purga lo volvía invisible pero no le quitaba poderes,
    que es justo lo contrario de lo que promete la máquina de dos fases.
    """

    def _mesa_con_un_purgado(self, manager, monkeypatch, room_clients,
                             room_id, nombres):
        """Mesa en partida con UN jugador purgado y su socket todavía abierto."""
        clients, sids, room = _start_seq_game(manager, room_clients, room_id, nombres)
        _answer_heartbeats(room)                 # con pruebas: política normal
        _set_politica(manager, monkeypatch, 0.05, 0)

        victima = nombres[-1]
        assert room.players[sids[victima]].heartbeat_pongs == 1
        room.players[sids[victima]].last_seen = time.time() - 10 ** 6
        _dos_pasadas(manager)

        p = room.players[sids[victima]]
        assert p.presence.state == PURGADO
        assert p.is_connected is False
        assert room.state == 'playing', "la partida sigue en pie con los otros"
        return clients, sids, room, sids[victima], victima

    def test_el_asiento_purgado_no_esta(self, manager, monkeypatch, room_clients):
        _, _, room, victima, _ = self._mesa_con_un_purgado(
            manager, monkeypatch, room_clients, "A1",
            ["Ana", "Beto", "Caro", "Dani"])
        assert room.asiento(victima) is None
        assert room.players.get(victima) is not None, "el asiento se conserva"

    def test_el_purgado_no_juega(self, manager, monkeypatch, room_clients):
        clients, _, room, victima, nombre = self._mesa_con_un_purgado(
            manager, monkeypatch, room_clients, "A2",
            ["Ana", "Beto", "Caro", "Dani"])
        jugadas = len(room.played_cards)
        mano = len(room.players[victima].hand)

        clients[nombre].emit("play_card", {"room_id": "A2", "card_index": 0})

        assert len(room.played_cards) == jugadas, "una jugada de un purgado no entra"
        assert len(room.players[victima].hand) == mano, "y no pierde cartas"
        assert room.players[victima].played_card is None

    def test_el_purgado_no_chatea(self, manager, monkeypatch, room_clients):
        clients, _, room, victima, nombre = self._mesa_con_un_purgado(
            manager, monkeypatch, room_clients, "A3",
            ["Ana", "Beto", "Caro", "Dani"])
        for c in clients.values():
            c.get_received()

        clients[nombre].emit("chat", {"room_id": "A3", "msg": "sigo aquí"})

        chats = [e for c in clients.values() for e in c.get_received()
                 if e['name'] == 'chat_message']
        assert chats == [], f"el purgado todavía habla: {chats}"

    def test_el_purgado_no_vota(self, manager, monkeypatch, room_clients):
        clients, _, room, victima, nombre = self._mesa_con_un_purgado(
            manager, monkeypatch, room_clients, "A5",
            ["Ana", "Beto", "Caro", "Dani"])

        clients[nombre].emit("vote_renew", {"room_id": "A5"})

        assert victima not in room.renew_votes
        assert len(room.renew_votes) == 0

    def test_el_desconectado_tampoco_juega(self, manager, room_clients):
        """El caso corriente: el mismo asiento pero desconectado en vez de
        purgado. Misma puerta, mismo resultado."""
        clients, sids = {}, {}
        for nombre in ["Ana", "Beto", "Caro"]:
            clients[nombre], sids[nombre] = room_clients(nombre, "A4")
        room = manager.rooms["A4"]
        room.options['turn_order'] = 'sequential'
        manager.start_game(sids["Ana"], "A4")
        _answer_heartbeats(room)

        cazar = sids["Caro"]
        manager.disconnect(cazar)
        assert room.players[cazar].is_connected is False
        jugadas = len(room.played_cards)

        clients["Caro"].emit("play_card", {"room_id": "A4", "card_index": 0})

        assert len(room.played_cards) == jugadas
        assert room.players[cazar].played_card is None

    def test_el_asiento_vivo_sigue_pudiendo_hacer_todo(self, manager, monkeypatch,
                                                        room_clients):
        """La otra mitad, que es la que importa: el accessor no ha de cerrar la
        puerta a nadie. Un jugador de los conectados juega con normalidad."""
        clients, sids, room, _victima, _ = self._mesa_con_un_purgado(
            manager, monkeypatch, room_clients, "A6",
            ["Ana", "Beto", "Caro", "Dani"])
        ana = sids["Ana"]
        assert room.asiento(ana) is not None
        mano_ana = len(room.players[ana].hand)

        clients["Beto"].emit("play_card", {"room_id": "A6", "card_index": 0})

        assert len(room.played_cards) == 1
        assert room.played_cards[0]['sid'] == sids["Beto"]
        assert len(room.players[ana].hand) == mano_ana  # solo pierde quien juega
