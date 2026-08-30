import json
import os
from flask import Flask, request, send_from_directory
from flask_socketio import SocketIO, join_room, emit
from models.game_manager import GameManager

# Configurar Flask para servir archivos estáticos del frontend de React
app = Flask(__name__, static_folder='../frontend/dist', static_url_path='/')
app.config['SECRET_KEY'] = 'secreto_super_seguro'
# Permitimos CORS a Vite
socketio = SocketIO(app, cors_allowed_origins="*")

CARTAS_BLANCAS = []
CARTAS_NEGRAS = []

def cargar_cartas():
    global CARTAS_BLANCAS, CARTAS_NEGRAS
    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidatos = [
        os.path.join(base_dir, "DataScraping", "CAH-es-set-actualizado.json"),
        os.path.join(base_dir, "www", "Cartas", "CAH-es-set.json"),
        "DataScraping/CAH-es-set-actualizado.json",
        "backend/DataScraping/CAH-es-set-actualizado.json"
    ]
    archivo = None
    for cand in candidatos:
        if os.path.exists(cand):
            archivo = cand
            break

    if not archivo:
        print("Error: No se encontró ningún archivo de cartas.")
        return

    try:
        with open(archivo, 'r', encoding='utf-8') as f:
            datos = json.load(f)
            CARTAS_BLANCAS = list(datos.get('whiteCards', []))
            CARTAS_NEGRAS = list(datos.get('blackCards', []))
            print(f"Cartas base cargadas ({archivo}): {len(CARTAS_BLANCAS)} blancas, {len(CARTAS_NEGRAS)} negras.")
    except Exception as e:
        print("Error cargando cartas base:", e)

    # Cargar / inicializar cartasCustom.json
    custom_path = os.path.join(base_dir, "DataScraping", "cartasCustom.json")
    if not os.path.exists(custom_path):
        try:
            os.makedirs(os.path.dirname(custom_path), exist_ok=True)
            with open(custom_path, 'w', encoding='utf-8') as f:
                json.dump({"whiteCards": [], "blackCards": []}, f, ensure_ascii=False, indent=2)
            print(f"Creado archivo local cartasCustom.json en {custom_path}")
        except Exception as e:
            print("Error creando cartasCustom.json:", e)

    if os.path.exists(custom_path):
        try:
            with open(custom_path, 'r', encoding='utf-8') as f:
                custom_data = json.load(f)
                c_whites = custom_data.get('whiteCards', [])
                c_blacks = custom_data.get('blackCards', [])
                CARTAS_BLANCAS.extend(c_whites)
                CARTAS_NEGRAS.extend(c_blacks)
                print(f"Cartas personalizadas cargadas de cartasCustom.json: {len(c_whites)} blancas, {len(c_blacks)} negras.")
        except Exception as e:
            print("Error leyendo cartasCustom.json:", e)

cargar_cartas()

manager = GameManager(socketio, CARTAS_BLANCAS, CARTAS_NEGRAS)

@app.route('/')
def index():
    return app.send_static_file('index.html')

@app.route('/<path:path>')
def serve_static(path):
    return app.send_static_file(path)

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

@socketio.on('get_custom_cards')
def on_get_custom_cards():
    def respond_cb(res):
        emit('custom_cards_list', res)
    manager.get_custom_cards(respond_cb)

@socketio.on('delete_custom_card')
def on_delete_custom_card(data):
    def respond_cb(res):
        emit('custom_card_deleted', res)
    manager.delete_custom_card(data.get('type'), data.get('text', ''), respond_cb)

@socketio.on('add_room_cards')
def on_add_room_cards(data):
    manager.add_room_cards(data.get('room_id', '').upper(), data.get('cards', ''))

@socketio.on('send_chat')
def on_send_chat(data):
    manager.send_chat(request.sid, data.get('room_id', '').upper(), data.get('msg', ''))

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=3000, debug=True)
