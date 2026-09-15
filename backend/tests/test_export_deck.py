"""Matriz de exportación y round-trip (puerto del harness de /tmp a pytest).

Cubre el contrato que ejercitaba el harness E2E del exportador:
  - Formato del set exportado: exactamente whiteCards/blackCards, blancas como
    texto y negras como {text, pick} en rango 1-3.
  - Paridad disco: lo que devuelve get_custom_cards es lo que hay en
    cartasCustom.json (ruta interna) y en su espejo de la carpeta externa.
  - Tolerancia legacy: negras guardadas como texto plano se normalizan a
    {text, pick: 1} en la exportación (buildExportPayload del taller).
  - Robustez: cartasCustom.json corrupto no rompe el taller (se recupera).
  - Round-trip: el archivo exportado se re-importa en un mazo vacío sin
    descartes (0 inválidas, 0 duplicadas).
"""
import json

from conftest import get_event


def export_deck(client):
    """Lee el mazo por socket: es exactamente lo que serializa el botón
    'Exportar mazo' (CardWorkshop.buildExportPayload sobre custom_cards_list)."""
    client.emit("get_custom_cards")
    listing = get_event(client.get_received(), "custom_cards_list")
    assert listing["success"] is True
    return listing


def normalize_export(listing):
    """Normalización idéntica a buildExportPayload (CardWorkshop.jsx):
    blancas a texto plano y negras sueltas a {text, pick: 1}."""
    return {
        "whiteCards": [w if isinstance(w, str) else w["text"] for w in listing["whiteCards"]],
        "blackCards": [
            b if isinstance(b, dict) else {"text": b, "pick": 1}
            for b in listing["blackCards"]
        ],
    }


def assert_base_set_format(deck):
    """Invariante del formato base (lo que validaba e2e_export.py)."""
    assert set(deck.keys()) == {"whiteCards", "blackCards"}
    assert all(isinstance(w, str) and w.strip() for w in deck["whiteCards"]), "blanca no-string o vacía"
    assert all(
        isinstance(b, dict)
        and isinstance(b.get("text"), str) and b["text"].strip()
        and isinstance(b.get("pick"), int) and 1 <= b["pick"] <= 3
        for b in deck["blackCards"]
    ), "negra mal formada"


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class TestExportFormat:
    def test_export_empty_deck_is_valid_base_format(self, client):
        deck = normalize_export(export_deck(client))
        assert deck == {"whiteCards": [], "blackCards": []}
        assert_base_set_format(deck)

    def test_exported_deck_matches_base_set_format(self, client):
        client.emit("import_custom_cards", {"json": {
            "whiteCards": ["Blanca exportable uno", "Blanca exportable dos"],
            "blackCards": [{"text": "Negra exportable _", "pick": 1}],
        }})
        get_event(client.get_received(), "custom_cards_imported")

        deck = normalize_export(export_deck(client))
        assert_base_set_format(deck)
        assert deck["whiteCards"] == ["Blanca exportable uno", "Blanca exportable dos"]
        assert deck["blackCards"] == [{"text": "Negra exportable _", "pick": 1}]

    def test_legacy_plain_string_black_normalized_on_export(self, client, test_paths):
        # Carta legacy escrita a mano en disco (formato antiguo del archivo)
        with open(test_paths["internal"], "w", encoding="utf-8") as f:
            json.dump({"whiteCards": [], "blackCards": ["Negra legacy en texto plano _"]}, f, ensure_ascii=False)

        deck = normalize_export(export_deck(client))
        assert_base_set_format(deck)
        assert deck["blackCards"] == [{"text": "Negra legacy en texto plano _", "pick": 1}]


