"""Fixtures compartidas del suite de tests del backend.

Aísla las rutas de cartas custom (interna y externa) en un directorio temporal
para no tocar jamás los datos reales, importa la app una sola vez por sesión y
expone un cliente Socket.IO en proceso (socketio.test_client), sin abrir puerto
de red ni arrancar servidor.
"""
import copy
import json
import os
import shutil
import sys
import tempfile

import pytest

# `backend/` debe ser la raíz de imports (app, models, ...)
BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


@pytest.fixture(scope="session")
def test_paths():
    """Directorios temporales para el archivo interno y la carpeta externa."""
    root = tempfile.mkdtemp(prefix="cah_tests_")
    paths = {
        "root": root,
        "internal": os.path.join(root, "internal", "cartasCustom.json"),
        "external_dir": os.path.join(root, "external"),
        "external": os.path.join(root, "external", "cartasCustom.json"),
    }
    os.environ["INTERNAL_CUSTOM_PATH"] = paths["internal"]
    os.environ["EXTERNAL_CARDS_DIR"] = paths["external_dir"]
    yield paths
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="session")
def app_module(test_paths):
    """Importa backend/app.py una vez, con las rutas ya aisladas.

    cargar_cartas() se ejecuta en el import: con los archivos temporales aún
    vacíos, el mazo global queda solo con las cartas base, que son las que se
    snapshotan para restaurar el estado entre tests.
    """
    import app as app_module

    app_module.manager.global_white_cards_snapshot = copy.deepcopy(app_module.manager.global_white_cards)
    app_module.manager.global_black_cards_snapshot = copy.deepcopy(app_module.manager.global_black_cards)
    yield app_module


@pytest.fixture()
def manager(app_module, test_paths):
    """GameManager con estado limpio: mazo base + cartasCustom.json vacío."""
    m = app_module.manager
    m.rooms.clear()
    m.global_white_cards[:] = copy.deepcopy(m.global_white_cards_snapshot)
    m.global_black_cards[:] = copy.deepcopy(m.global_black_cards_snapshot)
    os.makedirs(os.path.dirname(test_paths["internal"]), exist_ok=True)
    with open(test_paths["internal"], "w", encoding="utf-8") as f:
        json.dump({"whiteCards": [], "blackCards": []}, f, ensure_ascii=False)
    os.makedirs(test_paths["external_dir"], exist_ok=True)
    for fname in os.listdir(test_paths["external_dir"]):
        os.remove(os.path.join(test_paths["external_dir"], fname))
    return m


@pytest.fixture()
def client(app_module, manager):
    """Cliente Socket.IO en proceso conectado a la app real."""
    client = app_module.socketio.test_client(app_module.app)
    client.get_received()  # descartar eventos de conexión
    return client


@pytest.fixture()
def emit(client):
    """Emite un evento y devuelve el primer payload recibido de `event_name`."""

    def _emit(event_name, payload=None, response_event=None, timeout_guard=True):
        client.emit(event_name, payload or {})
        received = client.get_received()
        if response_event:
            for msg in received:
                if msg["name"] == response_event:
                    return msg["args"][0]
        return received

    return _emit


# --- Atajos de dominio -------------------------------------------------------

BASE_SET = {
    "whiteCards": [
        "Carta importada A",
        "Carta importada B",
        "Carta importada A",        # duplicada dentro del archivo
        "",                          # inválida: vacía
        12345,                       # inválida: no es texto
        "x" * 250,                   # inválida: demasiado larga
        "Un sombrero muy guay",      # duplicada con el mazo base
    ],
    "blackCards": [
        {"text": "Pregunta negra de prueba con _ y _.", "pick": 2},
        {"text": "Pregunta negra simple de prueba _", "pick": 1},
        {"text": "Pregunta negra simple de prueba _", "pick": 1},   # duplicada
        {"text": "Pregunta negra con pick alto _", "pick": 5},      # inválida: fuera de rango
        {"text": "Pregunta negra sin hueco", "pick": 2},            # inválida: sin _
        {"text": "Pregunta negra con pick raro _", "pick": "dos"},  # inválida: no numérico
        "Pregunta negra como texto plano _",                        # válida (pick=1)
        {"text": "", "pick": 1},                                    # inválida: vacía
    ],
}
IMPORTED_WHITES = 2   # A y B
IMPORTED_BLACKS = 3   # 2 objetos válidos + texto plano tolerado


def import_payload(data):
    """Payload estándar para el evento import_custom_cards."""
    return {"json": data}


def get_event(received, name):
    """Devuelve el primer argumento del evento `name` en una lista de recibidos."""
    for msg in received:
        if msg["name"] == name:
            return msg["args"][0]
    return None
