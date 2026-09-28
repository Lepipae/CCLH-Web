"""Emisión de eventos: se encola con el cerrojo global, se envía sin él.

El GameManager serializa TODA la partida con un RLock global, y hasta ahora
emitía también con el cerrojo tomado. Medido con un `emit` de 5 ms (un socket
de verdad, no una función): una difusión a 8 jugadores tardaba 41 ms de los
cuales 40 eran E/S esperando al siguiente cliente, y una pasada del reaper con
41 salas y 49 conectados retenía el cerrojo 245 ms. Con Gunicorn
(`-w 1 --threads 100`) no hay aislamiento posible: un jugador con el móvil en
datos móviles paraliza a los demás.

La partición es: con el cerrojo tomado se LEE el estado y se SERIALIZA el
snapshot de cada jugador (microsegundos, y es lo único que necesita
consistencia); la escritura en el socket ocurre al salir de la transacción. Un
cliente lento sigue reteniendo a quien lo atiende, pero ya no retiene el cerrojo
global, que es lo que compartían todas las mesas.

Lo que este módulo garantiza (ver tests/test_emit_locking.py):

1. Ninguna E/S ocurre con el cerrojo global tomado. Cuesta lo que cueste el
   socket, la partida no se bloquea.
2. Se conserva el orden de entrega por sala. Con el cerrojo global las
   emisiones estaban totalmente ordenadas; sin él, dos hilos podrían salir en
   desorden y un cliente ver un estado viejo después de uno nuevo. Cada sala
   tiene un canal con cerrojo propio y un número de versión, y una instantánea
   construida ANTES que otra que ya salió se descarta en vez de ensuciar la
   vista del cliente. El precio es que DENTRO de una mesa el tráfico sigue
   serializado (igual que antes): un cliente con el socket atascado retrasa la
   entrega de mensajes a los demás de SU mesa. Lo que ya no hace es bloquear la
   partida: ni el estado de la sala, ni las otras mesas, ni las E/S de nadie más.
3. Las difusiones se coalescen dentro de una transacción: un `game_update` es
   un estado completo de la sala, así que si la misma sala se difunde dos veces
   antes de salir (p. ej. al unirse con el juez de turno ya marchado) solo
   sale la última.
4. Los mensajes que llevan datos propios no se coalescen ni se descartan:
   nunca se pierde un join_error, un mensaje de chat, un latido o una
   respuesta a una petición de la interfaz de cartas.
"""
import threading
from contextlib import contextmanager, nullcontext
from functools import wraps

# Cerrojo "nulo" para mensajes sin sala de referencia (un join_error de una
# sala que ya no existe, p. ej.): se emiten sin orden relativo a nada.
SIN_CANAL = nullcontext()


def transactional(fn):
    """Convierte el método en una transacción: el cuerpo entero se ejecuta con
    el cerrojo global tomado y lo que encole sale al salir de él.

    Se aplica a los métodos públicos que mutan estado o encolan mensajes. El
    nombre del método y su valor de retorno se conservan (los clientes de
    Socket.IO usan el retorno de los handlers en algunos sitios y los tests
    depends de `join_game`)."""

    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        return self._outbox.run(fn, self, *args, **kwargs)

    return wrapper


