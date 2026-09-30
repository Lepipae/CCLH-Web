"""El mazo como único dueño de las cartas blancas, y su conservación.

La conservación se comprueba por IDENTIDAD de carta, no contando: con dos
cartas de texto idéntico, contar no puede decir cuál se perdió, pero los ids sí.

La comprobación de que la partida real no pierde cartas no está aquí: la corre
la fixture `deck_audit` de conftest.py después de cada acción, en todos los
tests del proyecto. Estos tests cubren el mazo en sí y los caminos concretos
que antes perdían cartas.
"""
import time

from models.deck import Deck
from models.presence import PresencePolicy

from test_disconnect_roles import room_clients  # noqa: F401 (fixture)


def _start_seq_game(manager, room_clients, room_id, names, hand_size=None):
    """Une `names` (el primero es líder), fija turn_order='sequential' y arranca.

    `hand_size` se ajusta ANTES de arrancar: las opciones solo se pueden cambiar
    en 'waiting', así que ponerla después no tendría efecto."""
    clients, sids = {}, {}
    for name in names:
        clients[name], sids[name] = room_clients(name, room_id)
    room = manager.rooms[room_id]
    room.options["turn_order"] = "sequential"
    if hand_size is not None:
        room.options["hand_size"] = hand_size
    clients[names[0]].emit("start_game", {"room_id": room_id})
    clients[names[0]].get_received()
    return clients, sids, room


# --- El mazo por dentro ------------------------------------------------------

class TestDeckUnit:
    def test_deal_moves_cards_out_of_the_pool(self):
        deck = Deck([f"carta {i}" for i in range(10)])
        drawn = deck.deal(4)
        assert len(drawn) == 4
        assert deck.count() == 6
        assert deck.size == 10  # el total no cambia al repartir

    def test_return_puts_exactly_the_same_cards_back(self):
        deck = Deck([f"carta {i}" for i in range(10)])
        drawn = deck.deal(4)
        deck.return_(drawn)
        assert deck.count() == 10
        # Devueltas, ya no están en ninguna mano: nada repartido y nada perdido
        assert deck.audit() == []

    def test_deal_returns_nothing_when_the_pool_is_short(self):
        """Preferimos repartir de menos a perder cartas: si no hay suficientes,
        no se toca el mazo."""
        deck = Deck(["a", "b"])
        assert deck.deal(5) == []
        assert deck.count() == 2

    def test_deal_zero_or_negative_is_a_no_op(self):
        deck = Deck(["a", "b"])
        assert deck.deal(0) == []
        assert deck.deal(-3) == []
        assert deck.count() == 2

    def test_inject_grows_the_deck_and_is_reported(self):
        deck = Deck(["a", "b"])
        assert deck.inject(["c", "d", "e"]) == 3
        assert deck.size == 5
        assert deck.audit() == []

    def test_inject_ignores_empty_entries(self):
        deck = Deck(["a", "b"])
        assert deck.inject(["c", "", "   ", None]) == 1
        assert deck.size == 3

    def test_pool_is_a_view_and_does_not_mutate(self):
        deck = Deck(["a", "b", "c"])
        view = deck.pool()
        view.clear()
        assert deck.count() == 3  # la vista no toca el mazo

    def test_summary_reports_the_split(self):
        deck = Deck([f"c{i}" for i in range(8)])
        deck.deal(3)
        assert deck.summary() == "8 cartas, 5 en el mazo, 3 repartidas"

    def test_cards_are_still_plain_strings_for_the_client(self):
        """El frontend no debe enterarse del cambio: la carta sigue siendo un
        str que se serializa a JSON exactamente igual que antes."""
        import json
        card = Deck(["hola"]).deal(1)[0]
        assert isinstance(card, str)
        assert card == "hola"
        assert json.dumps({"hand": [card]}) == '{"hand": ["hola"]}'
        assert f"{card}" == "hola"
        assert card.upper() == "HOLA"
        assert card.startswith("hol")


# --- La auditoría es por identidad, no por conteo ----------------------------

