"""Presencia en dos fases: vivo -> sospechoso -> purgado, con periodo de gracia.

Antes esto era un único número mágico por entidad: `ZOMBIE_TIMEOUT_SECONDS` para
los jugadores y `ROOM_TTL_EMPTY` para las salas. Un solo número no puede decir
dos cosas a la vez, y aquí hacía falta decir las dos:

1. Cuándo hay motivo para sospechar que alguien se ha ido.
2. Cuánto se le concede para demostrar que está aquí.

Con un solo umbral, la purga era inseparable de la sospecha: en la pasada en la
que el reaper se daba cuenta de que un cliente llevaba 300 s callado, lo echaba.
No había ni un instante de aviso, ni un segundo intento, ni nada que el cliente
pudiera mostrar. Con dos fases el mismo silencio se convierte en un proceso
visible y reversible:

    vivo --(silencio >= suspect_after)--> sospechoso --(gracia cumplida)--> purgado
      ^                                          |
      +--------(responde: pong o cualquier tráfico)---+

La fase sospechosa es un PERIODO DE GRACIA con fecha de caducidad, no una espera
pasiva: en cuanto se entra en ella se emite un aviso a la sala y un reto
(`presence_challenge`) al propio cliente, de modo que un navegador que vuelve de
una pestaña suspendida recupera su asiento en el sitio y la mesa no se entera de
nada. Solo si la gracia se agota se libera el asiento.

Números, no constantes repartidas: los dos umbrales viven en una
`PresencePolicy` con nombre, el plazo total es una propiedad derivada
(`purge_after`) y no un tercer número que mantener sincronizado a mano. El
envío se sigue haciendo por la caja de salida (models/outbox.py): desde aquí
solo se describe el estado, nadie escribe en el socket.

La máquina la comparten jugadores y salas porque el problema es el mismo; lo
único que cambia es el reloj que la alimenta (`quiet_since`: el último pong para
un jugador, el momento de quedarse vacía para una sala).
"""
import os
import time

# Estados. Son cadenas porque viajan tal cual en los eventos al cliente: un
# `state` que el frontend no reconoce tiene que ser al menos legible en un log.
VIVO = 'vivo'
SOSPECHOSO = 'sospechoso'
PURGADO = 'purgado'


class PresencePolicy:
    """Los dos umbrales de una entidad, con nombre y con su invariante.

    - `suspect_after`: silencio a partir del cual se suspiciona. Marca CUÁNDO se
      avisa, no cuándo se purga.
    - `grace`: margen desde el aviso hasta la purga. Es lo que hace el proceso
      reversible: durante este tiempo el cliente puede volver y conservarlo todo.

    El plazo real hasta la purga es `purge_after` (suspect_after + grace) MÁS
    hasta un escaneo de desfase, porque el reaper solo decide cada
    ROOM_REAP_INTERVAL segundos. El desfase va siempre hacia el lado
    conservador: nunca se purga a nadie antes de tiempo, y eso es lo que
    importa cuando el error es dejar a un jugador sin asiento.
    """

    __slots__ = ('suspect_after', 'grace', 'nombre')

    def __init__(self, suspect_after, grace, nombre='presencia'):
        if suspect_after < 0 or grace < 0:
            raise ValueError(
                f"umbrales de {nombre} negativos: sospechar en {suspect_after}s "
                f"con {grace}s de gracia no describe ningún plazo real")
        self.suspect_after = float(suspect_after)
        self.grace = float(grace)
        self.nombre = nombre

    @classmethod
    def from_env(cls, prefix, suspect_after, grace):
        """Lee `{PREFIX}_SUSPECT_SECONDS` y `{PREFIX}_GRACE_SECONDS`.

        Un valor no numérico o negativo no impide el arranque: se avisa por log y
        se cae al valor por defecto. Un despliegue con la variable mal puesta
        tiene que arrancar igual, solo que con la política nominal.
        """
        def valor(sufijo, defecto):
            crudo = os.environ.get(f"{prefix}_{sufijo}")
            if crudo is None:
                return defecto
            try:
                leido = float(crudo)
            except (TypeError, ValueError):
                return defecto
            # Un umbral negativo no es un plazo: es una configuración
            # equivocada, y con ella la máquina expulsaría a la primera pasada.
            return leido if leido >= 0 else defecto

        return cls(valor("SUSPECT_SECONDS", suspect_after),
                   valor("GRACE_SECONDS", grace),
                   nombre=prefix.lower())

    @property
    def purge_after(self):
        """Plazo total de silencio hasta la purga. Derivado, no configurado."""
        return self.suspect_after + self.grace

    def __repr__(self):
        return (f"PresencePolicy({self.nombre}: sospecha a los "
                f"{self.suspect_after:g}s, purga a los {self.purge_after:g}s "
                f"[{self.grace:g}s de gracia])")


def contadores():
    """Contadores de fases, a cero. Los lleva el GameManager como estado propio
    y los tests los reinician con esta misma función."""
    return {
        'jugadores': {VIVO: 0, SOSPECHOSO: 0, PURGADO: 0},
        'salas': {VIVO: 0, SOSPECHOSO: 0, PURGADO: 0},
    }


