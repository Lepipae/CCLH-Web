import time

from models.presence import Presence


class Player:
    def __init__(self, sid, name, initial_hand=None, is_spectator=False):
        self.sid = sid
        self.name = name
        self.points = 0
        self.hand = initial_hand if initial_hand is not None else []
        self.played_card = None
        self.is_connected = True
        self.waiting_next_round = is_spectator

        # Máquina de presencia (models/presence.py): vivo -> sospechoso ->
        # purgado. Ella lleva el reloj del silencio (`quiet_since`), que es lo
        # que el reaper mide para acusar, y el plazo de gracia, que es lo que
        # impide que la sospecha sea una sentencia en la misma pasada.
        self.presence = Presence(quiet_since=time.time())

        # Cuántos latidos ha respondido este cliente. No es un seguro de vida,
        # es el selector de política: mientras sea 0 el reaper juzga a este
        # jugador con PRESENCE_SIN_PRUEBA (más margen, pero judgment incluido)
        # y a partir del primero con PRESENCE. Antes de esto valía como
        # exención permanente y un cliente que nunca contestaba fijaba su sala
        # para siempre, porque su transporte seguía vivo aunque su JS no.
        self.heartbeat_pongs = 0

    # `last_seen` es la ÚNICA fuente del reloj de silencio, y ahora vive dentro
    # de la máquina de presencia. Se conserva como atributo de lectura/escritura
    # porque media partida lo usa (el reaper lo mide, `_touch_player` lo
    # renueva), pero la asignación va por `set_quiet_since`: fijar el reloj no
    # es una demostración de vida, así que no perdona una sospecha. Quien de
    # verdad acaba de demostrar que está aquí llama a `presence.signal()`.
    @property
    def last_seen(self):
        return self.presence.quiet_since

    @last_seen.setter
    def last_seen(self, moment):
        self.presence.set_quiet_since(moment)

    def to_dict(self):
        return {
            'id': self.sid,
            'name': self.name,
            'points': self.points,
            'has_played': self.played_card is not None,
            'waiting_next_round': self.waiting_next_round,
            # Estado de presencia en la foto de la sala. Es determinista a
            # propósito (marcas absolutas, sin "queda" calculada en el
            # momento): el payload tiene que poder reconstruirse después y
            # dar exactamente lo mismo, y la cuenta atrás la lleva el evento
            # `presence_alert`, que sí se mide al emitirse.
            'presence': self.presence.to_dict(),
        }
