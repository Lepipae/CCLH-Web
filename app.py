import json
import random
import os
import difflib
from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO, emit, join_room, leave_room
import os

app = Flask(__name__, static_folder='static', static_url_path='')
app.config['SECRET_KEY'] = 'secreto_super_seguro'
socketio = SocketIO(app, cors_allowed_origins="*")

# Cargar cartas
CARTAS_BLANCAS = []
CARTAS_NEGRAS = []

def cargar_cartas():
    global CARTAS_BLANCAS, CARTAS_NEGRAS
    archivo = "DataScraping/CAH-es-set-actualizado.json"
    if not os.path.exists(archivo):
        archivo = "www/Cartas/CAH-es-set.json"
    try:
        with open(archivo, 'r', encoding='utf-8') as f:
            datos = json.load(f)
            CARTAS_BLANCAS = datos.get('whiteCards', [])
            CARTAS_NEGRAS = datos.get('blackCards', [])
            print(f"Cartas cargadas: {len(CARTAS_BLANCAS)} blancas, {len(CARTAS_NEGRAS)} negras.")
    except Exception as e:
        print("Error cargando cartas:", e)

cargar_cartas()

# Estado del juego
# rooms = { room_id: { players: { sid: {name, points, hand, played_card, is_connected} }, state, czar, black_card, available_whites, available_blacks, played_cards: [{sid, card_text}] } }
rooms = {}

def get_random_cards(available_list, count):
    if len(available_list) < count:
        return []
    drawn = random.sample(available_list, count)
    for c in drawn:
        available_list.remove(c)
    return drawn

def send_room_update(room_id):
    room = rooms[room_id]
    
    # Public info to all
    public_players = []
    for sid, p in room['players'].items():
        if p['is_connected']:
            public_players.append({
                'id': sid,
                'name': p['name'],
                'points': p['points'],
                'has_played': p['played_card'] is not None,
                'is_czar': sid == room['czar']
            })
    
    played_cards_public = []
    if room['state'] == 'judging' or room['state'] == 'round_end':
        for c in room['played_cards']:
            if c.get('revealed') or room['state'] == 'round_end':
                played_cards_public.append({'id': c['id'], 'cards': c['cards'], 'revealed': True, 'sid': c['sid'] if room['state'] == 'round_end' else None})
            else:
                played_cards_public.append({'id': c['id'], 'cards': [], 'revealed': False, 'sid': None})
        
    for sid, p in room['players'].items():
        if p['is_connected']:
            payload = {
                'room_id': room_id,
                'state': room['state'],
                'black_card': room['black_card'],
                'players': public_players,
                'czar': room['czar'],
                'leader': room.get('leader'),
                'hand': p['hand'],
                'played_cards': played_cards_public,
                'winner_sid': room.get('last_winner'),
                'renew_votes': len(room.get('renew_votes', [])),
                'has_voted_renew': sid in room.get('renew_votes', []),
                'total_active': len([s for s, pl in room['players'].items() if pl['is_connected']])
            }
            socketio.emit('game_update', payload, to=sid)

@app.route('/')
def index():
    return send_file('templates/index.html')

@app.route('/admin-cards')
def admin_cards():
    return send_file('templates/admin_cards.html')

