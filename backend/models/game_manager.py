import random
import threading
import time
import uuid
import os
import logging
from contextlib import contextmanager, ExitStack
from models.room import Room
from models.player import Player
from models.outbox import Outbox
from models.presence import (PresencePolicy, contadores, VIVO, SOSPECHOSO,
                             PURGADO)
from models.custom_cards import CustomCardsMixin

# Logger del módulo: WARNING explícito (en vez de print) para deriva de cartas
# y depuración de zombis, integrable con la telemetría del despliegue.
logger = logging.getLogger(__name__)

# Tope del mensaje de chat. El `maxlength` del input del cliente es una
# comodidad, no una defensa: lo que llega por el socket se difunde a la mesa.
MAX_CHAT = 500

# Las cartas propias (alta, baja, import, export) viven en models/custom_cards.py.
# Se mezclan aquí y no se delegan porque mutan el mazo global y los mazos de las
# salas vivas: el GameManager es su dueño y su estado no se puede partir en dos.
class GameManager(CustomCardsMixin):
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

    # La misma máquina para quien NUNCA ha contestado un latido. Antes esto no
    # era una política sino un salto eterno: `heartbeat_pongs == 0` sacaba al
    # jugador del juicio para siempre, con la idea de que si no sabe contestar
    # es que no está ahí. El problema es que "no saber contestar" y "no estar"
    # no son lo mismo, y quien no contesta nunca es justo el caso que hay que
    # juzgar: un cliente con el JS de aplicación muerto (adblocker, hidratación
    # fallida, bundle viejo en caché) mantiene el TRANSPORTE vivo, así que el
    # `disconnect` de engine.io no llega nunca. Medido contra un servidor real:
    # 25 clientes así llenaron MAX_ROOMS=20 y, tras 100 s y siete pasadas del
    # reaper, las 20 seguían vivas y sin un solo aviso de presencia. El techo
    # de salas era una irreversible sin salida.
    #
    # Los números son más largos que los de PRESENCE, no más cortos: cuanto
    # menos evidencia hay, más margen se concede. Sospecha a los 240 s (16
    # pasadas) y concede 300 s de gracia, de modo que quien vuelva dentro del
    # plazo recupera el asiento igual que un jugador normal, y el aviso incluye
    # el mismo `presence_challenge` de siempre. Lo que cambia es que el plazo
    # existe: el peor caso de una sala fijada pasa de infinito a 9 minutos.
    PRESENCE_SIN_PRUEBA = PresencePolicy.from_env('PRESENCE_SIN_PRUEBA',
                                                  suspect_after=240, grace=300)

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
        # Hay DOS cerrojos, y la diferencia entre ellos es el sentido del cambio:
        #
        # 1. `Room.lock` (en cada sala, models/room.py): el ESTADO DE JUEGO de una
        #    mesa. Antes esto era un unico RLock global para todo el proceso, con
        #    lo que una mesa con un cliente lento paraba a las demás y una pasada
        #    del reaper con 41 salas retenía el cerrojo 245 ms. Ahora dos mesas
        #    juegan en paralelo y el reaper no puede congelar la partida.
        #
        # 2. `self._registro_lock` (este): lo que NO es de una sala. El dict
        #    `self.rooms` y las listas de cartas del mazo global. Se toma un
        #    instante y se suelta: nunca alrededor de lógica de juego ni de E/S.
        #
        # 3. `self._contadores_lock` (este): un cerrojo HOJA para los contadores de
        #    fases. Hoja significa que no espera a ningún otro cerrojo, así que
        #    no participa en el orden de abajo y por eso puede tomarlo quien ya
        #    tenga una sala (que es el caso normal: el reaper cuenta una
        #    transición por mesa). Hace falta porque `d[k] += 1` no es atómico:
        #    sin él, dos transiciones simultáneas se comerían una cuenta.
        #
        # EL ORDEN ENTRE ELLOS ES FIJO: registro ANTES que sala, nunca al revés.
        # Es la condición para que no haya deadlock (un hilo con la sala A
        # esperando el registro, y otro con el registro esperando la sala A).
        # No es una convención: `_registro()` lo verifica y lanza RuntimeError si
        # alguien lo incumple, porque un deadlock aquí es el servidor parado.
        self._registro_lock = threading.RLock()
        # Profundidad de cerrojos de sala que tiene el hilo AHORA MISMO. Vive en
        # un thread-local porque la respuesta es por hilo: el hilo B puede estar
        # con la sala A tomada sin que eso moleste al hilo A.
        self._prof_salas = threading.local()
        # El cerrojo protege el ESTADO, nunca la E/S: los payloads se serializan
        # con el cerrojo tomado y se emiten al salir (models/outbox.py). El outbox
        # ya no es dueño de ningún cerrojo de juego: solo encola y drena.
        self._outbox = Outbox(self.socketio)
        # Contadores de fases de presencia. No sustituyen al log: dan el número
        # agregado sin tener que parsear líneas, que es lo que se quiere
        # responder a "¿cuánta gente se nos cae al día?".
        self.presencia = contadores()
        # Salas que se ha negado a crear por MAX_ROOMS. Es un contador de
        # ataque, no de juego: si sube solo, alguien (o un cliente atascado en
        # un bucle) está probando códigos de sala.
        self.salas_rechazadas = 0
        # Cerrojo hoja de los contadores. Ver el punto 3 del comentario de arriba.
        self._contadores_lock = threading.Lock()
        self._start_room_reaper()

    # --- Cerrojos y transacciones -------------------------------------------

    def _profundidad_de_salas(self):
        """Cuántos cerrojos de sala tiene este hilo ahora mismo."""
        return getattr(self._prof_salas, "n", 0)

    @contextmanager
    def _registro(self):
        """Cerrojo del REGISTRO: el dict de salas, los contadores globales y las
        listas de cartas del mazo global.

        Prohibido tomarlo con un cerrojo de sala en la mano. Sería el deadlock
        clásico: el hilo A tiene la sala y espera el registro, el hilo B tiene el
        registro y espera la sala. Comprobado y no confiado: aquí un deadlock
        sería el servidor entero parado, sin test que lo detecte a tiempo, así
        que se falla ruidosamente en el sitio donde se introduce el error.
        """
        if self._profundidad_de_salas():
            raise RuntimeError(
                "se ha intentado tomar el cerrojo de registro con un cerrojo de "
                f"sala tomado (profundidad {self._profundidad_de_salas()}): el "
                "orden de cerrojos es registro -> sala, nunca al revés, o se "
                "produce un deadlock")
        with self._registro_lock:
            yield

    @contextmanager
    def _sala(self, room):
        """Cerrojo de UNA sala, y solo ella. Reentrante: las transacciones se
        anidan con naturalidad (choose_winner -> send_room_update -> ...)."""
        self._prof_salas.n = self._profundidad_de_salas() + 1
        try:
            with room.lock:
                yield
        finally:
            self._prof_salas.n -= 1

    @contextmanager
    def _tx_sala(self, room_id, room=None):
        """Transacción de una sala: toma SU cerrojo, ejecuta el cuerpo y drena
        el lote de salida FUERA del cerrojo.

        `room` se pasa cuando el llamante ya lo tiene resuelto (así se evita
        buscarlo dos veces); si no, se busca aquí. Si la sala no existe se
        entrega None y el cuerpo decide qué hacer: casi siempre, no hacer nada.
        """
        if room is None:
            with self._registro():
                room = self.rooms.get(room_id)
        if room is None:
            yield None
            return
        with self._sala(room):
            with self._outbox.lote() as lote:
                yield room
        self._outbox.drenar(lote)

    @contextmanager
    def _tx_registro(self):
        """Transacción que solo toca el registro (y las listas globales de
        cartas). La usan las operaciones que no son de ninguna mesa: dar de alta
        una carta propia, importar un set entero, avisar de que el servidor está
        lleno. Lo que hacen con las salas vivas es tomarlas de una en una desde
        aquí, con el orden bueno (registro -> sala)."""
        with self._registro():
            with self._outbox.lote() as lote:
                yield lote
        self._outbox.drenar(lote)

    @contextmanager
    def _tx_salas(self, *salas):
        """Transacción sobre VARIAS salas a la vez, en un orden TOTAL y fijo:
        por `room_id`. Es lo que hace falta para las operaciones que cruzan mesas
        (unirse soltando la sala de origen, una desconexión que recorre todas).

        El orden no es cosmético: si dos hilos cogieran las salas en orden
        distinto (A->B y B->A) se quedarían esperando el uno al otro para
        siempre. Ordenando por `room_id`, el orden de adquisición es el mismo en
        todos los hilos y el deadlock es imposible por construcción. El cerrojo
        de registro NO se puede tomar aquí (lo verifica `_registro`), así que
        quien llame tiene que tener resuelto qué salas son antes, con el registro.
        """
        valves = ExitStack()
        with valves:
            for sala in sorted(salas, key=lambda r: r.room_id):
                valves.enter_context(self._sala(sala))
            with self._outbox.lote() as lote:
                yield salas
        self._outbox.drenar(lote)

    def _salas_de(self, sid):
        """Salas en las que `sid` tiene algún asiento (conectado o no). Solo el
        registro, sin tomar el cerrojo de ninguna sala: es la lista de candidatas
        que luego se bloquean y se comprueban ya con su cerrojo tomado."""
        with self._registro():
            return [room for room in self.rooms.values() if sid in room.players]

    @contextmanager
    def _sala_de_id(self, room_id):
        """Cerrojo de la sala `room_id` SIN lote de salida. Para las lecturas
        puras (las métricas), que no encolan nada y no quieren pagar un lote
        vacío por sala. Entrega None si la sala ya no existe."""
        with self._registro():
            room = self.rooms.get(room_id)
        if room is None:
            yield None
            return
        with self._sala(room):
            yield room

    def _contar(self, ambito, fase, n=1):
        """Suma `n` transiciones al contador de fases de `ambito` ('jugadores' o
        'salas'). Cerrojo hoja: se puede llamar con una sala en la mano."""
        with self._contadores_lock:
            fases = self.presencia[ambito]
            fases[fase] = fases.get(fase, 0) + n

    def _contar_sala_rechazada(self):
        """Contador de ataque: una sala rechazada por el techo. Va con cerrojo
        propio y no con el del registro porque `/metrics` lo lee sin tomar el
        registro (para no retener a las mesas)."""
        with self._contadores_lock:
            self.salas_rechazadas += 1

    def send_room_update(self, room_id, _room=None):
        # Reentrante: la llaman casi siempre desde DENTRO de la transacción de su
        # propia sala (jugar, votar, revelar...), donde el cerrojo ya está
        # tomado y el lote ya está abierto. Para eso está `_room`: quien ya lo
        # tiene se lo pasa, porque volver a buscar la sala en el registro sería
        # tomar el registro con una sala en la mano, que es exactamente el orden
        # que deadlockea (y `_registro()` lo hace saltar). Cuando llega de fuera
        # (el reaper, o un test) lo resuelve ella y abre su propia transacción.
        # En ambos casos solo lee estado y serializa una foto de la sala POR
        # JUGADOR (Room.to_dict ya devuelve una copia de la mano, no la lista
        # viva: entre que el payload se construye y que sale por el socket la
        # mano puede haber cambiado).
        #
        # IMPORTANTE: emitir NO renueva `last_seen`. Escribir en el búfer de
        # salida no demuestra que el cliente siga ahí, y hacerlo convertía el
        # umbral de zombis en un detector de "sala silenciosa" (mataba a todo
        # el mundo a la vez) mientras un solo jugador hablando resucitaba a los
        # muertos. La señal de vida llega por `heartbeat` (pong del cliente).
        with self._tx_sala(room_id, room=_room) as room:
            if room is None:
                return
            for sid, p in room.players.items():
                if p.is_connected:
                    self._outbox.add_update(room_id, sid, room.to_dict(for_sid=sid))

    def send_chat_system(self, room_id, message, _room=None):
        with self._tx_sala(room_id, room=_room) as room:
            if room is None:
                return
            self._outbox.add_direct(room_id, 'chat_message',
                                    {'msg': message, 'system': True}, to=room_id)

    def join_game(self, sid, name, room_id):
        """Da de alta a `sid` en `room_id`.

        Es la operación más larga de todas: toca el REGISTRO (puede crear la sala,
        comprobar el techo e incrementar el contador de rechazo) y luego la SALA, y
        además puede tener que soltar al jugador de la sala en la que estuviera.
        Por eso es la única que necesita bloqueos de dos niveles, y por eso los
        toma en el orden que no puede deadlockear: registro, y después las salas
        por `room_id` (ver `_tx_salas`).

        Lo que NO se puede hacer desde dentro de una sala es tocar el registro, y
        lo que no se puede hacer desde el registro es decidir nada de juego: por
        eso el registro solo resuelve NOMBRES de salas, y la verdad vive ya en la
        siguiente fase, con los cerrojos de sala tomados. Tampoco se emite nada
        con el registro tomado: el `join_error` del servidor lleno sale por una
        transacción de registro, ya fuera de su cerrojo.
        """
        # --- Fase 1: registro. Solo resolución y creación; nada de juego. ---
        with self._registro():
            room = self.rooms.get(room_id)
            # Techo de salas. Va ANTES de crear nada: una sala rechazada no
            # ocupa memoria ni aparece en el `game_update` de nadie. Solo crear
            # sala nueva cuenta; entrar en una que ya existe nunca se rechaza por
            # esto, porque no crea nada nuevo.
            lleno = room is None and len(self.rooms) >= self.MAX_ROOMS
            if lleno:
                self._contar_sala_rechazada()
                logger.warning(
                    "Sala '%s' rechazada: el proceso ya tiene %d salas (MAX_ROOMS)",
                    room_id, len(self.rooms))
                origenes = []
            else:
                if room is None:
                    room = self.rooms[room_id] = Room(
                        room_id, sid, self.global_white_cards, self.global_black_cards)
                # Las salas donde este sid ya tiene un asiento: hay que soltarlas
                # ANTES de tomar el nuevo asiento (si no, quedaba un Player
                # fantasma 'conectado' en la sala anterior).
                origenes = [r for r in self._salas_de(sid) if r is not room]

        if lleno:
            with self._tx_registro():
                self._outbox.add_direct(room_id, 'join_error', {
                    'message': 'El servidor está lleno de salas ahora mismo. '
                               'Prueba con otro código en un momento.'}, to=sid)
            return False

        # --- Fase 2: las salas, en orden total por room_id. ---
        with self._tx_salas(*([room] + origenes)):
            return self._join_game(sid, name, room_id, room, origenes)

    def _join_game(self, sid, name, room_id, room, origenes):
        # --- Check duplicates
        for existing_sid, p in room.players.items():
            if p.name == name and p.is_connected and existing_sid != sid:
                self._outbox.add_direct(room_id, 'join_error',
                                        {'message': 'Ese nombre ya está en uso en esta sala.'}, to=sid)
                return False

        # Asignar None a `empty_since` es "aquí hay alguien": perdona la
        # sospecha de la sala y cancela su periodo de gracia, de modo que quien
        # vuelve antes de que venza recupera la partida tal cual la dejó. Se
        # comprueba el estado ANTES porque el setter no devuelve nada.
        if room.presence.state == SOSPECHOSO:
            self._contar('salas', VIVO)
        room.empty_since = None

        # Un sid, un asiento. Si venía jugando en otra sala, se suelta de ella
        # ANTES de tomar el nuevo asiento: si no, quedaba un Player fantasma
        # 'conectado' en la sala anterior y la purga de zombis lo expulsaba
        # también de ésta. Va después del chequeo de nombre duplicado para no
        # tocar la sala de origen si la unión va a ser rechazada.
        #
        # `origenes` ya viene resuelto y con su cerrojo tomado: volver a buscarlo
        # aquí tocaría el registro con una sala en la mano, que es exactamente el
        # orden que produce el deadlock.
        for otra in origenes:
            if sid in otra.players:
                self._release_from_room(otra, otra.room_id, sid)

        reconnected = False
        for existing_sid, p in list(room.players.items()):
            if p.name == name or existing_sid == sid:
                # Dos maneras de reconocer al mismo jugador: por el nombre (ha
                # vuelto con otro sid) o por el sid (sigue en la mesa y lo que ha
                # cambiado es el nombre que ha escrito). La segunda no se puede
                # tratar como "nuevo jugador": antes caía en el `else` de abajo y
                # creaba un Player nuevo encima del viejo, lo que perdía su mano
                # y su marcador PARA SIEMPRE (las cartas del Player viejo no
                # estaban en ninguna parte y el mazo no las volría a ver). Aquí se
                # conserva el asiento y solo se actualiza el nombre.
                #
                # El chequeo de nombre duplicado de arriba ya ha descartado que ese
                # nombre sea de OTRO jugador conectado, así que renombrarse no
                # puede pisar a nadie.
                nombre_anterior = p.name
                p.sid = sid
                p.name = name
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
                elif nombre_anterior != name:
                    print(f"{nombre_anterior} ahora se llama {name} en {room_id}")
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
        # (pero no cuenta como prueba de latido; ver _touch_player). Se le pasa
        # la sala porque su cerrojo ya está tomado y `_touch_player` no puede
        # volver a buscar en el registro con una sala en la mano.
        self._touch_player(sid, _salas_previas=[room])
        self.send_room_update(room_id, _room=room)
        return True

    def heartbeat(self, sid):
        """Punto de entrada público del latido: el cliente ha contestado al
        'heartbeat_ping' del reaper. Es la prueba de que ese cliente habla el
        protocolo de latidos, que es lo que autoriza al reaper a purgarlo si
        luego deja de contestar. Unir a la sala no cuenta: demostrar que sabes
        contestar es justo lo que hay que comprobar.

        Cruza salas: el mismo sid puede tener un asiento en más de una mientras
        se resuelve la desconexión, así que se bloquean todas las suyas (por
        `room_id`) antes de tocar nada."""
        self._touch_player(sid, proof_of_life=True)

    def _touch_player(self, sid, proof_of_life=False, _salas_previas=None):
        """Renueva el reloj de silencio del jugador `sid` en todas las salas donde
        aparezca, y le perdona la sospecha si estaba en periodo de gracia.
        `proof_of_life=True` además incrementa su contador de latidos
        respondidos. Nunca se llama desde una difusión del servidor: solo desde
        unión o pong, es decir, algo que demuestra que hubo tráfico de verdad.
        Se invoca SIEMPRE con el cerrojo de las salas de `sid` tomado, igual que
        el reaper, para que la lectura/escritura del estado no se intercale con
        los handlers de juego.

        El perdón es lo que convierte la sospecha en periodo de gracia real: un
        navegador que vuelve de una pestaña suspendida contesta el reto, sale de
        la fase sospechosa y conserva su asiento. Y no hace falta que sea un
        pong: entrar a la sala con el mismo nombre es también tráfico de verdad.

        Nota: como el reaper hace ping cada ROOM_REAP_INTERVAL s, en la
        práctica el reloj es "último pong" y cualquier cliente vivo lo
        renueva solo; las acciones de juego no necesitan tocarlo.

        `_salas_previas` permite saltarse la búsqueda cuando quien llama ya sabe
        qué salas bloquear (así lo hace `join_game`, que viene con las suyas ya
        resueltas y no puede volver a tocar el registro con una sala tomada)."""
        if _salas_previas is None:
            salas = self._salas_de(sid)
        else:
            salas = [s for s in _salas_previas if s is not None]
        if not salas:
            return
        with self._tx_salas(*salas):
            now = time.time()
            for room in salas:
                # Solo los asientos activos: un pong de un asiento ya purgado no
                # renueva nada, porque ese jugador dejó de mandar hace rato y lo
                # que debe hacer es volver a entrar por `join_game`.
                p = room.asiento(sid)
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

        Es una consulta de LECTURA: no modifica nada, así que no necesita el
        cerrojo de ninguna sala. Solo el del registro, un instante, para que el
        recorrido del dict sea coherente mientras el reaper borra salas.

        Si quedara un asiento desconectado con el mismo sid (un fantasma), se
        ignora: lo que interesa es dónde está el jugador de verdad."""
        fallback = None
        with self._registro():
            for room_id, room in self.rooms.items():
                if room.asiento(sid) is not None:
                    return room_id
                if room.players.get(sid) is not None:
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
                self.advance_to_next_round(room.room_id, room)
            else:
                self._reassign_czar_after_disconnect(room)

    def disconnect(self, sid):
        """Desconexión formal de `sid`.

        Cruza salas: el mismo sid puede tener un asiento en más de una mientras
        se resuelve una migración, así que se localizan sus salas (solo
        registro, sin tomar ninguna) y se bloquean todas a la vez, en el orden
        total de `_tx_salas`. No se recorren una a una porque `_release_from_room`
        necesita el registro para el `send_room_update` de quien se queda, y
        tomarlo ya con el cerrojo de una mesa sería el deadlock prohibido.

        Un sid tiene como mucho un asiento. Sin esto, moverse de una sala a otra
        dejaba un Player fantasma en la anterior que seguía marcado como
        conectado: el reaper lo daba por zombi y, al purgarlo, le cortaba la
        sesión en la sala nueva donde sí estaba jugando.        Por eso `join_game` reutiliza esta misma rutina con la sala de origen ya
        resuelta y bloqueada.
        """
        salas = self._salas_de(sid)
        if not salas:
            return
        with self._tx_salas(*salas):
            for room in salas:
                self._release_from_room(room, room.room_id, sid)

    def _release_from_room(self, room, room_id, sid):
        """Marca a `sid` como desconectado de `room` y deja la sala en un estado
        coherente. Se llama SIEMPRE con el cerrojo de `room` tomado: es el cuerpo
        compartido por la desconexión formal (`disconnect`) y por la purga de un
        zombi (`_reap_jugadores_de_sala`), que son las dos cosas que hacen lo
        mismo con un asiento. Reutilizar la misma rutina también sanea la sala
        abandonada: relevo de líder/juez, revert a 'waiting' y activación de
        espectadores."""
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
            self.send_room_update(room_id, _room=room)
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
            self.advance_to_next_round(room.room_id, room)
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

    def reap_empty_rooms(self):
        """Pasada periódica del reaper (ciclo de ~ROOM_REAP_INTERVAL s): pide un
        latido a cada cliente conectado y hace avanzar las máquinas de presencia
        (jugadores y salas) un paso.

        UNA PASADA INDEPENDIENTE POR MESA. Antes esto era una única transacción
        con el cerrojo global encima, lo que tenía dos costes que no se ven
        hasta que duelen: una mesa con un cliente lento paraba a las otras
        cuarenta, y corregir una mesa exigía retener el estado de todas. Ahora el
        registro se lee una vez (una lista de ids, un instante) y después cada
        sala se abre y se cierra por separado, con SU cerrojo. Que las salas se
        purguen en un orden distinto al de la lista solo cambia el orden de los
        avisos, no el resultado: nadie se purga antes por ir el segundo.

        Ninguna purga ocurre en la pasada en la que se detecta el silencio: se
        sospecha primero y la purga espera a la pasada siguiente. Es la
        diferencia entre "el reaper se enteró" y "el reaper se equivocó": lo
        primero puede ser una pestaña en segundo plano, una
        cobertura mala o un móvil que acaba de cambiar de red, y en ninguno de
        esos casos la respuesta correcta es echar a alguien.

        Los latidos salen al terminar la transacción de cada mesa, no durante:
        son uno por jugador conectado y son la E/S más barata de todas (una
        línea), pero se emiten a un socket INDIVIDUAL y con 41 salas y 49
        conectados eran 245 ms de cerrojo global quemados en mensajes de tres
        campos.

        Aquí ya no se audita la conservación de cartas: esa comprobación vive en
        la suite de tests, que la corre después de cada acción. En producción
        solo produjo un WARNING anónimo que decía 'algo se perdió' sin señalar
        qué ni cuándo, y obligaba a recorrer el estado entero de cada sala cada
        15 s bajo el cerrojo global."""
        now = time.time()
        # El registro se lee UNA vez y se suelta: solo la lista de ids. A partir
        # de aquí cada mesa va por su cuenta, y ninguna operación de la pasada
        # vuelve a necesitar el registro (el borrado sí, y va al final).
        with self._registro():
            room_ids = list(self.rooms)

        purgadas = []
        for room_id in room_ids:
            with self._tx_sala(room_id) as room:
                # La sala puede haber desaparecido entre la foto y ahora (la
                # borra otra pasada): no hay nada que hacer con ella.
                if room is None:
                    continue

                # --- Latido: la única forma de renovar el reloj de silencio es
                # que el cliente conteste. Se emite a TODOS los conectados
                # (también a los que aún no han contestado nunca, y también a
                # los que están en periodo de gracia: es justo a ellos a quien
                # hay que poder grabarle la prueba de vida) para que puedan
                # demostrar que viven. Es la contrapartida de la purga: no
                # podemos exigirle a nadie que responda a un ping que no le
                # hemos hecho.
                for sid, p in list(room.players.items()):
                    if p.is_connected:
                        self._outbox.add_direct(room_id, 'heartbeat_ping',
                                                {'room_id': room_id}, to=sid)

                self._reap_jugadores_de_sala(room, now)
                if self._reap_sala(room, now):
                    purgadas.append(room_id)

        # El BORRADO es lo único de la pasada que necesita el registro, y va al
        # final, con todas las salas ya sueltas. Entrar al registro con una sala
        # en la mano es el orden que produce el deadlock, y una mesa no puede
        # quedarse congelada porque haya que purgar otra.
        if purgadas:
            por_sacar = []
            with self._registro():
                for room_id in purgadas:
                    por_sacar += self._borrar_sala_purgada(room_id, now)
            # FUERA del registro (y de cualquier cerrojo): sacar a alguien de
            # una sala de Socket.IO toca el estado de ese servidor y, en otras
            # versiones de la librería, escribe al socket. La E/S no se hace con
            # un cerrojo de juego tomado, y esta regla no se negocia.
            self._sacar_del_socket_io(por_sacar)

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
        del cerrojo de la sala aunque haya 41 mesas y 49 conectados. Se llama
        con ese cerrojo tomado (lo tienen `_touch_player` y el reaper), y por
        eso el contador de la transición va con su cerrojo hoja y no con el del
        registro: tomarlo aquí sería tomarlo al revés."""
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

        self._contar('jugadores', estado)
        logger.info("Presencia: %s pasa a '%s' en la sala %s (%s)", p.name, estado,
                    room.room_id, motivo)

    def _reap_jugadores_de_sala(self, room, now):
        """Un paso de la máquina de presencia de los jugadores de UNA sala, con
        su cerrojo tomado y sin tocar el registro.

        Se separa en dos listas porque las transiciones no tocan el estado de
        la sala y hay que recorrerla entera antes de liberar a nadie: purgar
        muta la sala (reasigna juez, revierte a 'waiting', renueva la partida) y
        no se puede hacer eso en mitad de un bucle.

        Reglas:

        - Nadie queda sin juicio. La evidencia decide QUÉ política se aplica,
          no si se aplica: quien ya contestó alguna vez se juzga con PRESENCE, y
          quien nunca contestó se juzga con PRESENCE_SIN_PRUEBA, que es la misma
          máquina con más margen. Saltarse a quien no ha dado pruebas era lo que
          dejaba fijar salas para siempre (ver PRESENCE_SIN_PRUEBA).
        - Sospechar y purgar son mutuamente excluyentes en la misma pasada
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
        for sid, p in list(room.players.items()):
            if not p.is_connected:
                continue
            # La prueba decide la política, no la aplicabilidad: sin pruebas se
            # juzga igual, pero con plazos más largos.
            politica = (self.PRESENCE if p.heartbeat_pongs
                        else self.PRESENCE_SIN_PRUEBA)
            if p.presence.accuse(now, politica):
                sospechosos.append((sid, p))
            elif p.presence.purge_due(now):
                a_purgar.append((sid, p))

        for sid, p in sospechosos:
            # El motivo lleva el dato que hace falta al leer el log sin más
            # contexto: cuánto lleva callado y cuánto le queda de gracia.
            if p.heartbeat_pongs:
                motivo = ("silencio de {:.0f}s tras {} pongs; si no vuelve, su "
                          "asiento se libera en {:.0f}s".format(
                              now - p.last_seen, p.heartbeat_pongs,
                              p.presence.segundos_para_purga(now)))
            else:
                motivo = ("nunca ha contestado un solo latido en {:.0f}s; si no "
                          "contesta al reto, su asiento se libera en {:.0f}s".format(
                              now - p.last_seen,
                              p.presence.segundos_para_purga(now)))
            self._anunciar_presencia(room, sid, SOSPECHOSO, now, motivo=motivo)

        for sid, p in a_purgar:
            # El aviso va antes de marcar PURGADO: así conserva el plazo que
            # acaba de cumplirse, que es la información útil para el resto.
            self._anunciar_presencia(
                room, sid, PURGADO, now,
                motivo="el periodo de gracia ha vencido: {:.0f}s sospechoso y "
                       "{} latidos sin contestar".format(
                           now - p.presence.since, p.heartbeat_pongs))
            p.presence.purge(now)
            self._release_from_room(room, room.room_id, sid)

    def _reap_sala(self, room, now):
        """Un paso de la máquina de presencia de UNA sala. Devuelve True si su
        gracia se ha agotado y toca purgarla; el BORRADO no se hace aquí (es del
        registro) sino en `_borrar_sala_purgada`, ya con el cerrojo de la sala
        suelto.

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
        if room.empty_since is None:
            # Alguien ha entrado: el setter de `empty_since` ya le ha indultado
            # y ha devuelto la sala a vivo. Solo queda contarlo.
            if room.presence.state != VIVO:
                self._contar('salas', VIVO)
            return False
        politica = (self.ROOM_LIFECYCLE if room.ha_empezado
                    else self.ROOM_LIFECYCLE_NUEVA)
        if room.presence.accuse(now, politica):
            self._contar('salas', SOSPECHOSO)
            logger.info(
                "Reaper: la sala %s lleva %.0fs vacía; se purga en %.0fs si "
                "no vuelve nadie%s", room.room_id, now - room.empty_since,
                room.presence.segundos_para_purga(now),
                "" if room.ha_empezado else " (nunca llegó a empezar)")
            return False
        return room.presence.purge_due(now)

    def _borrar_sala_purgada(self, room_id, now):
        """Purga terminal de una sala cuya gracia se agotó. La llama el reaper con
        el REGISTRO tomado y sin ningún cerrojo de sala, y hace las dos cosas en
        el orden correcto: el estado de la mesa se toca con su cerrojo (y con el
        registro ya encima, que es el orden bueno), y el borrado del dict, que es
        del registro, va al final.

        Devuelve los `(room_id, sid)` que hay que sacar de la sala de
        Socket.IO: la sala del GameManager y la de Socket.IO no son lo mismo."""
        sala = self.rooms.get(room_id)
        if sala is None:
            return []
        with self._sala(sala):
            logger.info("Reaper: sala %s purgada tras %.0fs vacía (gracia "
                        "agotada%s)", room_id, now - sala.empty_since,
                        "" if sala.ha_empezado else ", sin llegar a empezar")
            self._contar('salas', PURGADO)
            sala.presence.purge(now)
        return self._delete_room(room_id)

    def _sacar_del_socket_io(self, pares):
        """Saca a los clientes de la sala de Socket.IO de una mesa ya purgada.

        app.py mete a cada cliente en una sala de Socket.IO con su código de
        sala, y solo se sale de ella al cambiar de sala (o al desconectarse, que
        lo hace la propia librería). Un cliente purgado por el reaper, o que se
        fue del juego sin que llegara su 'disconnect', sigue conectado y sigue
        dentro: si más adelante alguien crea una mesa con el MISMO código, ese
        cliente invisible se seguiría enterando de su chat.

        Se llama FUERA de los cerrojos y a través del servidor de la librería
        (no del `leave_room` de flask_socketio, que necesita un contexto de
        petición y aquí no hay: esto corre en el hilo del reaper)."""
        servidor = getattr(self.socketio, 'server', None)
        if servidor is None:      # socket de mentira (tests, banco): nada que sacar
            return
        for room_id, sid in pares:
            try:
                servidor.leave_room(sid, room_id)
            except Exception as e:
                # Un socket que ya no está no se puede sacar de ninguna sala.
                # Es lo más normal del mundo con el reaper de por medio.
                logger.debug("No se pudo sacar a %s de la sala %s: %s", sid, room_id, e)

    def _delete_room(self, room_id):
        """Saca la sala del registro y olvida su estado. Se llama con el
        registro tomado; su cerrojo se toma DESPUÉS de sacarla del dict, con la
        sala ya descolgada, para no competir con ningún hilo que la tuviera
        resuelta antes. Devuelve los sids que tenía, para que el llamante pueda
        sacarlos de la sala de Socket.IO."""
        with self._registro():
            room = self.rooms.pop(room_id, None)
        if not room:
            return []
        sids = list(room.players)
        # El estado de emisión se olvida con la sala: sus canales no se
        # reutilizan y lo que otro hilo tenga en la mano se descarta al drenar.
        self._outbox.close_room(room_id)
        with self._sala(room):
            room.players.clear()
            room.played_cards = []
            room.deck.clear()
            room.available_blacks = []
            room.renew_votes.clear()
            room.join_order = []
        return [(room_id, sid) for sid in sids]

    # --- Observabilidad -------------------------------------------------------

    def metrics(self):
        """Instantánea numérica del proceso para el endpoint `/metrics`.

        NO se toma bajo un cerrojo único, y eso es deliberado en las dos
        direcciones. Ni por cortesía (meter el registro entero alrededor de la
        lectura convertiría un endpoint de diagnóstico en el siguiente cliente
        del reaper) ni por necesidad: el reaper ya no borra salas por debajo de
        esta lectura, así que el `RuntimeError: dictionary changed size during
        iteration` que justificaba el cerrojo global ya no puede ocurrir. Lo
        que hace es tomar el registro un instante para COPIAR la lista de salas
        y los contadores, y después leer cada mesa con SU cerrojo, una por una.

        La foto no es atómica, y no puede serlo sin volver al cerrojo global,
        pero sí es coherente sala a sala: ningún número mezcla dos estados de la
        misma mesa, y una mesa que desaparece a mitad del recorrido simplemente
        no cuenta.

        Mezcla dos clases de número a propósito, y por eso las separa en dos
        bloques en vez de soltar todo junto:

        - MEDIDORES (gauge): el estado de AHORA. Suben y bajan; una sala purgada
          deja de contar. Es lo que se mira en un panel.
        - CONTADORES (acumulado): sumas desde el arranque del proceso. Solo
          suben. `self.presencia` y `salas_rechazadas` son de este tipo, así que
          `presencia.salas.purgado` NO son las salas purgadas ahora mismo, son
          todas las que se han purgado en la vida del proceso. Leerlos como un
          gauge es la forma más fácil de equivocarse al leer esta respuesta, y
          por eso el instantaneous va al lado para poder contrastarlos.

        Alcance: solo se ven los clientes con asiento en alguna sala. Un sid
        conectado al socket que nunca ha entrado a un juego no aparece en
        ningún sitio del GameManager, así que estos números son de "clientes en
        partida", no de "sockets abiertos". Contar los segundos exigiría
        instrumentar el ciclo de vida de Socket.IO, que no existe aquí.
        """
        ahora = time.time()
        with self._registro():
            total_salas = len(self.rooms)
            room_ids = list(self.rooms)
        with self._contadores_lock:
            presencia = {ambito: dict(fases)
                         for ambito, fases in self.presencia.items()}
            rechazadas = self.salas_rechazadas

        salas_por_estado = {}
        salas_fase = {VIVO: 0, SOSPECHOSO: 0, PURGADO: 0}
        jugadores_fase = {VIVO: 0, SOSPECHOSO: 0, PURGADO: 0}
        conectados = 0
        asientos = 0
        mudos = 0

        for room_id in room_ids:
            with self._sala_de_id(room_id) as room:
                if room is None:
                    continue
                datos = self._datos_de_sala(room)
            # Se acumula FUERA del cerrojo: los números no son estado de nadie y
            # no necesitan estar protegidos mientras se suman.
            estado, fase = datos['estado'], datos['fase']
            salas_por_estado[estado] = salas_por_estado.get(estado, 0) + 1
            salas_fase[fase] = salas_fase.get(fase, 0) + 1
            for fase_j, n in datos['fases'].items():
                jugadores_fase[fase_j] = jugadores_fase.get(fase_j, 0) + n
            asientos += datos['asientos']
            conectados += datos['conectados']
            mudos += datos['mudos']

        return {
            'timestamp': ahora,
            'salas': {
                # El total es el de la MISMA foto que la lista de ids que se
                # recorren debajo: si el reaper purga una mesa entremedias, este
                # bloque y el de `por_estado` lo cuentan igual, y quien lea el
                # panel ve un número que cuadra con lo que se midió.
                'total': total_salas,
                'max': self.MAX_ROOMS,
                'por_estado': salas_por_estado,
            },
            'jugadores': {
                'conectados': conectados,
                'asientos': asientos,
                'mudos': mudos,
            },
            'salas_rechazadas': rechazadas,
            # Acumulado de transiciones desde el arranque del proceso, ya
            # copiado: quien lo lee después no está mirando un dict que otro
            # hilo está mutando.
            'presencia': presencia,
            # Medidor: quién está en qué fase AHORA.
            'presencia_actual': {
                'jugadores': jugadores_fase,
                'salas': salas_fase,
            },
        }

    def _datos_de_sala(self, room):
        """Lo que UNA mesa aporta a `/metrics`. Se mide con su cerrojo tomado y
        se devuelve como datos, no como referencias vivas: el llamante suma
        fuera del cerrojo y no necesita retener a nadie para hacerlo.

        - Un asiento purgado o desconectado sigue en `players` a propósito
          (conserva mano y marcador), así que `asientos` y `conectados` no son
          el mismo número y la diferencia es justamente la que se está fugando.
        - Un "mudo" es un cliente conectado que aún no ha contestado ni un solo
          latido. Es el grupo que el reaper juzga con PRESENCE_SIN_PRUEBA, y la
          razón de que una sala siga ocupada sin que nadie juegue: el
          transporte vive aunque el JS haya muerto, y así es como se cuelan en
          el techo de salas.
        """
        datos = {'estado': room.state, 'fase': room.presence.state, 'fases': {},
                 'asientos': 0, 'conectados': 0, 'mudos': 0}
        for p in room.players.values():
            datos['asientos'] += 1
            fase_j = p.presence.state
            datos['fases'][fase_j] = datos['fases'].get(fase_j, 0) + 1
            if not p.is_connected:
                continue
            datos['conectados'] += 1
            if not p.heartbeat_pongs:
                datos['mudos'] += 1
        return datos

    def start_game(self, sid, room_id):
        with self._tx_sala(room_id) as room:
            if room is not None:
                self._start_game_locked(sid, room_id, room)

    def _start_game_locked(self, sid, room_id, room=None):
        if room is not None:
            if room.state == 'waiting' and room.leader == sid and room.asiento(sid) is not None:
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
                self.send_room_update(room_id, _room=room)

    def update_options(self, sid, room_id, data):
        with self._tx_sala(room_id) as room:
            if room is not None:
                self._update_options_locked(sid, room_id, data, room)

    def _update_options_locked(self, sid, room_id, data, room=None):
        if room is not None:
            if (room.state == 'waiting' and room.leader == sid
                    and room.asiento(sid) is not None):
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
                    
                self.send_room_update(room_id, _room=room)

    def play_card(self, sid, room_id, card_index):
        with self._tx_sala(room_id) as room:
            if room is not None:
                self._play_card_locked(sid, room_id, card_index, room)

    def _play_card_locked(self, sid, room_id, card_index, room=None):
        if room is not None:
            p = room.asiento(sid)
            if room.state == 'playing' and sid != room.czar and p is not None:
                if p.waiting_next_round or p.played_card is not None:
                    return

                # Los índices vienen del cliente y no son de fiar tal cual. Se
                # filtran a enteros EN RANGO y se deduplican ANTES de tocar la
                # mano, y no después, por dos razones concretas:
                #
                # 1. `0 <= i` con un `None` o un texto revienta con TypeError, y
                #    con eso se cae la jugada entera: un elemento mal formado
                #    entre los válidos se lleva por delante la selección buena.
                # 2. Un índice repetido ([0, 0]) jugaba la MISMA carta dos veces
                #    y sacaba DOS cartas de la mano: la segunda se perdía del
                #    sistema para siempre. Es conservación de cartas, que es
                #    justo lo que este código no puede dejar en manos del
                #    cliente.
                indices = card_index if isinstance(card_index, list) else [card_index]
                valid_indices = []
                for i in indices:
                    if isinstance(i, int) and 0 <= i < len(p.hand) and i not in valid_indices:
                        valid_indices.append(i)
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
                        
                    self.send_room_update(room_id, _room=room)

    def reveal_card(self, sid, room_id, sub_id):
        with self._tx_sala(room_id) as room:
            if room is not None:
                self._reveal_card_locked(sid, room_id, sub_id, room)

    def _reveal_card_locked(self, sid, room_id, sub_id, room=None):
        if room is not None:
            if (room.state == 'judging' and sid == room.czar
                    and room.asiento(sid) is not None):
                for c in room.played_cards:
                    if c.get('id') == sub_id:
                        c['revealed'] = True
                        break
                self.send_room_update(room_id, _room=room)

    def choose_winner(self, sid, room_id, sub_id):
        with self._tx_sala(room_id) as room:
            if room is not None:
                self._choose_winner_locked(sid, room_id, sub_id, room)

    def _choose_winner_locked(self, sid, room_id, sub_id, room=None):
        if room is not None:
            if (room.state == 'judging' and sid == room.czar
                    and room.asiento(sid) is not None):
                winner_sid = None
                for c in room.played_cards:
                    if c.get('id') == sub_id:
                        winner_sid = c['sid']
                        break
                        
                # No premiar a quien se ha desconectado durante la ronda:
                # last_winner apuntaría a un sid muerto y congelaría la rotación
                ganador = room.asiento(winner_sid) if winner_sid else None
                if ganador is not None:
                    ganador.points += 1
                    room.state = 'round_end'
                    room.last_winner = ganador.sid
                    room.last_winning_card = [c for c in room.played_cards if c.get('id') == sub_id][0]['cards']
                    room.last_winner_name = ganador.name
                    self.send_room_update(room_id, _room=room)
                    
                    self.socketio.start_background_task(self.auto_next_round, room_id, sid)

    def auto_next_round(self, room_id, old_czar_sid):
        # La espera va FUERA de la transacción a propósito: dentro del cerrojo
        # serían 5 s de sala parada (y con el registro global encima, 5 s de
        # servidor parado) por un simple auto-avance de ronda.
        self.socketio.sleep(5)
        # La comprobación va DENTRO del cerrojo de esa sala: entre el sleep y
        # aquí el juez puede haber cambiado o la ronda haberse-advanzado, y un
        # auto-avance a destiempo rehece una ronda que ya no existe.
        with self._tx_sala(room_id) as room:
            if room is not None and room.state == 'round_end' and room.czar == old_czar_sid:
                self.advance_to_next_round(room_id, room)

    def force_next_round(self, sid, room_id):
        with self._tx_sala(room_id) as room:
            if room is not None:
                self._force_next_round_locked(sid, room_id, room)

    def _force_next_round_locked(self, sid, room_id, room=None):
        if room is not None:
            if (room.state == 'round_end' and room.czar == sid
                    and room.asiento(sid) is not None):
                self.advance_to_next_round(room_id, room)

    def advance_to_next_round(self, room_id, _ya_en_mano=None):
        # Público porque lo invocan auto_next_round, _force_next_round_locked,
        # _heal_stale_roles y los tests.
        #
        # `_ya_en_mano` es la sala cuya transacción sigue abierta en ESTE hilo:
        # quien la pasa ya tiene su cerrojo tomado, y volver a buscarla en el
        # registro sería tomar el registro con una sala en la mano, que es el
        # orden prohibido. Si no se pasa, se resuelve sola y abre su propia
        # transacción (el caso de `auto_next_round` y de los tests).
        #
        # Anidada y reentrante por diseño: su lote se funde con el de fuera en
        # vez de provocar un drenaje propio en mitad de la partida.
        with self._tx_sala(room_id, room=_ya_en_mano) as room:
            if room is None:
                return
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
                    
            self.send_room_update(room_id, _room=room)

    def vote_card(self, sid, room_id, card_id):
        with self._tx_sala(room_id) as room:
            if room is not None:
                self._vote_card_locked(sid, room_id, card_id, room)

    def _vote_card_locked(self, sid, room_id, card_id, room=None):
        if room is not None:
            if (room.state in ['judging', 'round_end'] and sid != room.czar
                    and room.asiento(sid) is not None):
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
                        
                self.send_room_update(room_id, _room=room)

    def vote_renew(self, sid, room_id):
        with self._tx_sala(room_id) as room:
            if room is not None:
                self._vote_renew_locked(sid, room_id, room)

    def _vote_renew_locked(self, sid, room_id, room=None):
        if room is not None:
            if room.asiento(sid) is not None:
                if sid in room.renew_votes:
                    room.renew_votes.remove(sid)
                else:
                    room.renew_votes.add(sid)
                
                self.check_and_execute_renew(room)
                self.send_room_update(room_id, _room=room)

    def check_and_execute_renew(self, room):
        # Anidada: la llaman vote_renew y _release_from_room, que ya tienen el
        # cerrojo de esta misma sala. Reentrante, y su lote se funde con el de
        # fuera en vez de provocar un drenaje propio en medio.
        with self._sala(room), self._outbox.lote() as lote:
            self._check_and_execute_renew(room)
        self._outbox.drenar(lote)

    def _check_and_execute_renew(self, room):
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
                self.send_chat_system(room.room_id, '¡Se han renovado las cartas de todos los jugadores!', _room=room)

    def change_black_card(self, sid, room_id):
        with self._tx_sala(room_id) as room:
            if room is not None:
                self._change_black_card_locked(sid, room_id, room)

    def _change_black_card_locked(self, sid, room_id, room=None):
        if room is not None:
            if (room.state == 'playing' and room.czar == sid
                    and room.asiento(sid) is not None):
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
                self.send_chat_system(room_id, 'El juez ha cambiado la carta negra. ¡Tenéis que volver a jugar!', _room=room)
                self.send_room_update(room_id, _room=room)

    def add_room_cards(self, room_id, cards_str):
        """Inyecta cartas propias en el mazo de UNA sala. No toca el mazo global
        (eso es del registro, en models/custom_cards.py): aquí solo crece el
        mazo de la mesa, con su cerrojo.

        `cards_str` llega del cliente y puede no ser una cadena (un `null` en el
        JSON es un `None` aquí): se descarta lo que no sea texto en vez de
        reventar el handler entero."""
        if not isinstance(cards_str, str):
            return
        new_cards = [c.strip() for c in cards_str.split(',') if c.strip()]
        if not new_cards:
            return
        with self._tx_sala(room_id) as room:
            if room is None:
                return
            room.deck.inject(new_cards)
            self.send_chat_system(room_id, f'Se han añadido {len(new_cards)} cartas personalizadas a la sala.', _room=room)

    def send_chat(self, sid, room_id, msg):
        # El mensaje también es dato del cliente: un `null` o un objeto se
        # descarta, y se recorta a lo que mide un mensaje (el `maxlength` del
        # navegador no es una defensa: el chat se difunde a la mesa entera).
        if not isinstance(msg, str) or not msg.strip():
            return
        msg = msg[:MAX_CHAT]
        with self._tx_sala(room_id) as room:
            jugador = room.asiento(sid) if room is not None else None
            if jugador is not None:
                self._outbox.add_direct(room_id, 'chat_message',
                                        {'msg': msg, 'sender': jugador.name,
                                         'system': False}, to=room_id)
