"""Defensas del reaper: conservación de cartas y purga de zombis.

Brainstorm #3: en cada pasada se audita el invariante
    sum(len(p.hand)) + cartas jugadas + available_whites == room.deck_size
Una deriva delata cartas huérfanas y se reporta con WARNING sin romper la
partida (solo telemetría).

Brainstorm #4: un jugador marcado como conectado sin señal de vida (acciones
entrantes ni difusiones entregadas) durante ZOMBIE_TIMEOUT_SECONDS es un
socket half-open que nunca emitió 'disconnect': el reaper lo purga
reutilizando la rutina de desconexión formal.
"""
import logging
import time

from test_disconnect_roles import room_clients  # noqa: F401 (fixture)


def _set_zombie_timeout(manager, seconds):
    """Baja el umbral de zombis para no esperar 300s reales en los tests."""
    manager.ZOMBIE_TIMEOUT_SECONDS = seconds


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

class TestCardConservation:
    def test_no_drift_when_everything_is_consistent(self, manager, room_clients, caplog):
        clients, sids, room = _start_seq_game(manager, room_clients, "CC1", ["Alice", "Bob", "Carol"])
        m = manager
        m.play_card(sids["Bob"], "CC1", 0)
        m.play_card(sids["Carol"], "CC1", 0)  # estado 'judging', dos submissions

        with caplog.at_level(logging.WARNING, logger="models.game_manager"):
            m.reap_empty_rooms()

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "Card drift" in r.message]
        assert warnings == []
        assert room.state == "judging"  # la partida no se toca

    def test_drift_is_logged_with_warning_and_room_survives(self, manager, room_clients, caplog):
        clients, sids, room = _start_seq_game(manager, room_clients, "CC2", ["Alice", "Bob", "Carol"])
        # Simular la fuga: una carta desaparece del pool sin rastro
        room.available_whites.pop()

        with caplog.at_level(logging.WARNING, logger="models.game_manager"):
            manager.reap_empty_rooms()

        drift = [r for r in caplog.records if r.levelno == logging.WARNING and "Card drift" in r.message]
        assert len(drift) == 1
        msg = drift[0].getMessage()
        assert "CC2" in msg
        assert f"esperadas={room.deck_size}" in msg        # total esperado
        assert f"actuales={room.deck_size - 1}" in msg     # total actual
        assert "drift=-1" in msg

        # La sala sigue viva y jugable: el chequeo es solo telemetría
        assert room.state == "playing"
        manager.play_card(sids["Bob"], "CC2", 0)
        manager.play_card(sids["Carol"], "CC2", 0)
        assert room.state == "judging"

    def test_drift_counts_cards_inside_played_submissions(self, manager, room_clients, caplog):
        clients, sids, room = _start_seq_game(manager, room_clients, "CC3", ["Alice", "Bob", "Carol"])
        manager.play_card(sids["Bob"], "CC3", 0)  # 1 carta fuera de la mano, en played_cards

        # Sustraer una carta: el invariante debe descontar la de played_cards
        room.available_whites.pop()
        with caplog.at_level(logging.WARNING, logger="models.game_manager"):
            manager.reap_empty_rooms()

        drift = [r for r in caplog.records if r.levelno == logging.WARNING and "Card drift" in r.message]
        assert len(drift) == 1
        assert f"actuales={room.deck_size - 1}" in drift[0].getMessage()

    def test_renew_vote_preserves_invariant(self, manager, room_clients, caplog):
        """La renovación total devuelve las manos al pool y reparte de nuevo:
        el invariante debe seguir cuadrando tras el ciclo completo."""
        clients, sids, room = _start_seq_game(manager, room_clients, "CC4", ["Alice", "Bob", "Carol"])
        manager.play_card(sids["Bob"], "CC4", 0)
        room.options["renew_threshold"] = 0.1
        manager.vote_renew(sids["Bob"], "CC4")   # supera el umbral al instante
        assert len(room.played_cards) == 0       # la renovación devolvió la submission

        with caplog.at_level(logging.WARNING, logger="models.game_manager"):
            manager.reap_empty_rooms()
        assert not [r for r in caplog.records if "Card drift" in r.message]

    def test_custom_card_injection_updates_deck_size(self, manager, room_clients, caplog):
        """Añadir cartas custom en directo agranda el mazo: deck_size debe
        crecer con ellas o el reaper daría un falso positivo."""
        clients, sids, room = _start_seq_game(manager, room_clients, "CC5", ["Alice", "Bob", "Carol"])
        before = room.deck_size
        manager.add_room_cards("CC5", "Carta inyectada 1,Carta inyectada 2")
        assert room.deck_size == before + 2

        with caplog.at_level(logging.WARNING, logger="models.game_manager"):
            manager.reap_empty_rooms()
        assert not [r for r in caplog.records if "Card drift" in r.message]


