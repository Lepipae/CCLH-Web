import random
from models.player import Player

class Room:
    def __init__(self, room_id, leader_sid, available_whites, available_blacks):
        self.room_id = room_id
        self.leader = leader_sid
        self.state = 'waiting'  # waiting, playing, judging, round_end
        self.czar = None
        
        self.players = {}  # sid: Player
        self.join_order = []
        
        self.available_whites = list(available_whites)
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

    def add_player(self, player):
        self.players[player.sid] = player
        if player.sid not in self.join_order:
            self.join_order.append(player.sid)
            
    def remove_player(self, sid):
        if sid in self.players:
            self.players[sid].is_connected = False
            self.renew_votes.discard(sid)

    def get_active_players(self):
        return [p for p in self.players.values() if p.is_connected]

    def deal_cards(self, count):
        if len(self.available_whites) < count:
            return []
        drawn = random.sample(self.available_whites, count)
        for c in drawn:
            self.available_whites.remove(c)
        return drawn

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
                if c.get('revealed') or self.state == 'round_end':
                    played_cards_public.append({
                        'id': c['id'], 
                        'cards': c['cards'], 
                        'revealed': True, 
                        'sid': c['sid'] if self.state == 'round_end' else None
                    })
                else:
                    played_cards_public.append({'id': c['id'], 'cards': [], 'revealed': False, 'sid': None})
        elif self.state == 'playing':
            # Mostrar las cartas boca abajo mientras la gente está jugando
            for c in self.played_cards:
                played_cards_public.append({'id': c['id'], 'cards': [], 'revealed': False, 'sid': None})
                    
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
            'total_active': len(self.get_active_players()),
            'options': self.options
        }
        
        if for_sid in self.players and self.players[for_sid].is_connected:
            payload['hand'] = self.players[for_sid].hand
            
        return payload
