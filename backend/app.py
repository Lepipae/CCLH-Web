import json
import os
from flask import Flask, request
from flask_socketio import SocketIO, join_room, emit
from models.game_manager import GameManager

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secreto_super_seguro'
# Permitimos CORS a Vite
socketio = SocketIO(app, cors_allowed_origins="*")

CARTAS_BLANCAS = []
CARTAS_NEGRAS = []

def cargar_cartas():
    global CARTAS_BLANCAS, CARTAS_NEGRAS
    archivo = "DataScraping/CAH-es-set-actualizado.json"
    if not os.path.exists(archivo):
        archivo = "www/Cartas/CAH-es-set.json"
    if not os.path.exists(archivo):
        # Fallback if run from different dir
        archivo = "../DataScraping/CAH-es-set-actualizado.json"
    try:
        with open(archivo, 'r', encoding='utf-8') as f:
            datos = json.load(f)
            CARTAS_BLANCAS = datos.get('whiteCards', [])
            CARTAS_NEGRAS = datos.get('blackCards', [])
            print(f"Cartas cargadas: {len(CARTAS_BLANCAS)} blancas, {len(CARTAS_NEGRAS)} negras.")
    except Exception as e:
        print("Error cargando cartas:", e)

cargar_cartas()

manager = GameManager(socketio, CARTAS_BLANCAS, CARTAS_NEGRAS)

@socketio.on('join_game')
def on_join(data):
    name = data.get('name', 'Anon').strip()
    room_id = data.get('room_id', 'lobby').strip().upper()
    if name and room_id:
        if manager.join_game(request.sid, name, room_id):
            join_room(room_id)

@socketio.on('disconnect')
def on_disconnect():
    manager.disconnect(request.sid)

@socketio.on('start_game')
def on_start_game(data):
    manager.start_game(request.sid, data.get('room_id', '').upper())

@socketio.on('update_room_options')
def on_update_room_options(data):
    manager.update_options(request.sid, data.get('room_id', '').upper(), data)

@socketio.on('play_card')
def on_play_card(data):
    manager.play_card(request.sid, data.get('room_id', '').upper(), data.get('card_index'))

@socketio.on('reveal_card')
def on_reveal_card(data):
    manager.reveal_card(request.sid, data.get('room_id', '').upper(), data.get('sub_id'))

@socketio.on('choose_winner')
def on_choose_winner(data):
    manager.choose_winner(request.sid, data.get('room_id', '').upper(), data.get('sub_id'))

@socketio.on('next_round')
def on_next_round(data):
    manager.force_next_round(request.sid, data.get('room_id', '').upper())

@socketio.on('vote_renew')
def on_vote_renew(data):
    manager.vote_renew(request.sid, data.get('room_id', '').upper())

@socketio.on('change_black_card')
def on_change_black_card(data):
    manager.change_black_card(request.sid, data.get('room_id', '').upper())

@socketio.on('add_custom_card')
def on_add_custom_card(data):
    def respond_cb(res):
        emit('custom_card_result', res)
    manager.add_custom_card(data.get('type'), data.get('text', ''), data.get('pick', 1), respond_cb)

@socketio.on('add_room_cards')
def on_add_room_cards(data):
    manager.add_room_cards(data.get('room_id', '').upper(), data.get('cards', ''))

@socketio.on('send_chat')
def on_send_chat(data):
    manager.send_chat(request.sid, data.get('room_id', '').upper(), data.get('msg', ''))

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=3000, debug=True)
