"""Fuzz determinista de las reglas del juego.

El bug de "dos cartas perdidas por ronda" vivió meses porque nadie ejecutó una
secuencia larga de acciones: los tests escritos a mano cubren los caminos que su
autor imaginó. Aquí se tiran miles de acciones legales e ilegales al motor y,
después de CADA una, se comprueba:

  * conservación de cartas por identidad (`Room.audit_cards`),
  * legalidad estructural del estado de cada sala,
  * que las acciones que no deberían poder hacer nada no hacen nada,
  * y, al final, que el fuzz ha cubierto el juego de verdad (si no, se vuelve
    un no-op que pasa sin comprobar nada).

Todo con semilla fija: el mismo fallo se reproduce siempre.

    CAH_FUZZ_SEED=1337  python -m pytest tests/test_fuzz_rules.py -q
    CAH_FUZZ_STEPS=5000 python -m pytest tests/test_fuzz_rules.py -q

Control del motor para que sea reproducible:
  * `random` global con semilla (lo usan `shuffle`, `sample` y `choice`),
  * `uuid.uuid4` sustituido por un contador (ids de submission),
  * `auto_next_round` se encola en vez de dormir 5 s, y el fuzz decide cuándo
    dispara el temporizador del avance automático, para poder explorar la
    carrera entre "el juez se va" y "el auto-avance de la ronda anterior".
"""
import os
import random

import pytest

from models import game_manager as gm_module
from models.presence import contadores, SOSPECHOSO, PURGADO

BASE_SEEDS = [1, 7, 42, 1337, 20260928]
STEPS = int(os.environ.get("CAH_FUZZ_STEPS", "400"))

ESTADOS = {"waiting", "playing", "judging", "round_end"}
NOMBRES = ["Ana", "Beto", "Caro", "Dani", "Elsa", "Fede", "Gema", "Hugo"]

# Cuánto tira el fuzz de cada tipo de acción una vez que la mesa está montada.
# 'desconectar' pesa poco a propósito: es la acción más destructiva y, si pesa
# mucho, sevacía las salas y el fuzz se queda sin mesa con la que jugar.
PESOS = {
    "unirse": 9,
    "jugar": 16,
    "revelar": 7,
    "ganador": 9,
    "votar": 9,
    "siguiente": 9,
    "renovar": 10,
    "carta_negra": 3,
    "opciones": 2,
    "latido": 3,
    "reaper": 2,
    "silencio": 4,
    "inyectar": 1,
    "temporizador": 7,
    "desconectar": 1,
    "chat": 1,
    "avance": 4,
    "arrancar": 4,
}

# Pasos iniciales solo de colocación. Sin ellos el fuzz se dispersa por salas
# nuevas y nunca se forma una mesa con dos jugadores conectados, que es el
# mínimo para que exista una partida.
PASOS_DE_COLOCACION = 12


# --- Motor determinista ------------------------------------------------------

class _UuidContador:
    """Sustituye a uuid4 para que los ids de submission sean reproducibles.

    Ojo al formato: el motor hace `str(uuid4())[:8]`, así que el devuelto tiene
    que ser ÚNICO ya en sus 8 primeros caracteres."""

    def __init__(self):
        self.n = 0

    def uuid4(self):
        self.n += 1
        return f"{self.n:08x}-fuzz"


def _auto_next_round_sin_dormir(manager, room_id, czar_sid):
    """El `auto_next_round` real, sin los 5 s de espera.

    Es la tarea en segundo plano que lanza `choose_winner`. En vez de dispararse
    sola tras 5 s (imposible de sincronizar), el fuzz la encola y la ejecuta
    cuando le parece. replica el cerrojo POR SALA del original: la sala se
    resuelve, se bloquea, y la sala ya bloqueada se le pasa a
    `advance_to_next_round` para que no vuelva a buscarla en el registro."""
    room = manager.rooms.get(room_id)
    if room is None:
        return
    with manager._sala(room):
        if room.state == "round_end" and room.czar == czar_sid:
            manager.advance_to_next_round(room_id, room)


@pytest.fixture()
def motor(monkeypatch):
    """GameManager con el motor aleatorio y las tareas de fondo bajo control."""
    import app as app_module

    m = app_module.manager
    m.rooms.clear()
    monkeypatch.setattr(gm_module, "uuid", _UuidContador())
    monkeypatch.setattr(gm_module.GameManager, "auto_next_round", _auto_next_round_sin_dormir)
    cola = []
    monkeypatch.setattr(m.socketio, "start_background_task", lambda fn, *a: cola.append((fn, a)))
    m._fuzz_cola = cola
    yield m
    m.rooms.clear()


