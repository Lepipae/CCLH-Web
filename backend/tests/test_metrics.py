"""El endpoint `/metrics`: una foto numérica del proceso, sin efectos.

Lo que se comprueba aquí no es que el JSON tenga las claves correctas, sino las
dos cosas que pueden salir mal de verdad y que un test de "devuelve 200" no
pilla:

1. **Que no se lo coma el `catch-all` de estáticos.** La app sirve
   `frontend/dist` con un `/<path:path>` que se declara justo debajo. Si
   `/metrics` se registra después, Flask se la queda el estático y el endpoint
   responde el index del SPA. Es un fallo silencioso: 200, y una página HTML
   donde se esperaba un número.

2. **Que los contadores acumulados no se lean como medidores.** `presencia` y
   `salas_rechazadas` suman desde el arranque del proceso; `presencia_actual` y
   las salas/jugadores son el estado de ahora. Mezclarlos daría un panel que
   dice "hay 400 salas purgadas" cuando no hay ni una, así que se comprueba que
   los dos bloques se mueven de forma independiente.
"""
import time

import pytest

from models.presence import PresencePolicy, SOSPECHOSO, VIVO
from test_disconnect_roles import make_client, room_clients  # noqa: F401 (fixture)


@pytest.fixture()
def http(app_module):
    """Cliente HTTP de Flask (distinto del de Socket.IO)."""
    return app_module.app.test_client()


def _m(http):
    """GET /metrics como dict, fallando con el cuerpo si no es 200."""
    resp = http.get("/metrics")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


# --- La ruta: existe, y no la captura el estático ---------------------------

class TestLaRuta:

    def test_devuelve_json(self, http):
        resp = http.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("application/json")

    def test_no_la_se_lista_el_catch_all_de_estaicos(self, http):
        """`/metrics` compite con el `catch-all` `/<path:path>`, que sirve
        `frontend/dist`. Este test NO comprueba el orden de declaración (para
        eso no serviría: Werkzeug ordena por especificidad y no por orden, así
        que la ruta literal gana aunque se declare después). Comprueba lo que
        de verdad importa: que lo que sale por aquí son los números y no el
        index del SPA."""
        resp = http.get("/metrics")
        # El estático devuelve HTML; los números, JSON.
        assert "text/html" not in resp.headers["Content-Type"]
        assert isinstance(resp.get_json(), dict)

    def test_una_ruta_literal_gana_al_catch_all(self, app_module):
        """El porqué de lo anterior, comprobado contra el enrutador real en
        vez de contra un comentario: aunque el `catch-all` se declarara
        primero, `/metrics` debe seguir resolviendo a su propio handler.

        Es la propiedad de la que depende el endpoint entero. Si algún día el
        `catch-all` dejara de ser un `path` o se tocara el esquema, este test
        es el que se entera antes de que un monitor reciba HTML."""
        reglas = {r.endpoint: r.rule
                  for r in app_module.app.url_map.iter_rules()
                  if r.rule in ("/metrics", "/<path:path>")}
        assert reglas.get("metrics") == "/metrics"
        assert reglas.get("serve_static") == "/<path:path>", (
            "el catch-all sigue siendo un conversor de path: por eso la ruta "
            "literal puede ganarle")
        endpoint, _ = app_module.app.url_map.bind("localhost").match("/metrics")
        assert endpoint == "metrics", (
            "una ruta literal debe ganar al catch-all; si no, /metrics "
            "devuelve el index del SPA en vez de los números")

    def test_responde_a_servidor_vacio(self, http, manager):
        """Sin salas ni jugadores todos los números son 0, y el endpoint
        sigue siendo JSON válido. Es el estado en el que arranca el proceso."""
        m = _m(http)
        assert m["salas"]["total"] == 0
        assert m["jugadores"] == {"conectados": 0, "asientos": 0, "mudos": 0}
        assert m["salas"]["por_estado"] == {}


# --- Salas y jugadores -------------------------------------------------------

