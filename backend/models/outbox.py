"""Emisión de eventos: se encola bajo el cerrojo de la sala, se envía sin él.

El GameManager serializa la partida con un cerrojo POR SALA (el RLock de cada
`Room`), no con un cerrojo global. Medido con un `emit` de 5 ms (un socket de
verdad, no una función): una difusión a 8 jugadores tardaba 41 ms de los cuales
40 eran E/S esperando al siguiente cliente, y una pasada del reaper con 41 salas
y 49 conectados retenía el cerrojo 245 ms. Con Gunicorn (`-w 1 --threads 100`)
no hay aislamiento posible: un jugador con el móvil en datos móviles paraliza a
los demás.

Hay dos niveles de cerrojo, y la diferencia es lo que hace que esto funcione:

- **Cerrojo de sala** (`Room.lock`): el estado de una mesa. Dos mesas que
  juegan a la vez no se ven. Es el cerrojo que toma el 99% de las operaciones.
- **Cerrojo de registro** (`GameManager._registro_lock`): el DICT de salas, los
  contadores globales de presencia y las listas de cartas del mazo global. Se
  toma un instante (unas microsegundos) y nunca alrededor de lógica de juego ni
  de E/S. Es lo que permite que el reaper recorra el registro sin que eso le
  impida corregir una sala en medio.

El orden entre ellos es FIJO y no negociable: registro ANTES que sala, nunca al
re revés. Es lo que evita el deadlock clásico (un hilo con el cerrojo de la sala
A esperando el registro, y otro con el registro esperando la sala A). Ese orden
no es una convención social: `GameManager._registro()` lo verifica y revienta
con un RuntimeError si alguien lo incumple, porque un deadlock aquí es un
servidor parado y no un test rojo.

La partición entre estado y E/S no ha cambiado: con el cerrojo tomado se LEE el
estado y se SERIALIZA el snapshot de cada jugador (microsegundos, y es lo único
que necesita consistencia); la escritura en el socket ocurre al salir de la
transacción. Un cliente lento sigue reteniendo a quien lo atiende, pero ya no
retiene ni el cerrojo de su mesa ni el de las demás.

Lo que este módulo garantiza (ver tests/test_emit_locking.py):

1. Ninguna E/S ocurre con un cerrojo de juego tomado. Cuesta lo que cueste el
   socket, la partida no se bloquea.
2. Se conserva el orden de entrega por sala. Cada sala tiene un canal con cerrojo
   propio y un número de versión, y una instantánea construida ANTES que otra
   que ya salió se descarta en vez de ensuciar la vista del cliente. El precio es
   que DENTRO de una mesa el tráfico sigue serializado (igual que antes): un
   cliente con el socket atascado retrasa la entrega de mensajes a los demás de
   SU mesa. Lo que ya no hace es bloquear la partida: ni el estado de la sala, ni
   las otras mesas, ni las E/S de nadie más.
3. Las difusiones se coalescen dentro de una transacción: un `game_update` es
   un estado completo de la sala, así que si la misma sala se difunde dos veces
   antes de salir (p. ej. al unirse con el juez de turno ya marchado) solo
   sale la última.
4. Los mensajes que llevan datos propios no se coalescen ni se descartan:
   nunca se pierde un join_error, un mensaje de chat, un latido o una
   respuesta a una petición de la interfaz de cartas.

Sobre `add_update` y los números de versión: se asignan bajo el cerrojo de la
sala a la que pertenecen, que es lo que serializa las difusiones de una misma
mesa. `last_emitted` se compara bajo el cerrojo del CANAL, al drenar y sin
ningún cerrojo de juego. Que la asignación y la comparación ocurran bajo
cerrojos distintos es seguro porque las versiones solo crecen.
"""
import threading
from contextlib import contextmanager, nullcontext

# Cerrojo "nulo" para mensajes sin sala de referencia (un join_error de una
# sala que ya no existe, p. ej.): se emiten sin orden relativo a nada.
SIN_CANAL = nullcontext()


