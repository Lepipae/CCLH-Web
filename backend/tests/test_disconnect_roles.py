"""Edge cases de desconexión: liderazgo, juez y rotación.

Comprueba que al marcharse el líder o el juez la sala nunca queda congelada:
el relevo siempre recae en un jugador conectado, la rotación secuencial no se
reinicia, un ganador desconectado no bloquea el avance y una sala "huérfana"
se autoasana cuando vuelve a entrar alguien.
"""
import pytest


def make_client(app_module):
    """Cliente Socket.IO adicional e independiente (cada uno con su sid)."""
    c = app_module.socketio.test_client(app_module.app)
    c.get_received()
    return c


def sid_of(room, name):
    """Sid de un jugador por nombre; falla si no está en la sala."""
    for s, p in room.players.items():
        if p.name == name:
            return s
    raise AssertionError(f"{name} no está en la sala {room.room_id}")


@pytest.fixture()
def room_clients(app_module, manager):
    """Fábrica de jugadores: join(name, room_id) -> (cliente, sid)."""
    created = []

    def _join(name, room_id):
        c = make_client(app_module)
        c.emit("join_game", {"name": name, "room_id": room_id})
        created.append(c)
        room = app_module.manager.rooms[room_id]
        return c, sid_of(room, name)

    yield _join

    for c in created:
        try:
            c.disconnect()
        except Exception:
            pass


def start_seq_game(app_module, room_clients, room_id, names):
    """Une `names` (el primero es líder), fija turn_order='sequential' y arranca.

    Devuelve (clientes, sids, room). Con sequential el czar inicial es el
    primero que entró, así los tests son deterministas.
    """
    clients, sids = {}, {}
    for name in names:
        clients[name], sids[name] = room_clients(name, room_id)
    room = app_module.manager.rooms[room_id]
    room.options["turn_order"] = "sequential"
    clients[names[0]].emit("start_game", {"room_id": room_id})
    clients[names[0]].get_received()
    return clients, sids, room


def sub_of(room, sid):
    """Submission (entrada de played_cards) del jugador sid."""
    for c in room.played_cards:
        if c["sid"] == sid:
            return c
    raise AssertionError(f"{sid} no tiene carta en juego")


class TestLeaderHandoff:
    def test_waiting_room_leader_promotes_a_connected_player(self, app_module, room_clients):
        _, alice = room_clients("Alice", "LW1")
        _, bob = room_clients("Bob", "LW1")
        _, carol = room_clients("Carol", "LW1")
        room = app_module.manager.rooms["LW1"]
        assert room.leader == alice

        app_module.manager.disconnect(alice)

        assert room.leader in (bob, carol)
        assert room.players[room.leader].is_connected

    def test_leader_handoff_skips_spectators_after_reorder(self, app_module, room_clients):
        """Bob reconecta y pasa al final del diccionario de jugadores; aunque una
        espectadora le preceda en el orden, el liderazgo debe recaer en él."""
        clients, sids, room = start_seq_game(app_module, room_clients, "LW2", ["Alice", "Bob"])
        room_clients("Dave", "LW2")  # entra con la partida en marcha: espectadora
        m = app_module.manager

        m.disconnect(sids["Bob"])
        _, bob2 = room_clients("Bob", "LW2")  # reconecta: queda al final del dict
        m.disconnect(sids["Alice"])           # líder fuera; quedan Dave y Bob

        assert room.leader == bob2
        assert room.players[room.leader].waiting_next_round is False

    def test_leader_falls_back_to_spectator_when_only_they_remain(self, app_module, room_clients):
        """Si al marcharse el líder solo quedan espectadoras, entran al juego y
        una de ellas hereda liderazgo y turno de juez: la partida continúa."""
        clients, sids, room = start_seq_game(app_module, room_clients, "LW3", ["Alice", "Bob"])
        room_clients("Carol", "LW3")
        room_clients("Dave", "LW3")
        m = app_module.manager

        m.disconnect(sids["Alice"])  # el liderazgo pasa a Bob (jugador real)
        assert room.leader == sids["Bob"]

        m.disconnect(sids["Bob"])    # solo quedan las espectadoras

        carol, dave = sid_of(room, "Carol"), sid_of(room, "Dave")
        assert room.state == "playing"
        assert room.leader == carol
        assert room.czar == carol
        assert room.players[carol].waiting_next_round is False
        assert room.players[dave].waiting_next_round is False
        assert len(room.players[dave].hand) == room.options["hand_size"]


