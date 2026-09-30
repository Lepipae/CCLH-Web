"""Presencia en dos fases: la máquina y su efecto en la partida.

Antes, la purga era un único número (`ZOMBIE_TIMEOUT_SECONDS`): en la pasada
en la que el reaper se enteraba de que un cliente llevaba rato sin contestar, lo
echaba. Aquí se comprueba que el silencio es un proceso y no un veredicto:

- la sospecha avisa y tiene fecha de caducidad,
- la gracia devuelve el asiento a quien contesta,
- la purga solo llega si nadie ha dicho nada,
- y lo que le llega al cliente dice cuándo y por qué.

La primera mitad prueba la máquina sola (models/presence.py), sin GameManager:
relojes, plazos y perdones, con el tiempo pasado a mano. La segunda mitad
comprueba que el reaper la usa y que los eventos son los que el cliente espera.
"""
import logging
import time

import pytest

from models.presence import Presence, PresencePolicy, VIVO, SOSPECHOSO, PURGADO
from conftest import get_event
from test_disconnect_roles import room_clients  # noqa: F401 (fixture)

AHORA = 1_000_000.0   # reloj fijo: los plazos se comprueban sin dormir


def _silenciada(desde):
    return Presence(quiet_since=desde)


# --- La máquina sola ---------------------------------------------------------

class TestMaquinaDePresencia:
    def test_sin_silencio_no_se_sospecha(self):
        p = _silenciada(AHORA)
        assert p.accuse(AHORA + 119, PresencePolicy(120, 180)) is False
        assert p.state == VIVO

    def test_el_silencio_abre_la_fase_sospechosa_con_fecha_de_caducidad(self):
        p = _silenciada(AHORA)
        assert p.accuse(AHORA + 121, PresencePolicy(120, 180)) is True
        assert p.state == SOSPECHOSO
        # El plazo se mide desde la detección: es lo que el cliente ve y lo que
        # garantiza una ventana completa de vuelta, aunque el silencio empezara
        # mucho antes.
        assert p.deadline == pytest.approx(AHORA + 121 + 180)
        assert p.segundos_para_purga(AHORA + 121) == pytest.approx(180)

    def test_la_gracia_no_se_empuja_con_cada_pasada(self):
        """Un reaper lento no puede regalarse gracia infinita.

        Si cada pasada dejara correr el plazo desde el `now` de esa pasada, un
        cliente que calla entre pasada y pasada no llegaría nunca a la purga: el
        plazo se reiniciaría solo. El reaper pasa cada ROOM_REAP_INTERVAL, así
        que esto no es teórico."""
        p = _silenciada(AHORA)
        politica = PresencePolicy(120, 180)
        assert p.accuse(AHORA + 121, politica) is True
        plazo = p.deadline

        assert p.accuse(AHORA + 130, politica) is False   # pasada siguiente
        assert p.accuse(AHORA + 200, politica) is False
        assert p.deadline == plazo
        assert p.purge_due(AHORA + 200) is False
        assert p.purge_due(plazo) is True

    def test_no_se_purga_mientras_dura_la_gracia(self):
        p = _silenciada(AHORA)
        p.accuse(AHORA + 121, PresencePolicy(120, 180))
        assert p.purge_due(AHORA + 300) is False   # un segundo antes del plazo
        assert p.purge_due(AHORA + 301) is True

    def test_una_senal_perdona_la_sospecha_y_rompe_el_plazo(self):
        """La vuelta del cliente es el caso bueno: contesta y lo conserva todo."""
        p = _silenciada(AHORA)
        p.accuse(AHORA + 121, PresencePolicy(120, 180))

        assert p.signal(AHORA + 130) is True    # hubo transición: hay que avisar
        assert p.state == VIVO
        assert p.deadline is None
        # Y ni aunque el silencio vuelva a empezar, el plazo viejo resucita.
        assert p.purge_due(AHORA + 10_000) is False

    def test_una_senal_en_vivo_no_transiciona(self):
        p = _silenciada(AHORA)
        assert p.signal(AHORA + 10) is False
        assert p.state == VIVO
        assert p.quiet_since == AHORA + 10     # el reloj sí se renueva

    def test_sin_voz_no_se_purga_nunca(self):
        p = Presence()   # quiet_since=None: "aquí hay alguien"
        assert p.accuse(AHORA, PresencePolicy(0, 0)) is False
        assert p.state == VIVO
        assert p.purge_due(AHORA) is False

    def test_limpiar_el_silencio_tambien_perdona(self):
        """La vía de las salas: "aquí hay alguien" no es un evento fechado."""
        p = _silenciada(AHORA)
        p.accuse(AHORA + 121, PresencePolicy(120, 180))
        assert p.clear_silence() is True
        assert p.state == VIVO
        assert p.quiet_since is None

    def test_el_plazo_total_es_derivado_y_no_otro_numero(self):
        politica = PresencePolicy(120, 180)
        assert politica.purge_after == 300
        # Coherente con el umbral único que sustituye: no se purga antes que
        # antes, ahora repartido en dos fases.
        assert politica.purge_after == 120 + 180

    def test_el_entorno_manda_pero_un_valor_malo_no_impide_arrancar(self, monkeypatch):
        monkeypatch.setenv("PRESENCE_SUSPECT_SECONDS", "45")
        monkeypatch.setenv("PRESENCE_GRACE_SECONDS", "no soy un número")
        politica = PresencePolicy.from_env('PRESENCE', suspect_after=120, grace=180)
        assert politica.suspect_after == 45
        assert politica.grace == 180    # el inválido cae al de por defecto

        monkeypatch.setenv("PRESENCE_SUSPECT_SECONDS", "-5")
        politica = PresencePolicy.from_env('PRESENCE', suspect_after=120, grace=180)
        assert politica.suspect_after == 120

    def test_umbrales_negativos_son_un_error_de_programacion(self):
        with pytest.raises(ValueError):
            PresencePolicy(-1, 10)

    def test_el_estado_se_publica_en_forma_de_para_el_cliente(self):
        p = _silenciada(AHORA)
        # Determinista: sin "queda", que dependería del instante en que se
        # serialice. La cuenta atrás la lleva el evento, que sí se mide al
        # emitirse, y la foto de la sala tiene que poder reconstruirse después.
        assert p.to_dict() == {'state': VIVO, 'since': None}

        p.accuse(AHORA + 121, PresencePolicy(120, 180))
        foto = p.to_dict()
        assert foto['state'] == SOSPECHOSO
        assert foto['grace'] == 180
        assert foto['deadline'] == p.deadline
        assert p.to_dict(AHORA + 121)['left'] == pytest.approx(180)

    def test_purge_es_terminal(self):
        p = _silenciada(AHORA)
        p.accuse(AHORA + 121, PresencePolicy(120, 180))
        p.purge(AHORA + 400)
        assert p.state == PURGADO
        # Ni se re-acusa ni se vuelve a purgar: el asiento ya está liberado.
        assert p.accuse(AHORA + 900, PresencePolicy(0, 0)) is False
        assert p.purge_due(AHORA + 900) is False


