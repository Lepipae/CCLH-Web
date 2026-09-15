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