class RoomChannel:
    """Canal de salida de una sala: serializa sus emisiones y recuerda hasta
    qué versión se ha emitido.

    `lock` es por sala, nunca global: dos mesas pueden estar emitiendo a la
    vez sin pisarse, que es justo el punto. `version` se incrementa bajo el
    cerrojo de la sala (es la marca de "estado más reciente que se ha
    construido") y se compara con `last_emitted` bajo `lock`, al emitir, para
    descartar lo obsoleto."""

    __slots__ = ('lock', 'version', 'last_emitted', 'closed')

    def __init__(self):
        self.lock = threading.Lock()
        self.version = 0
        self.last_emitted = 0
        # La sala se borró mientras alguien tenía un snapshot en la mano.
        self.closed = False

    def next_version(self):
        """Siguiente marca de versión. Solo bajo el cerrojo de su sala."""
        self.version += 1
        return self.version


class EmitBatch:
    """Lo que una transacción quiere que salga, todavía sin salir."""

    def __init__(self):
        # room_id -> (canal, {sid: (version, payload)})
        self.updates = {}
        # (canal, evento, payload, destinatario)
        self.directs = []
        # (respond_cb, payload)
        self.replies = []

    def add_update(self, room_id, channel, sid, version, payload):
        entry = self.updates.get(room_id)
        if entry is None:
            self.updates[room_id] = (channel, {sid: (version, payload)})
        elif version >= entry[1].get(sid, (0, None))[0]:
            # Coalescencia: la sala ya se está difunziendo en esta misma
            # transacción y este snapshot es posterior, así que el anterior
            # está de más: el `game_update` lleva el estado COMPLETO de la sala.
            entry[1][sid] = (version, payload)

    def merge(self, other):
        """Absorbe una transacción anidada. Su salida no sale aquí: la de la
        transacción que la rodea sigue en curso, y es la que se drena. Unir (que
        puede sanear la mesa y difundirla antes de volver a difundirla) es el
        caso típico."""
        for room_id, (channel, per_sid) in other.updates.items():
            entry = self.updates.get(room_id)
            if entry is None:
                self.updates[room_id] = (channel, dict(per_sid))
            else:
                for sid, (version, payload) in per_sid.items():
                    if version >= entry[1].get(sid, (0, None))[0]:
                        entry[1][sid] = (version, payload)
        self.directs.extend(other.directs)
        self.replies.extend(other.replies)