class Presence:
    """Estado de presencia de una entidad (un jugador o una sala).

    Guarda el estado, cuándo entró en él, y el reloj del silencio
    (`quiet_since`). `quiet_since is None` significa "no hay silencio que
    juzgar": para una sala, que tiene alguien dentro; es lo que impide acusar a
    una sala que acaba de llenarse.

    Todos los métodos son idempotentes y se llaman bajo el cerrojo global del
    GameManager (los de transición los invoca el reaper, los de `signal` y
    `clear_silence` los handlers de juego). Aquí no hay E/S: esto solo lleva la
    cuenta de un plazo.
    """

    __slots__ = ('state', 'since', 'quiet_since', 'deadline', 'grace')

    def __init__(self, state=VIVO, since=None, quiet_since=None):
        self.state = state
        # Momento de la transición al estado actual. `None` en VIVO: el estado
        # inicial no es una transición que nadie haya que ver.
        self.since = since
        self.quiet_since = quiet_since
        # Plazo de gracia que se prometió al acusar, y su longitud. Se guardan
        # en la máquina y no se vuelven a consultar a la política porque son la
        # promesa concreta que se le المركزي al cliente: el aviso dice "hasta
        # las T", y eso no puede cambiar aunque mañana se reconfiguren los
        # umbrales con la sala ya en periodo de gracia.
        self.deadline = None
        self.grace = None

    # --- Reloj de silencio ---------------------------------------------------

    def signal(self, now=None):
        """Hubo señal de vida: renueva el reloj y perdona la sospecha.

        Devuelve True si ha transiciónado la sospecha, es decir, si ha cambiado
        el estado (de sospechoso a vivo). Así el llamante sabe si hay algo que
        anunciar. Es la vía de regreso del periodo de gracia: un pong, un
        `join_game`, cualquier tráfico real del cliente.
        """
        now = time.time() if now is None else now
        self.quiet_since = now
        if self.state == SOSPECHOSO:
            self.state = VIVO
            self.since = now
            self.deadline = None
            self.grace = None
            return True
        return False

    def clear_silence(self):
        """Ya no hay silencio (hay alguien en la sala): reloj a cero.

        Distinto de `signal` en que no hay instante que registrar: para una sala
        "hay gente" no es un evento fechado, es su estado normal. Perdona igual
        que `signal`: una sala vacía sospechosa a la que vuelve alguien sale del
        periodo de gracia y su estado de partida se conserva.
        """
        self.quiet_since = None
        if self.state == SOSPECHOSO:
            self.state = VIVO
            self.since = None
            self.deadline = None
            self.grace = None
            return True
        return False

    def set_quiet_since(self, moment):
        """Fija el reloj de silencio a un valor concreto (sin perdonar).

        Es el asignador de `last_seen` / `empty_since`: quien escribe ahí sabe
        exactamente qué instante quiere dejar, y eso no es una demostración de
        vida (los tests agingan la ranciedad a mano, y fijar el reloj tampoco
        debería perdonar una sospecha). Para "hubo señal de verdad" está
        `signal()`.
        """
        self.quiet_since = moment

    # --- Fases ---------------------------------------------------------------

    def accuse(self, now, policy):
        """Vivo -> sospechoso si el silencio ha superado `suspect_after`.

        Devuelve True solo en la pasada en la que se entra en la fase: las
        siguientes pasadas devuelven False aunque siga callado, porque el plazo
        de gracia ya está corriendo y no se puede ir empujando con cada escaneo
        (si no, un reaper lento se concedería un periodo de
        gracia infinito y la purga no ocurriría nunca).

        Quien llama decide si hay pruebas suficientes para acusar: la máquina no
        sabe nada de pongs, solo de relojes.
        """
        if self.state != VIVO:
            return False
        if self.quiet_since is None:
            return False  # hay alguien dentro: nada que juzgar
        if now - self.quiet_since < policy.suspect_after:
            return False
        self.state = SOSPECHOSO
        # `since` es el momento en que el reaper SE ENTERÓ, no el instante en
        # que empezó el silencio. Contabiliza desde la detección porque es lo
        # que el cliente puede ver (nadie le avisó antes) y porque solo
        # garantiza un plazo completo de gracia a partir del aviso.
        self.since = now
        self.grace = policy.grace
        self.deadline = now + policy.grace
        return True

    def purge_due(self, now):
        """Sospechoso y con la gracia cumplida.

        Nunca True en VIVO ni en PURGADO: la purga es irreversible y solo puede
        llegar desde la fase intermedia, que es justo lo que garantiza que a
        nadie se le expulsa sin habérselo avisado antes.
        """
        return (self.state == SOSPECHOSO
                and self.deadline is not None
                and now >= self.deadline)

    def purge(self, now):
        """Marca el estado terminal. El asiento ya lo libera el llamante."""
        self.state = PURGADO
        self.since = now
        self.deadline = None
        self.grace = None

    def segundos_para_purga(self, now):
        """Margen que le queda (0 si ya venció, None si no está en gracia)."""
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - now)

    def to_dict(self, now=None):
        """Foto publicable del estado. Va en el `game_update` de cada jugador y
        en los eventos `presence_alert`, para que el cliente pueda pintar el
        estado sin depender de haber visto la transición.

        Incluye el reloj (`since`) y el margen que queda, que es lo que
        transforma un aviso en algo accionable: no "Alice se ha ido" sino "Alice
        lleva 2 min sin responder; su asiento se libera en 54 s".
        """
        foto = {'state': self.state, 'since': self.since}
        if self.deadline is not None:
            foto['deadline'] = self.deadline
            foto['grace'] = self.grace
            if now is not None:
                foto['left'] = self.segundos_para_purga(now)
        return foto

    def __repr__(self):
        if self.deadline is not None:
            return f"Presence({self.state}, plazo hasta {self.deadline:.0f})"
        return f"Presence({self.state}, silencio desde {self.quiet_since})"
