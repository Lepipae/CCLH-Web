"""Edge cases de desconexión: liderazgo, rotación de juez y autoasanado.

Comprueba que al marcharse el líder o el juez la sala nunca queda congelada:
el relevo siempre recae en un jugador conectado y la partida continúa (o
vuelve a la pantalla de espera si ya no hay jugadores suficientes).
"""
import time

import pytest


def make_client(app_module):
    """Cliente Socket.IO adicional e independiente (cada uno con su sid)."""
    c = app_module.socketio.test_client(app_module.app)
    c.get_received()
    return c


def sid_of(room, name):
    """Sid de un jugador por nombre; falla si no está en la sala."""
    for s, p in room.players.items():
        if p.name == name:
            return s
    raise AssertionError(f"{name} no está en la sala {room.room_id}")


def join(manager, room_id, *names):
    """Une varios jugadores (cada uno con su cliente) y devuelve (room, sids)."""
    clients = {}
    from flask import Flask  # noqa: F401  (solo para dejar claro el contexto)

    raise NotImplementedError


@pytest.fixture()
def room_clients(app_module, manager):
    """Fábrica de jugadores: join(name) -> cliente; los sids se leen del room."""
    created = []

    def _join(name, room_id):
        c = make_client(app_module)
        c.emit("join_game", {"name": name, "room_id": room_id})
        created.append(c)
        room = app_module.manager.rooms[room_id]
        return c, sid_of(room, name)

    yield _join

    for c in created:
        try:
            c.disconnect()
        except Exception:
            pass


class TestLeaderHandoff:
    def test_leader_disconnect_promotes_a_connected_player(self, room_clients):
        _, alice = room_clients("Alice", "T1")
        _, bob = room_clients("Bob", "T1")
        _, carol = room_clients("Carol", "T1")
        room = room_clients.__self__ if False else None  # noqa: F841
        from app import manager
        room = manager.rooms["T1"]

        manager.disconnect(alice)

        assert room.leader in (bob, carol)
        assert room.leader != alice
        assert room.players[room.leader].is_connected

    def test_leader_disconnect_prefers_non_spectator(self, app_module, room_clients):
        from app import manager
        _, alice = room_clients("Alice", "T2")
        _, bob = room_clients("Bob", "T2")
        room = manager.rooms["T2"]
        room.options["turn_order"] = "sequential"  # czar determinista
        from flask import json  # noqa: F401
        app_module.socketio.test_client  # noqa: B018
        # Alice (líder) inicia la partida con Bob; Carol entra luego como espectadora
        alice_client = _client_for(app_module, alice)
        alice_client.emit("start_game", {"room_id": "T2"})
        _, carol = room_clients("Carol", "T2")
        assert manager.rooms["T2"].players[carol].waiting_next_round is True

        manager.disconnect(alice)

        assert room.leader == bob  # espectadora no puede recibir el liderazgo


def _client_for(app_module, sid):
    """Cliente asociado a un sid (los sids de test client son estables)."""
    for c in app_module.socketio.server.eio.clients.values() if hasattr(app_module.socketio, "server") else []:
        pass
    raise NotImplementedError