class TestSalasYJugadores:

    def test_cuenta_salas_y_jugadores(self, http, manager, room_clients):
        room_clients("Ana", "S1")
        room_clients("Beto", "S1")
        room_clients("Caro", "S2")

        m = _m(http)
        assert m["salas"]["total"] == 2
        assert m["jugadores"]["conectados"] == 3
        # Cada uno en su propia sala, y las dos en 'waiting'.
        assert m["salas"]["por_estado"] == {"waiting": 2}

    def test_publica_el_techo_de_salas(self, http, manager, monkeypatch):
        """El techo junto al total es lo que dice si el servidor está a punto de
        rechazar a alguien; separado, obliga a ir a buscar la constante."""
        monkeypatch.setattr(manager, "MAX_ROOMS", 7)
        assert _m(http)["salas"]["max"] == 7

    def test_los_asientos_conserpados_no_cuentan_como_conectados(
            self, http, manager, room_clients):
        """Un asiento purgado sigue en `players` (conserva mano y marcador) pero
        no está conectado. La diferencia entre `asientos` y `conectados` es
        exactamente lo que se está fugando de la sala."""
        room_clients("Ana", "S1")
        sala = manager.rooms["S1"]
        antes = _m(http)
        assert antes["jugadores"] == {"conectados": 1, "asientos": 1, "mudos": 1}

        sala.remove_player(sid_de(sala, "Ana"))

        m = _m(http)
        assert m["jugadores"]["conectados"] == 0
        assert m["jugadores"]["asientos"] == 1, (
            "el asiento sigue en la sala: borrarlo de aquí mentiría sobre lo "
            "que se está fugando")


# --- Clientes mudos ----------------------------------------------------------

class TestClientesMudos:

    def test_un_cliente_sin_latidos_es_mudo(self, http, manager, room_clients):
        """Nadie ha contestado un solo latido: el reaper lo juzga con
        PRESENCE_SIN_PRUEBA. Este es el número que explica por qué una sala
        sigue ocupada sin que nadie juegue."""
        room_clients("Ana", "S1")
        assert _m(http)["jugadores"]["mudos"] == 1

    def test_contestar_un_latido_deja_de_contar_como_mudo(
            self, http, manager, room_clients):
        """`mudos` es un medidor, no un acumulado: en cuanto el cliente prueba
        que vive deja de contar, aunque su siguiente latido tarde en llegar."""
        cliente, _ = room_clients("Ana", "S1")
        assert _m(http)["jugadores"]["mudos"] == 1

        cliente.emit("heartbeat_pong")

        assert _m(http)["jugadores"]["mudos"] == 0
        assert _m(http)["jugadores"]["conectados"] == 1, (
            "contestar un latido no desconecta a nadie")

    def test_cuenta_los_mudos_de_todas_las_salas(self, http, manager, room_clients):
        """El medidor es de clientes, no de salas: dos mudos en la misma sala
        cuentan dos."""
        room_clients("Ana", "S1")
        room_clients("Beto", "S1")
        room_clients("Caro", "S2")

        m = _m(http)
        assert m["jugadores"]["conectados"] == 3
        assert m["jugadores"]["mudos"] == 3  # aún nadie ha contestado

    def test_un_mudo_purgado_deja_de_contar(self, http, manager, room_clients,
                                             monkeypatch):
        """Al purgarlo se le suelta el asiento, así que sale del medidor: si
        siguiera contando, el número crecería sin que creciese el problema."""
        room_clients("Ana", "S1")
        sala = manager.rooms["S1"]
        _politicas(manager, monkeypatch, 0.01, 0)
        _envejecer(manager, sala, 10 ** 6)

        manager.reap_empty_rooms()
        manager.reap_empty_rooms()

        m = _m(http)
        assert m["jugadores"]["conectados"] == 0
        assert m["jugadores"]["mudos"] == 0
        # Pero su transición sigue en el acumulado: el histórico no se olvida.
        assert m["presencia"]["jugadores"][SOSPECHOSO] >= 1


# --- Salas rechazadas --------------------------------------------------------

class TestSalasRechazadas:

    def test_refleja_el_rechazo_por_techo(self, http, app_module, manager,
                                           room_clients, monkeypatch):
        """`salas_rechazadas` es un contador de ataque, no de juego: por eso
        vive en la raíz del payload y no dentro de `salas`, que es un estado
        del servidor."""
        monkeypatch.setattr(manager, "MAX_ROOMS", 1)
        room_clients("Ana", "LLENA")

        # Este cliente NO acaba en la sala (por eso no usa `room_clients`):
        # se queda en el login con su `join_error`.
        intruso = make_client(app_module)
        intruso.get_received()
        intruso.emit("join_game", {"name": "Intruso", "room_id": "OTRA"})

        m = _m(http)
        assert m["salas_rechazadas"] == 1
        assert m["salas"]["total"] == 1, "una sala rechazada no ocupa sitio"


# --- Fases de presencia ------------------------------------------------------

