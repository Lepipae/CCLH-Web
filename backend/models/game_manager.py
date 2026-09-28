import random
import threading
import time
import uuid
import json
import os
import logging
import difflib
from models.room import Room
from models.player import Player
from models.outbox import Outbox, transactional
from models.presence import (PresencePolicy, contadores, VIVO, SOSPECHOSO,
                             PURGADO)

# Logger del módulo: WARNING explícito (en vez de print) para deriva de cartas
# y depuración de zombis, integrable con la telemetría del despliegue.
logger = logging.getLogger(__name__)

class GameManager:
    # --- Presencia: dos umbrales con nombre, no un número mágico ----------------
    # Un solo número no puede decir dos cosas a la vez, y aquí había que decir
    # las dos: cuándo hay motivo para sospechar de un cliente, y cuánto se le
    # concede para demostrar que sigue ahí. Con un único umbral
    # (ZOMBIE_TIMEOUT_SECONDS, ya retirado) la purga era inseparable de la
    # sospecha: en la misma pasada en que el reaper se enteraba de que un
    # cliente llevaba 300 s callado, lo echaba. Sin aviso, sin segunda
    # oportunidad y sin nada que el cliente pudiera mostrar.
    #
    # Ahora el silencio es un proceso de dos fases (models/presence.py):
    #
    #   vivo --(silencio >= suspect_after)--> sospechoso --(gracia)--> purgado
    #
    # Al entrar en la fase sospechosa se avisa a la sala (`presence_alert`) y se
    # reta al propio cliente (`presence_challenge`); si contesta, recupera su
    # asiento y la mesa no se entera. Solo tras la gracia se libera el asiento.
    #
    # Los valores por defecto conservan el plazo total del umbral antiguo
    # (120 + 180 = 300 s): no se purga antes que antes, pero ahora con dos
    # minutos de aviso y tres de reversión. Los números vienen de lo que hay
    # que tolerar: el latido lo contesta JS del cliente y los navegadores
    # ralentizan los timers de las pestañas en segundo plano a ~1/min (Chrome) o
    # los suspenden del todo (móvil), así que sospechar antes de los 120 s
    # empezaría a señalar a gente que solo tiene el móvil en la mano; y la
    # gracia de 180 s es holgada a propósito porque es la ventana de vuelta.
    #
    # Configurable por entorno sin tocar código (PRESENCE_SUSPECT_SECONDS,
    # PRESENCE_GRACE_SECONDS, ROOM_SUSPECT_SECONDS, ROOM_GRACE_SECONDS). Un
    # valor inválido o negativo no impide el arranque: se usa el de por defecto.
    PRESENCE = PresencePolicy.from_env('PRESENCE', suspect_after=120, grace=180)

    # Techo de salas vivas por proceso. `join_game` acepta cualquier código de
    # sala que le manden, así que sin un tope cualquiera puede crear miles de
    # salas vacías (cada una con su mazo y su lista de jugadores) y comerse la
    # memoria del proceso. Con el tope, crear la sala N+1 falla con un
    # `join_error` legible en lugar de tumbar el servidor.
    #
    # El reaper es la válvula de escape: las salas vacías se van solas, así que
    # un servidor lleno se vacía solo en cuanto expiran sus salas. 200 es
    # holgado para un juego familiar y acotado para que la pasada del reaper
    # (que recorre TODAS las salas bajo el cerrojo global) siga siendo barata.
    try:
        MAX_ROOMS = int(os.environ.get('MAX_ROOMS', 200))
    except (TypeError, ValueError):
        MAX_ROOMS = 200

    # Las salas pasan por la misma máquina con plazos más cortos. No hay a quién
    # avisar (están vacías, por definición), pero la gracia sirve igual: dentro
    # de ella, quien vuelve recupera la sala con su estado de partida entero en
    # lugar de encontrar un lobby nuevo.
    ROOM_LIFECYCLE = PresencePolicy.from_env('ROOM', suspect_after=30, grace=60)

    # Las salas que NUNCA llegaron a empezar se van mucho antes. No hay nada que
    # conservar: ni ronda, ni manos repartidas, ni puntuación, solo la lista de
    # jugadores que se quedaron a medias y las opciones del creador. Y son
    # justo la vía barata de agotar la memoria (crear y salir), así que no
    # tienen por qué esperar el mismo plazo que una partida de verdad.
    ROOM_LIFECYCLE_NUEVA = PresencePolicy.from_env('ROOM_NUEVA', suspect_after=5,
                                                    grace=15)

    # Intervalo de escaneo del reaper en segundo plano (segundos)
    ROOM_REAP_INTERVAL = 15

    def __init__(self, socketio, white_cards, black_cards):
        self.rooms = {}
        self.socketio = socketio
        self.logger = logger
        self.global_white_cards = white_cards
        self.global_black_cards = black_cards
        # Bajo async_mode='threading' los handlers corren en hilos reales y pueden
        # intercalarse a mitad de una mutación (reparto de cartas, cambio de estado,
        # import al mazo global...). Este cerrojo serializa TODAS las operaciones
        # del juego: granularidad gruesa a propósito, la partida es por turnos y el
        # coste es despreciable frente a la E/S de red. RLock (no Lock) porque hay
        # reentradas legítimas: choose_winner -> send_room_update, etc.
        self._lock = threading.RLock()
        # El cerrojo protege el ESTADO, nunca la E/S: los payloads se serializan
        # con él tomado y se emiten al salir de la transacción (models/outbox.py).
        # Es lo que evita que un cliente lento deje parado todo el servidor.
        self._outbox = Outbox(self.socketio, self._lock)
        # Contadores de fases de presencia. No sustituyen al log: dan el número
        # agregado sin tener que parsear líneas, que es lo que se quiere
        # responder a "¿cuánta gente se nos cae al día?".
        self.presencia = contadores()
        # Salas que se ha negado a crear por MAX_ROOMS. Es un contador de
        # ataque, no de juego: si sube solo, alguien (o un cliente atascado en
        # un bucle) está probando códigos de sala.
        self.salas_rechazadas = 0
        self._start_room_reaper()

    @transactional
    def send_room_update(self, room_id):
        # Se ejecuta dentro del cerrojo del llamador; solo lee estado y
        # serializa una foto de la sala POR JUGADOR (Room.to_dict ya devuelve
        # una copia de la mano, no la lista viva: entre que el payload se
        # construye y que sale por el socket la mano puede haber cambiado).
        # Los payloads se encolan y salen al salir del cerrojo.
        #
        # IMPORTANTE: emitir NO renueva `last_seen`. Escribir en el búfer de
        # salida no demuestra que el cliente siga ahí, y hacerlo convertía el
        # umbral de zombis en un detector de "sala silenciosa" (mataba a todo
        # el mundo a la vez) mientras un solo jugador hablando resucitaba a los
        # muertos. La señal de vida llega por `heartbeat` (pong del cliente).
        room = self.rooms.get(room_id)
        if room is None:
            return
        for sid, p in room.players.items():
            if p.is_connected:
                self._outbox.add_update(room_id, sid, room.to_dict(for_sid=sid))

    @transactional
    def send_chat_system(self, room_id, message):
        self._outbox.add_direct(room_id, 'chat_message', {'msg': message, 'system': True}, to=room_id)

    @transactional
    def join_game(self, sid, name, room_id):
        return self._join_game_locked(sid, name, room_id)

    def _join_game_locked(self, sid, name, room_id):
        if room_id in self.rooms:
            # Check duplicates
            for existing_sid, p in self.rooms[room_id].players.items():
                if p.name == name and p.is_connected and existing_sid != sid:
                    self._outbox.add_direct(room_id, 'join_error',
                                            {'message': 'Ese nombre ya está en uso en esta sala.'}, to=sid)
                    return False

        if room_id not in self.rooms:
            # Techo de salas. Va ANTES de crear nada: una sala rechazada no
            # ocupa memoria ni aparece en el `game_update` de nadie. Solo
            # crear sala nueva cuenta; entrar en una que ya existe nunca se
            # rechaza por esto, porque no crea nada nuevo.
            if len(self.rooms) >= self.MAX_ROOMS:
                self.salas_rechazadas += 1
                logger.warning(
                    "Sala '%s' rechazada: el proceso ya tiene %d salas (MAX_ROOMS)",
                    room_id, len(self.rooms))
                self._outbox.add_direct(room_id, 'join_error', {
                    'message': 'El servidor está lleno de salas ahora mismo. '
                               'Prueba con otro código en un momento.'}, to=sid)
                return False
            self.rooms[room_id] = Room(room_id, sid, self.global_white_cards, self.global_black_cards)

        room = self.rooms[room_id]
        # Asignar None a `empty_since` es "aquí hay alguien": perdona la
        # sospecha de la sala y cancela su periodo de gracia, de modo que quien
        # vuelve antes de que venza recupera la partida tal cual la dejó. Se
        # comprueba el estado ANTES porque el setter no devuelve nada.
        if room.presence.state == SOSPECHOSO:
            self.presencia['salas'][VIVO] += 1
        room.empty_since = None

        # Un sid, un asiento. Si venía jugando en otra sala, se suelta de ella
        # ANTES de tomar el nuevo asiento: si no, quedaba un Player fantasma
        # 'conectado' en la sala anterior y la purga de zombis lo expulsaba
        # también de ésta. Va después del chequeo de nombre duplicado para no
        # tocar la sala de origen si la unión va a ser rechazada.
        self._release_other_rooms(sid, room_id)

        reconnected = False
        for existing_sid, p in list(room.players.items()):
            if p.name == name:
                p.sid = sid
                p.is_connected = True
                room.players[sid] = p
                if existing_sid != sid:
                    del room.players[existing_sid]
                    if room.czar == existing_sid:
                        room.czar = sid
                    if room.leader == existing_sid:
                        room.leader = sid
                    for pc in room.played_cards:
                        if pc['sid'] == existing_sid:
                            pc['sid'] = sid
                    try:
                        idx = room.join_order.index(existing_sid)
                        room.join_order[idx] = sid
                    except ValueError:
                        room.join_order.append(sid)
                reconnected = True
                break
                
        if not reconnected:
            hand_size = room.options['hand_size']
            is_playing = room.state != 'waiting'
            initial_hand = room.deal_cards(hand_size) if is_playing else []
            new_player = Player(sid, name, initial_hand=initial_hand, is_spectator=is_playing)
            room.add_player(new_player)

        self._heal_stale_roles(room)

        print(f"{name} se unió a {room_id}")
        # Un join exitoso es tráfico real del cliente: renueva su last_seen
        # (pero no cuenta como prueba de latido; ver _touch_player)
        self._touch_player(sid)
        self.send_room_update(room_id)
        return True

    @transactional
    def heartbeat(self, sid):
        """Punto de entrada público del latido: el cliente ha contestado al
        'heartbeat_ping' del reaper. Es la prueba de que ese cliente habla el
        protocolo de latidos, que es lo que autoriza al reaper a purgarlo si
        luego deja de contestar. Unir a la sala no cuenta: demostrar que sabes
        contestar es justo lo que hay que comprobar."""
        self._touch_player(sid, proof_of_life=True)

    def _touch_player(self, sid, proof_of_life=False):
        """Renueva el reloj de silencio del jugador `sid` en todas las salas donde
        aparezca, y le perdona la sospecha si estaba en periodo de gracia.
        `proof_of_life=True` además incrementa su contador de latidos
        respondidos. Nunca se llama desde una difusión del servidor: solo desde
        unión o pong, es decir, algo que demuestra que hubo tráfico de verdad.
        Se invoca SIEMPRE bajo self._lock (RLock de la sala/GM), igual que el
        reaper, para que la lectura/escritura del estado no se intercale con
        los handlers de juego.

        El perdón es lo que convierte la sospecha en periodo de gracia real: un
        navegador que vuelve de una pestaña suspendida contesta el reto, sale de
        la fase sospechosa y conserva su asiento. Y no hace falta que sea un
        pong: entrar a la sala con el mismo nombre es también tráfico de verdad.

        Nota: como el reaper hace ping cada ROOM_REAP_INTERVAL s, en la
        práctica el reloj es "último pong" y cualquier cliente vivo lo
        renueva solo; las acciones de juego no necesitan tocarlo."""
        now = time.time()
        for room in self.rooms.values():
            p = room.players.get(sid)
            if p is not None:
                if proof_of_life:
                    p.heartbeat_pongs += 1
                # `signal` renueva el reloj y, si estaba sospechoso, lo devuelve
                # a vivo. Se avisa a la sala de esa vuelta: sin este anuncio el
                # cliente se queda con el cartel de "se ha ido" sobre alguien que
                # está delante de la pantalla.
                if p.presence.signal(now):
                    self._anunciar_presencia(room, sid, VIVO, now,
                                             motivo='ha vuelto a dar señales de vida')

    def room_of(self, sid):
        """Sala en la que `sid` tiene asiento activo, o None. Se deriva de la
        única fuente de verdad (`room.players`) en vez de mantener un índice
        aparte que pueda desincronizarse. La usa app.py para abandonar la sala
        anterior de Socket.IO antes de entrar en la nueva.

        Si quedara un asiento desconectado con el mismo sid (un fantasma), se
        ignora: lo que interesa es dónde está el jugador de verdad."""
        fallback = None
        for room_id, room in self.rooms.items():
            p = room.players.get(sid)
            if p is None:
                continue
            if p.is_connected:
                return room_id
            fallback = room_id
        return fallback

    def _heal_stale_roles(self, room):
        """Autosanado al entrar alguien a la sala: si el líder o el juez
        registrados están desconectados (por ejemplo, un disconnect perdido),
        se reasignan para que la sala no quede inoperante."""
        active = room.get_active_players()
        if not active:
            return
        leader_p = room.players.get(room.leader)
        if leader_p is None or not leader_p.is_connected:
            room.leader = self._pick_new_leader(room, active)
        if room.state != 'waiting' and len([p for p in active if not p.waiting_next_round]) < 2:
            self._revert_to_waiting(room)
            return
        self._activate_spectators_if_abandoned(room, active)
        czar_p = room.players.get(room.czar)
        if room.czar and (czar_p is None or not czar_p.is_connected):
            if room.state == 'round_end':
                self.advance_to_next_round(room.room_id)
            else:
                self._reassign_czar_after_disconnect(room)

    @transactional
    def disconnect(self, sid):
        self._disconnect_locked(sid)

    def _disconnect_locked(self, sid):
        for room_id, room in self.rooms.items():
            if sid in room.players:
                self._release_from_room(room, room_id, sid)

    def _release_other_rooms(self, sid, keep_room_id):
        """Suelta a `sid` de cualquier sala que no sea `keep_room_id`.

        Un sid tiene como mucho un asiento. Sin esto, moverse de una sala a
        otra dejaba un Player fantasma en la anterior que seguía marcado como
        conectado: el reaper lo daba por zombi y, al purgarlo con
        _disconnect_locked (que recorre todas las salas), le cortaba la sesión
        en la sala nueva donde sí estaba jugando. Reutilizar la misma rutina
        que una desconexión real también sanea la sala abandonada: relevo de
        líder/juez, revert a 'waiting' y activación de espectadores."""
        for room_id, room in self.rooms.items():
            if room_id != keep_room_id and sid in room.players:
                self._release_from_room(room, room_id, sid)

    def _release_from_room(self, room, room_id, sid):
        """Marca a `sid` como desconectado de `room` y deja la sala en un estado
        coherente. Es el cuerpo compartido por la desconexión formal
        (`disconnect`) y por el abandono de sala al migrar (`_release_other_rooms`)."""
        room.remove_player(sid)
        active = room.get_active_players()
        if active:
            if room.leader == sid:
                room.leader = self._pick_new_leader(room, active)
            if room.state != 'waiting' and len(active) < 2:
                # Con un solo jugador no puede seguir ninguna partida
                self._revert_to_waiting(room)
            else:
                # Si solo quedan espectadores, entran al juego para que continúe
                self._activate_spectators_if_abandoned(room, active)
                if room.czar == sid:
                    # El juez se marcha: reasignar el turno para no congelar la ronda
                    self._reassign_czar_after_disconnect(room)
                if room.state == 'playing':
                    active_non_czars = [pl for pl in active if pl.sid != room.czar and not pl.waiting_next_round]
                    if active_non_czars and all(pl.played_card is not None for pl in active_non_czars):
                        room.state = 'judging'
                        random.shuffle(room.played_cards)
                if room.state != 'waiting':
                    self.check_and_execute_renew(room)
            self.send_room_update(room_id)
        else:
            # Sala vacía: se marca el momento y el reaper la borrará si nadie vuelve
            room.empty_since = time.time()

    def _pick_new_leader(self, room, active):
        """Nuevo líder al marcharse el actual: prefiere a un jugador conectado que
        no sea espectador (puede iniciar la partida); si solo quedan espectadores,
        el primero por orden de llegada. None si no queda nadie conectado.

        Nota histórica: la regla original era devolver siempre ``active[0].sid``
        (el primero conectado por orden de llegada, aunque fuera espectador y no
        pudiera arrancar la partida); ahora se prioriza a los participantes
        activos (no espectadores) y solo como último recurso se cae a ese
        comportamiento antiguo."""
        candidates = [p for p in active if not p.waiting_next_round]
        if candidates:
            return candidates[0].sid
        return active[0].sid if active else None

    def _reassign_czar_after_disconnect(self, room):
        """El juez se ha desconectado: se pasa el turno a otro jugador conectado
        según el estado de la ronda, para que la partida no se quede congelada."""
        active = room.get_active_players()
        if not active:
            return
        if room.state == 'judging':
            played = [p for p in active if p.played_card is not None and not p.waiting_next_round]
            if played:
                # Quien ya jugó puede revelar y elegir ganador al instante.
                # Caso degenerado asumido: si solo queda su propia carta en juego,
                # podría premiarse a sí mismo; es preferible a congelar la sala.
                room.czar = played[0].sid
                return
            # No queda ningún jugador conectado con carta jugada: se descarta el
            # recuento huérfano y la ronda vuelve a 'playing'
            self._discard_round_cards(room)
            room.state = 'playing'
            for p in room.players.values():
                if not p.is_connected and p.played_card is not None:
                    p.played_card = None  # podrán volver a jugar al reconectar
        if room.state == 'round_end':
            # El recuento ya está cerrado: se avanza la ronda directamente
            # (el auto-avance pendiente quedará obsoleto: comprueba el czar antiguo)
            self.advance_to_next_round(room.room_id)
            return
        self._rotate_czar_from(room, room.czar)

    def _pick_connected_czar(self, room, preferred_sid):
        """El czar preferido si sigue conectado; si no, el primero conectado por
        orden de llegada. None si no queda nadie conectado."""
        if preferred_sid and preferred_sid in room.players and room.players[preferred_sid].is_connected:
            return preferred_sid
        for jsid in room.join_order:
            p = room.players.get(jsid)
            if p is not None and p.is_connected:
                return jsid
        return None

    def _rotate_czar_from(self, room, old_czar_sid):
        """Siguiente juez según la opción turn_order, saltándose a los
        desconectados. Nunca deja de czar a alguien que no esté conectado."""
        active = room.get_active_players()
        if not active:
            return
        turn_order = room.options['turn_order']
        if turn_order == 'random':
            room.czar = random.choice(active).sid
            return
        if turn_order == 'sequential':
            connected_ordered = [s for s in room.join_order if s in room.players and room.players[s].is_connected]
            if connected_ordered:
                if old_czar_sid in connected_ordered:
                    idx = connected_ordered.index(old_czar_sid)
                    room.czar = connected_ordered[(idx + 1) % len(connected_ordered)]
                elif old_czar_sid in room.join_order:
                    # El juez se fue: continuar la rotación desde su antigua posición
                    # en vez de reiniciarla (siguiente conectado tras él)
                    pos = room.join_order.index(old_czar_sid)
                    after = [s for s in room.join_order[pos + 1:] if s in connected_ordered]
                    room.czar = after[0] if after else connected_ordered[0]
                else:
                    room.czar = connected_ordered[0]
                return
        # 'winner' (o secuencial sin candidatos): el ganador anterior si sigue
        # conectado; si no, el primero conectado
        room.czar = self._pick_connected_czar(room, room.last_winner) or active[0].sid

    def _activate_spectators_if_abandoned(self, room, active):
        """Si en una partida en curso solo quedan espectadores conectados, pasan
        a jugadores para que la sala no quede eternamente esperando cartas."""
        if room.state in ('playing', 'judging') and active and all(p.waiting_next_round for p in active):
            for p in active:
                p.waiting_next_round = False
                faltan = room.options['hand_size'] - len(p.hand)
                if faltan > 0:
                    p.hand.extend(room.deal_cards(faltan))

    def _discard_round_cards(self, room):
        """Descarta las cartas jugadas de la ronda: las devuelve al mazo, baraja
        y limpia las submissiones (played_cards). No altera room.state ni los
        played_card de los jugadores: eso lo decide cada llamador según a qué
        estado esté volviendo la sala."""
        for c in room.played_cards:
            room.deck.return_(c.get('cards', []))
        room.deck.shuffle()
        room.played_cards = []

    def _revert_to_waiting(self, room):
        """Devuelve la sala a la pantalla de espera: con menos de dos jugadores
        reales no puede haber partida en curso."""
        room.state = 'waiting'
        self._discard_round_cards(room)
        room.renew_votes.clear()
        room.czar = None
        room.black_card = None
        for p in room.players.values():
            p.played_card = None
            p.waiting_next_round = False

    def _start_room_reaper(self):
        """Lanza la tarea en segundo plano que limpia periódicamente las salas vacías."""
        self.socketio.start_background_task(self._room_reaper_loop)

    def _room_reaper_loop(self):
        while True:
            try:
                self.reap_empty_rooms()
            except Exception as e:
                print("Error en el reaper de salas:", e)
            self.socketio.sleep(self.ROOM_REAP_INTERVAL)

    @transactional
    def reap_empty_rooms(self):
        """Pasada periódica del reaper (ciclo de ~ROOM_REAP_INTERVAL s), bajo el
        RLock global: pide un latido a cada cliente conectado y hace avanzar las
        máquinas de presencia (jugadores y salas) un paso.

        Ninguna purga ocurre en la pasada en la que se detecta el silencio: se
        sospecha primero y la purga espera a la pasada siguiente. Es la
        diferencia entre "el reaper se enteró" y "el reaper se equivocó": lo
        primero puede ser una pestaña en segundo plano, una
        cobertura mala o un móvil que acaba de cambiar de red, y en ninguno de
        esos casos la respuesta correcta es echar a alguien.

        Los latidos salen al terminar la pasada, no durante: son uno por
        jugador conectado y son la E/S más barata de todas (una línea), pero se
        emiten a un socket INDIVIDUAL y con 41 salas y 49 conectados eran 245
        ms de cerrojo global quemados en mensajes de tres campos.

        Aquí ya no se audita la conservación de cartas: esa comprobación vive en
        la suite de tests, que la corre después de cada acción. En producción
        solo produjo un WARNING anónimo que decía 'algo se perdió' sin señalar
        qué ni cuándo, y obligaba a recorrer el estado entero de cada sala cada
        15 s bajo el cerrojo global."""
        now = time.time()

        # --- Latido: la única forma de renovar el reloj de silencio es que el
        # cliente conteste. Se emite a TODOS los conectados (también a los que
        # aún no han contestado nunca, y también a los que están en periodo de
        # gracia: es justo a ellos a quien hay que poder grabarle la prueba de
        # vida) para que puedan demostrar que viven. Es la contrapartida de la
        # purga: no podemos exigirle a nadie que responda a un ping que no le
        # hemos hecho.
        for room in self.rooms.values():
            for sid, p in room.players.items():
                if p.is_connected:
                    self._outbox.add_direct(room.room_id, 'heartbeat_ping',
                                            {'room_id': room.room_id}, to=sid)

        self._reap_jugadores(now)
        self._reap_salas(now)

    # --- Presencia: las dos fases --------------------------------------------

    def _anunciar_presencia(self, room, sid, estado, now, motivo=''):
        """Encola el aviso de un cambio de fase de presencia.

        Dos audiencias y dos eventos, porque son dos cosas que hacer:

        - `presence_alert` a TODOS los conectados de la sala (incluido el
          afectado): es el estado del que se pinta el aviso. Va con la marca de
          tiempo y con el margen que queda, para que el cliente pueda decir
          "lleva 2 min sin responder; su asiento se libera en 54 s" en vez de un
          "se ha ido" sin plazo.
        - `presence_challenge` solo al sospechoso: es un reto explícito ("si
          estás ahí, contesta"), no un latido más. El cliente lo contesta con el
          mismo `heartbeat_pong` que ya conoce.

        Se encolan por la caja de salida como todo lo demás: la E/S sale fuera
        del cerrojo global aunque haya 41 salas y 49 conectados."""
        p = room.players[sid]
        presencia = p.presence
        aviso = {
            'room_id': room.room_id,
            'sid': sid,
            'name': p.name,
            'state': estado,
            'since': presencia.since,
            'reason': motivo,
        }
        # El plazo va solo si la máquina lo tiene: al purgar, el aviso conserva
        # el vencimiento que acaba de cumplirse (por eso se encola ANTES de
        # marcar PURGADO, que lo borra), que es lo que le dice al resto de la
        # mesa cuánto llevaba callado el que se va.
        if presencia.deadline is not None:
            aviso['deadline'] = presencia.deadline
            aviso['grace'] = presencia.grace
            aviso['left'] = presencia.segundos_para_purga(now)

        for otro_sid, otro in room.players.items():
            if otro.is_connected:
                self._outbox.add_direct(room.room_id, 'presence_alert', aviso,
                                        to=otro_sid)

        if estado == SOSPECHOSO:
            reto = {
                'room_id': room.room_id,
                'name': p.name,
                'since': presencia.since,
                'deadline': presencia.deadline,
                'grace': presencia.grace,
                'left': presencia.segundos_para_purga(now),
            }
            self._outbox.add_direct(room.room_id, 'presence_challenge', reto, to=sid)

        self.presencia['jugadores'][estado] = \
            self.presencia['jugadores'].get(estado, 0) + 1
        logger.info("Presencia: %s pasa a '%s' en la sala %s (%s)", p.name, estado,
                    room.room_id, motivo)

    def _reap_jugadores(self, now):
        """Un paso de la máquina de presencia de cada jugador conectado.

        Se separa en dos listas porque las transiciones no tocan el estado de
        la sala y hay que recorrerla entera antes de liberar a nadie: purgar
        muta la sala (reasigna juez, revierte a 'waiting', renueva la partida) y
        no se puede hacer eso en mitad de un bucle.

        Reglas:

        - `heartbeat_pongs > 0` es la condición previa. Solo se juzga a quien ya
          demostró que sabe contestar un latido, así que su silencio posterior
          es prueba y no "el jugador está leyendo sin tocar nada". Un cliente
          que nunca ha contestado (build antiguo en caché, JS bloqueado) no se
          toca jamás: para él no tenemos evidencia, y a esos casos los cubre el
          ping/pong interno de engine.io, que dispara 'disconnect' en <=45 s.
        -        Sospechar y purgar son mutuamente excluyentes en la misma pasada
          (`elif`): aunque la gracia valga 0, nadie se expulsa a quien no se le
          ha avisado antes. En la práctica la purga necesita como mínimo dos
          pasadas, y con ROOM_REAP_INTERVAL = 15 s y una gracia de 180 s son
          unos tres minutos de margen de vuelta. Cada transición produce una
          línea de log (la emite `_anunciar_presencia`, con el motivo dentro) y
          los eventos que ve el cliente.
        - La purga reutiliza `_release_from_room`, la misma rutina que la
          desconexión formal: mismos relevés de líder y juez, mismo revert, misma
          devolución de cartas al mazo. Se libera de ESTA sala y no globalmente
          como un disconnect: el reaper sabe en qué sala lo encontró, y un
          asiento de otra sala no es de su incumbencia.
        """
        sospechosos, a_purgar = [], []
        for room in self.rooms.values():
            for sid, p in room.players.items():
                if not p.is_connected or p.heartbeat_pongs == 0:
                    continue
                if p.presence.accuse(now, self.PRESENCE):
                    sospechosos.append((room, sid, p))
                elif p.presence.purge_due(now):
                    a_purgar.append((room, sid, p))

        for room, sid, p in sospechosos:
            # El motivo lleva el dato que hace falta al leer el log sin más
            # contexto: cuánto lleva callado y cuánto le queda de gracia.
            self._anunciar_presencia(
                room, sid, SOSPECHOSO, now,
                motivo="silencio de {:.0f}s tras {} pongs; si no vuelve, su "
                       "asiento se libera en {:.0f}s".format(
                           now - p.last_seen, p.heartbeat_pongs,
                           p.presence.segundos_para_purga(now)))

        for room, sid, p in a_purgar:
            # El aviso va antes de marcar PURGADO: así conserva el plazo que
            # acaba de cumplirse, que es la información útil para el resto.
            self._anunciar_presencia(
                room, sid, PURGADO, now,
                motivo="el periodo de gracia ha vencido: {:.0f}s sospechoso y "
                       "{} latidos sin contestar".format(
                           now - p.presence.since, p.heartbeat_pongs))
            p.presence.purge(now)
            self._release_from_room(room, room.room_id, sid)

    def _reap_salas(self, now):
        """Un paso de la máquina de presencia de cada sala.

        La sala vive mientras tenga alguien conectado; al quedarse vacía arranca
        su reloj de silencio y pasa por las mismas dos fases que un jugador. No
        hay a quién avisar, así que las transiciones solo quedan en el log y en
        los contadores, pero la gracia sirve igual de necessary: quien vuelve
        dentro de ella recupera la sala con su estado de partida entero en
        lugar de encontrar un lobby vacío, y quien vuelve después encuentra una
        sala nueva sin cartas ni turnos perdidos.

        El plazo depende de si la sala LLEGÓ a tener una partida (`ha_empezado`):
        una que sí la tiene conserva marcador y turnos y se le da el plazo
        largo; una que nunca arrancó solo contiene una lista de sids y las
        opciones del creador, así que se va con el plazo corto. Esa distinción
        es también la que hace inofensivo `join_game` con códigos inventados:
        crear y salir no puede acumular salas indefinidamente.
        """
        purgadas = []
        for room_id, room in list(self.rooms.items()):
            if room.empty_since is None:
                # Alguien ha entrado: el setter de `empty_since` ya le ha
                # indultado y ha devuelto la sala a vivo. Solo queda contarlo.
                if room.presence.state != VIVO:
                    self.presencia['salas'][VIVO] += 1
                continue
            politica = (self.ROOM_LIFECYCLE if room.ha_empezado
                        else self.ROOM_LIFECYCLE_NUEVA)
            if room.presence.accuse(now, politica):
                self.presencia['salas'][SOSPECHOSO] += 1
                logger.info(
                    "Reaper: la sala %s lleva %.0fs vacía; se purga en %.0fs si "
                    "no vuelve nadie%s", room_id, now - room.empty_since,
                    room.presence.segundos_para_purga(now),
                    "" if room.ha_empezado else " (nunca llegó a empezar)")
            elif room.presence.purge_due(now):
                purgadas.append(room_id)

        for room_id in purgadas:
            sala = self.rooms[room_id]
            logger.info("Reaper: sala %s purgada tras %.0fs vacía (gracia "
                        "agotada%s)", room_id, now - sala.empty_since,
                        "" if sala.ha_empezado else ", sin llegar a empezar")
            self.presencia['salas'][PURGADO] += 1
            sala.presence.purge(now)
            self._delete_room(room_id)

    def _delete_room(self, room_id):
        room = self.rooms.pop(room_id, None)
        if not room:
            return
        # El estado de emisión se olvida con la sala: sus canales no se
        # reutilizan y lo que otro hilo tenga en la mano se descarta al drenar.
        self._outbox.close_room(room_id)
        room.players.clear()
        room.played_cards = []
        room.deck.clear()
        room.available_blacks = []
        room.renew_votes.clear()
        room.join_order = []

    @transactional
    def start_game(self, sid, room_id):
        self._start_game_locked(sid, room_id)

    def _start_game_locked(self, sid, room_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if room.state == 'waiting' and room.leader == sid:
                active = room.get_active_players()
                # Guard temprano: sin dos jugadores conectados no se inicia nada.
                # Va antes de cualquier reparto o mutación de estado para no quemar
                # cartas del mazo ni activar has_played en el cliente.
                if len(active) < 2:
                    # Sin jugadores suficientes no se inicia: no se tocan el czar,
                    # la carta negra ni el estado de la sala
                    return

                room.state = 'playing'
                room.ha_empezado = True
                room.renew_votes.clear()

                hand_size = room.options['hand_size']
                for p in active:
                    p.played_card = None
                    p.waiting_next_round = False
                    faltan = hand_size - len(p.hand)
                    if faltan > 0:
                        p.hand.extend(room.deal_cards(faltan))

                turn_order = room.options['turn_order']
                if turn_order == 'sequential':
                    room.czar = self._pick_connected_czar(room, None) or active[0].sid
                else:
                    room.czar = random.choice(active).sid
                    
                if not room.available_blacks:
                    room.available_blacks = list(self.global_black_cards)
                
                drawn = random.choice(room.available_blacks)
                room.available_blacks.remove(drawn)
                room.black_card = drawn
                self.send_room_update(room_id)

    @transactional
    def update_options(self, sid, room_id, data):
        self._update_options_locked(sid, room_id, data)

    def _update_options_locked(self, sid, room_id, data):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if room.state == 'waiting' and room.leader == sid:
                try:
                    hsize = int(data.get('hand_size', room.options['hand_size']))
                    if 1 <= hsize <= 20:
                        room.options['hand_size'] = hsize
                except ValueError:
                    pass
                
                try:
                    thresh = float(data.get('renew_threshold', room.options['renew_threshold']))
                    if 0.1 <= thresh <= 1.0:
                        room.options['renew_threshold'] = thresh
                except ValueError:
                    pass
                    
                t_order = data.get('turn_order')
                if t_order in ['winner', 'random', 'sequential']:
                    room.options['turn_order'] = t_order
                    
                self.send_room_update(room_id)

    @transactional
    def play_card(self, sid, room_id, card_index):
        self._play_card_locked(sid, room_id, card_index)

    def _play_card_locked(self, sid, room_id, card_index):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if room.state == 'playing' and sid != room.czar and sid in room.players:
                p = room.players[sid]
                if p.waiting_next_round or p.played_card is not None:
                    return
                
                indices = card_index if isinstance(card_index, list) else [card_index]
                valid_indices = [i for i in indices if 0 <= i < len(p.hand)]
                if valid_indices:
                    # Preservar el orden original en el que el jugador seleccionó las cartas
                    played_texts = [p.hand[i] for i in valid_indices]
                    
                    # Eliminar las cartas de la mano (de mayor a menor índice para no alterar los demás)
                    valid_indices.sort(reverse=True)
                    for i in valid_indices:
                        p.hand.pop(i)
                        
                    p.played_card = played_texts
                    
                    sub_id = str(uuid.uuid4())[:8]
                    room.played_cards.append({'id': sub_id, 'sid': sid, 'cards': played_texts, 'revealed': False})
                    
                    active_non_czars = [pl for pl in room.get_active_players() if pl.sid != room.czar and not pl.waiting_next_round]
                    if all(pl.played_card is not None for pl in active_non_czars):
                        room.state = 'judging'
                        random.shuffle(room.played_cards)
                        
                    self.send_room_update(room_id)

    @transactional
    def reveal_card(self, sid, room_id, sub_id):
        self._reveal_card_locked(sid, room_id, sub_id)

    def _reveal_card_locked(self, sid, room_id, sub_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if room.state == 'judging' and sid == room.czar:
                for c in room.played_cards:
                    if c.get('id') == sub_id:
                        c['revealed'] = True
                        break
                self.send_room_update(room_id)

    @transactional
    def choose_winner(self, sid, room_id, sub_id):
        self._choose_winner_locked(sid, room_id, sub_id)

    def _choose_winner_locked(self, sid, room_id, sub_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if room.state == 'judging' and sid == room.czar:
                winner_sid = None
                for c in room.played_cards:
                    if c.get('id') == sub_id:
                        winner_sid = c['sid']
                        break
                        
                if winner_sid and winner_sid in room.players:
                    # No premiar a quien se ha desconectado durante la ronda:
                    # last_winner apuntaría a un sid muerto y congelaría la rotación
                    if not room.players[winner_sid].is_connected:
                        return
                    room.players[winner_sid].points += 1
                    room.state = 'round_end'
                    room.last_winner = winner_sid
                    room.last_winning_card = [c for c in room.played_cards if c.get('id') == sub_id][0]['cards']
                    room.last_winner_name = room.players[winner_sid].name
                    self.send_room_update(room_id)
                    
                    self.socketio.start_background_task(self.auto_next_round, room_id, sid)

    def auto_next_round(self, room_id, old_czar_sid):
        # La espera va FUERA de la transacción a propósito: dentro del RLock
        # global serían 5 s de servidor parado (la partida entera, todas las
        # salas) por un auto-avance de ronda.
        self.socketio.sleep(5)
        with self._outbox.transaction():
            room = self.rooms.get(room_id)
            if room and room.state == 'round_end' and room.czar == old_czar_sid:
                self.advance_to_next_round(room_id)

    @transactional
    def force_next_round(self, sid, room_id):
        self._force_next_round_locked(sid, room_id)

    def _force_next_round_locked(self, sid, room_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if room.state == 'round_end' and room.czar == sid:
                self.advance_to_next_round(room_id)

    @transactional
    def advance_to_next_round(self, room_id):
        # Público porque lo invocan auto_next_round, _force_next_round_locked,
        # _heal_stale_roles y los tests: anidado en una transacción en curso no
        # encola nada propio, se sale con la de fuera.
        if room_id in self.rooms:
            room = self.rooms[room_id]
            
            turn_order = room.options['turn_order']
            active_players = room.get_active_players()
            
            if turn_order == 'random':
                if active_players:
                    room.czar = random.choice(active_players).sid
            elif turn_order == 'sequential':
                self._rotate_czar_from(room, room.czar)
            else: # winner
                # El ganador anterior pudo desconectarse durante el recuento
                lw = room.players.get(room.last_winner)
                if lw is not None and lw.is_connected:
                    room.czar = room.last_winner
                elif room.czar and room.czar in room.players and room.players[room.czar].is_connected:
                    pass  # el juez actual sigue conectado: sigue juzgando
                else:
                    self._rotate_czar_from(room, room.czar)
                
            room.state = 'playing'
            # Las cartas de la ronda que acaba de terminar vuelven al mazo. Antes
            # se haces `played_cards = []` a secas y esas cartas desaparecian del
            # sistema para siempre: dos cartas perdidas en CADA ronda, sin que
            # nada las reclamara. El audit de conservación de los tests lo
            # delata al primer round_end; el WARNING del reaper lo llevaba
            # señalando desde hacia tiempo sin que nadie lo mirara.
            self._discard_round_cards(room)
            room.renew_votes.clear()
            
            if not room.available_blacks:
                room.available_blacks = list(self.global_black_cards)
            
            drawn = random.choice(room.available_blacks)
            room.available_blacks.remove(drawn)
            room.black_card = drawn
            
            hand_size = room.options['hand_size']
            for p in room.players.values():
                p.played_card = None
                p.waiting_next_round = False
                faltan = hand_size - len(p.hand)
                if faltan > 0:
                    p.hand.extend(room.deal_cards(faltan))
                    
            self.send_room_update(room_id)

    @transactional
    def vote_card(self, sid, room_id, card_id):
        self._vote_card_locked(sid, room_id, card_id)

    def _vote_card_locked(self, sid, room_id, card_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if room.state in ['judging', 'round_end'] and sid != room.czar and sid in room.players:
                target_sub = None
                for c in room.played_cards:
                    if c.get('id') == card_id:
                        target_sub = c
                        break
                        
                if target_sub:
                    # Un jugador no puede votar por su propia carta
                    if target_sub.get('sid') == sid:
                        return
                        
                    if 'votes' not in target_sub or not isinstance(target_sub['votes'], set):
                        target_sub['votes'] = set()
                        
                    has_voted_this = sid in target_sub['votes']
                    
                    # Quitar voto previo de cualquier otra carta en esta sala
                    for c in room.played_cards:
                        if 'votes' in c and isinstance(c['votes'], set):
                            c['votes'].discard(sid)
                            
                    # Alternar voto: si no estaba votado, lo añade
                    if not has_voted_this:
                        target_sub['votes'].add(sid)
                        
                self.send_room_update(room_id)

    @transactional
    def vote_renew(self, sid, room_id):
        self._vote_renew_locked(sid, room_id)

    def _vote_renew_locked(self, sid, room_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if sid in room.players and room.players[sid].is_connected:
                if sid in room.renew_votes:
                    room.renew_votes.remove(sid)
                else:
                    room.renew_votes.add(sid)
                
                self.check_and_execute_renew(room)
                self.send_room_update(room_id)

    @transactional
    def check_and_execute_renew(self, room):
        active = room.get_active_players()
        eligible = [p for p in active if p.sid != room.czar and not p.waiting_next_round]
        voters = eligible if eligible else active
        if voters:
            thresh = room.options['renew_threshold']
            if (len(room.renew_votes) / len(voters)) >= thresh:
                room.renew_votes.clear()
                # Se recogen las cartas de TODOS los jugadores, no solo de los
                # activos: antes solo se vaciaban las manos de los conectados y
                # las jugadas de los desconectados se perdían al hacer
                # `played_cards = []` (una carta destruida por cada uno que se
                # había marchado jugando). El audit de los tests lo cazaba.
                for p in room.players.values():
                    if p.hand:
                        room.deck.return_(p.hand)
                    if p.played_card:
                        room.deck.return_(p.played_card if isinstance(p.played_card, list)
                                          else [p.played_card])
                    p.hand = []
                    p.played_card = None

                room.deck.shuffle()
                hand_size = room.options['hand_size']
                for p in active:
                    p.hand = room.deck.deal(hand_size)

                # Las submissions ya han vuelto al mazo una carta cada una: sus
                # textos son los mismos objetos que los de `p.played_card`.
                room.played_cards = []
                if room.state == 'judging':
                    room.state = 'playing'
                self.send_chat_system(room.room_id, '¡Se han renovado las cartas de todos los jugadores!')

    @transactional
    def change_black_card(self, sid, room_id):
        self._change_black_card_locked(sid, room_id)

    def _change_black_card_locked(self, sid, room_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if room.state == 'playing' and room.czar == sid:
                old_black = room.black_card
                if not room.available_blacks:
                    room.available_blacks = list(self.global_black_cards)
                    
                drawn = random.choice(room.available_blacks)
                room.available_blacks.remove(drawn)
                room.black_card = drawn
                room.available_blacks.append(old_black)
                
                for p in room.players.values():
                    if p.played_card is not None:
                        if isinstance(p.played_card, list):
                            p.hand.extend(p.played_card)
                        else:
                            p.hand.append(p.played_card)
                        p.played_card = None
                room.played_cards = []
                self.send_chat_system(room_id, 'El juez ha cambiado la carta negra. ¡Tenéis que volver a jugar!')
                self.send_room_update(room_id)

    def _get_custom_file_path(self):
        import os
        # Sobrescribible por entorno: los tests la aíslan para no tocar datos reales
        env_path = os.environ.get("INTERNAL_CUSTOM_PATH")
        if env_path:
            return os.path.abspath(env_path)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.abspath(os.path.join(base_dir, "..", "DataScraping", "cartasCustom.json"))

    def _get_external_custom_dir(self):
        import os
        env_dir = os.environ.get("EXTERNAL_CARDS_DIR")
        if env_dir:
            return os.path.abspath(env_dir)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.abspath(os.path.join(base_dir, "..", "..", "custom_cards"))

    def _save_custom_data(self, custom_data):
        import os
        internal_path = self._get_custom_file_path()
        os.makedirs(os.path.dirname(internal_path), exist_ok=True)
        with open(internal_path, 'w', encoding='utf-8') as f:
            json.dump(custom_data, f, ensure_ascii=False, indent=2)

        try:
            external_dir = self._get_external_custom_dir()
            os.makedirs(external_dir, exist_ok=True)
            external_path = os.path.join(external_dir, "cartasCustom.json")
            with open(external_path, 'w', encoding='utf-8') as f:
                json.dump(custom_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("Error exportando cartas a carpeta externa:", e)

    @transactional
    def add_custom_card(self, card_type, text, pick, respond_cb):
        # Transacción completa: muta el mazo global, el archivo y las salas.
        # El callback se difiere: escribirle al cliente que pregunta es E/S, y
        # aquí el cerrojo es el global de la partida entera.
        respond_cb = self._outbox.deferred(respond_cb)
        self._add_custom_card_locked(card_type, text, pick, respond_cb)

    def _add_custom_card_locked(self, card_type, text, pick, respond_cb):
        import os
        text = text.strip()
        if not text:
            respond_cb({'success': False, 'message': 'El texto está vacío.'})
            return
            
        def check_sim(new_txt, lst, thresh=0.85):
            n_l = new_txt.lower()
            for c in lst:
                tc = c if isinstance(c, str) else c.get('text', '')
                if difflib.SequenceMatcher(None, n_l, tc.lower().strip()).ratio() >= thresh:
                    return True, tc
            return False, None
            
        custom_path = self._get_custom_file_path()
        external_path = os.path.join(self._get_external_custom_dir(), "cartasCustom.json")
        custom_data = {"whiteCards": [], "blackCards": []}
        
        target_path = custom_path if os.path.exists(custom_path) else (external_path if os.path.exists(external_path) else None)
        if target_path:
            try:
                with open(target_path, 'r', encoding='utf-8') as f:
                    custom_data = json.load(f)
            except Exception as e:
                print("Error leyendo json custom:", e)

        if card_type == 'white':
            is_sim, match = check_sim(text, self.global_white_cards)
            if is_sim:
                respond_cb({'success': False, 'message': f'Muy similar a una carta existente: "{match}"'})
                return
            self.global_white_cards.append(text)
            if 'whiteCards' not in custom_data:
                custom_data['whiteCards'] = []
            if text not in custom_data['whiteCards']:
                custom_data['whiteCards'].append(text)
            for room in self.rooms.values():
                room.deck.inject([text])  # el mazo crece solo, sin contador paralelo
        elif card_type == 'black':
            is_sim, match = check_sim(text, self.global_black_cards)
            if is_sim:
                respond_cb({'success': False, 'message': f'Muy similar a una carta existente: "{match}"'})
                return
            new_c = {'text': text, 'pick': pick}
            self.global_black_cards.append(new_c)
            if 'blackCards' not in custom_data:
                custom_data['blackCards'] = []
            custom_data['blackCards'].append(new_c)
            for room in self.rooms.values():
                room.available_blacks.append(new_c)
                random.shuffle(room.available_blacks)
        else:
            respond_cb({'success': False, 'message': 'Tipo de carta inválido.'})
            return
            
        try:
            self._save_custom_data(custom_data)
        except Exception as e:
            respond_cb({'success': False, 'message': f'Error guardando cartas custom: {str(e)}'})
            return
            
        respond_cb({'success': True, 'whiteCards': custom_data.get('whiteCards', []), 'blackCards': custom_data.get('blackCards', [])})

    @transactional
    def get_custom_cards(self, respond_cb):
        respond_cb = self._outbox.deferred(respond_cb)
        import os
        custom_path = self._get_custom_file_path()
        external_path = os.path.join(self._get_external_custom_dir(), "cartasCustom.json")
        target_path = custom_path if os.path.exists(custom_path) else (external_path if os.path.exists(external_path) else None)
        
        if target_path:
            try:
                with open(target_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    respond_cb({'success': True, 'whiteCards': data.get('whiteCards', []), 'blackCards': data.get('blackCards', [])})
                    return
            except Exception as e:
                respond_cb({'success': False, 'message': str(e)})
                return
        respond_cb({'success': True, 'whiteCards': [], 'blackCards': []})

    @transactional
    def delete_custom_card(self, card_type, text, respond_cb):
        respond_cb = self._outbox.deferred(respond_cb)
        self._delete_custom_card_locked(card_type, text, respond_cb)

    def _delete_custom_card_locked(self, card_type, text, respond_cb):
        import os
        custom_path = self._get_custom_file_path()
        external_path = os.path.join(self._get_external_custom_dir(), "cartasCustom.json")
        target_path = custom_path if os.path.exists(custom_path) else (external_path if os.path.exists(external_path) else None)
        
        if not target_path:
            respond_cb({'success': False, 'message': 'Archivo no encontrado.'})
            return
        try:
            with open(target_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            if card_type == 'white':
                w_list = data.get('whiteCards', [])
                data['whiteCards'] = [c for c in w_list if (c if isinstance(c, str) else c.get('text')) != text]
                if text in self.global_white_cards:
                    self.global_white_cards.remove(text)
            elif card_type == 'black':
                b_list = data.get('blackCards', [])
                data['blackCards'] = [c for c in b_list if (c if isinstance(c, str) else c.get('text')) != text]
                self.global_black_cards = [c for c in self.global_black_cards if (c if isinstance(c, str) else c.get('text')) != text]
            
            self._save_custom_data(data)
            respond_cb({'success': True, 'whiteCards': data.get('whiteCards', []), 'blackCards': data.get('blackCards', [])})
        except Exception as e:
            respond_cb({'success': False, 'message': str(e)})

    @staticmethod
    def _card_texts(cards):
        """Textos normalizados de una lista de cartas (acepta str o {text, pick})."""
        out = set()
        for c in cards:
            t = c if isinstance(c, str) else (c.get('text') if isinstance(c, dict) else None)
            if isinstance(t, str) and t.strip():
                out.add(t.strip())
        return out

    @transactional
    def import_custom_cards(self, raw_json, respond_cb):
        """
        Importa un set completo de cartas desde el contenido de un archivo .json
        con el mismo formato que los sets base (p. ej. CAH-es-set.json):

            { "whiteCards": ["texto", ...],
              "blackCards": [ { "text": "...", "pick": 2 }, ... ] }

        Valida carta a carta: las inválidas (tipo/longitud/pick/huecos) y las
        duplicadas (dentro del archivo, en cartasCustom.json o en el mazo base)
        se descartan individualmente; el resto se guarda y se inyecta en el
        mazo global y en las salas activas, igual que add_custom_card.
        """
        respond_cb = self._outbox.deferred(respond_cb)
        self._import_custom_cards_locked(raw_json, respond_cb)

    def _import_custom_cards_locked(self, raw_json, respond_cb):
        import os

        MAX_TEXT_LEN = 200   # longitud máxima por carta
        MAX_PICK = 3         # máximo de cartas a jugar por ronda (coincide con el taller)
        MAX_DETAILS = 10     # ejemplos de rechazo/omisión devueltos al cliente

        # 1) Parsear el JSON crudo y exigir la estructura del formato base
        if isinstance(raw_json, (dict, list)):
            data = raw_json  # el cliente también puede enviar el objeto ya parseado
        else:
            try:
                data = json.loads(raw_json)
            except (TypeError, ValueError) as e:
                respond_cb({'success': False, 'message': f'JSON inválido: {e}'})
                return

        if not isinstance(data, dict) or ('whiteCards' not in data and 'blackCards' not in data):
            respond_cb({'success': False,
                        'message': 'Estructura no reconocida: se esperaba un objeto con "whiteCards" y/o "blackCards".'})
            return

        def clean_text(value):
            """Texto listo para guardar, o None si no es válido como carta."""
            if not isinstance(value, str):
                return None
            text = value.strip()
            if not text or len(text) > MAX_TEXT_LEN:
                return None
            return text

        rejected, ignored = [], []
        n_rejected = n_ignored = 0

        def reject(reason):
            nonlocal n_rejected
            n_rejected += 1
            if len(rejected) < MAX_DETAILS:
                rejected.append(reason)

        def ignore(reason):
            nonlocal n_ignored
            n_ignored += 1
            if len(ignored) < MAX_DETAILS:
                ignored.append(reason)

        # Cartas que ya están en juego (base + customs cargadas al arrancar)
        existing_whites = self._card_texts(self.global_white_cards)
        existing_blacks = self._card_texts(self.global_black_cards)

        # 2) Blancas: cadenas de texto no vacías y no duplicadas
        new_whites = []
        raw_whites = data.get('whiteCards', [])
        if not isinstance(raw_whites, list):
            reject('whiteCards: no es una lista')
            raw_whites = []
        for i, item in enumerate(raw_whites):
            text = clean_text(item)
            if text is None:
                if not isinstance(item, str):
                    reject(f'Blanca #{i + 1}: debe ser texto, no {type(item).__name__}')
                elif len(item.strip()) > MAX_TEXT_LEN:
                    reject(f'Blanca #{i + 1}: demasiado larga (máx. {MAX_TEXT_LEN} caracteres)')
                else:
                    reject(f'Blanca #{i + 1}: vacía')
                continue
            if text in new_whites:
                ignore(f'Blanca duplicada en el archivo: "{text[:40]}"')
                continue
            if text in existing_whites:
                ignore(f'Ya existe en el mazo: "{text[:40]}"')
                continue
            new_whites.append(text)

        # 3) Negras: {text, pick}; se tolera texto plano como pick=1
        new_blacks = []
        new_black_texts = set()
        raw_blacks = data.get('blackCards', [])
        if not isinstance(raw_blacks, list):
            reject('blackCards: no es una lista')
            raw_blacks = []
        for i, item in enumerate(raw_blacks):
            if isinstance(item, dict):
                text = clean_text(item.get('text'))
                raw_pick = item.get('pick', 1)
            elif isinstance(item, str):
                text = clean_text(item)
                raw_pick = 1
            else:
                text, raw_pick = None, 1

            if text is None:
                reject(f'Negra #{i + 1}: falta o es inválido el texto')
                continue

            try:
                pick = int(raw_pick)  # se tolera "2" o 2.0
            except (TypeError, ValueError):
                reject(f'Negra #{i + 1}: "pick" no es un número ({raw_pick!r})')
                continue
            if not 1 <= pick <= MAX_PICK:
                reject(f'Negra #{i + 1}: "pick" fuera de rango (1-{MAX_PICK}): {pick}')
                continue
            # Una negra pick>=2 necesita al menos `pick` huecos (_) para ser jugable
            if pick >= 2 and text.count('_') < pick:
                reject(f'Negra #{i + 1}: necesita al menos {pick} guion(es) bajo(s) "_" para pick={pick}')
                continue
            if text in new_black_texts:
                ignore(f'Negra duplicada en el archivo: "{text[:40]}"')
                continue
            if text in existing_blacks:
                ignore(f'Ya existe en el mazo: "{text[:40]}"')
                continue

            new_black_texts.add(text)
            new_blacks.append({'text': text, 'pick': pick})

        if not new_whites and not new_blacks:
            respond_cb({'success': False,
                        'message': f'Ninguna carta válida para importar ({n_rejected} inválidas, {n_ignored} duplicadas).',
                        'rejected': rejected,
                        'ignored': ignored})
            return

        # 4) Fusionar con cartasCustom.json y guardar (ruta interna + externa)
        custom_path = self._get_custom_file_path()
        external_path = os.path.join(self._get_external_custom_dir(), "cartasCustom.json")
        target_path = custom_path if os.path.exists(custom_path) else (external_path if os.path.exists(external_path) else None)
        custom_data = {"whiteCards": [], "blackCards": []}
        if target_path:
            try:
                with open(target_path, 'r', encoding='utf-8') as f:
                    custom_data = json.load(f)
            except Exception as e:
                print("Error leyendo json custom:", e)
        if not isinstance(custom_data, dict):
            custom_data = {"whiteCards": [], "blackCards": []}

        file_whites = self._card_texts(custom_data.get('whiteCards', []))
        file_blacks = self._card_texts(custom_data.get('blackCards', []))
        for w in new_whites:
            if w not in file_whites:
                custom_data.setdefault('whiteCards', []).append(w)
        for b in new_blacks:
            if b['text'] not in file_blacks:
                custom_data.setdefault('blackCards', []).append(b)

        try:
            self._save_custom_data(custom_data)
        except Exception as e:
            respond_cb({'success': False, 'message': f'Error guardando cartas custom: {e}'})
            return

        # 5) Inyectar en el mazo global y en las salas activas (en directo)
        self.global_white_cards.extend(new_whites)
        self.global_black_cards.extend(new_blacks)
        for room in self.rooms.values():
            if new_whites:
                room.deck.inject(new_whites)
            if new_blacks:
                room.available_blacks.extend(new_blacks)
                random.shuffle(room.available_blacks)

        respond_cb({'success': True,
                    'imported': {'white': len(new_whites), 'black': len(new_blacks)},
                    'rejected_count': n_rejected,
                    'ignored_count': n_ignored,
                    'rejected': rejected,
                    'ignored': ignored,
                    'whiteCards': custom_data.get('whiteCards', []),
                    'blackCards': custom_data.get('blackCards', [])})

    @transactional
    def add_room_cards(self, room_id, cards_str):
        room = self.rooms.get(room_id)
        if room:
            new_cards = [c.strip() for c in cards_str.split(',') if c.strip()]
            if new_cards:
                room.deck.inject(new_cards)
                self.send_chat_system(room_id, f'Se han añadido {len(new_cards)} cartas personalizadas a la sala.')

    @transactional
    def send_chat(self, sid, room_id, msg):
        room = self.rooms.get(room_id)
        if room and msg and sid in room.players:
            pname = room.players[sid].name
            self._outbox.add_direct(room_id, 'chat_message',
                                    {'msg': msg, 'sender': pname, 'system': False}, to=room_id)