class TestDiskParity:
    def test_export_reflects_internal_file(self, client, test_paths):
        client.emit("import_custom_cards", {"json": {"whiteCards": ["Paridad interna"]}})
        get_event(client.get_received(), "custom_cards_imported")

        listing = export_deck(client)
        assert listing["whiteCards"] == ["Paridad interna"]
        assert read_json(test_paths["internal"])["whiteCards"] == ["Paridad interna"]

    def test_internal_and_external_mirrors_stay_in_sync(self, client, test_paths):
        client.emit("import_custom_cards", {"json": {"whiteCards": ["Espejo externo"]}})
        get_event(client.get_received(), "custom_cards_imported")

        internal = read_json(test_paths["internal"])
        external = read_json(test_paths["external"])
        assert internal == external
        assert "Espejo externo" in external["whiteCards"]

    def test_add_custom_card_mirrors_to_external_file(self, client, test_paths):
        client.emit("add_custom_card", {"type": "white", "text": "Añadida y espejada"})
        assert get_event(client.get_received(), "custom_card_result")["success"] is True

        external = read_json(test_paths["external"])
        assert "Añadida y espejada" in external["whiteCards"]


class TestCorruptionRobustness:
    def test_corrupted_internal_file_does_not_break_workshop(self, client, test_paths):
        with open(test_paths["internal"], "w", encoding="utf-8") as f:
            f.write("{esto no es json válido")

        client.emit("get_custom_cards")
        listing = get_event(client.get_received(), "custom_cards_list")
        assert listing["success"] is False
        assert "whiteCards" not in listing or listing.get("whiteCards") is None

    def test_import_survives_corrupted_custom_file(self, client, test_paths):
        with open(test_paths["internal"], "w", encoding="utf-8") as f:
            f.write("no-json")

        res_prep = None  # la corrupción no debe impedir una importación válida
        client.emit("import_custom_cards", {"json": {"whiteCards": ["Importa pese a corrupción"]}})
        res = get_event(client.get_received(), "custom_cards_imported")
        assert res["success"] is True
        assert read_json(test_paths["internal"])["whiteCards"] == ["Importa pese a corrupción"]

    def test_missing_files_report_empty_deck(self, client, test_paths):
        import os
        os.remove(test_paths["internal"])

        client.emit("get_custom_cards")
        listing = get_event(client.get_received(), "custom_cards_list")
        assert listing["success"] is True
        assert listing["whiteCards"] == []
        assert listing["blackCards"] == []


class TestRoundTrip:
    """Puerto de e2e_roundtrip.py: exportar → vaciar mazo → reimportar sin
    descartes. El archivo exportado debe ser idempotente consigo mismo."""

    def test_export_reimports_into_empty_deck_without_discards(self, client):
        # 1) Poblar y exportar
        client.emit("import_custom_cards", {"json": {
            "whiteCards": ["Ida y vuelta 1", "Ida y vuelta 2", "Ida y vuelta 3"],
            "blackCards": [{"text": "Ronda de ida y vuelta _", "pick": 1}],
        }})
        get_event(client.get_received(), "custom_cards_imported")
        exported = normalize_export(export_deck(client))
        assert_base_set_format(exported)

        # 2) Vaciar el mazo borrando carta a carta por socket (como el e2e)
        for w in list(exported["whiteCards"]):
            client.emit("delete_custom_card", {"type": "white", "text": w})
            get_event(client.get_received(), "custom_card_deleted")
        for b in list(exported["blackCards"]):
            client.emit("delete_custom_card", {"type": "black", "text": b["text"]})
            get_event(client.get_received(), "custom_card_deleted")

        emptied = export_deck(client)
        assert emptied["whiteCards"] == [] and emptied["blackCards"] == []

        # 3) Reimportar el archivo exportado (cliente envía objeto ya parseado)
        client.emit("import_custom_cards", {"json": exported})
        res = get_event(client.get_received(), "custom_cards_imported")
        assert res["success"] is True
        assert res["imported"] == {"white": 3, "black": 1}
        assert res["rejected_count"] == 0
        assert res["ignored_count"] == 0

        # 4) El mazo recuperado es idéntico al exportado
        recovered = normalize_export(export_deck(client))
        assert recovered == exported
