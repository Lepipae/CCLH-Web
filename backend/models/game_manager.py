import random
import uuid
import json
import difflib
from models.room import Room
from models.player import Player

class GameManager:
    def __init__(self, socketio, white_cards, black_cards):
        self.rooms = {}
        self.socketio = socketio
        self.global_white_cards = white_cards
        self.global_black_cards = black_cards

    def send_room_update(self, room_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            for sid, p in room.players.items():
                if p.is_connected:
                    payload = room.to_dict(for_sid=sid)
                    self.socketio.emit('game_update', payload, to=sid)

    def send_chat_system(self, room_id, message):
        self.socketio.emit('chat_message', {'msg': message, 'system': True}, to=room_id)

    def join_game(self, sid, name, room_id):
        if room_id in self.rooms:
            # Check duplicates
            for existing_sid, p in self.rooms[room_id].players.items():
                if p.name == name and p.is_connected and existing_sid != sid:
                    self.socketio.emit('join_error', {'message': 'Ese nombre ya está en uso en esta sala.'}, to=sid)
                    return False

        if room_id not in self.rooms:
            self.rooms[room_id] = Room(room_id, sid, self.global_white_cards, self.global_black_cards)

        room = self.rooms[room_id]

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

        print(f"{name} se unió a {room_id}")
        self.send_room_update(room_id)
        return True

    def disconnect(self, sid):
        for room_id, room in self.rooms.items():
            if sid in room.players:
                room.remove_player(sid)
                active = room.get_active_players()
                if active:
                    if room.leader == sid:
                        room.leader = active[0].sid
                    if room.state == 'playing':
                        active_non_czars = [pl for pl in active if pl.sid != room.czar and not pl.waiting_next_round]
                        if active_non_czars and all(pl.played_card is not None for pl in active_non_czars):
                            room.state = 'judging'
                            random.shuffle(room.played_cards)
                    self.send_room_update(room_id)

    def start_game(self, sid, room_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if room.state == 'waiting' and room.leader == sid:
                active = room.get_active_players()
                if len(active) >= 2:
                    room.state = 'playing'
                    
                    hand_size = room.options['hand_size']
                    for p in active:
                        p.played_card = None
                        p.waiting_next_round = False
                        faltan = hand_size - len(p.hand)
                        if faltan > 0:
                            p.hand.extend(room.deal_cards(faltan))
                            
                turn_order = room.options['turn_order']
                if turn_order == 'sequential':
                    for jsid in room.join_order:
                        if jsid in room.players and room.players[jsid].is_connected:
                            room.czar = jsid
                            break
                else:
                    room.czar = random.choice(active).sid
                    
                if not room.available_blacks:
                    room.available_blacks = list(self.global_black_cards)
                
                drawn = random.choice(room.available_blacks)
                room.available_blacks.remove(drawn)
                room.black_card = drawn
                self.send_room_update(room_id)

    def update_options(self, sid, room_id, data):
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

    def play_card(self, sid, room_id, card_index):
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

    def reveal_card(self, sid, room_id, sub_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if room.state == 'judging' and sid == room.czar:
                for c in room.played_cards:
                    if c.get('id') == sub_id:
                        c['revealed'] = True
                        break
                self.send_room_update(room_id)

    def choose_winner(self, sid, room_id, sub_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if room.state == 'judging' and sid == room.czar:
                winner_sid = None
                for c in room.played_cards:
                    if c.get('id') == sub_id:
                        winner_sid = c['sid']
                        break
                        
                if winner_sid and winner_sid in room.players:
                    room.players[winner_sid].points += 1
                    room.state = 'round_end'
                    room.last_winner = winner_sid
                    room.last_winning_card = [c for c in room.played_cards if c.get('id') == sub_id][0]['cards']
                    room.last_winner_name = room.players[winner_sid].name
                    self.send_room_update(room_id)
                    
                    self.socketio.start_background_task(self.auto_next_round, room_id, sid)

    def auto_next_round(self, room_id, old_czar_sid):
        self.socketio.sleep(5)
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if room.state == 'round_end' and room.czar == old_czar_sid:
                self.advance_to_next_round(room_id)

    def force_next_round(self, sid, room_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if room.state == 'round_end' and room.czar == sid:
                self.advance_to_next_round(room_id)

    def advance_to_next_round(self, room_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            
            turn_order = room.options['turn_order']
            active_players = room.get_active_players()
            
            if turn_order == 'random':
                if active_players:
                    room.czar = random.choice(active_players).sid
            elif turn_order == 'sequential':
                connected_ordered = [s for s in room.join_order if s in room.players and room.players[s].is_connected]
                if connected_ordered:
                    if room.czar in connected_ordered:
                        idx = connected_ordered.index(room.czar)
                        room.czar = connected_ordered[(idx + 1) % len(connected_ordered)]
                    else:
                        room.czar = connected_ordered[0]
            else: # winner
                room.czar = room.last_winner if room.last_winner else room.czar
                
            room.state = 'playing'
            room.played_cards = []
            
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

    def vote_renew(self, sid, room_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if sid in room.players and room.players[sid].is_connected:
                room.renew_votes.add(sid)
                
                active = room.get_active_players()
                if active:
                    thresh = room.options['renew_threshold']
                    if (len(room.renew_votes) / len(active)) >= thresh:
                        room.renew_votes.clear()
                        for p in active:
                            room.available_whites.extend(p.hand)
                            p.hand = []
                            
                        random.shuffle(room.available_whites)
                        hand_size = room.options['hand_size']
                        for p in active:
                            p.hand = room.deal_cards(hand_size)
                            p.played_card = None
                            
                        room.played_cards = []
                        if room.state == 'judging':
                            room.state = 'playing'
                            
                self.send_room_update(room_id)

    def change_black_card(self, sid, room_id):
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

    def add_custom_card(self, card_type, text, pick, respond_cb):
        text = text.strip()
        if not text:
            respond_cb({'success': False, 'message': 'El texto está vacío.'})
            return
            
        def check_sim(new_txt, lst, thresh=0.8):
            n_l = new_txt.lower()
            for c in lst:
                tc = c if isinstance(c, str) else c.get('text', '')
                if difflib.SequenceMatcher(None, n_l, tc.lower().strip()).ratio() >= thresh:
                    return True, tc
            return False, None
            
        try:
            with open('DataScraping/CAH-es-set-actualizado.json', 'r', encoding='utf-8') as f:
                full_data = json.load(f)
        except Exception:
            try:
                with open('www/Cartas/CAH-es-set.json', 'r', encoding='utf-8') as f:
                    full_data = json.load(f)
            except Exception as e:
                respond_cb({'success': False, 'message': f'Error leyendo JSON: {str(e)}'})
                return
                
        if card_type == 'white':
            is_sim, match = check_sim(text, self.global_white_cards)
            if is_sim:
                respond_cb({'success': False, 'message': f'Muy similar a: "{match}"'})
                return
            self.global_white_cards.append(text)
            full_data['whiteCards'].append(text)
            for room in self.rooms.values():
                room.available_whites.append(text)
                random.shuffle(room.available_whites)
        elif card_type == 'black':
            is_sim, match = check_sim(text, self.global_black_cards)
            if is_sim:
                respond_cb({'success': False, 'message': f'Muy similar a: "{match}"'})
                return
            new_c = {'text': text, 'pick': pick}
            self.global_black_cards.append(new_c)
            full_data['blackCards'].append(new_c)
            for room in self.rooms.values():
                room.available_blacks.append(new_c)
                random.shuffle(room.available_blacks)
        else:
            respond_cb({'success': False, 'message': 'Tipo inválido.'})
            return
            
        try:
            with open('DataScraping/CAH-es-set-actualizado.json', 'w', encoding='utf-8') as f:
                json.dump(full_data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
            
        respond_cb({'success': True})

    def add_room_cards(self, room_id, cards_str):
        if room_id in self.rooms:
            new_cards = [c.strip() for c in cards_str.split(',') if c.strip()]
            if new_cards:
                self.rooms[room_id].available_whites.extend(new_cards)
                random.shuffle(self.rooms[room_id].available_whites)
                self.send_chat_system(room_id, f'Se han añadido {len(new_cards)} cartas personalizadas a la sala.')

    def send_chat(self, sid, room_id, msg):
        if room_id in self.rooms and msg:
            room = self.rooms[room_id]
            if sid in room.players:
                pname = room.players[sid].name
                self.socketio.emit('chat_message', {'msg': msg, 'sender': pname, 'system': False}, to=room_id)