class Outbox:
    """Cola de salida del GameManager.

    NO es dueña de ningún cerrojo de juego: encola y drena, y punto. El cerrojo
    lo toma quien llama (el de la sala, o el del registro) y `lote()` se limita a
    abrir un lote apilado en el hilo. Separar las dos cosas es lo que permite
    que `GameManager` elija el cerrojo que cada operación necesita en vez de
    tener uno impuesto desde aquí.

    Lo único que sí lleva cerrojo propio es el DICT de canales: dos hilos pueden
    crear el canal de una sala a la vez, y `close_room` lo borra desde el reaper.
    """

    def __init__(self, socketio):
        self._socketio = socketio
        # room_id -> RoomChannel, protegido por su propio cerrojo (corto): lo
        # crean y lo borran hilos distintos y sin que medie el cerrojo de juego.
        self._channels = {}
        self._canales_lock = threading.Lock()
        self._local = threading.local()

    # --- Lote (se abre YA con el cerrojo del llamante tomado) -----------------

    def channel(self, room_id):
        """Canal de una sala, creado si no existe."""
        with self._canales_lock:
            channel = self._channels.get(room_id)
            if channel is None:
                channel = self._channels[room_id] = RoomChannel()
            return channel

    def add_update(self, room_id, sid, payload):
        """Encola el snapshot de una sala para un jugador (`game_update`).

        La versión se asigna aquí, y el llamante tiene que tener el cerrojo de
        esa sala: es lo que serializa las difusiones de una misma mesa."""
        channel = self.channel(room_id)
        self._batch().add_update(room_id, channel, sid, channel.next_version(), payload)

    def add_direct(self, room_id, event, payload, to):
        """Encola un mensaje que no se puede coalescer ni descartar (chat,
        join_error, latido). `room_id` solo da orden respecto al resto de tráfico
        de esa sala; si la sala no tiene canal aún, se emite sin orden."""
        with self._canales_lock:
            channel = self._channels.get(room_id)
        self._batch().directs.append((channel, event, payload, to))

    def add_reply(self, callback, payload):
        """Encola una respuesta a una petición con callback (interfaz de cartas
        custom). El callback escribe en el socket del cliente que pregunta."""
        self._batch().replies.append((callback, payload))

    def deferred(self, callback):
        """Versión diferida de un callback: llama a la función devuelta y lo que
        salga se emite al cerrar el lote, no dentro del cerrojo."""
        return lambda payload: self.add_reply(callback, payload)

    def close_room(self, room_id):
        """Olvida el estado de emisión de una sala borrada. Los mensajes que
        otro hilo ya tenía en la mano se descartan al drenar: sus jugadores ya
        no existen. Mejor esfuerzo: un drenaje que ya tiene el cerrojo del canal
        puede colarse, y emitirle a un sid muerto es un no-op de todas formas."""
        with self._canales_lock:
            channel = self._channels.pop(room_id, None)
        if channel is not None:
            channel.closed = True

    def _stack(self):
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        return stack

    def _batch(self):
        stack = self._stack()
        if not stack:
            raise RuntimeError(
                "se ha encolado una emisión fuera de una transacción: "
                "envuelve la llamada en GameManager._tx_sala() o _tx_registro()")
        return stack[-1]

    # --- Transacción ----------------------------------------------------------

    @contextmanager
    def lote(self):
        """Abre un lote de salida. NO toma ningún cerrojo: el llamante ya lo
        tiene (o es el reaper, que abre uno para toda su pasada).

        Anidamiento: el lote más interno se fusiona con el de fuera y no drena
        nada por su cuenta, así que la E/S ocurre una sola vez, al cerrar el
        más externo, y nunca con un cerrojo de juego tomado."""
        batch = EmitBatch()
        stack = self._stack()
        stack.append(batch)
        try:
            yield batch
        finally:
            stack.pop()
            if stack:
                # Anidado: se entrega al lote que lo rodea, que es el único que
                # puede drenar sin cerrojo de juego tomado.
                stack[-1].merge(batch)

    def drenar(self, batch):
        """Escribe en el socket lo que encoló un lote. Solo el lote MÁS EXTERNO
        drena; los anidados ya se han fusionado con él. Lo llama el GameManager
        DESPUÉS de soltar su cerrojo, nunca desde dentro."""
        if not self._stack():  # estamos en el lote más externo
            self.flush(batch)

    # --- Fuera de los cerrojos de juego --------------------------------------

    def flush(self, batch):
        """Escribe en el socket lo que encoló un lote. Nunca con un cerrojo de
        juego tomado: el estado ya está serializado en los payloads y no se
        vuelve a leer nada del `Room`."""
        if batch is None:
            return
        emit = self._socketio.emit
        for channel, per_sid in batch.updates.values():
            with channel.lock:
                if channel.closed:
                    continue
                emitida = channel.last_emitted
                for sid, (version, payload) in per_sid.items():
                    if version <= emitida:
                        # Obsoleta: otro hilo ya entregó un estado más nuevo de
                        # esta sala. Emitirla sería hacer retroceder la vista
                        # del cliente.
                        continue
                    emit('game_update', payload, to=sid)
                    emitida = version
                channel.last_emitted = emitida
        for channel, event, payload, to in batch.directs:
            with channel.lock if channel is not None else SIN_CANAL:
                if channel is not None and channel.closed:
                    continue
                emit(event, payload, to=to)
        for callback, payload in batch.replies:
            callback(payload)