class TestDeckAudit:
    def test_clean_deck_has_no_problems(self):
        deck = Deck([f"c{i}" for i in range(5)])
        assert deck.audit() == []

    def test_two_cards_with_the_same_text_are_different_cards(self):
        deck = Deck(["se repite", "se repite", "otra"])
        repartir = deck.deal(3)  # las tres fuera del mazo
        iguales = [c for c in repartir if str(c) == "se repite"]
        a, b = iguales
        assert str(a) == str(b) == "se repite"
        assert a.card_id != b.card_id
        assert deck.audit([("una mano", c) for c in repartir]) == []

    def test_audit_detects_a_lost_card(self):
        deck = Deck(["a", "b", "c"])
        a, b = deck.deal(2)
        assert deck.audit([("la mano de Ana", a), ("la mano de Beto", b)]) == []
        # La de Beto se evapora
        problems = deck.audit([("la mano de Ana", a)])
        assert len(problems) == 1
        assert "ha desaparecido" in problems[0]
        assert str(b) in problems[0]

    def test_audit_detects_a_card_in_two_places_at_once(self):
        deck = Deck(["a", "b"])
        card = deck.deal(1)[0]
        problems = deck.audit([("la mano de Ana", card), ("otra mano", card)])
        assert any("duplicada" in p for p in problems)
        assert any("la mano de Ana" in p and "otra mano" in p for p in problems)

    def test_audit_catches_what_counting_cannot(self):
        """El motivo de auditar por identidad. Aquí el CONTEO cuadra
        perfectamente (3 cartas ni más ni menos) y sin embargo se ha roto la
        conservación: una carta se ha sustituido por su texto plano."""
        deck = Deck(["se repite", "se repite", "otra"])
        a, b = deck.deal(2)
        assert deck.audit([("la mano de Ana", a), ("la mano de Beto", b)]) == []
        assert len(deck.pool()) + 2 == deck.size  # el conteo sigue cuadrando

        # Alguien sustituye su carta por el texto (p. ej. al rehidratar estado)
        problems = deck.audit([("la mano de Ana", a), ("la mano de Beto", str(b))])
        assert any("sin identidad" in p for p in problems)
        assert any("ha desaparecido" in p for p in problems)

    def test_audit_flags_a_card_the_deck_never_issued(self):
        from models.deck import Card
        deck = Deck(["a", "b"])
        ajena = Card("ajena", "forged-id")
        problems = deck.audit([("la mano de Ana", ajena)])
        assert any("no es de este mazo" in p for p in problems)

    def test_returning_an_unknown_card_registers_it_instead_of_losing_it(self):
        """Un mazo que recibe una carta que no conoce la da de alta: mejor un
        registro de más (que `audit` verá) que perderla en silencio."""
        from models.deck import Card
        deck = Deck(["a", "b"])
        ajena = Card("ajena", "forged-id")
        deck.return_([ajena])
        assert deck.count() == 3
        assert deck.size == 3
        assert str(ajena) in [str(c) for c in deck.pool()]
        assert deck.audit() == []


# --- Integración con la sala -------------------------------------------------

class TestRoomDeck:
    def test_deck_size_is_derived_not_counted(self, manager, room_clients):
        """No hay contador paralelo: `deck_size` sale del mazo, así que inyectar
        cartas custom no puede olvidarse de actualizar nada."""
        clients, sids, room = _start_seq_game(manager, room_clients, "D1", ["Ana", "Beto", "Caro"])
        before = room.deck_size
        assert before == room.deck.size

        manager.add_room_cards("D1", "Inyectada 1,Inyectada 2")
        assert room.deck_size == before + 2
        assert room.audit_cards() == []

    def test_room_audit_covers_hands_and_submissions(self, manager, room_clients):
        clients, sids, room = _start_seq_game(manager, room_clients, "D2", ["Ana", "Beto", "Caro"])
        manager.play_card(sids["Beto"], "D2", 0)
        manager.play_card(sids["Caro"], "D2", 0)  # judging, dos submissions
        assert room.state == "judging"
        assert room.audit_cards() == []

    def test_revealing_and_choosing_a_winner_keeps_the_cards(self, manager, room_clients):
        clients, sids, room = _start_seq_game(manager, room_clients, "D3", ["Ana", "Beto", "Caro"])
        manager.play_card(sids["Beto"], "D3", 0)
        manager.play_card(sids["Caro"], "D3", 0)
        czar = room.czar
        manager.reveal_card(czar, "D3", room.played_cards[0]["id"])
        manager.choose_winner(czar, "D3", room.played_cards[0]["id"])
        assert room.state == "round_end"
        assert room.audit_cards() == []


# --- Los caminos que perdían cartas de verdad --------------------------------

