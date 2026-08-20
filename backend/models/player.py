class Player:
    def __init__(self, sid, name, initial_hand=None, is_spectator=False):
        self.sid = sid
        self.name = name
        self.points = 0
        self.hand = initial_hand if initial_hand is not None else []
        self.played_card = None
        self.is_connected = True
        self.waiting_next_round = is_spectator

    def to_dict(self):
        return {
            'id': self.sid,
            'name': self.name,
            'points': self.points,
            'has_played': self.played_card is not None,
            'waiting_next_round': self.waiting_next_round
        }
