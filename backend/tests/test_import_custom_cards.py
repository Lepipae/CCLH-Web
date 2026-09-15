"""Matriz de validación de import_custom_cards (puerto del harness de /tmp).

Cubre: estructura del archivo, validación carta a carta, deduplicación contra
el mazo y contra cartasCustom.json, idempotencia de reimportación, inyección
en salas activas y persistencia en disco.
"""
import json

from conftest import (
    BASE_SET,
    IMPORTED_BLACKS,
    IMPORTED_WHITES,
    get_event,
)


def import_json(client, data):
    client.emit("import_custom_cards", {"json": data})
    return get_event(client.get_received(), "custom_cards_imported")


def read_internal_file(test_paths):
    with open(test_paths["internal"], encoding="utf-8") as f:
        return json.load(f)


class TestStructure:
    def test_broken_json_string_rejected(self, client):
        res = import_json(client, "{esto no es json")
        assert res["success"] is False
        assert "JSON inválido" in res["message"]

    def test_flat_list_rejected(self, client):
        res = import_json(client, ["una", "lista", "plana"])
        assert res["success"] is False
        assert "Estructura no reconocida" in res["message"]

    def test_unknown_object_rejected(self, client):
        res = import_json(client, {"foo": ["bar"]})
        assert res["success"] is False
        assert "Estructura no reconocida" in res["message"]

    def test_null_json_rejected(self, client):
        res = import_json(client, None)
        assert res["success"] is False

    def test_empty_object_rejected(self, client):
        res = import_json(client, {})
        assert res["success"] is False
        assert "Estructura no reconocida" in res["message"]


class TestPerCardValidation:
    def test_mixed_set_counts_and_reasons(self, client):
        res = import_json(client, BASE_SET)
        assert res["success"] is True
        assert res["imported"] == {"white": IMPORTED_WHITES, "black": IMPORTED_BLACKS}
        assert res["rejected_count"] == 7
        assert res["ignored_count"] == 3

        joined = "\n".join(res["rejected"])
        assert "vacía" in joined
        assert "no int" in joined            # blanca numérica
        assert "demasiado larga" in joined
        assert "fuera de rango" in joined    # pick=5
        assert "guion" in joined             # pick=2 sin huecos
        assert "no es un número" in joined   # pick="dos"
        assert "falta o es inválido el texto" in joined

    def test_black_as_plain_string_tolerated_as_pick_1(self, client, test_paths):
        res = import_json(client, {"blackCards": ["Solo texto plano _"]})
        assert res["success"] is True
        assert res["imported"] == {"white": 0, "black": 1}
        state = read_internal_file(test_paths)
        assert state["blackCards"] == [{"text": "Solo texto plano _", "pick": 1}]

    def test_pick_bounds(self, client):
        res = import_json(client, {"blackCards": [
            {"text": "a _", "pick": 0},
            {"text": "b _", "pick": 4},
            {"text": "tres huecos _ _ _", "pick": "3"},
        ]})
        # Solo pasa el coercible a entero válido Y con huecos suficientes
        assert (res.get("imported") or {"black": 0})["black"] == 1
        assert any("fuera de rango" in r for r in res["rejected"])

    def test_missing_underscores_for_pick(self, client):
        res = import_json(client, {"blackCards": [{"text": "sin huecos", "pick": 3}]})
        # Todo rechazado: la respuesta no incluye la clave 'imported'
        assert (res.get("imported") or {}).get("black", 0) == 0
        assert any("guion" in r for r in res["rejected"])


class TestDedup:
    def test_duplicates_within_file_skipped(self, client):
        res = import_json(client, {"whiteCards": ["Repetida", "Repetida", "Repetida"]})
        assert res["imported"]["white"] == 1
        assert res["ignored_count"] == 2

    def test_duplicates_vs_base_deck_ignored(self, client):
        res = import_json(client, {"whiteCards": ["Un sombrero muy guay"]})
        assert res["success"] is False
        # En fallo total la respuesta detalla los motivos en la lista 'ignored'
        assert any("Ya existe en el mazo" in g for g in res["ignored"])

    def test_reimport_is_idempotent(self, client):
        first = import_json(client, {"whiteCards": ["Solo una"], "blackCards": [{"text": "N _", "pick": 1}]})
        assert first["success"] is True
        second = import_json(client, {"whiteCards": ["Solo una"], "blackCards": [{"text": "N _", "pick": 1}]})
        assert second["success"] is False
        assert (second.get("imported") or {"white": 0, "black": 0})["white"] == 0
        assert any("Ya existe en el mazo" in g for g in second["ignored"])

    def test_all_duplicates_fails_with_message(self, client):
        import_json(client, {"whiteCards": ["Dup target"]})
        res = import_json(client, {"whiteCards": ["Dup target"]})
        assert res["success"] is False
        assert "Ninguna carta válida" in res["message"]


class TestPersistence:
    def test_imported_cards_persisted_to_internal_file(self, client, test_paths):
        import_json(client, BASE_SET)
        state = read_internal_file(test_paths)
        texts = {w if isinstance(w, str) else w.get("text") for w in state["whiteCards"]}
        assert "Carta importada A" in texts
        assert "Carta importada B" in texts
        assert len(state["whiteCards"]) == IMPORTED_WHITES
        assert len(state["blackCards"]) == IMPORTED_BLACKS

    def test_imported_cards_persisted_to_external_file(self, client, test_paths):
        import_json(client, {"whiteCards": ["Para la carpeta externa"]})
        with open(test_paths["external"], encoding="utf-8") as f:
            state = json.load(f)
        assert "Para la carpeta externa" in state["whiteCards"]

    def test_get_custom_cards_reflects_import(self, client):
        import_json(client, BASE_SET)
        client.emit("get_custom_cards")
        listing = get_event(client.get_received(), "custom_cards_list")
        assert listing["success"] is True
        assert len(listing["whiteCards"]) == IMPORTED_WHITES
        assert len(listing["blackCards"]) == IMPORTED_BLACKS


class TestLiveRooms:
    def test_import_injects_into_active_room_pools(self, client, app_module):
        client.emit("join_game", {"name": "Tester", "room_id": "TESTX"})
        room = app_module.manager.rooms["TESTX"]
        whites_before = set(map(str, room.available_whites))
        res = import_json(client, {"whiteCards": ["Carta para sala activa"]})
        assert res["success"] is True
        assert "Carta para sala activa" in room.available_whites
        assert "Carta para sala activa" not in whites_before
        assert "Carta para sala activa" in app_module.manager.global_white_cards

    def test_global_deck_grows(self, client, app_module):
        n0 = len(app_module.manager.global_white_cards)
        import_json(client, {"whiteCards": ["Crece el global 1", "Crece el global 2"]})
        assert len(app_module.manager.global_white_cards) == n0 + 2