@socketio.on('join_game')
def on_join(data):
    name = data.get('name', 'Anon').strip()
    room_id = data.get('room_id', 'lobby').strip().upper()
    if not name or not room_id:
        return
        
    sid = request.sid
    join_room(room_id)
    
    if room_id not in rooms:
        rooms[room_id] = {
            'players': {},
            'state': 'waiting',
            'czar': None,
            'leader': sid,
            'black_card': None,
            'available_whites': list(CARTAS_BLANCAS),
            'available_blacks': list(CARTAS_NEGRAS),
            'played_cards': [],
            'renew_votes': []
        }
    
    # Si el jugador se reconecta (buscamos por nombre)
    reconnected = False
    for existing_sid, p in list(rooms[room_id]['players'].items()):
        if p['name'] == name:
            rooms[room_id]['players'][sid] = p
            rooms[room_id]['players'][sid]['is_connected'] = True
            if existing_sid != sid:
                del rooms[room_id]['players'][existing_sid]
                if rooms[room_id]['czar'] == existing_sid:
                    rooms[room_id]['czar'] = sid
                if rooms[room_id].get('leader') == existing_sid:
                    rooms[room_id]['leader'] = sid
                for pc in rooms[room_id]['played_cards']:
                    if pc['sid'] == existing_sid:
                        pc['sid'] = sid
            reconnected = True
            break
            
    if not reconnected:
        rooms[room_id]['players'][sid] = {
            'name': name,
            'points': 0,
            'hand': get_random_cards(rooms[room_id]['available_whites'], 10),
            'played_card': None,
            'is_connected': True
        }
        
    print(f"{name} se unió a {room_id}")
    send_room_update(room_id)

@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    for room_id, room in rooms.items():
        if sid in room['players']:
            room['players'][sid]['is_connected'] = False
            if sid in room.get('renew_votes', []):
                room['renew_votes'].remove(sid)
            active_players = [s for s, p in room['players'].items() if p['is_connected']]
            if not active_players:
                # Opcionalmente borrar sala, pero la mantenemos por si se reconectan
                pass
            else:
                if room.get('leader') == sid:
                    room['leader'] = active_players[0]
                send_room_update(room_id)

@socketio.on('start_game')
def on_start_game(data):
    room_id = data.get('room_id', '').upper()
    sid = request.sid
    if room_id in rooms and rooms[room_id]['state'] == 'waiting':
        room = rooms[room_id]
        if room.get('leader') == sid:
            active_players = [s for s, p in room['players'].items() if p['is_connected']]
            if len(active_players) >= 2:
                room['state'] = 'playing'
            room['czar'] = random.choice(active_players)
            if not room['available_blacks']:
                room['available_blacks'] = list(CARTAS_NEGRAS)
            
            drawn_black = random.choice(room['available_blacks'])
            room['available_blacks'].remove(drawn_black)
            room['black_card'] = drawn_black
            send_room_update(room_id)

@socketio.on('play_card')
def on_play_card(data):
    room_id = data.get('room_id', '').upper()
    card_index = data.get('card_index')
    sid = request.sid
    
    if room_id in rooms:
        room = rooms[room_id]
        if room['state'] == 'playing' and sid != room['czar'] and sid in room['players']:
            p = room['players'][sid]
            if p['played_card'] is None:
                indices = card_index if isinstance(card_index, list) else [card_index]
                indices = [i for i in indices if 0 <= i < len(p['hand'])]
                if indices:
                    indices.sort(reverse=True)
                    played_texts = []
                    for i in indices:
                        played_texts.insert(0, p['hand'].pop(i))
                    p['played_card'] = played_texts
                    
                    import uuid
                    sub_id = str(uuid.uuid4())[:8]
                    room['played_cards'].append({'id': sub_id, 'sid': sid, 'cards': played_texts, 'revealed': False})
                    
                    # Check if all non-czar players played
                    active_non_czars = [s for s, pl in room['players'].items() if pl['is_connected'] and s != room['czar']]
                    all_played = all(room['players'][s]['played_card'] is not None for s in active_non_czars)
                    
                    if all_played:
                        room['state'] = 'judging'
                        random.shuffle(room['played_cards']) # Anonymize
                        
                    send_room_update(room_id)

@socketio.on('choose_winner')
def on_choose_winner(data):
    room_id = data.get('room_id', '').upper()
    sub_id = data.get('sub_id')
    sid = request.sid
    
    if room_id in rooms:
        room = rooms[room_id]
        if room['state'] == 'judging' and sid == room['czar']:
            winner_sid = None
            for c in room['played_cards']:
                if c.get('id') == sub_id:
                    winner_sid = c['sid']
                    break
                    
            if winner_sid and winner_sid in room['players']:
                room['players'][winner_sid]['points'] += 1
                room['state'] = 'round_end'
                # Guardar en 'czar' el ganador para usarlo en la prox ronda temporalmente
                room['last_winner'] = winner_sid
                room['last_winning_card'] = [c for c in room['played_cards'] if c.get('id') == sub_id][0]['cards']
                room['last_winner_name'] = room['players'][winner_sid]['name']
                send_room_update(room_id)
                socketio.start_background_task(auto_next_round, room_id, sid)