# --- El fuzzer ---------------------------------------------------------------

class Fuzzer:
    def __init__(self, manager, seed, pasos=STEPS, salas=2, jugadores=5):
        self.m = manager
        self.seed = seed
        self.pasos = pasos
        self.n_salas = salas
        self.n_jugadores = jugadores
        self.rng = random.Random(seed)
        self.sids = [f"f{seed}-{i}" for i in range(jugadores)]
        self.estados_vistos = set()
        self.tipos_vistos = set()
        self.rondas = 0
        self.trail = []

    # -- gramática ------------------------------------------------------------

    def _opciones(self, colocando=False):
        """(tipo, descripción, callable) disponibles AHORA, en este estado.

        En fase de colocación solo se pueden sentar jugadores: si se mezclan
        acciones de juego con la mesa a medio montar, ninguna llega a tener los
        dos jugadores conectados que exige `_start_game_locked`."""
        ops = []
        m = self.m
        rng = self.rng

        for i, sid in enumerate(self.sids):
            nombre = NOMBRES[i % len(NOMBRES)]
            destino = self._destino_de(sid, i, colocando)
            ops.append(("unirse", f"unirse {nombre} a {destino}",
                        lambda s=sid, n=nombre, d=destino: m.join_game(s, n, d), None))

        if not colocando and getattr(m, "_fuzz_cola", None):
            ops.append(("temporizador", "dispara el auto-avance pendiente",
                        self._drenar_cola, None))

        if not colocando:
            ops.append(("reaper", "pasada del reaper", m.reap_empty_rooms, None))
            ops.append(("silencio", "salto del reloj de 10 min y pasada del reaper",
                        self._salto_del_reloj, None))
            ops.append(("chat", "chat",
                        lambda: self._al_azar_por_jugador(m.send_chat, "hola"), None))

        for room in list(m.rooms.values()):
            rid = room.room_id
            todos = list(room.players.values())
            vivos = [p for p in todos if p.is_connected]

            if not colocando:
                ops.append(("inyectar", f"inyectar cartas en {rid}",
                            lambda r=rid: m.add_room_cards(r, "carta nueva A, carta nueva B"), rid))

            for p in todos:
                if not colocando:
                    ops.append(("desconectar", f"desconectar {p.name} de {rid}",
                                lambda p=p: m.disconnect(p.sid), rid))
                ops.append(("latido", f"latido de {p.name}@{rid}",
                            lambda p=p: m.heartbeat(p.sid), rid))
            for p in vivos:
                ops.append(("arrancar", f"arrancar con {p.name}@{rid}",
                            lambda p=p: m.start_game(p.sid, rid), rid))
                ops.append(("opciones", f"opciones de {p.name}@{rid}",
                            lambda p=p: m.update_options(p.sid, rid, {
                                "hand_size": rng.randrange(1, 12),
                                "turn_order": rng.choice(["winner", "random", "sequential"]),
                                "renew_threshold": 0.5,
                            }), rid))
                if not colocando:
                    # Índice 0: siempre válido si la mano no está vacía, para que
                    # 'jugar' de verdad juegue y la partida avance.
                    ops.append(("jugar", f"{p.name} juega@{rid}",
                                lambda p=p: m.play_card(p.sid, rid, 0), rid))
                    ops.append(("siguiente", f"{p.name} pasa de ronda@{rid}",
                                lambda p=p: m.force_next_round(p.sid, rid), rid))
                    ops.append(("renovar", f"{p.name} vota renovar@{rid}",
                                lambda p=p: m.vote_renew(p.sid, rid), rid))
                    ops.append(("carta_negra", f"{p.name} cambia la negra@{rid}",
                                lambda p=p: m.change_black_card(p.sid, rid), rid))
                    ops.append(("chat", f"chat de {p.name}@{rid}",
                                lambda p=p: m.send_chat(p.sid, rid, "hola"), rid))
            if not colocando:
                for sub in list(room.played_cards):
                    for p in vivos:
                        ops.append(("revelar", f"{p.name} revela {sub['id']}@{rid}",
                                    lambda p=p, s=sub: m.reveal_card(p.sid, rid, s["id"]), rid))
                        ops.append(("ganador", f"{p.name} elige {sub['id']}@{rid}",
                                    lambda p=p, s=sub: m.choose_winner(p.sid, rid, s["id"]), rid))
                        ops.append(("votar", f"{p.name} vota {sub['id']}@{rid}",
                                    lambda p=p, s=sub: m.vote_card(p.sid, rid, s["id"]), rid))
            if room.state == "round_end":
                ops.append(("avance", f"avance forzado@{rid}",
                            lambda r=rid: m.advance_to_next_round(r), rid))
        return ops

    def _destino_de(self, sid, indice, colocando):
        """Dónde se mete este jugador.

        En colocación se reparte de forma determinista entre las salas base para
        garantizar mesas. Durante el juego, casi siempre vuelve **a la mesa
        donde ya tiene asiento**: si cada unión fuese a sala aleatoria, los
        jugadores se dispersarían y las mesas se quedarían con un solo
        conectado, que es justo lo que impide que exista una partida. De vez en
        cuando sí se muda de sala, que es el caso interesante (asiento
        fantasma) y lo que el fuzz tiene que cazar."""
        if colocando:
            return f"S{indice % self.n_salas}"
        for room in list(self.m.rooms.values()):
            if sid in room.players:
                if self.rng.random() < 0.85:
                    return room.room_id
                break
        return self.rng.choice([f"S{j}" for j in range(self.n_salas)] +
                               [f"NUEVA{self.rng.randrange(20)}"])

    def _drenar_cola(self):
        """Dispara las tareas en segundo plano pendientes.

        `socketio.start_background_task(self.auto_next_round, room_id, sid)`
        guarda el método YA LIGADO al manager, así que se llama sin `self`."""
        for fn, args in list(self.m._fuzz_cola):
            self.m._fuzz_cola.remove((fn, args))
            fn(*args)

    def _al_azar_por_jugador(self, fn, *a):
        for room in list(self.m.rooms.values()):
            for p in list(room.players.values()):
                if p.is_connected:
                    fn(p.sid, room.room_id, *a)

    def _salto_del_reloj(self, segundos=600):
        """Adelanta diez minutos todos los relojes de silencio y hace una pasada
        del reaper.

        Es lo que permite que el fuzz recorra la máquina de presencia: sin esto
        el `reaper` no encuentra jamás a nadie sospechoso, porque todo el mundo
        acaba de contestar un latido, y la purga (con todo lo que arrastra:
        relevo de juez, revert a 'waiting', devolución de cartas al mazo) no se
        ejercita nunca.

        El salto y la pasada van juntos a propósito: separados, el `latido` que
        saliese entre medias devolvería a un sospechoso a la vida y la sospecha
        se escaparía siempre. Dos saltos seguidos cierran el ciclo entero,
        sospechar y purgar.

        Se mueven los relojes por interpolación directa, NO con
        `presence.signal()`: que pase el tiempo no significa que el cliente haya
        hablado, y perdonar una sospecha sin pong sería falsear el modelo.

        Se adelanta también el plazo de gracia ya aberto, que es un reloj de
        pared como los otros: si solo se moviera `last_seen`, un sospechoso
        anterior al salto no se purgaría nunca.
        """
        for room in list(self.m.rooms.values()):
            for p in list(room.players.values()):
                if p.last_seen is not None:
                    p.last_seen -= segundos
                if p.presence.deadline is not None:
                    p.presence.deadline -= segundos
            if room.empty_since is not None:
                room.empty_since -= segundos
            if room.presence.deadline is not None:
                room.presence.deadline -= segundos
        self.m.reap_empty_rooms()

    def _sala_con_renuevo_en_juego(self):
        """¿Hay alguna sala en pleno recuento con un jugador que se marchó
        DESPUÉS de jugar?

        Esa es la combinación que hace perder cartas al renovar: su submission
        sigue en la ronda y `played_cards = []` se la lleva. Es una ventana de
        dos o tres acciones, porque en cuanto alguien elige ganador la ronda
        avanza y la limpia. Cuando el fuzz la ve, deja de empujar el cierre."""
        for room in self.m.rooms.values():
            if room.state != "judging" or not room.played_cards:
                continue
            if any(p.played_card is not None and not p.is_connected
                   for p in room.players.values()):
                return room.room_id
        return None

    def _pesos(self):
        """Pesos base, ajustados al estado actual.

        Sin esto el fuzz deambula: puede quedarse mucho rato en 'playing' sin
        llegar nunca a cerrar una ronda. Subir el peso de la transición que
        desbloquea el estado siguiente hace que recorra la máquina de estados en
        vez de dar vueltas por el mismo nodo."""
        pesos = dict(PESOS)
        estados = {r.state for r in self.m.rooms.values()}
        if self._sala_con_renuevo_en_juego() is not None:
            # Ventana delicada: no hay que cerrar la ronda todavía, o se pierde
            # la situación. Se empuja lo que pasa por 'return_' al mazo.
            pesos.pop("ganador", None)
            pesos["ganador"] = 1
            pesos["renovar"] *= 8
            pesos["desconectar"] *= 3
            return pesos
        if "judging" in estados:
            # Alguien tiene que elegir ganador, o la ronda no se cierra nunca
            pesos["ganador"] *= 10
            pesos["revelar"] *= 4
            # 'Meterse en medio del recuento' es el momento interesante
            pesos["desconectar"] *= 6
        if "round_end" in estados:
            # Y hay que empujar la ronda hacia la siguiente
            for tipo in ("siguiente", "avance", "temporizador"):
                pesos[tipo] *= 8
        if not estados or estados == {"waiting"}:
            pesos["arrancar"] *= 6
            pesos["unirse"] *= 3
        return pesos

    def _elegir(self, ops):
        """Elige por peso de tipo, para que el fuzz llegue a jugar de verdad.

        Si hay una sala con la ventana de renovación abierta, la mayoría de las
        veces se fuerza un voto de renovar EN ESA SALA: el impulso tiene que ser
        dirigido, porque las salas son independientes y un voto en otra no
        ejercita el camino. Se deja un 40% de decisiones libres para que el resto
        de la gramática no se quede sin ejecutar."""
        ventana = self._sala_con_renuevo_en_juego()
        if ventana is not None and self.rng.random() < 0.6:
            candidatos = [o for o in ops if o[0] == "renovar" and o[3] == ventana]
            if candidatos:
                return self.rng.choice(candidatos)
        pesos = self._pesos()
        por_tipo = [pesos.get(tipo, 1) for tipo, _d, _f, _r in ops]
        return ops[self.rng.choices(range(len(ops)), weights=por_tipo, k=1)[0]]

    # -- huellas --------------------------------------------------------------

    def _huella(self, room):
        """Foto del estado. Dos huellas iguales = la acción no ha hecho nada."""
        return (
            room.state, room.czar, room.leader, room.black_card, room.last_winner,
            room.deck.count(), room.deck.size, tuple(sorted(room.renew_votes)),
            tuple(sorted(
                (p.sid, len(p.hand), tuple(c.card_id for c in p.hand),
                 tuple(c.card_id for c in p.played_card) if isinstance(p.played_card, list)
                 else p.played_card)
                for p in room.players.values())),
            tuple(sorted(
                (c["id"], c["sid"], tuple(x.card_id for x in c["cards"]),
                 tuple(sorted(c.get("votes", ()))))
                for c in room.played_cards)),
        )

    # -- sondas ilegales ------------------------------------------------------

    def _sondas_ilegales(self, room):
        """Acciones que NO deberían poder hacer nada, y sin embargo se ejecutan.

        Son el otro lado de la aleatoriedad: no basta con que el motor sobreviva
        a lo que le tiramos, tiene que rechazar en silencio lo que no le toca.
        Cada sonda devuelve True si el estado ha quedado intacto."""
        m = self.m
        rid = room.room_id
        vivos = [p for p in room.players.values() if p.is_connected]
        ajenos = "f9999-fantasma"          # un sid que no está en ninguna sala

        def inerte(fn):
            """Ejecuta fn y comprueba que no ha cambiado nada."""
            antes = self._huella(room)
            fn()
            return self._huella(room) == antes

        # Ojo: 'unirse' NO es sonda inerte: entrar en una sala con un sid nuevo
        # es exactamente lo que debe hacer.
        sondas = [
            ("desconectar a un sid ajeno", lambda: inerte(lambda: m.disconnect(ajenos))),
            ("jugar con un sid ajeno", lambda: inerte(lambda: m.play_card(ajenos, rid, 0))),
            ("renovar con un sid ajeno", lambda: inerte(lambda: m.vote_renew(ajenos, rid))),
            ("arrancar con un sid ajeno", lambda: inerte(lambda: m.start_game(ajenos, rid))),
            ("elegir ganador con un sid ajeno",
             lambda: inerte(lambda: m.choose_winner(ajenos, rid, "nada"))),
            ("revelar una submission inexistente",
             lambda: inerte(lambda: m.reveal_card(ajenos, rid, "no-existe"))),
            ("cambiar la negra con un sid ajeno",
             lambda: inerte(lambda: m.change_black_card(ajenos, rid))),
            ("pasar de ronda con un sid ajeno",
             lambda: inerte(lambda: m.force_next_round(ajenos, rid))),
        ]

        no_lider = [p for p in vivos if p.sid != room.leader]
        if no_lider:
            sondas.append(("arrancar sin ser líder",
                           lambda p=no_lider[0]: inerte(lambda: m.start_game(p.sid, rid))))
            sondas.append(("cambiar opciones sin ser líder",
                           lambda p=no_lider[0]: inerte(
                               lambda: m.update_options(p.sid, rid, {"hand_size": 99}))))

        # El juez NO juega: la sonda usa al propio juez, no a otro jugador. Con
        # otro sería una jugada legal y la sonda no comprobaría nada.
        juez = room.players.get(room.czar) if room.czar is not None else None
        if juez is not None and juez.is_connected:
            sondas.append((f"el juez {juez.name} intenta jugar",
                           lambda p=juez: inerte(lambda: m.play_card(p.sid, rid, 0))))
        no_czar = [p for p in vivos if p.sid != room.czar]
        if no_czar and room.czar is not None:
            sondas.append(("elegir ganador sin ser juez",
                           lambda p=no_czar[0]: inerte(
                               lambda: m.choose_winner(p.sid, rid, "nada"))))
            sondas.append(("revelar sin ser juez",
                           lambda p=no_czar[0]: inerte(
                               lambda: m.reveal_card(p.sid, rid, "nada"))))
            sondas.append(("cambiar la carta negra sin ser juez",
                           lambda p=no_czar[0]: inerte(
                               lambda: m.change_black_card(p.sid, rid))))
            sondas.append(("pasar de ronda sin ser juez",
                           lambda p=no_czar[0]: inerte(
                               lambda: m.force_next_round(p.sid, rid))))

            def jugar_dos_veces(p):
                """La primera jugada puede ser legal; la segunda ya no: una sola
                submission por jugador y ronda."""
                m.play_card(p.sid, rid, 0)
                return inerte(lambda: m.play_card(p.sid, rid, 0))

            sondas.append(("jugar dos veces la misma ronda",
                           lambda p=no_czar[0]: jugar_dos_veces(p)))

        for sub in list(room.played_cards):
            dueno = room.players.get(sub["sid"])
            if dueno is not None and dueno.is_connected:
                sondas.append((f"votar su propia carta ({dueno.name})",
                               lambda p=dueno, s=sub: inerte(
                                   lambda: m.vote_card(p.sid, rid, s["id"]))))
        return sondas

    # -- comprobaciones -------------------------------------------------------

    def _problemas_de_mundo(self, salas):
        """Invariantes que son de varias salas a la vez.

        El motor garantiza 'un sid, un asiento': al migrar de sala suelta el
        anterior. Si se pierde esa liberación, queda un Player fantasma
        'conectado' en la sala vieja, y no lo detecta ni la conservación de
        cartas ni ninguna comprobación por sala: solo se ve mirando el
        conjunto."""
        problemas = []
        conectados = {}
        for room in salas:
            for sid, p in room.players.items():
                if p.is_connected:
                    if sid in conectados:
                        problemas.append(
                            f"[{room.room_id}] {p.name} (sid={sid}) está conectado "
                            f"aquí y también en {conectados[sid]}: un sid no puede "
                            f"tener dos asientos")
                    conectados[sid] = room.room_id
        return problemas

    def _problemas_de_sala(self, room):
        p = []
        rid = room.room_id

        if room.state not in ESTADOS:
            p.append(f"[{rid}] estado '{room.state}' no existe")

        # Conservación de cartas POR IDENTIDAD
        for problema in room.audit_cards():
            p.append(f"[{rid}] {problema}")

        # Juez y líder tienen que ser alguien de la sala
        for rol, sid in (("czar", room.czar), ("lider", room.leader)):
            if sid is not None and sid not in room.players:
                p.append(f"[{rid}] {rol} {sid} no esta en la sala")

        # Coherencia de la carta negra con el estado
        if room.state == "waiting":
            if room.black_card is not None:
                p.append(f"[{rid}] en 'waiting' pero hay carta negra")
            if room.czar is not None:
                p.append(f"[{rid}] en 'waiting' pero hay juez")
            if room.played_cards:
                p.append(f"[{rid}] en 'waiting' pero quedan {len(room.played_cards)} jugadas")
        elif room.black_card is None:
            p.append(f"[{rid}] en '{room.state}' pero no hay carta negra")

        # Espejo played_card <-> played_cards: quien tiene carta jugada tiene
        # que tener su submission, y al revés.
        con_jugada = {c["sid"] for c in room.played_cards}
        for j in room.players.values():
            tiene = j.played_card is not None
            if tiene != (j.sid in con_jugada):
                p.append(f"[{rid}] {j.name}: played_card={tiene} pero submission={j.sid in con_jugada}")

        # OJO: no se comprueba `len(mano) <= hand_size`. Bajar `hand_size` solo
        # afecta a las manos que se reparten a partir de ese momento: el motor
        # rellena hasta el tamaño pedido pero nunca recorta las que ya había.
        # Es una decisión de diseño, no un invariante.

        # Ids de submission únicos
        ids = [c["id"] for c in room.played_cards]
        if len(set(ids)) != len(ids):
            p.append(f"[{rid}] ids de submission repetidos: {ids}")

        return p

    # -- bucle ----------------------------------------------------------------

    def _prologo(self):
        """Colocación determinista: sentar la mesa, ajustar opciones y arrancar.

        Sin esto el fuzz depende de que la casualidad junte a dos jugadores
        conectados en la misma sala, y hay semillas donde eso no ocurre en 400
        pasos: el fuzz se queda dando vueltas por el lobby sin jugar nunca.
        Arrancando desde una partida real, la aleatoriedad se dedica a explorar
        la máquina de estados, que es lo que interesa.

        El umbral de renovación se deja en 0.1, el mínimo que acepta la UI
        (`_update_options_locked` solo deja 0.1 <= umbral <= 1.0) y que además
        solo se puede cambiar en 'waiting'. Con 0.75 las renovaciones casi nunca
        se dan y el fuzz no llega a esa parte del motor, que es justo donde se
        devuelven las manos al mazo."""
        m = self.m
        for i, sid in enumerate(self.sids):
            m.join_game(sid, NOMBRES[i % len(NOMBRES)], f"S{i % self.n_salas}")
        for rid in (f"S{i}" for i in range(self.n_salas)):
            room = m.rooms.get(rid)
            if room is not None:
                m.update_options(room.leader, rid, {"renew_threshold": 0.1})
                m.start_game(room.leader, rid)

    def ejecutar(self):
        """None si todo cuadra; si no (paso, descripción, problemas)."""
        random.seed(self.seed)  # shuffle / sample / choice del motor
        self._prologo()

        for paso in range(self.pasos):
            ops = self._opciones(colocando=paso < PASOS_DE_COLOCACION)
            if not ops:
                break
            _tipo, desc, aplicar, _rid = self._elegir(ops)
            self.trail.append(desc)
            self.tipos_vistos.add(_tipo)

            try:
                aplicar()
            except Exception as exc:
                return paso, desc, [f"la acción '{desc}' lanzó {type(exc).__name__}: {exc}"]

            problemas = []
            salas = list(self.m.rooms.values())
            for room in salas:
                self.estados_vistos.add(room.state)
                problemas += self._problemas_de_sala(room)
            problemas += self._problemas_de_mundo(salas)
            if problemas:
                return paso, desc, problemas

            # Sondas: lo que no debería poder nada, no puede hacer nada
            salas = list(self.m.rooms.values())
            if salas and paso >= PASOS_DE_COLOCACION:
                room = self.rng.choice(salas)
                self.estados_vistos.add(room.state)
                for nombre_sonda, sonda in self._sondas_ilegales(room):
                    try:
                        intacto = sonda()
                    except Exception as exc:
                        return paso, f"sonda '{nombre_sonda}'", [
                            f"la sonda '{nombre_sonda}' lanzó {type(exc).__name__}: {exc}"]
                    if not intacto:
                        return paso, f"sonda '{nombre_sonda}'", [
                            f"[{room.room_id}] '{nombre_sonda}' cambió el estado, "
                            f"y no debería poder"]
        return None