class TestConservationOfARealGame:
    """REGRESIÓN: al pasar de ronda a ronda, `advance_to_next_round` hacía
    `played_cards = []` sin devolver las cartas al mazo. Se perdían las cartas
    jugadas en CADA ronda, de forma monótona, y el mazo se vaciaba para siempre.
    El WARNING del reaper lo llevaba señalando sin que nadie lo mirara; la
    fixture `deck_audit` lo falla en la primera ronda."""

    def _play_a_full_round(self, manager, sids, room, room_id):
        czar = room.czar
        for sid in sids.values():
            if sid != czar:
                manager.play_card(sid, room_id, 0)
        assert room.state == "judging"
        manager.choose_winner(czar, room_id, room.played_cards[0]["id"])
        assert room.state == "round_end"
        manager.force_next_round(czar, room_id)
        assert room.state == "playing"

    def test_cards_survive_repeated_rounds(self, manager, room_clients):
        clients, sids, room = _start_seq_game(manager, room_clients, "R1", ["Ana", "Beto", "Caro"], hand_size=5)
        in_pool = room.deck.count()

        for _ in range(6):  # seis rondas completas
            self._play_a_full_round(manager, sids, room, "R1")
            assert room.audit_cards() == []

        # El mazo no se ha ido vaciando: cada ronda devuelve lo que se jugó
        assert room.deck.count() == in_pool

    def test_renew_with_a_departed_player_keeps_their_cards(self, manager, room_clients):
        """REGRESIÓN: al renovar solo se vaciaban las manos de los jugadores
        conectados, y las submissions de los que se habían marchado se perdían
        al hacer `played_cards = []`. Una carta destruida por cada desconectado
        que hubiera jugado."""
        clients, sids, room = _start_seq_game(manager, room_clients, "R2", ["Ana", "Beto", "Caro"])
        manager.play_card(sids["Beto"], "R2", 0)
        manager.play_card(sids["Caro"], "R2", 0)
        assert len(room.played_cards) == 2

        manager.disconnect(sids["Beto"])       # se va DESPUÉS de jugar
        assert len(room.played_cards) == 2     # su carta sigue en la ronda

        room.options["renew_threshold"] = 0.1
        manager.vote_renew(sids["Caro"], "R2")  # renueva de golpe

        assert room.played_cards == []
        assert room.audit_cards() == []
        # La carta de Beto ha vuelto al mazo, no se ha evaporado
        assert room.deck.size == manager.rooms["R2"].deck_size

    def test_cards_returned_to_a_departed_player_reach_the_pool(self, manager, room_clients):
        """Un jugador que se marcha ya no se queda con sus cartas varadas para
        siempre: al renovar (o al revertirse la sala) vuelven al mazo."""
        clients, sids, room = _start_seq_game(manager, room_clients, "R3", ["Ana", "Beto", "Caro"], hand_size=4)
        antes = room.deck.count()
        manager.disconnect(sids["Beto"])

        room.options["renew_threshold"] = 0.1
        manager.vote_renew(sids["Caro"], "R3")

        assert room.audit_cards() == []
        # Ana y Caro se reparten de nuevo; las de Beto han vuelto al mazo
        assert room.deck.count() > antes

    def test_purged_zombie_does_not_strand_its_hand(self, manager, room_clients, monkeypatch):
        """Purgar un zombi tampoco puede dejar cartas en un asiento muerto: la
        mano vuelve al mazo al liberar el asiento."""
        clients, sids, room = _start_seq_game(manager, room_clients, "R4", ["Ana", "Beto", "Caro"], hand_size=4)
        manager.play_card(sids["Beto"], "R4", 0)

        # Beto es un zombi real: contestó latidos y luego se congeló
        clients["Beto"].emit("heartbeat_pong")
        room.players[sids["Beto"]].last_seen = time.time() - 10 ** 6
        monkeypatch.setattr(manager, "PRESENCE", PresencePolicy(0.05, 0))

        # Sospecha y purga son dos pasos: nunca se expulsa a quien no se le ha
        # avisado antes.
        manager.reap_empty_rooms()
        manager.reap_empty_rooms()

        assert room.players[sids["Beto"]].is_connected is False
        assert room.audit_cards() == []

    def test_cambiarse_de_nombre_no_pierde_la_mano(self, manager, room_clients):
        """Reentrarse con OTRO nombre no puede crear un jugador nuevo encima del
        viejo: eso descartaba su mano y su marcador, y las cartas de la mano
        vieja no estaban ya en ninguna parte (el mazo no las volvía a ver nunca
        más). Perder cartas es justo lo que este test existe para cazar.

        El caso real: el jugador sigue en la mesa, cambia el apodo en el login y
        vuelve a entrar."""
        clients, sids, room = _start_seq_game(manager, room_clients, "R5", ["Ana", "Beto", "Caro"])
        ana = room.players[sids["Ana"]]
        mano = list(ana.hand)
        puntos = ana.points

        assert manager.join_game(sids["Ana"], "Ana2", "R5") is True

        assert room.players[sids["Ana"]].name == "Ana2"
        assert list(room.players[sids["Ana"]].hand) == mano
        assert room.players[sids["Ana"]].points == puntos
        assert room.audit_cards() == []
        # Y sigue siendo la misma persona para la mesa: no hay un asiento más.
        assert len(room.players) == 3

    def test_renombrarse_a_un_nombre_ocupado_se_rechaza(self, manager, room_clients):
        """Contrapeso del anterior: recuperar (o renombrar) el asiento propio no
        puede pisar el nombre de OTRO jugador conectado. Este test no
        distingue el arreglo del comportamiento de antes (el chequeo de
        duplicados ya lo cubría): fija el contrato para que el arreglo no lo
        rompa por el camino."""
        clients, sids, room = _start_seq_game(manager, room_clients, "R6", ["Ana", "Beto", "Caro"])
        mano = list(room.players[sids["Ana"]].hand)

        assert manager.join_game(sids["Ana"], "Beto", "R6") is False
        assert room.players[sids["Ana"]].name == "Ana"
        assert list(room.players[sids["Ana"]].hand) == mano
        assert room.audit_cards() == []

    def test_un_indice_repetido_no_juega_la_misma_carta_dos_veces(self, manager, room_clients):
        """`[0, 0]` es una elección degenerada que el cliente no puede producir,
        pero el servidor no se fía: jugaba la misma carta dos veces y sacaba dos
        cartas de la mano, y la segunda se perdía del sistema para siempre."""
        clients, sids, room = _start_seq_game(manager, room_clients, "R7", ["Ana", "Beto"], hand_size=5)
        beto = room.players[sids["Beto"]]
        antes = len(beto.hand)

        manager.play_card(sids["Beto"], "R7", [0, 0])

        assert len(beto.played_card) == 1
        assert len(beto.hand) == antes - 1
        assert room.audit_cards() == []

    def test_una_jugada_con_indices_basura_no_revierte_nada(self, manager, room_clients):
        """Lo que llega por el socket no es de fiar: un `null`, un texto o un
        índice fuera de rango no pueden reventar el handler (TypeError en la
        comparación) ni devolver una jugada a medias. Con el cerrojo de la sala
        tomado, una excepción ahí se lleva por delante la partida entera."""
        clients, sids, room = _start_seq_game(manager, room_clients, "R8", ["Ana", "Beto"], hand_size=5)
        beto = room.players[sids["Beto"]]
        antes = list(beto.hand)

        for basura in (None, "hola", [], [-1], [99], {"a": 1}):
            manager.play_card(sids["Beto"], "R8", basura)
            assert beto.played_card is None, f"{basura!r} no puede contar como jugada"
            assert list(beto.hand) == antes, f"{basura!r} no puede tocar la mano"

        # Lo mixed: una selección con un elemento bueno y uno basura juega la
        # buena y se come la basura. Antes esto reventaba con TypeError y el
        # jugador perdía la jugada entera por un elemento que ni era suyo.
        manager.play_card(sids["Beto"], "R8", [1, "x"])
        assert beto.played_card == [antes[1]]
        assert list(beto.hand) == antes[:1] + antes[2:]
        assert room.audit_cards() == []

    def test_anadir_cartas_basura_no_tumba_el_handler(self, manager, room_clients):
        """`add_room_cards` con un `null` en el JSON llegaba a `.split()` y
        reventaba. Se descarta lo que no sea texto."""
        clients, sids, room = _start_seq_game(manager, room_clients, "R9", ["Ana", "Beto"])
        antes = room.deck.size
        for basura in (None, 42, {"a": 1}, []):
            manager.add_room_cards("R9", basura)
        assert room.deck.size == antes