# --- La máquina en la partida ------------------------------------------------

def _mesa(manager, room_clients, room_id, nombres, con_latido=True):
    """Sienta a los jugadores y arranca con juez fijo.

    `con_latido=False` deja la sala sin una sola prueba de vida: es lo que
    separa "no contesta" de "no sabemos si contesta", que no es lo mismo.
    Devuelve (clientes, sids, room)."""
    clientes, sids = {}, {}
    for nombre in nombres:
        cliente, sid = room_clients(nombre, room_id)
        clientes[nombre] = cliente
        sids[nombre] = sid
    room = manager.rooms[room_id]
    room.options['turn_order'] = 'sequential'
    manager.start_game(sids[nombres[0]], room_id)
    if con_latido:
        for sid in sids.values():
            manager.heartbeat(sid)   # prueba de vida: fija PRESENCE en vez de
            #                            PRESENCE_SIN_PRUEBA, que es más generosa
    return clientes, sids, room


def _calla(room, segundos):
    """Inmoviliza los relojes de la sala: nadie contesta ningún latido."""
    rancio = time.time() - segundos
    for p in room.players.values():
        p.last_seen = rancio


def _calla_vacia(room, segundos):
    """Envejece el reloj de una sala que se ha quedado vacía."""
    room.empty_since = time.time() - segundos


def _politica(manager, monkeypatch, sospecha, gracia, atributo='PRESENCE'):
    monkeypatch.setattr(manager, atributo, PresencePolicy(sospecha, gracia))


def _eventos(cliente, evento):
    return [e['args'][0] for e in cliente.get_received() if e['name'] == evento]