class TestFasesDePresencia:

    def test_publica_las_tres_fases_de_jugadores_y_salas(self, http, manager):
        """El endpoint tiene que nombrar las fases aunque no haya ninguna, para
        que un panel no tenga que distinguir "0" de "no hay ese dato"."""
        m = _m(http)
        for ambito in ("jugadores", "salas"):
            for fase in (VIVO, SOSPECHOSO, "purgado"):
                assert m["presencia"][ambito][fase] == 0
                assert m["presencia_actual"][ambito][fase] == 0

    def test_acumulado_no_es_medidor(self, http, manager, room_clients,
                                     monkeypatch):
        """La separación que sostiene todo el payload: `presencia` suma
        transiciones desde el arranque, `presencia_actual` dice quién está en
        qué fase ahora. Un jugador sospechoso y luego purgado deja el
        acumulado en 1 sospechoso + 1 purgado, y el medidor en 1 purgado."""
        room_clients("Ana", "S1")
        sala = manager.rooms["S1"]
        _politicas(manager, monkeypatch, 0.01, 0)
        _envejecer(manager, sala, 10 ** 6)

        manager.reap_empty_rooms()  # sospecha
        sospechoso = _m(http)
        assert sospechoso["presencia_actual"]["jugadores"][SOSPECHOSO] == 1
        assert sospechoso["presencia"]["jugadores"][SOSPECHOSO] == 1

        manager.reap_empty_rooms()  # purga
        purgado = _m(http)
        # El acumulado conserva las dos transiciones...
        assert purgado["presencia"]["jugadores"][SOSPECHOSO] == 1
        assert purgado["presencia"]["jugadores"]["purgado"] == 1
        # ...y el medidor solo refleja dónde está AHORA.
        assert purgado["presencia_actual"]["jugadores"][SOSPECHOSO] == 0
        assert purgado["presencia_actual"]["jugadores"]["purgado"] == 1

    def test_una_sala_vacia_se_sospecha(self, http, manager, room_clients,
                                        monkeypatch):
        """La sala recorre las mismas fases que un jugador, así que también
        tiene que salir en `presencia_actual.salas`."""
        # El código de sala se normaliza a MAYÚSCULAS al unirse, así que la
        # fixture `room_clients` (que busca por el literal) solo funciona con
        # códigos ya en mayúsculas.
        _, sids = room_clients("Ana", "VACIA")
        sala = manager.rooms["VACIA"]
        _politicas_sala(manager, monkeypatch, 0.01, 0)
        for s in sids:
            sala.remove_player(s)
        sala.empty_since = 10 ** 6  # reloj muy atrasado, sin dormir

        manager.reap_empty_rooms()

        m = _m(http)
        assert m["presencia_actual"]["salas"][SOSPECHOSO] == 1
        assert m["salas"]["total"] == 1, "sospechar no borra la sala todavía"

        manager.reap_empty_rooms()  # la gracia se cumple y sí se va

        m = _m(http)
        assert m["salas"]["total"] == 0, "purgada: fuera del medidor"
        assert m["presencia"]["salas"]["purgado"] == 1, "pero en el acumulado"


# --- Utilidades --------------------------------------------------------------

def sid_de(sala, nombre):
    for s, p in sala.players.items():
        if p.name == nombre:
            return s
    raise AssertionError(f"{nombre} no está en {sala.room_id}")


def _politicas(manager, monkeypatch, sospecha, gracia):
    """Jugadores y sin-prueba con el mismo plazo corto (sin dormir)."""
    politica = PresencePolicy(sospecha, gracia)
    monkeypatch.setattr(manager, "PRESENCE", politica)
    monkeypatch.setattr(manager, "PRESENCE_SIN_PRUEBA", politica)


def _politicas_sala(manager, monkeypatch, sospecha, gracia):
    politica = PresencePolicy(sospecha, gracia)
    monkeypatch.setattr(manager, "ROOM_LIFECYCLE", politica)
    monkeypatch.setattr(manager, "ROOM_LIFECYCLE_NUEVA", politica)


def _envejecer(manager, sala, segundos):
    """Envejece el reloj de silencio de los jugadores de `sala` a mano.

    Igual que `_go_silent` en la suite del reaper: fijar el reloj a mano es lo
    que permite probar plazos de minutos sin dormirlos. No usa `signal()`, así
    que no perdona la sospecha: aquí lo que se quiere es envejecer, no volver.
    """
    viejo = time.time() - segundos
    for p in sala.players.values():
        p.last_seen = viejo
