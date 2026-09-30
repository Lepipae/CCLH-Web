"""Presencia en dos fases: vivo -> sospechoso -> purgado, con periodo de gracia.

Un solo umbral no puede decir dos cosas a la vez, y aquí hacía falta decir las
dos: cuándo sospechar que alguien se ha ido, y cuánto se le concede para
demostrar que sigue ahí. Con uno solo la purga era inseparable de la sospecha:
en la pasada en la que el reaper se enteraba del silencio, lo echaba. Sin aviso,
sin segunda oportunidad y sin nada que el cliente pudiera mostrar.

    vivo --(silencio >= suspect_after)--> sospechoso --(gracia cumplida)--> purgado
      ^                                          |
      +--------(responde: pong o cualquier tráfico)---+

La fase sospechosa es un periodo de gracia con fecha de caducidad, no una espera
pasiva: al entrar en ella se avisa a la sala (`presence_alert`) y se reta al
propio cliente (`presence_challenge`), de modo que un navegador que vuelve de
una pestaña suspendida recupera su asiento sin que la mesa se entere.

Números con nombre, no constantes repartidas: los dos umbrales viven en una
`PresencePolicy` y el plazo total es una propiedad derivada (`purge_after`), no
un tercer número que mantener sincronizado a mano. El desfase del escaneo va
siempre hacia el lado conservador: nunca se purga a nadie antes de tiempo, que es
lo que importa cuando el error es dejar a un jugador sin asiento.

La máquina la comparten jugadores y salas porque el problema es el mismo; lo
único que cambia es el reloj que la alimenta (`quiet_since`). Aquí no hay E/S:
esto solo lleva la cuenta de un plazo, y el envío lo hace models/outbox.py.
"""
import os
import time

# Estados. Son cadenas porque viajan tal cual en los eventos al cliente: un
# `state` que el frontend no reconoce tiene que ser al menos legible en un log.
VIVO = 'vivo'
SOSPECHOSO = 'sospechoso'
PURGADO = 'purgado'


class PresencePolicy:
    """Los dos umbrales de una entidad: cuándo se avisa y cuánto se concede para
    volver. El plazo real es la suma de los dos más un escaneo de desfase."""

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
        """Lee `{PREFIX}_SUSPECT_SECONDS` y `{PREFIX}_GRACE_SECONDS`. Un valor no
        numérico o negativo no impide el arranque (se avisa por log y se cae al
        valor nominal), porque un despliegue con la variable mal puesta tiene que
        levantar igual."""
        def valor(sufijo, defecto):
            crudo = os.environ.get(f"{prefix}_{sufijo}")
            if crudo is None:
                return defecto
            try:
                leido = float(crudo)
            except (TypeError, ValueError):
                return defecto
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
    """Contadores de fases, a cero. Los lleva el GameManager como estado propio."""
    return {
        'jugadores': {VIVO: 0, SOSPECHOSO: 0, PURGADO: 0},
        'salas': {VIVO: 0, SOSPECHOSO: 0, PURGADO: 0},
    }


class Presence:
    """Fase, momento de la transición y reloj del silencio (`quiet_since`). Éste
    es `None` cuando no hay silencio que juzgar —para una sala, que tiene alguien
    dentro—, que es lo que impide acusar a una sala que acaba de llenarse.

    Los métodos son idempotentes y se llaman bajo el cerrojo global: los de
    transición los invoca el reaper, los de reloj los handlers de juego.
    """

    __slots__ = ('state', 'since', 'quiet_since', 'deadline', 'grace')

    def __init__(self, state=VIVO, since=None, quiet_since=None):
        self.state = state
        # Momento de la transición al estado actual; `None` en VIVO, que no es una
        # transición que nadie tenga que ver.
        self.since = since
        self.quiet_since = quiet_since
        # El plazo prometido al acusar y su longitud, congelados aquí y no
        # reconsultados a la política: son la promesa concreta que se le
        # centraliza al cliente, y no puede cambiar aunque mañana se reconfiguren
        # los umbrales con la entidad ya en gracia.
        self.deadline = None
        self.grace = None

    # --- Reloj de silencio ---------------------------------------------------

    def signal(self, now=None):
        """Señal de vida: renueva el reloj y perdona la sospecha. True si ha
        transiciónado, para que el llamante sepa si hay algo que anunciar."""
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
        """Ya no hay silencio: reloj a cero, y perdona igual que `signal`. Se
        distingue en que no hay instante que registrar, porque para una sala
        "hay gente" es su estado normal y no un evento."""
        self.quiet_since = None
        if self.state == SOSPECHOSO:
            self.state = VIVO
            self.since = None
            self.deadline = None
            self.grace = None
            return True
        return False

    def set_quiet_since(self, moment):
        """Fija el reloj sin perdonar la sospecha. Es el asignador de `last_seen` /
        `empty_since`: ageingar la ranciedad no es una demostración de vida, para
        eso está `signal()`."""
        self.quiet_since = moment

    # --- Fases ---------------------------------------------------------------

    def accuse(self, now, policy):
        """Vivo -> sospechoso si el silencio supera `suspect_after`. True solo en
        la pasada en la que se entra: si no, un reaper lento se concedería gracia
        infinita. `since` cuenta desde la detección, que es lo único que el
        cliente puede ver y lo único que garantiza un plazo completo."""
        if self.state != VIVO:
            return False
        if self.quiet_since is None:
            return False  # hay alguien dentro: nada que juzgar
        if now - self.quiet_since < policy.suspect_after:
            return False
        self.state = SOSPECHOSO
        self.since = now
        self.grace = policy.grace
        self.deadline = now + policy.grace
        return True

    def purge_due(self, now):
        """Nunca True en VIVO ni en PURGADO: la purga es irreversible y solo
        puede llegar desde la fase intermedia, que es lo que garantiza que a nadie
        se le expulsa sin habérselo avisado antes."""
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
        """Margen que le queda: 0 si venció, None si no está en gracia."""
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - now)

    def to_dict(self, now=None):
        """Foto publicable, en el `game_update` y en los `presence_alert`, para
        que el cliente pueda pintar la fase sin haber visto la transición. El
        plazo solo aparece si la máquina lo tiene: al purgar, el aviso conserva
        el vencimiento que acaba de cumplirse, que es lo que le dice a la mesa
        cuánto llevaba callado el que se va."""
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
