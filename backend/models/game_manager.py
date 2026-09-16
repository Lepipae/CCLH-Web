import random
import threading
import time
import uuid
import json
import difflib
from models.room import Room
from models.player import Player

class GameManager:
    # Reaper: borra salas sin jugadores conectados tras este tiempo (segundos)
    ROOM_TTL_EMPTY = 60
    # Intervalo de escaneo del reaper en segundo plano (segundos)
    ROOM_REAP_INTERVAL = 15

    def __init__(self, socketio, white_cards, black_cards):
        self.rooms = {}
        self.socketio = socketio
        self.global_white_cards = white_cards
        self.global_black_cards = black_cards
        # Bajo async_mode='threading' los handlers corren en hilos reales y pueden
        # intercalarse a mitad de una mutación (reparto de cartas, cambio de estado,
        # import al mazo global...). Este cerrojo serializa TODAS las operaciones
        # del juego: granularidad gruesa a propósito, la partida es por turnos y el
        # coste es despreciable frente a la E/S de red. RLock (no Lock) porque hay
        # reentradas legítimas: choose_winner -> send_room_update, etc.
        self._lock = threading.RLock()
        self._start_room_reaper()

    def send_room_update(self, room_id):
        # Se ejecuta dentro del cerrojo del llamador; solo lee estado
        if room_id in self.rooms:
            room = self.rooms[room_id]
            for sid, p in room.players.items():
                if p.is_connected:
                    payload = room.to_dict(for_sid=sid)
                    self.socketio.emit('game_update', payload, to=sid)

    def send_chat_system(self, room_id, message):
        self.socketio.emit('chat_message', {'msg': message, 'system': True}, to=room_id)

    def join_game(self, sid, name, room_id):
        with self._lock:
            return self._join_game_locked(sid, name, room_id)

    def _join_game_locked(self, sid, name, room_id):
        if room_id in self.rooms:
            # Check duplicates
            for existing_sid, p in self.rooms[room_id].players.items():
                if p.name == name and p.is_connected and existing_sid != sid:
                    self.socketio.emit('join_error', {'message': 'Ese nombre ya está en uso en esta sala.'}, to=sid)
                    return False

        if room_id not in self.rooms:
            self.rooms[room_id] = Room(room_id, sid, self.global_white_cards, self.global_black_cards)

        room = self.rooms[room_id]
        room.empty_since = None

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

        self._heal_stale_roles(room)

        print(f"{name} se unió a {room_id}")
        self.send_room_update(room_id)
        return True

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
                self.advance_to_next_round(room.room_id)
            else:
                self._reassign_czar_after_disconnect(room)

    def disconnect(self, sid):
        with self._lock:
            self._disconnect_locked(sid)

    def _disconnect_locked(self, sid):
        for room_id, room in self.rooms.items():
            if sid in room.players:
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
                    self.send_room_update(room_id)
                else:
                    # Sala vacía: se marca el momento y el reaper la borrará si nadie vuelve
                    room.empty_since = time.time()

    def _pick_new_leader(self, room, active):
        """Nuevo líder al marcharse el actual: prefiere a un jugador conectado que
        no sea espectador (puede iniciar la partida); si solo quedan espectadores,
        el primero por orden de llegada. None si no queda nadie conectado."""
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
            for c in room.played_cards:
                room.available_whites.extend(c.get('cards', []))
            random.shuffle(room.available_whites)
            room.played_cards = []
            room.state = 'playing'
            for p in room.players.values():
                if not p.is_connected and p.played_card is not None:
                    p.played_card = None  # podrán volver a jugar al reconectar
        if room.state == 'round_end':
            # El recuento ya está cerrado: se avanza la ronda directamente
            # (el auto-avance pendiente quedará obsoleto: comprueba el czar antiguo)
            self.advance_to_next_round(room.room_id)
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

    def _revert_to_waiting(self, room):
        """Devuelve la sala a la pantalla de espera: con menos de dos jugadores
        reales no puede haber partida en curso."""
        room.state = 'waiting'
        for c in room.played_cards:
            room.available_whites.extend(c.get('cards', []))
        random.shuffle(room.available_whites)
        room.played_cards = []
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
        """Elimina las salas que llevan más de ROOM_TTL_EMPTY sin jugadores conectados."""
        with self._lock:
            now = time.time()
            expired = [rid for rid, room in self.rooms.items()
                       if room.empty_since is not None and now - room.empty_since >= self.ROOM_TTL_EMPTY]
            for room_id in expired:
                self._delete_room(room_id)
        if expired:
            print(f"Reaper: {len(expired)} sala(s) vacía(s) eliminada(s): {', '.join(expired)}")

    def _delete_room(self, room_id):
        room = self.rooms.pop(room_id, None)
        if not room:
            return
        room.players.clear()
        room.played_cards = []
        room.available_whites = []
        room.available_blacks = []
        room.renew_votes.clear()
        room.join_order = []

    def start_game(self, sid, room_id):
        with self._lock:
            self._start_game_locked(sid, room_id)

    def _start_game_locked(self, sid, room_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if room.state == 'waiting' and room.leader == sid:
                active = room.get_active_players()
                if len(active) >= 2:
                    room.state = 'playing'
                    room.renew_votes.clear()
                    
                    hand_size = room.options['hand_size']
                    for p in active:
                        p.played_card = None
                        p.waiting_next_round = False
                        faltan = hand_size - len(p.hand)
                        if faltan > 0:
                            p.hand.extend(room.deal_cards(faltan))
                            
                if len(active) < 2:
                    # Sin jugadores suficientes no se inicia: no se tocan el czar,
                    # la carta negra ni el estado de la sala
                    return

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
                self.send_room_update(room_id)

    def update_options(self, sid, room_id, data):
        with self._lock:
            self._update_options_locked(sid, room_id, data)

    def _update_options_locked(self, sid, room_id, data):
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
        with self._lock:
            self._play_card_locked(sid, room_id, card_index)

    def _play_card_locked(self, sid, room_id, card_index):
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
        with self._lock:
            self._reveal_card_locked(sid, room_id, sub_id)

    def _reveal_card_locked(self, sid, room_id, sub_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if room.state == 'judging' and sid == room.czar:
                for c in room.played_cards:
                    if c.get('id') == sub_id:
                        c['revealed'] = True
                        break
                self.send_room_update(room_id)

    def choose_winner(self, sid, room_id, sub_id):
        with self._lock:
            self._choose_winner_locked(sid, room_id, sub_id)

    def _choose_winner_locked(self, sid, room_id, sub_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if room.state == 'judging' and sid == room.czar:
                winner_sid = None
                for c in room.played_cards:
                    if c.get('id') == sub_id:
                        winner_sid = c['sid']
                        break
                        
                if winner_sid and winner_sid in room.players:
                    # No premiar a quien se ha desconectado durante la ronda:
                    # last_winner apuntaría a un sid muerto y congelaría la rotación
                    if not room.players[winner_sid].is_connected:
                        return
                    room.players[winner_sid].points += 1
                    room.state = 'round_end'
                    room.last_winner = winner_sid
                    room.last_winning_card = [c for c in room.played_cards if c.get('id') == sub_id][0]['cards']
                    room.last_winner_name = room.players[winner_sid].name
                    self.send_room_update(room_id)
                    
                    self.socketio.start_background_task(self.auto_next_round, room_id, sid)

    def auto_next_round(self, room_id, old_czar_sid):
        # Tarea en segundo plano: también debe respetar el cerrojo
        self.socketio.sleep(5)
        with self._lock:
            room = self.rooms.get(room_id)
            if room and room.state == 'round_end' and room.czar == old_czar_sid:
                self.advance_to_next_round(room_id)

    def force_next_round(self, sid, room_id):
        with self._lock:
            self._force_next_round_locked(sid, room_id)

    def _force_next_round_locked(self, sid, room_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if room.state == 'round_end' and room.czar == sid:
                self.advance_to_next_round(room_id)

    def advance_to_next_round(self, room_id):
        # Público porque lo invocan auto_next_round y _force_next_round_locked;
        # ambos ya llegan dentro del cerrojo
        if room_id in self.rooms:
            room = self.rooms[room_id]
            
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
            room.played_cards = []
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
                    
            self.send_room_update(room_id)

    def vote_card(self, sid, room_id, card_id):
        with self._lock:
            self._vote_card_locked(sid, room_id, card_id)

    def _vote_card_locked(self, sid, room_id, card_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if room.state in ['judging', 'round_end'] and sid != room.czar and sid in room.players:
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
                        
                self.send_room_update(room_id)

    def vote_renew(self, sid, room_id):
        with self._lock:
            self._vote_renew_locked(sid, room_id)

    def _vote_renew_locked(self, sid, room_id):
        if room_id in self.rooms:
            room = self.rooms[room_id]
            if sid in room.players and room.players[sid].is_connected:
                if sid in room.renew_votes:
                    room.renew_votes.remove(sid)
                else:
                    room.renew_votes.add(sid)
                
                self.check_and_execute_renew(room)
                self.send_room_update(room_id)

    def check_and_execute_renew(self, room):
        active = room.get_active_players()
        eligible = [p for p in active if p.sid != room.czar and not p.waiting_next_round]
        voters = eligible if eligible else active
        if voters:
            thresh = room.options['renew_threshold']
            if (len(room.renew_votes) / len(voters)) >= thresh:
                room.renew_votes.clear()
                for p in active:
                    room.available_whites.extend(p.hand)
                    if p.played_card:
                        if isinstance(p.played_card, list):
                            room.available_whites.extend(p.played_card)
                        else:
                            room.available_whites.append(p.played_card)
                    p.hand = []
                    p.played_card = None
                    
                random.shuffle(room.available_whites)
                hand_size = room.options['hand_size']
                for p in active:
                    p.hand = room.deal_cards(hand_size)
                    
                room.played_cards = []
                if room.state == 'judging':
                    room.state = 'playing'
                self.send_chat_system(room.room_id, '¡Se han renovado las cartas de todos los jugadores!')

    def change_black_card(self, sid, room_id):
        with self._lock:
            self._change_black_card_locked(sid, room_id)

    def _change_black_card_locked(self, sid, room_id):
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

    def _get_custom_file_path(self):
        import os
        # Sobrescribible por entorno: los tests la aíslan para no tocar datos reales
        env_path = os.environ.get("INTERNAL_CUSTOM_PATH")
        if env_path:
            return os.path.abspath(env_path)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.abspath(os.path.join(base_dir, "..", "DataScraping", "cartasCustom.json"))

    def _get_external_custom_dir(self):
        import os
        env_dir = os.environ.get("EXTERNAL_CARDS_DIR")
        if env_dir:
            return os.path.abspath(env_dir)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.abspath(os.path.join(base_dir, "..", "..", "custom_cards"))

    def _save_custom_data(self, custom_data):
        import os
        internal_path = self._get_custom_file_path()
        os.makedirs(os.path.dirname(internal_path), exist_ok=True)
        with open(internal_path, 'w', encoding='utf-8') as f:
            json.dump(custom_data, f, ensure_ascii=False, indent=2)

        try:
            external_dir = self._get_external_custom_dir()
            os.makedirs(external_dir, exist_ok=True)
            external_path = os.path.join(external_dir, "cartasCustom.json")
            with open(external_path, 'w', encoding='utf-8') as f:
                json.dump(custom_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("Error exportando cartas a carpeta externa:", e)

    def add_custom_card(self, card_type, text, pick, respond_cb):
        # Cerrojo alrededor de todo: muta el mazo global, el archivo y las salas
        with self._lock:
            self._add_custom_card_locked(card_type, text, pick, respond_cb)

    def _add_custom_card_locked(self, card_type, text, pick, respond_cb):
        import os
        text = text.strip()
        if not text:
            respond_cb({'success': False, 'message': 'El texto está vacío.'})
            return
            
        def check_sim(new_txt, lst, thresh=0.85):
            n_l = new_txt.lower()
            for c in lst:
                tc = c if isinstance(c, str) else c.get('text', '')
                if difflib.SequenceMatcher(None, n_l, tc.lower().strip()).ratio() >= thresh:
                    return True, tc
            return False, None
            
        custom_path = self._get_custom_file_path()
        external_path = os.path.join(self._get_external_custom_dir(), "cartasCustom.json")
        custom_data = {"whiteCards": [], "blackCards": []}
        
        target_path = custom_path if os.path.exists(custom_path) else (external_path if os.path.exists(external_path) else None)
        if target_path:
            try:
                with open(target_path, 'r', encoding='utf-8') as f:
                    custom_data = json.load(f)
            except Exception as e:
                print("Error leyendo json custom:", e)

        if card_type == 'white':
            is_sim, match = check_sim(text, self.global_white_cards)
            if is_sim:
                respond_cb({'success': False, 'message': f'Muy similar a una carta existente: "{match}"'})
                return
            self.global_white_cards.append(text)
            if 'whiteCards' not in custom_data:
                custom_data['whiteCards'] = []
            if text not in custom_data['whiteCards']:
                custom_data['whiteCards'].append(text)
            for room in self.rooms.values():
                room.available_whites.append(text)
                random.shuffle(room.available_whites)
        elif card_type == 'black':
            is_sim, match = check_sim(text, self.global_black_cards)
            if is_sim:
                respond_cb({'success': False, 'message': f'Muy similar a una carta existente: "{match}"'})
                return
            new_c = {'text': text, 'pick': pick}
            self.global_black_cards.append(new_c)
            if 'blackCards' not in custom_data:
                custom_data['blackCards'] = []
            custom_data['blackCards'].append(new_c)
            for room in self.rooms.values():
                room.available_blacks.append(new_c)
                random.shuffle(room.available_blacks)
        else:
            respond_cb({'success': False, 'message': 'Tipo de carta inválido.'})
            return
            
        try:
            self._save_custom_data(custom_data)
        except Exception as e:
            respond_cb({'success': False, 'message': f'Error guardando cartas custom: {str(e)}'})
            return
            
        respond_cb({'success': True, 'whiteCards': custom_data.get('whiteCards', []), 'blackCards': custom_data.get('blackCards', [])})

    def get_custom_cards(self, respond_cb):
        import os
        custom_path = self._get_custom_file_path()
        external_path = os.path.join(self._get_external_custom_dir(), "cartasCustom.json")
        target_path = custom_path if os.path.exists(custom_path) else (external_path if os.path.exists(external_path) else None)
        
        if target_path:
            try:
                with open(target_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    respond_cb({'success': True, 'whiteCards': data.get('whiteCards', []), 'blackCards': data.get('blackCards', [])})
                    return
            except Exception as e:
                respond_cb({'success': False, 'message': str(e)})
                return
        respond_cb({'success': True, 'whiteCards': [], 'blackCards': []})

    def delete_custom_card(self, card_type, text, respond_cb):
        with self._lock:
            self._delete_custom_card_locked(card_type, text, respond_cb)

    def _delete_custom_card_locked(self, card_type, text, respond_cb):
        import os
        custom_path = self._get_custom_file_path()
        external_path = os.path.join(self._get_external_custom_dir(), "cartasCustom.json")
        target_path = custom_path if os.path.exists(custom_path) else (external_path if os.path.exists(external_path) else None)
        
        if not target_path:
            respond_cb({'success': False, 'message': 'Archivo no encontrado.'})
            return
        try:
            with open(target_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            if card_type == 'white':
                w_list = data.get('whiteCards', [])
                data['whiteCards'] = [c for c in w_list if (c if isinstance(c, str) else c.get('text')) != text]
                if text in self.global_white_cards:
                    self.global_white_cards.remove(text)
            elif card_type == 'black':
                b_list = data.get('blackCards', [])
                data['blackCards'] = [c for c in b_list if (c if isinstance(c, str) else c.get('text')) != text]
                self.global_black_cards = [c for c in self.global_black_cards if (c if isinstance(c, str) else c.get('text')) != text]
            
            self._save_custom_data(data)
            respond_cb({'success': True, 'whiteCards': data.get('whiteCards', []), 'blackCards': data.get('blackCards', [])})
        except Exception as e:
            respond_cb({'success': False, 'message': str(e)})

    @staticmethod
    def _card_texts(cards):
        """Textos normalizados de una lista de cartas (acepta str o {text, pick})."""
        out = set()
        for c in cards:
            t = c if isinstance(c, str) else (c.get('text') if isinstance(c, dict) else None)
            if isinstance(t, str) and t.strip():
                out.add(t.strip())
        return out

    def import_custom_cards(self, raw_json, respond_cb):
        """
        Importa un set completo de cartas desde el contenido de un archivo .json
        con el mismo formato que los sets base (p. ej. CAH-es-set.json):

            { "whiteCards": ["texto", ...],
              "blackCards": [ { "text": "...", "pick": 2 }, ... ] }

        Valida carta a carta: las inválidas (tipo/longitud/pick/huecos) y las
        duplicadas (dentro del archivo, en cartasCustom.json o en el mazo base)
        se descartan individualmente; el resto se guarda y se inyecta en el
        mazo global y en las salas activas, igual que add_custom_card.
        """
        with self._lock:
            self._import_custom_cards_locked(raw_json, respond_cb)

    def _import_custom_cards_locked(self, raw_json, respond_cb):
        import os

        MAX_TEXT_LEN = 200   # longitud máxima por carta
        MAX_PICK = 3         # máximo de cartas a jugar por ronda (coincide con el taller)
        MAX_DETAILS = 10     # ejemplos de rechazo/omisión devueltos al cliente

        # 1) Parsear el JSON crudo y exigir la estructura del formato base
        if isinstance(raw_json, (dict, list)):
            data = raw_json  # el cliente también puede enviar el objeto ya parseado
        else:
            try:
                data = json.loads(raw_json)
            except (TypeError, ValueError) as e:
                respond_cb({'success': False, 'message': f'JSON inválido: {e}'})
                return

        if not isinstance(data, dict) or ('whiteCards' not in data and 'blackCards' not in data):
            respond_cb({'success': False,
                        'message': 'Estructura no reconocida: se esperaba un objeto con "whiteCards" y/o "blackCards".'})
            return

        def clean_text(value):
            """Texto listo para guardar, o None si no es válido como carta."""
            if not isinstance(value, str):
                return None
            text = value.strip()
            if not text or len(text) > MAX_TEXT_LEN:
                return None
            return text

        rejected, ignored = [], []
        n_rejected = n_ignored = 0

        def reject(reason):
            nonlocal n_rejected
            n_rejected += 1
            if len(rejected) < MAX_DETAILS:
                rejected.append(reason)

        def ignore(reason):
            nonlocal n_ignored
            n_ignored += 1
            if len(ignored) < MAX_DETAILS:
                ignored.append(reason)

        # Cartas que ya están en juego (base + customs cargadas al arrancar)
        existing_whites = self._card_texts(self.global_white_cards)
        existing_blacks = self._card_texts(self.global_black_cards)

        # 2) Blancas: cadenas de texto no vacías y no duplicadas
        new_whites = []
        raw_whites = data.get('whiteCards', [])
        if not isinstance(raw_whites, list):
            reject('whiteCards: no es una lista')
            raw_whites = []
        for i, item in enumerate(raw_whites):
            text = clean_text(item)
            if text is None:
                if not isinstance(item, str):
                    reject(f'Blanca #{i + 1}: debe ser texto, no {type(item).__name__}')
                elif len(item.strip()) > MAX_TEXT_LEN:
                    reject(f'Blanca #{i + 1}: demasiado larga (máx. {MAX_TEXT_LEN} caracteres)')
                else:
                    reject(f'Blanca #{i + 1}: vacía')
                continue
            if text in new_whites:
                ignore(f'Blanca duplicada en el archivo: "{text[:40]}"')
                continue
            if text in existing_whites:
                ignore(f'Ya existe en el mazo: "{text[:40]}"')
                continue
            new_whites.append(text)

        # 3) Negras: {text, pick}; se tolera texto plano como pick=1
        new_blacks = []
        new_black_texts = set()
        raw_blacks = data.get('blackCards', [])
        if not isinstance(raw_blacks, list):
            reject('blackCards: no es una lista')
            raw_blacks = []
        for i, item in enumerate(raw_blacks):
            if isinstance(item, dict):
                text = clean_text(item.get('text'))
                raw_pick = item.get('pick', 1)
            elif isinstance(item, str):
                text = clean_text(item)
                raw_pick = 1
            else:
                text, raw_pick = None, 1

            if text is None:
                reject(f'Negra #{i + 1}: falta o es inválido el texto')
                continue

            try:
                pick = int(raw_pick)  # se tolera "2" o 2.0
            except (TypeError, ValueError):
                reject(f'Negra #{i + 1}: "pick" no es un número ({raw_pick!r})')
                continue
            if not 1 <= pick <= MAX_PICK:
                reject(f'Negra #{i + 1}: "pick" fuera de rango (1-{MAX_PICK}): {pick}')
                continue
            # Una negra pick>=2 necesita al menos `pick` huecos (_) para ser jugable
            if pick >= 2 and text.count('_') < pick:
                reject(f'Negra #{i + 1}: necesita al menos {pick} guion(es) bajo(s) "_" para pick={pick}')
                continue
            if text in new_black_texts:
                ignore(f'Negra duplicada en el archivo: "{text[:40]}"')
                continue
            if text in existing_blacks:
                ignore(f'Ya existe en el mazo: "{text[:40]}"')
                continue

            new_black_texts.add(text)
            new_blacks.append({'text': text, 'pick': pick})

        if not new_whites and not new_blacks:
            respond_cb({'success': False,
                        'message': f'Ninguna carta válida para importar ({n_rejected} inválidas, {n_ignored} duplicadas).',
                        'rejected': rejected,
                        'ignored': ignored})
            return

        # 4) Fusionar con cartasCustom.json y guardar (ruta interna + externa)
        custom_path = self._get_custom_file_path()
        external_path = os.path.join(self._get_external_custom_dir(), "cartasCustom.json")
        target_path = custom_path if os.path.exists(custom_path) else (external_path if os.path.exists(external_path) else None)
        custom_data = {"whiteCards": [], "blackCards": []}
        if target_path:
            try:
                with open(target_path, 'r', encoding='utf-8') as f:
                    custom_data = json.load(f)
            except Exception as e:
                print("Error leyendo json custom:", e)
        if not isinstance(custom_data, dict):
            custom_data = {"whiteCards": [], "blackCards": []}

        file_whites = self._card_texts(custom_data.get('whiteCards', []))
        file_blacks = self._card_texts(custom_data.get('blackCards', []))
        for w in new_whites:
            if w not in file_whites:
                custom_data.setdefault('whiteCards', []).append(w)
        for b in new_blacks:
            if b['text'] not in file_blacks:
                custom_data.setdefault('blackCards', []).append(b)

        try:
            self._save_custom_data(custom_data)
        except Exception as e:
            respond_cb({'success': False, 'message': f'Error guardando cartas custom: {e}'})
            return

        # 5) Inyectar en el mazo global y en las salas activas (en directo)
        self.global_white_cards.extend(new_whites)
        self.global_black_cards.extend(new_blacks)
        for room in self.rooms.values():
            if new_whites:
                room.available_whites.extend(new_whites)
                random.shuffle(room.available_whites)
            if new_blacks:
                room.available_blacks.extend(new_blacks)
                random.shuffle(room.available_blacks)

        respond_cb({'success': True,
                    'imported': {'white': len(new_whites), 'black': len(new_blacks)},
                    'rejected_count': n_rejected,
                    'ignored_count': n_ignored,
                    'rejected': rejected,
                    'ignored': ignored,
                    'whiteCards': custom_data.get('whiteCards', []),
                    'blackCards': custom_data.get('blackCards', [])})

    def add_room_cards(self, room_id, cards_str):
        with self._lock:
            room = self.rooms.get(room_id)
            if room:
                new_cards = [c.strip() for c in cards_str.split(',') if c.strip()]
                if new_cards:
                    room.available_whites.extend(new_cards)
                    random.shuffle(room.available_whites)
                    self.send_chat_system(room_id, f'Se han añadido {len(new_cards)} cartas personalizadas a la sala.')

    def send_chat(self, sid, room_id, msg):
        with self._lock:
            room = self.rooms.get(room_id)
            if room and msg and sid in room.players:
                pname = room.players[sid].name
                self.socketio.emit('chat_message', {'msg': msg, 'sender': pname, 'system': False}, to=room_id)