@socketio.on('reveal_card')
def on_reveal_card(data):
    room_id = data.get('room_id', '').upper()
    sub_id = data.get('sub_id')
    sid = request.sid
    if room_id in rooms:
        room = rooms[room_id]
        if room['state'] == 'judging' and sid == room['czar']:
            for c in room['played_cards']:
                if c.get('id') == sub_id:
                    c['revealed'] = True
                    break
            send_room_update(room_id)

def auto_next_round(room_id, old_czar_sid):
    socketio.sleep(5)
    if room_id in rooms:
        room = rooms[room_id]
        if room['state'] == 'round_end' and room['czar'] == old_czar_sid:
            advance_to_next_round(room_id)

def advance_to_next_round(room_id):
    if room_id in rooms:
        room = rooms[room_id]
        room['czar'] = room.get('last_winner', room['czar'])
        room['state'] = 'playing'
        room['played_cards'] = []
        
        if not room['available_blacks']:
            room['available_blacks'] = list(CARTAS_NEGRAS)
        
        drawn_black = random.choice(room['available_blacks'])
        room['available_blacks'].remove(drawn_black)
        room['black_card'] = drawn_black
        
        # Repartir para mantener en 10
        for p_sid, p in room['players'].items():
            p['played_card'] = None
            faltan = 10 - len(p['hand'])
            if faltan > 0:
                p['hand'].extend(get_random_cards(room['available_whites'], faltan))
                
        send_room_update(room_id)

@socketio.on('next_round')
def on_next_round(data):
    room_id = data.get('room_id', '').upper()
    sid = request.sid
    if room_id in rooms:
        room = rooms[room_id]
        if room['state'] == 'round_end' and sid == room['czar']:
            advance_to_next_round(room_id)

@socketio.on('vote_renew')
def on_vote_renew(data):
    room_id = data.get('room_id', '').upper()
    sid = request.sid
    if room_id in rooms:
        room = rooms[room_id]
        if sid in room['players'] and room['players'][sid]['is_connected']:
            if 'renew_votes' not in room:
                room['renew_votes'] = []
            if sid not in room['renew_votes']:
                room['renew_votes'].append(sid)
                
                active_players = [s for s, p in room['players'].items() if p['is_connected']]
                if len(active_players) > 0:
                    ratio = len(room['renew_votes']) / len(active_players)
                    if ratio >= 0.75:
                        room['renew_votes'] = []
                        for p_sid, p in room['players'].items():
                            if p['is_connected']:
                                room['available_whites'].extend(p['hand'])
                                p['hand'] = []
                                
                        random.shuffle(room['available_whites'])
                        
                        for p_sid, p in room['players'].items():
                            if p['is_connected']:
                                p['hand'] = get_random_cards(room['available_whites'], 10)
                                p['played_card'] = None
                        
                        room['played_cards'] = []
                        if room['state'] == 'judging':
                            room['state'] = 'playing'
                            
                send_room_update(room_id)

@socketio.on('change_black_card')
def on_change_black_card(data):
    room_id = data.get('room_id', '').upper()
    sid = request.sid
    if room_id in rooms:
        room = rooms[room_id]
        if room['state'] == 'playing' and room['czar'] == sid:
            old_black = room['black_card']
            if not room['available_blacks']:
                room['available_blacks'] = list(CARTAS_NEGRAS)
            
            drawn_black = random.choice(room['available_blacks'])
            room['available_blacks'].remove(drawn_black)
            room['black_card'] = drawn_black
            
            # Devolver la carta negra anterior al mazo
            room['available_blacks'].append(old_black)
            
            # Devolver cartas jugadas a las manos de los jugadores (para que puedan volver a elegir)
            for p_sid, p in room['players'].items():
                if p['played_card'] is not None:
                    if isinstance(p['played_card'], list):
                        p['hand'].extend(p['played_card'])
                    else:
                        p['hand'].append(p['played_card'])
                    p['played_card'] = None
            room['played_cards'] = []
            
            socketio.emit('chat_message', {'msg': 'El juez ha cambiado la carta negra. ¡Tenéis que volver a jugar!', 'system': True}, to=room_id)
            send_room_update(room_id)