# --- Los tests ---------------------------------------------------------------

def _semillas():
    override = os.environ.get("CAH_FUZZ_SEED")
    return [int(override)] if override else BASE_SEEDS


def _informe(fuzzer, paso, desc, problemas):
    traza = " -> ".join(fuzzer.trail[max(0, paso - 6):paso + 1])
    return (
        f"Reglas rotas con la semilla {fuzzer.seed} en el paso {paso}\n"
        f"  últimas acciones: ...{traza}\n"
        f"  acción que lo provocó: {desc}\n"
        f"  - " + "\n  - ".join(problemas) + "\n\n"
        f"Reproducir: CAH_FUZZ_SEED={fuzzer.seed} python -m pytest tests/test_fuzz_rules.py -q"
    )


@pytest.mark.parametrize("seed", _semillas())
def test_reglas_conservan_cartas_y_estados(motor, seed):
    fuzzer = Fuzzer(motor, seed)
    fallo = fuzzer.ejecutar()
    if fallo is not None:
        paso, desc, problemas = fallo
        pytest.fail(_informe(fuzzer, paso, desc, problemas), pytrace=False)


@pytest.mark.parametrize("seed", _semillas())
def test_el_fuzz_realmente_cubre_el_juego(motor, seed):
    """Guarda contra un fuzz que se quede en no-op: cada semilla tiene que
    arrancar al menos una partida de verdad.

    Que se cubran los cuatro estados se comprueba después, sobre el conjunto de
    semillas: una caminata aleatoria de 400 pasos no tiene por qué llegar a
    `round_end` desde cualquier semilla, y obligarla a ello sería ajustar la
    gramática hasta que pase justo ese caso."""
    fuzzer = Fuzzer(motor, seed)
    fallo = fuzzer.ejecutar()
    assert fallo is None, _informe(fuzzer, *fallo)

    assert "playing" in fuzzer.estados_vistos, (
        f"con la semilla {seed} el fuzz no llegó ni a arrancar una partida "
        f"(pasó por {sorted(fuzzer.estados_vistos)})"
    )
    assert len(fuzzer.tipos_vistos) >= 8, (
        f"con la semilla {seed} solo ejecutó {sorted(fuzzer.tipos_vistos)}"
    )


