import time
from models.presence import Presence
from models.player import Player
from models.deck import Deck

class Room:
    def __init__(self, room_id, leader_sid, available_whites, available_blacks):
        self.room_id = room_id
        self.leader = leader_sid
        self.state = 'waiting'  # waiting, playing, judging, round_end
        self.czar = None
        
        self.players = {}  # sid: Player
        self.join_order = []
        
        # El mazo es el único dueño de las cartas blancas: reparte, recoge y
        # audita. No hay ninguna otra lista de blancas en la sala, así que no
        # puede existir un contador paralelo que se quede viejo.
        self.deck = Deck(available_whites)
        self.available_blacks = list(available_blacks)
        self.black_card = None
        self.played_cards = []
        
        self.renew_votes = set()
        self.options = {
            'hand_size': 10,
            'renew_threshold': 0.75,
            'turn_order': 'winner'
        }
        
        self.last_winner = None
        self.last_winning_card = None
        self.last_winner_name = None

        # Momento (time.time()) en el que la sala se quedó sin jugadores conectados.
        # None mientras haya alguien conectado. Es el reloj de silencio de la
        # máquina de presencia de la sala: el reaper lo mide igual que el last_seen
        # de un jugador, así que una sala vacía pasa por vivo -> sospechoso ->
        # purgada con los mismos umbrales con nombre y no con un TTL propio.
        # `empty_since` queda como atributo de lectura/escritura porque media
        # partida lo asigna; el estado de verdad está en `presence`.
        self.presence = Presence()
        self.empty_since = None

        # ¿Llegó a haber una partida en esta sala? Lo pone `_start_game_locked` y
        # NO vuelve a False: es lo que distingue "una sala que alguien usó y
        # cuyo marcador merece conservarse" de "un lobby que se creó y se quedó
        # vacío", que es la vía barata de gastar memoria y se purga antes.
        # Ojo: `state` no sirve para esto, porque al quedarse sin jugadores la
        # sala vuelve a 'waiting' por el revert, y eso no borra lo ocurrido.
        self.ha_empezado = False

    # Igual que en Player: `empty_since` es la vista del reloj de la máquina.
    # Asignar None significa "hay alguien dentro", que es como un viva: perdona
    # la sospecha de la sala y cancela su periodo de gracia, de modo que quien
    # vuelve durante la gracia recupera la sala tal cual estaba.
    @property
    def empty_since(self):
        return self.presence.quiet_since

    @empty_since.setter
    def empty_since(self, moment):
        if moment is None:
            self.presence.clear_silence()
        else:
            self.presence.set_quiet_since(moment)

    def add_player(self, player):
        self.players[player.sid] = player
        if player.sid not in self.join_order:
            self.join_order.append(player.sid)
            
    def remove_player(self, sid):
        if sid in self.players:
            self.players[sid].is_connected = False
            self.renew_votes.discard(sid)

    def get_active_players(self):
        active = [p for p in self.players.values() if p.is_connected]
        self.empty_since = None if active else (self.empty_since or time.time())
        return active

    def deal_cards(self, count):
        """Reparte del mazo. Menos cartas de las pedidas devuelve []."""
        return self.deck.deal(count)

    @property
    def deck_size(self):
        """Total de cartas blancas que existen en la sala. Derivado del mazo:
        inyectar cartas custom lo incrementa solo, sin que nadie tenga que
        acordarse de tocar un contador paralelo."""
        return self.deck.size

    def audit_cards(self):
        """Problemas de conservación de cartas. Vacío = todo cuadra.

        Ya no se ejecuta en el reaper: la suite de tests lo corre después de
        cada acción, que es donde un fallo de conservación se puede atribuir a
        un cambio concreto en vez de aparecer como un WARNING anónimo 15 s
        después."""
        held = []
        for p in self.players.values():
            held.extend((f"la mano de {p.name}", card) for card in p.hand)
        for played in self.played_cards:
            who = self.players[played['sid']].name if played['sid'] in self.players else played['sid']
            held.extend((f"la jugada de {who}", card) for card in played.get('cards', []))
        return self.deck.audit(held)

    def to_dict(self, for_sid):
        public_players = []
        for p in self.players.values():
            if p.is_connected:
                p_dict = p.to_dict()
                p_dict['is_czar'] = (p.sid == self.czar)
                public_players.append(p_dict)
                
        played_cards_public = []
        if self.state in ['judging', 'round_end']:
            for c in self.played_cards:
                votes = c.get('votes', set())
                is_revealed = bool(c.get('revealed') or self.state == 'round_end')
                played_cards_public.append({
                    'id': c['id'], 
                    'cards': c['cards'] if is_revealed else [], 
                    'revealed': is_revealed, 
                    'sid': c['sid'] if self.state == 'round_end' else None,
                    'votes_count': len(votes),
                    'has_voted': for_sid in votes,
                    'is_mine': (c.get('sid') == for_sid)
                })
        elif self.state == 'playing':
            # Mostrar las cartas boca abajo mientras la gente está jugando
            for c in self.played_cards:
                played_cards_public.append({
                    'id': c['id'], 
                    'cards': [], 
                    'revealed': False, 
                    'sid': None,
                    'votes_count': 0,
                    'has_voted': False,
                    'is_mine': (c.get('sid') == for_sid)
                })
                    
        active_players = self.get_active_players()
        voters = [p for p in active_players if p.sid != self.czar and not p.waiting_next_round]
        voters_count = len(voters) if voters else len(active_players)

        payload = {
            'room_id': self.room_id,
            'state': self.state,
            'black_card': self.black_card,
            'players': public_players,
            'czar': self.czar,
            'leader': self.leader,
            'played_cards': played_cards_public,
            'winner_sid': self.last_winner,
            'renew_votes': len(self.renew_votes),
            'has_voted_renew': for_sid in self.renew_votes,
            'total_active': voters_count,
            'options': self.options
        }
        
        if for_sid in self.players and self.players[for_sid].is_connected:
            # COPIA, no la lista viva: la emisión se difiere fuera del cerrojo,
            # así que entre que se construye el payload y que sale por el
            # socket la mano puede haber cambiado. Sin esta copia, el cliente
            # recibiría una mezcla de dos estados distintos.
            payload['hand'] = list(self.players[for_sid].hand)

        return payload