class TestReaperEnDosFases:
    def test_el_silencio_sospecha_pero_no_purga_a_nadie(self, manager, room_clients, monkeypatch):
        """El cambio de comportamiento: detectar el silencio ya no es echar a
        nadie. En la primera pasada todos están sospechosos y todos conservan su
        asiento."""
        _, _, room = _mesa(manager, room_clients, "P1", ["Ana", "Beto", "Caro"])
        _politica(manager, monkeypatch, 0.05, 180)
        _calla(room, 3600)

        manager.reap_empty_rooms()

        assert all(p.presence.state == SOSPECHOSO for p in room.players.values())
        assert all(p.is_connected for p in room.players.values())
        assert room.empty_since is None
        assert manager.presencia['jugadores'][SOSPECHOSO] == 3

    def test_la_sospecha_avisa_a_la_sala_con_plazo_y_motivo(self, manager, room_clients, monkeypatch):
        """Lo que el cliente recibe tiene que servir para explicarse: quién,
        desde cuándo, hasta cuándo y por qué."""
        clientes, sids, room = _mesa(manager, room_clients, "P2", ["Ana", "Beto", "Caro"])
        _politica(manager, monkeypatch, 0.05, 180)
        _calla(room, 3600)
        for c in clientes.values():
            c.get_received()

        manager.reap_empty_rooms()

        for nombre, cliente in clientes.items():
            avisos = [a for a in _eventos(cliente, 'presence_alert')
                      if a['name'] == nombre]
            assert len(avisos) == 1, f"{nombre} no se ha avisado a sí mismo"
            aviso = avisos[0]
            assert aviso['state'] == SOSPECHOSO
            assert aviso['room_id'] == "P2"
            assert aviso['grace'] == 180
            assert aviso['left'] == pytest.approx(180, abs=5)
            assert aviso['deadline'] > time.time()
            assert "silencio de" in aviso['reason']

    def test_el_reto_llega_solo_al_sospechoso(self, manager, room_clients, monkeypatch):
        """`presence_challenge` es el reto explícito: va al que se ha callado, no
        a la mesa. Y lleva la cuenta atrás."""
        clientes, sids, room = _mesa(manager, room_clients, "P3", ["Ana", "Beto"])
        _politica(manager, monkeypatch, 0.05, 180)
        # Solo una se calla; el otro sigue contestando
        room.players[sids["Ana"]].last_seen = time.time() - 3600
        for c in clientes.values():
            c.get_received()

        manager.reap_empty_rooms()

        retos = _eventos(clientes["Ana"], 'presence_challenge')
        assert len(retos) == 1, "el sospechoso tiene que recibir su reto"
        assert retos[0]['name'] == 'Ana'
        assert retos[0]['room_id'] == 'P3'
        assert retos[0]['left'] == pytest.approx(180, abs=5)
        assert retos[0]['deadline'] > time.time()
        # El que sí está vivo no recibe ningún reto: no tiene nada que demostrar
        assert _eventos(clientes["Beto"], 'presence_challenge') == []

    def test_incluso_sin_gracia_nadie_se_expulsa_sin_haber_sido_avisado(
            self, manager, room_clients, monkeypatch):
        """Con `grace = 0` la gracia dura lo que dura la detección, pero la
        purga sigue necesitando su propia pasada. Es el `elif` de `_reap_jugadores`
        lo que lo garantiza, y es lo que impide que una configuración de
        despliegue con margen cero se convierta en el umbral único de antes."""
        _, sids, room = _mesa(manager, room_clients, "P9", ["Ana", "Beto"])
        _politica(manager, monkeypatch, 0.05, 0)
        _calla(room, 3600)

        manager.reap_empty_rooms()
        assert all(p.presence.state == SOSPECHOSO for p in room.players.values())
        assert all(p.is_connected for p in room.players.values())
        assert manager.presencia['jugadores'][PURGADO] == 0

        manager.reap_empty_rooms()
        assert all(p.presence.state == PURGADO for p in room.players.values())
        assert manager.presencia['jugadores'][PURGADO] == 2

    def test_responder_durante_la_gracia_conserva_el_asiento(self, manager, room_clients, monkeypatch):
        """La fase intermedia sirve para esto: el cliente vuelve, contesta el
        reto y nadie pierde nada. Es el caso de las pestañas en segundo plano."""
        clientes, sids, room = _mesa(manager, room_clients, "P4", ["Ana", "Beto", "Caro"])
        _politica(manager, monkeypatch, 0.05, 0.3)
        _calla(room, 3600)
        for c in clientes.values():
            c.get_received()

        manager.reap_empty_rooms()   # sospecha, con 0,3 s de gracia
        assert room.players[sids["Ana"]].presence.state == SOSPECHOSO

        # El navegador vuelve: contesta el reto con el mismo pong de siempre
        manager.heartbeat(sids["Ana"])
        avisos = _eventos(clientes["Ana"], 'presence_alert')
        aviso = [a for a in avisos if a['name'] == 'Ana'][-1]
        assert aviso['state'] == VIVO
        assert manager.presencia['jugadores'][VIVO] == 1
        assert room.players[sids["Ana"]].presence.state == VIVO

        # Y la gracia de los otros sí se cumple: ellos se van
        _calla(room, 3600)
        time.sleep(0.35)
        manager.reap_empty_rooms()

        assert room.players[sids["Ana"]].is_connected
        assert room.players[sids["Beto"]].is_connected is False
        assert room.players[sids["Caro"]].is_connected is False
        assert manager.presencia['jugadores'][PURGADO] == 2

    def test_la_gracia_cumplida_libera_el_asiento_igual_que_una_desconexion(
            self, manager, room_clients, monkeypatch):
        clientes, sids, room = _mesa(manager, room_clients, "P5", ["Ana", "Beto", "Caro"])
        _politica(manager, monkeypatch, 0.05, 0)
        room.players[sids["Beto"]].last_seen = time.time() - 3600
        room.players[sids["Caro"]].last_seen = time.time() - 3600
        for c in clientes.values():
            c.get_received()

        manager.reap_empty_rooms()   # sospecha
        manager.reap_empty_rooms()   # purga

        assert room.players[sids["Ana"]].is_connected
        assert room.players[sids["Beto"]].is_connected is False
        assert room.players[sids["Beto"]].presence.state == PURGADO
        # El aviso de purga conserva el plazo que acaba de cumplirse
        purgas = [a for a in _eventos(clientes["Ana"], 'presence_alert')
                  if a['name'] == 'Beto']
        aviso = purgas[-1]
        assert aviso['state'] == PURGADO
        assert aviso['left'] == 0
        # Y la sala queda saneada como tras cualquier desconexión: con una sola
        # persona no puede seguir una partida, así que vuelve a la espera (y con
        # ella vuelven al mazo las cartas de la ronda).
        assert room.state == 'waiting'
        assert room.czar is None
        assert room.leader == sids["Ana"]
        assert room.audit_cards() == []

    def test_el_aviso_no_se_repite_en_cada_pasada(self, manager, room_clients, monkeypatch):
        """Si el reaper reenviara el aviso cada 15 s, la mesa vería un cartel
        parpadeante durante los tres minutos de gracia."""
        clientes, _, room = _mesa(manager, room_clients, "P6", ["Ana", "Beto"])
        _politica(manager, monkeypatch, 0.05, 180)
        _calla(room, 3600)
        ana = clientes["Ana"]
        ana.get_received()

        manager.reap_empty_rooms()
        vistos = _eventos(ana, 'presence_alert')
        manager.reap_empty_rooms()
        manager.reap_empty_rooms()
        vistos += _eventos(ana, 'presence_alert')

        assert len(vistos) == 2  # los dos sospechosos, un aviso cada uno

    def test_sin_prueba_se_juzga_con_la_politica_generosa(self, manager, room_clients, monkeypatch):
        """La falta de pruebas cambia QUÉ política se aplica, no si se aplica.

        Con la política normal puesta a cero y la de falta de prueba al
        revés, estos jugadores no son sospechosos: son los que no han
        demostrado que sepan contestar, y el reaper les concede el margen
        largo. Si el selector de política estuviese roto y usaran PRESENCE,
        serían sospechosos y purgados en las dos pasadas siguientes.
        """
        _, _, room = _mesa(manager, room_clients, "P7", ["Ana", "Beto", "Caro"],
                           con_latido=False)
        _politica(manager, monkeypatch, 0.01, 0)
        _politica(manager, monkeypatch, 10 ** 9, 10 ** 9, 'PRESENCE_SIN_PRUEBA')
        _calla(room, 10 ** 7)

        manager.reap_empty_rooms()
        manager.reap_empty_rooms()

        assert all(p.presence.state == VIVO for p in room.players.values())
        assert all(p.is_connected for p in room.players.values())
        assert manager.presencia['jugadores'] == {SOSPECHOSO: 0, VIVO: 0, PURGADO: 0}

    def test_sin_prueba_tambien_se_sospecha_y_se_purga(self, manager, room_clients, monkeypatch):
        """Y cuando su propia política vence, los mismos dos pasos que todos.

        Nadie queda sin juicio: un cliente que jamás contesta un latido es
        precisamente el que hay que juzgar, porque su transporte sigue vivo
        (JS bloqueado, hidratación rota, bundle viejo) y sin juicio fijaba
        su sala para siempre. La gracia sigueamia siendo la red: primero el
        aviso, después el reto, y solo luego el asiento.
        """
        clientes, _, room = _mesa(manager, room_clients, "P7B", ["Ana", "Beto", "Caro"],
                                  con_latido=False)
        _politica(manager, monkeypatch, 10 ** 9, 10 ** 9)
        _politica(manager, monkeypatch, 0.05, 0, 'PRESENCE_SIN_PRUEBA')
        _calla(room, 10 ** 7)

        manager.reap_empty_rooms()
        assert all(p.presence.state == SOSPECHOSO for p in room.players.values())
        for cliente in clientes.values():
            # Nadie se purga sin haber recibido antes su reto.
            assert len(_eventos(cliente, 'presence_challenge')) == 1

        manager.reap_empty_rooms()
        assert all(p.presence.state == PURGADO for p in room.players.values())
        assert not any(p.is_connected for p in room.players.values())

    def test_quien_contesta_al_reto_conserva_el_asiento(self, manager, room_clients, monkeypatch):
        """La salida sigue abierta para quien no había dado pruebas: el reto
        es la última puerta de vuelta y funciona igual de bien."""
        clientes, sids, room = _mesa(manager, room_clients, "P7C", ["Ana", "Beto", "Caro"],
                                     con_latido=False)
        _politica(manager, monkeypatch, 10 ** 9, 10 ** 9)
        _politica(manager, monkeypatch, 0.05, 300, 'PRESENCE_SIN_PRUEBA')
        _calla(room, 10 ** 7)

        manager.reap_empty_rooms()
        assert all(p.presence.state == SOSPECHOSO for p in room.players.values())

        # Contesta Ana al reto: vuelve a vivo y conserva su asiento.
        clientes['Ana'].emit('heartbeat_pong')
        ana = room.players[sids['Ana']]
        assert ana.presence.state == VIVO
        assert ana.is_connected
        assert ana.heartbeat_pongs == 1

        # Los otros dos, mudos, siguen siendo sospechosos: el margen no se
        # consume con el paso del tiempo ni se reinicia solo.
        manager.reap_empty_rooms()
        assert all(p.presence.state == SOSPECHOSO
                   for n, p in room.players.items() if n != sids['Ana'])

    def test_el_estado_de_presencia_tambien_viaja_en_el_game_update(
            self, manager, room_clients, monkeypatch):
        """La foto de la sala lleva el estado, no solo las transiciones: un
        cliente que entre a mitad de una sospecha tiene que poder pintarla sin
        haber visto el evento."""
        clientes, sids, room = _mesa(manager, room_clients, "P8", ["Ana", "Beto"])
        _politica(manager, monkeypatch, 0.05, 180)
        room.players[sids["Beto"]].last_seen = time.time() - 3600
        ana = clientes["Ana"]
        ana.get_received()

        manager.reap_empty_rooms()
        # La sospecha no emite game_update (el juego no ha cambiado): lo que lo
        # demuestra es la siguiente difusión de verdad, la de Beto jugando.
        manager.play_card(sids["Beto"], "P8", 0)

        updates = [e['args'][0] for e in ana.get_received() if e['name'] == 'game_update']
        beto = next(p for p in updates[-1]['players'] if p['id'] == sids['Beto'])
        assert beto['presence']['state'] == SOSPECHOSO
        assert beto['presence']['grace'] == 180