def test_entre_todas_las_semillas_se_cubre_la_maquina_de_estados(motor):
    """La cobertura se exige al fuzz completo, no a cada semilla suelta.

    Es la comprobación que evita que el conjunto de tests se quede 'verde' por
    un fuzz que en realidad no juega: entre todas las semillas tienen que pasar
    por los cuatro estados y ejecutar cada tipo de acción.

    Usa SIEMPRE `BASE_SEEDS`, aunque se haya limitado el fuzz a una semilla con
    CAH_FUZZ_SEED: esa variable sirve para reproducir rápido un fallo de
    reglas, no para reducir la cobertura que se exige al conjunto."""
    estados, tipos = set(), set()
    fases = contadores()
    for seed in BASE_SEEDS:
        fuzzer = Fuzzer(motor, seed)
        fallo = fuzzer.ejecutar()
        assert fallo is None, _informe(fuzzer, *fallo)
        estados |= fuzzer.estados_vistos
        tipos |= fuzzer.tipos_vistos
        for ambito in ("jugadores", "salas"):
            for fase, n in motor.presencia[ambito].items():
                fases[ambito][fase] += n

    faltan = ESTADOS - estados
    assert not faltan, f"el fuzz nunca pasó por {sorted(faltan)} (pasó por {sorted(estados)})"

    esperado = {"unirse", "jugar", "revelar", "ganador", "siguiente",
                "desconectar", "reaper", "silencio", "temporizador", "carta_negra",
                "renovar"}
    ausentes = esperado - tipos
    assert not ausentes, f"el fuzz nunca ejecutó: {sorted(ausentes)}"

    # Lo mismo con la máquina de presencia: el op 'silencio' existe para eso, y
    # un fuzz que solo sospeche y nunca purgue (o al revés) no está cubriendo el
    # ciclo de dos fases que es lo que hay que comprobar aquí. Los contadores son
    # acumulativos entre semillas, así que basta con que el conjunto pase por
    # todas las fases.
    for ambito in ("jugadores", "salas"):
        for fase in (SOSPECHOSO, PURGADO):
            assert fases[ambito][fase] > 0, (
                f"el fuzz nunca pasó por '{fase}' de {ambito} "
                f"(recorrió {fases[ambito]})")