class TestCzarHandoff:
    def test_czar_disconnect_mid_judging_passes_to_a_player(self, app_module, room_clients):
        clients, sids, room = start_seq_game(app_module, room_clients, "CJ1", ["Alice", "Bob", "Carol"])
        m = app_module.manager
        m.play_card(sids["Bob"], "CJ1", 0)
        m.play_card(sids["Carol"], "CJ1", 0)
        assert room.state == "judging"

        m.disconnect(sids["Alice"])  # la jueza se marcha con las cartas sin revelar

        new_czar = room.czar
        assert new_czar in (sids["Bob"], sids["Carol"])
        assert room.players[new_czar].played_card is not None
        assert room.state == "judging"

        # El nuevo juez puede cerrar la ronda con la submission del otro
        other_sid = sids["Carol"] if new_czar == sids["Bob"] else sids["Bob"]
        m.choose_winner(new_czar, "CJ1", sub_of(room, other_sid)["id"])
        assert room.state == "round_end"

    def test_czar_disconnect_mid_playing_keeps_round_alive(self, app_module, room_clients):
        clients, sids, room = start_seq_game(app_module, room_clients, "CJ2", ["Alice", "Bob", "Carol"])
        m = app_module.manager
        m.play_card(sids["Bob"], "CJ2", 0)
        assert room.state == "playing"

        m.disconnect(sids["Alice"])

        assert room.czar == sids["Bob"]     # la rotación continúa desde Alice
        assert room.state == "playing"
        assert len(room.played_cards) == 1  # la submission de Bob sigue en juego

        m.play_card(sids["Carol"], "CJ2", 0)
        assert room.state == "judging"
        m.choose_winner(sids["Bob"], "CJ2", sub_of(room, sids["Carol"])["id"])
        assert room.state == "round_end"
        assert room.last_winner == sids["Carol"]

    def test_sequential_rotation_continues_from_departed_czar(self, app_module, room_clients):
        """Al rotar con sequential, el turno sigue desde la posición del juez
        ausente (Carol) en vez de reiniciarse en el primero de la lista."""
        clients, sids, room = start_seq_game(app_module, room_clients, "CJ3", ["Alice", "Bob", "Carol"])
        m = app_module.manager
        m.disconnect(sids["Alice"])  # alice era czar y líder

        assert room.czar == sids["Bob"]
        assert room.leader == sids["Bob"]

        m.play_card(sids["Carol"], "CJ3", 0)
        assert room.state == "judging"
        m.choose_winner(sids["Bob"], "CJ3", sub_of(room, sids["Carol"])["id"])
        m.force_next_round(sids["Bob"], "CJ3")

        assert room.state == "playing"
        assert room.czar == sids["Carol"]

    def test_judging_with_no_connected_submissions_discards_round(self, app_module, room_clients):
        """Si el juez se va y ningún jugador conectado conserva carta jugada, el
        recuento huérfano se descarta (cartas devueltas al mazo), las
        espectadoras entran al juego y la ronda vuelve a 'playing'."""
        clients, sids, room = start_seq_game(app_module, room_clients, "CJ4", ["Alice", "Bob", "Carol"])
        m = app_module.manager
        m.play_card(sids["Bob"], "CJ4", 0)
        m.play_card(sids["Carol"], "CJ4", 0)
        assert room.state == "judging"
        room_clients("Dave", "CJ4")   # espectadoras de respaldo
        room_clients("Eve", "CJ4")
        pool = len(room.available_whites)

        m.disconnect(sids["Bob"])     # las dos submissiones quedan huérfanas
        m.disconnect(sids["Carol"])
        assert room.state == "judging"  # la jueza sigue: la ronda no se toca

        m.disconnect(sids["Alice"])

        assert room.state == "playing"
        assert room.played_cards == []
        # Las cartas de las dos submissiones descartadas vuelven al mazo
        # (las espectadoras ya habían recibido su mano al entrar)
        assert len(room.available_whites) == pool + 2
        assert room.players[sids["Bob"]].played_card is None
        dave = sid_of(room, "Dave")
        assert room.czar == dave
        assert room.players[dave].waiting_next_round is False


class TestWinnerRotation:
    def test_choose_winner_rejects_disconnected_submission(self, app_module, room_clients):
        clients, sids, room = start_seq_game(app_module, room_clients, "W1", ["Alice", "Bob", "Carol"])
        m = app_module.manager
        m.play_card(sids["Bob"], "W1", 0)
        m.play_card(sids["Carol"], "W1", 0)
        assert room.state == "judging"

        m.disconnect(sids["Bob"])
        m.choose_winner(sids["Alice"], "W1", sub_of(room, sids["Bob"])["id"])

        assert room.state == "judging"          # no se premia a un desconectado
        assert room.last_winner is None
        assert room.players[sids["Bob"]].points == 0

        m.choose_winner(sids["Alice"], "W1", sub_of(room, sids["Carol"])["id"])
        assert room.state == "round_end"
        assert room.last_winner == sids["Carol"]
        assert room.players[sids["Carol"]].points == 1

    def test_round_advances_when_winner_left_during_round_end(self, app_module, room_clients):
        clients, sids, room = start_seq_game(app_module, room_clients, "W2", ["Alice", "Bob", "Carol"])
        room.options["turn_order"] = "winner"  # el ganador pasa a juez: el caso que se congelaba
        m = app_module.manager
        m.play_card(sids["Bob"], "W2", 0)
        m.play_card(sids["Carol"], "W2", 0)
        m.choose_winner(sids["Alice"], "W2", sub_of(room, sids["Bob"])["id"])
        assert room.state == "round_end"
        assert room.last_winner == sids["Bob"]

        m.disconnect(sids["Bob"])   # el ganador se marcha antes del auto-avance
        m.advance_to_next_round("W2")

        assert room.state == "playing"
        assert room.czar in (sids["Alice"], sids["Carol"])
        assert room.players[room.czar].is_connected


