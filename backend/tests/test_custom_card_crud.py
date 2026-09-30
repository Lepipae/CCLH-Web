"""Tests de delete_custom_card y add_custom_card sobre el estado aislado."""
from conftest import get_event


def add_card(client, type_, text, pick=1):
    client.emit("add_custom_card", {"type": type_, "text": text, "pick": pick})
    return get_event(client.get_received(), "custom_card_result")


def delete_card(client, type_, text):
    client.emit("delete_custom_card", {"type": type_, "text": text})
    return get_event(client.get_received(), "custom_card_deleted")


class TestAddCustomCard:
    def test_add_and_delete_roundtrip(self, client):
        added = add_card(client, "white", "Carta de ciclo completa")
        assert added["success"] is True
        assert "Carta de ciclo completa" in added["whiteCards"]

        deleted = delete_card(client, "white", "Carta de ciclo completa")
        assert deleted["success"] is True
        assert "Carta de ciclo completa" not in deleted["whiteCards"]

    def test_empty_text_rejected(self, client):
        res = add_card(client, "white", "   ")
        assert res["success"] is False
        assert "vacío" in res["message"]

    def test_invalid_type_rejected(self, client):
        res = add_card(client, "azul", "Lo que sea")
        assert res["success"] is False
        assert "Tipo de carta inválido" in res["message"]

    def test_un_pick_imborrable_se_rechaza(self, client):
        """El `pick` de una carta negra decide cuántas cartas hay que jugar para
        poder ganar la ronda, y no es un dato inocuo: con `pick: 999` la mesa se
        queda en 'playing' para siempre, porque nadie puede completar una jugada
        de 999 cartas con una mano de 10. Ni purga ni botón: la sala se queda
        muerta. Se valida con el mismo tope que el importador de .json."""
        for pick in (999, 0, -1, 4):
            res = add_card(client, "black", f"Negra con pick {pick!r} _", pick)
            assert res["success"] is False, f"pick={pick!r} debería rechazarse"
            assert "entre 1 y 3" in res["message"]
        # Y lo que ni siquiera es un número, con su propio mensaje.
        for pick in ("muchas", None, [2]):
            res = add_card(client, "black", f"Negra con pick {pick!r} _", pick)
            assert res["success"] is False, f"pick={pick!r} debería rechazarse"
            assert "no es un número" in res["message"]

    def test_un_pick_valido_se_acepta_incluso_si_llega_como_texto(self, client):
        """El formulario llega como número, pero un cliente con un bundle viejo
        (o uno a propósito) puede mandar el string: se tolera, como hace el
        importador, en vez de rechazar la carta."""
        res = add_card(client, "black", "Negra de dos huecos _ y _", "2")
        assert res["success"] is True
        assert {"text": "Negra de dos huecos _ y _", "pick": 2} in res["blackCards"]

    def test_un_texto_desmedido_se_rechaza(self, client):
        """El límite de longitud vive en el servidor (200), no en el atributo
        `maxlength` del navegador: sin él, un texto de un megabyte se guarda en
        el archivo y viaja a cada cliente que juegue esa carta."""
        res = add_card(client, "white", "x" * 5000)
        assert res["success"] is False
        assert "demasiado largo" in res["message"]

    def test_near_duplicate_rejected(self, client):
        assert add_card(client, "white", "Carta verdaderamente original")["success"] is True
        res = add_card(client, "white", "Carta verdaderamente original!")
        assert res["success"] is False
        assert "Muy similar" in res["message"]

    def test_delete_nonexistent_card_is_noop(self, client):
        # Con el archivo interno existente (aunque vacío), borrar una carta
        # inexistente es un no-op exitoso
        res = delete_card(client, "white", "Nunca existió")
        assert res["success"] is True
        assert res["whiteCards"] == []