def check_similarity(new_text, existing_cards, threshold=0.8):
    new_lower = new_text.lower().strip()
    for card in existing_cards:
        text_to_compare = card if isinstance(card, str) else card.get('text', '')
        ratio = difflib.SequenceMatcher(None, new_lower, text_to_compare.lower().strip()).ratio()
        if ratio >= threshold:
            return True, text_to_compare
    return False, None

@socketio.on('add_custom_card')
def on_add_custom_card(data):
    card_type = data.get('type')
    text = data.get('text', '').strip()
    pick = data.get('pick', 1)
    
    if not text:
        emit('custom_card_result', {'success': False, 'message': 'El texto está vacío.'})
        return
        
    try:
        with open('CAH-es-set.json', 'r', encoding='utf-8') as f:
            full_data = json.load(f)
    except Exception as e:
        emit('custom_card_result', {'success': False, 'message': f'Error leyendo JSON: {str(e)}'})
        return
        
    if card_type == 'white':
        is_sim, match = check_similarity(text, CARTAS_BLANCAS)
        if is_sim:
            emit('custom_card_result', {'success': False, 'message': f'Muy similar a una carta blanca existente: "{match}"'})
            return
            
        # Añadir a la db global
        CARTAS_BLANCAS.append(text)
        full_data['whiteCards'].append(text)
        
        # Añadir a las salas activas
        for room_id, room in rooms.items():
            room['available_whites'].append(text)
            random.shuffle(room['available_whites'])
            
    elif card_type == 'black':
        is_sim, match = check_similarity(text, CARTAS_NEGRAS)
        if is_sim:
            emit('custom_card_result', {'success': False, 'message': f'Muy similar a una carta negra existente: "{match}"'})
            return
            
        new_card = {'text': text, 'pick': pick}
        CARTAS_NEGRAS.append(new_card)
        full_data['blackCards'].append(new_card)
        
        for room_id, room in rooms.items():
            room['available_blacks'].append(new_card)
            random.shuffle(room['available_blacks'])
            
    else:
        emit('custom_card_result', {'success': False, 'message': 'Tipo de carta no válido.'})
        return
        
    # Guardar en archivo
    try:
        with open('CAH-es-set.json', 'w', encoding='utf-8') as f:
            json.dump(full_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        emit('custom_card_result', {'success': False, 'message': f'Error guardando JSON: {str(e)}'})
        return
        
    emit('custom_card_result', {'success': True})

@socketio.on('add_room_cards')
def on_add_room_cards(data):
    room_id = data.get('room_id')
    cards_str = data.get('cards', '')
    
    if room_id not in rooms:
        return
        
    # Split by comma and strip whitespace
    new_cards = [c.strip() for c in cards_str.split(',') if c.strip()]
    
    if new_cards:
        rooms[room_id]['available_whites'].extend(new_cards)
        random.shuffle(rooms[room_id]['available_whites'])
        socketio.emit('chat_message', {'msg': f'Se han añadido {len(new_cards)} cartas personalizadas a la sala.', 'system': True}, to=room_id)

@socketio.on('send_chat')
def on_send_chat(data):
    room_id = data.get('room_id', '').upper()
    msg = data.get('msg', '').strip()
    sid = request.sid
    if room_id in rooms and msg:
        room = rooms[room_id]
        if sid in room['players']:
            player_name = room['players'][sid]['name']
            socketio.emit('chat_message', {'msg': msg, 'sender': player_name, 'system': False}, to=room_id)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=3000, debug=True)