class TestHealOnJoin:
    def test_join_heals_stale_leader(self, app_module, room_clients):
        _, alice = room_clients("Alice", "H1")
        _, bob = room_clients("Bob", "H1")
        room_clients("Carol", "H1")
        room = app_module.manager.rooms["H1"]
        room.players[alice].is_connected = False  # desconexión perdida (simulada)

        room_clients("Dave", "H1")  # cualquiera que entre autoasana la sala

        assert room.leader == bob
        assert room.players[room.leader].is_connected

    def test_join_heals_stale_czar_mid_round(self, app_module, room_clients):
        clients, sids, room = start_seq_game(app_module, room_clients, "H2", ["Alice", "Bob", "Carol"])
        assert room.czar == sids["Alice"]
        room.players[sids["Alice"]].is_connected = False  # desconexión perdida

        room_clients("Dave", "H2")

        assert room.czar == sids["Bob"]
        assert room.players[room.czar].is_connected
        assert room.leader == sids["Bob"]  # el liderazgo también se reasigna

    def test_join_heals_round_end_stuck_without_czar(self, app_module, room_clients):
        clients, sids, room = start_seq_game(app_module, room_clients, "H3", ["Alice", "Bob", "Carol"])
        m = app_module.manager
        m.play_card(sids["Bob"], "H3", 0)
        m.play_card(sids["Carol"], "H3", 0)
        m.choose_winner(sids["Alice"], "H3", sub_of(room, sids["Bob"])["id"])
        assert room.state == "round_end"
        black_before = room.black_card

        room.players[sids["Alice"]].is_connected = False  # la jueza se fue sin que se note

        room_clients("Dave", "H3")

        assert room.state == "playing"  # la ronda avanza al entrar alguien
        assert room.players[room.czar].is_connected
        assert room.black_card is not None and room.black_card != black_before


class TestStartGameGuard:
    def test_start_game_needs_two_players(self, app_module, room_clients):
        c_a, _ = room_clients("Alice", "S1")
        room = app_module.manager.rooms["S1"]

        c_a.emit("start_game", {"room_id": "S1"})
        c_a.get_received()

        assert room.state == "waiting"
        assert room.czar is None
        assert room.black_card is None

    def test_non_leader_cannot_start(self, app_module, room_clients):
        _, alice = room_clients("Alice", "S3")
        c_b, _ = room_clients("Bob", "S3")
        room = app_module.manager.rooms["S3"]

        c_b.emit("start_game", {"room_id": "S3"})
        c_b.get_received()

        assert room.state == "waiting"

    def test_start_game_starts_with_two(self, app_module, room_clients):
        _, sids, room = start_seq_game(app_module, room_clients, "S2", ["Alice", "Bob"])

        assert room.state == "playing"
        assert room.czar == sids["Alice"]
        assert room.black_card is not None


class TestReconnection:
    def test_rejoin_recovers_seat_and_keeps_playing(self, app_module, room_clients):
        """Al reconectar con el mismo nombre se recupera la plaza completa:
        conexión, mano y participación activa en la partida."""
        clients, sids, room = start_seq_game(app_module, room_clients, "R1", ["Alice", "Bob", "Carol"])
        m = app_module.manager

        m.disconnect(sids["Bob"])
        assert room.players[sids["Bob"]].is_connected is False
        assert room.czar == sids["Alice"]  # la sala no se toca por una desconexión

        _, bob2 = room_clients("Bob", "R1")

        assert room.players[bob2].is_connected is True
        assert sids["Bob"] not in room.players
        assert room.czar == sids["Alice"]
        # Vuelve activo (no espectador) y conserva su mano intacta
        assert room.players[bob2].waiting_next_round is False
        assert len(room.players[bob2].hand) == room.options["hand_size"]

        # Ronda siguiente: la rotación secuencial le entrega el turno de juez
        m.advance_to_next_round("R1")
        assert room.czar == bob2

        m.play_card(sids["Alice"], "R1", 0)
        m.play_card(sids["Carol"], "R1", 0)
        assert room.state == "judging"
        m.choose_winner(bob2, "R1", sub_of(room, sids["Alice"])["id"])
        assert room.state == "round_end"
        assert room.last_winner == sids["Alice"]
        assert room.players[sids["Alice"]].points == 1