# --- El ciclo de vida de la sala --------------------------------------------

class TestSalaEnDosFases:
    def test_medir_la_sala_no_la_mantiene_viva(self, manager, room_clients, monkeypatch):
        """Un getter que muta la máquina de presencia es una trampa: `to_dict`
        llama a `get_active_players` (para saber cuánta gente vota la
        renovación), y ese método también movía el reloj de la sala. Medir una
        mesa la mantenía viva, y una sala vacía se colgaba de su propia foto.
        El reloj de una sala solo puede moverlo la desconexión, que es el
        evento que lo significa."""
        _, sids, room = _mesa(manager, room_clients, "S1", ["Ana", "Beto"])
        manager.disconnect(sids["Beto"])
        manager.disconnect(sids["Ana"])
        vacia_desde = room.empty_since
        assert vacia_desde is not None

        # Serializar el estado no lo reinicia (ni lo indulta)
        for _ in range(3):
            room.to_dict(for_sid="fantasma")
        assert room.empty_since == vacia_desde

        # Y la sala sigue expirando exactamente igual que antes
        _politica(manager, monkeypatch, 0.05, 0, atributo='ROOM_LIFECYCLE')
        _calla_vacia(room, 3600)
        manager.reap_empty_rooms()
        assert room.presence.state == SOSPECHOSO
        manager.reap_empty_rooms()
        assert "S1" not in manager.rooms

    def test_una_sala_vacia_no_se_borra_hasta_que_venza_la_gracia(
            self, manager, room_clients, monkeypatch):
        _, sids, room = _mesa(manager, room_clients, "S1", ["Ana", "Beto"])
        manager.disconnect(sids["Beto"])
        manager.disconnect(sids["Ana"])
        assert room.empty_since is not None
        _politica(manager, monkeypatch, 0.05, 180, atributo='ROOM_LIFECYCLE')
        _calla_vacia(room, 3600)

        manager.reap_empty_rooms()
        assert room.presence.state == SOSPECHOSO
        assert "S1" in manager.rooms

        # Pasa el tiempo. Se adelanta el plazo en vez de reacortar la política,
        # porque el plazo que se le enseñó al cliente no se toca por el camino.
        room.presence.deadline = time.time() - 1
        manager.reap_empty_rooms()
        assert "S1" not in manager.rooms
        assert manager.presencia['salas'][PURGADO] == 1

    def test_una_sala_recuperada_durante_la_gracia_no_es_un_lobby_nuevo(
            self, manager, room_clients, monkeypatch):
        """Quien vuelve en la ventana de gracia se reencuentra con SU mesa: la
        misma sala, con la misma gente y el mismo marcador.

        Lo que NO se conserva es la ronda en curso, y no es culpa de la máquina
        de presencia: al quedarse la sala con menos de dos jugadores, el
        reaper de desconexión ya la había devuelto a 'waiting' y devuelto las
        cartas de la ronda al mazo (ver `_revert_to_waiting`). Lo que evita la
        gracia es que además se borre la sala entera, con su historial de
        puntos, sus opciones y sus asientos, y que quien vuelva encuentre un
        lobby recién estrenado donde ha perdido la partida."""
        _, sids, room = _mesa(manager, room_clients, "S2", ["Ana", "Beto", "Caro"])
        _politica(manager, monkeypatch, 0.05, 180, atributo='ROOM_LIFECYCLE')
        for p in room.players.values():
            p.points = 7
        opciones = dict(room.options)
        for sid in sids.values():
            manager.disconnect(sid)
        _calla_vacia(room, 3600)

        manager.reap_empty_rooms()   # la sala queda sospechosa
        assert room.presence.state == SOSPECHOSO
        assert "S2" in manager.rooms

        _, ana2 = room_clients("Ana", "S2")
        recuperada = manager.rooms["S2"]

        assert recuperada is room, "vuelve a su sala, no a una nueva"
        # Por nombre, no por sid: al reconectar, el sid es otro.
        assert {p.name: p.points for p in recuperada.players.values()} == {
            "Ana": 7, "Beto": 7, "Caro": 7}
        assert recuperada.options == opciones
        assert recuperada.presence.state == VIVO
        assert manager.presencia['salas'][VIVO] == 1
        assert room.players[ana2].is_connected
        assert room.audit_cards() == []

    def test_una_sala_sospechosa_queda_registrada_aunque_no_hay_nadie_que_lo_vea(
            self, manager, room_clients, monkeypatch, caplog):
        """La sala vacía no tiene audiencia: su sospecha solo queda en el log y
        en los contadores. Los eventos van a jugadores, que son los únicos que
        pueden verlos."""
        _, sids, room = _mesa(manager, room_clients, "S3", ["Ana"])
        _politica(manager, monkeypatch, 0.05, 180, atributo='ROOM_LIFECYCLE')
        manager.disconnect(sids["Ana"])
        _calla_vacia(room, 3600)

        with caplog.at_level(logging.INFO, logger="models.game_manager"):
            manager.reap_empty_rooms()

        assert any("S3" in r.getMessage() and "vacía" in r.getMessage()
                   for r in caplog.records)
        assert manager.presencia['salas'][SOSPECHOSO] == 1
        assert manager.presencia['jugadores'][SOSPECHOSO] == 0