class RoomChannel:
    """Canal de salida de una sala: serializa sus emisiones y recuerda hasta
    qué versión se ha emitido.

    `lock` es por sala, nunca global: dos mesas pueden estar emitiendo a la
    vez sin pisarse, que es justo el punto. `version` se incrementa bajo el
    cerrojo global (es la marca de "estado más reciente que se ha construido"),
    y se compara con `last_emitted` (bajo `lock`, al emitir) para descartar lo
    obsoleto. Que la asignación de versiones y la comparación ocurran bajo
    cerrojos distintos es seguro porque las versiones solo crecen."""

    __slots__ = ('lock', 'version', 'last_emitted', 'closed')

    def __init__(self):
        self.lock = threading.Lock()
        self.version = 0
        self.last_emitted = 0
        # La sala se borró mientras alguien tenía un snapshot en la mano.
        self.closed = False

    def next_version(self):
        """Siguiente marca de versión. Solo bajo el cerrojo global."""
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
        """Absorbe una transacción anidada. Su salida no sale aquí: el cerrojo
        global de la transacción que la rodea sigue tomado, y la de la que
        rodea es la que se drena. Unir (que puede sanear la mesa y difundirla
        antes de volver a difundirla) es el caso típico."""
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

    Vive junto al cerrojo global (que se le pasa) y es lo único que sabe
    escribir en el socket. `GameManager` solo encola."""

    def __init__(self, socketio, lock):
        self._socketio = socketio
        self._lock = lock
        # room_id -> RoomChannel. Se crea y se borra siempre bajo el cerrojo
        # global (channel() y close_room()), y el objeto se captura al encolar
        # para poder usarlo sin el cerrojo al drenar.
        self._channels = {}
        self._local = threading.local()

    # --- Dentro del cerrojo global -------------------------------------------

    def channel(self, room_id):
        """Canal de una sala, creado si no existe. Solo bajo el cerrojo global."""
        channel = self._channels.get(room_id)
        if channel is None:
            channel = self._channels[room_id] = RoomChannel()
        return channel

    def add_update(self, room_id, sid, payload):
        """Encola el snapshot de una sala para un jugador (`game_update`)."""
        channel = self.channel(room_id)
        self._batch().add_update(room_id, channel, sid, channel.next_version(), payload)

    def add_direct(self, room_id, event, payload, to):
        """Encola un mensaje que no se puede coalescer ni descartar (chat,
        join_error, latido). `room_id` solo da orden respecto al resto de tráfico
        de esa sala; si la sala ya no existe, se emite sin orden."""
        self._batch().directs.append((self._channels.get(room_id), event, payload, to))

    def add_reply(self, callback, payload):
        """Encola una respuesta a una petición con callback (interfaz de cartas
        custom). El callback escribe en el socket del cliente que pregunta."""
        self._batch().replies.append((callback, payload))

    def deferred(self, callback):
        """Versión diferida de un callback: llama a la función devuelta y lo que
        salga se emite al cerrar la transacción, no dentro del cerrojo."""
        return lambda payload: self.add_reply(callback, payload)

    def close_room(self, room_id):
        """Olvida el estado de emisión de una sala borrada. Los mensajes que
        otro hilo ya tenía en la mano se descartan al drenar: sus jugadores ya
        no existen. Mejor esfuerzo: un drenaje que ya tiene el cerrojo del canal
        puede colarse, y emitirle a un sid muerto es un no-op de todas formas."""
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
                "envuelve la llamada en @transactional o en Outbox.transaction()")
        return stack[-1]

    # --- Transacción ----------------------------------------------------------

    def run(self, fn, obj, *args, **kwargs):
        with self.transaction():
            return fn(obj, *args, **kwargs)

    @contextmanager
    def transaction(self):
        """Transacción explícita, para los call sites que no pueden ser un
        método decorado (típicamente: dormir ANTES de tomar el cerrojo, como el
        auto-avance de ronda, que si no retendría el cerrojo 5 segundos).

        Anidamiento: la transacción más interna encola en la más externa y no
        drena nada por su cuenta, así que la E/S nunca ocurre con el cerrojo
        global tomado, ni siquiera al anidar."""
        with self._lock:
            batch = EmitBatch()
            stack = self._stack()
            stack.append(batch)
            try:
                yield batch
            finally:
                stack.pop()
                if stack:
                    # Anidada: se entrega a la transacción que la rodea, que es
                    # la única que puede drenar sin tener el cerrojo tomado.
                    stack[-1].merge(batch)
                propia = not stack
        # Fuera del `with self._lock`: aquí es donde se escribe en el socket.
        if propia:
            self.flush(batch)

    # --- Fuera del cerrojo global --------------------------------------------

    def flush(self, batch):
        """Escribe en el socket lo que encoló una transacción. Nunca con el
        cerrojo global tomado: el estado ya está serializado en los payloads y
        no se vuelve a leer nada del `Room`."""
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