# --- Brainstorm #4: purga de jugadores zombi ---------------------------------

class TestZombieReaper:
    def test_idle_connected_player_is_purged(self, manager, room_clients):
        clients, sids, room = _start_seq_game(manager, room_clients, "Z1", ["Alice", "Bob", "Carol"])
        _set_zombie_timeout(manager, 0.05)  # umbral mínimo: cualquier ranciedad cuenta
        time.sleep(0.15)

        manager.reap_empty_rooms()

        assert room.players[sids["Bob"]].is_connected is False
        assert room.players[sids["Carol"]].is_connected is False
        # El proceso es idéntico a una desconexión formal: sin cambios de czar
        # por purgas (Alice sigue conectada... y también rancia: sale en el
        # mismo barrido) y la sala revertida o reasignada como toque siempre.
        assert all(p.is_connected is False for p in room.players.values())
        # Con la sala sin conectados, arranca el TTL de sala vacía
        assert room.empty_since is not None

    def test_active_player_is_never_purged(self, manager, room_clients):
        clients, sids, room = _start_seq_game(manager, room_clients, "Z2", ["Alice", "Bob", "Carol"])
        _set_zombie_timeout(manager, 300)  # umbral real: nadie es zombi aquí

        # Actividad reciente para todos (difusión de la acción de abajo)
        manager.play_card(sids["Bob"], "Z2", 0)
        for p in room.players.values():
            p.last_seen = time.time()  # refresco explícito de los tres

        manager.reap_empty_rooms()

        assert all(p.is_connected for p in room.players.values())
        assert room.empty_since is None

    def test_zombie_purge_reuses_formal_disconnect_routine(self, manager, room_clients):
        """La purga delega en _disconnect_locked: los relevo de líder y juez son
        idénticos a los de una desconexión normal."""
        clients, sids, room = _start_seq_game(manager, room_clients, "Z3", ["Alice", "Bob", "Carol"])
        _set_zombie_timeout(manager, 0.05)
        time.sleep(0.15)
        # Alice (czar y líder) se queda rancia; refrescar a los demás ANTES de purgar
        room.players[sids["Bob"]].last_seen = time.time()
        room.players[sids["Carol"]].last_seen = time.time()
        room.players[sids["Alice"]].last_seen = time.time() - 9999

        manager.reap_empty_rooms()

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
            manager.reap_empty_rooms()

        purged = [r for r in caplog.records if "purgando zombi" in r.message]
        assert purged == []  # Bob ya estaba desconectado: no se toca

    def test_reconnected_player_gets_fresh_last_seen(self, manager, room_clients):
        """Al reconectar, el last_seen del asiento se renueva: un objeto viejo
        con marca rancio no puede ser purgado justo al volver."""
        clients, sids, room = _start_seq_game(manager, room_clients, "Z5", ["Alice", "Bob", "Carol"])
        manager.disconnect(sids["Bob"])
        room.players[sids["Bob"]].last_seen = time.time() - 10**6

        _, bob2 = room_clients("Bob", "Z5")   # reconexión con el mismo nombre

        assert room.players[bob2].last_seen > time.time() - 60  # fresco
        assert room.players[bob2].is_connected
        _set_zombie_timeout(manager, 300)
        manager.reap_empty_rooms()
        assert room.players[bob2].is_connected  # sobrevive al barrido

    def test_zombie_purge_is_logged(self, manager, room_clients, caplog):
        clients, sids, room = _start_seq_game(manager, room_clients, "Z6", ["Alice", "Bob"])
        _set_zombie_timeout(manager, 0.05)
        time.sleep(0.15)

        with caplog.at_level(logging.INFO, logger="models.game_manager"):
            manager.reap_empty_rooms()

        purged = [r for r in caplog.records if "purgando zombi" in r.message]
        assert len(purged) == 2
        assert any("Alice" in r.getMessage() for r in purged)
