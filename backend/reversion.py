"""Comprobación por reversión de las defensas del servidor.

Para cada pieza del diseño se revierte el cambio, se ve qué tests lo detectan y
se restaura el fichero. Un test que no falla al deshacer lo que dice proteger no
está protegiendo nada.

AVISO: edita el código fuente de verdad (parchea y restaura en un `finally`).
No lo lances con el árbol en un estado que te importe perder.

    python reversion.py
"""
import pathlib
import re
import subprocess
import sys

CAMBIOS = [
    # --- Presencia en dos fases -------------------------------------------
    ("responder no perdona la sospecha (gracia no reversible)",
     "models/presence.py",
     "        if self.state == SOSPECHOSO:\n            self.state = VIVO\n            self.since = now\n            self.deadline = None\n            self.grace = None\n            return True\n        return False\n",
     "        return False\n",
     "tests/test_presence_machine.py tests/test_reaper_defenses.py"),

    ("la sospecha no avisa a nadie",
     "models/game_manager.py",
     "        for otro_sid, otro in room.players.items():\n            if otro.is_connected:\n                self._outbox.add_direct(room.room_id, 'presence_alert', aviso,\n                                        to=otro_sid)\n",
     "        for otro_sid, otro in []:\n            if otro.is_connected:\n                self._outbox.add_direct(room.room_id, 'presence_alert', aviso,\n                                        to=otro_sid)\n",
     "tests/test_presence_machine.py tests/test_emit_locking.py"),

    ("la gracia se renueva en cada pasada del reaper",
     "models/presence.py",
     "        if self.state != VIVO:\n            return False\n",
     "        if self.state == SOSPECHOSO:\n            self.deadline = now + policy.grace\n            return False\n        if self.state != VIVO:\n            return False\n",
     "tests/test_presence_machine.py"),

    ("sin prueba previa de latido se sospecha igual",
     "models/game_manager.py",
     "                if not p.is_connected or p.heartbeat_pongs == 0:\n                    continue\n",
     "                if not p.is_connected:\n                    continue\n",
     "tests/test_presence_machine.py tests/test_reaper_defenses.py"),

    ("sospechar y purgar en la misma pasada (elif -> if)",
     "models/game_manager.py",
     "                if p.presence.accuse(now, self.PRESENCE):\n"
     "                    sospechosos.append((room, sid, p))\n"
     "                elif p.presence.purge_due(now):\n",
     "                if p.presence.accuse(now, self.PRESENCE):\n"
     "                    sospechosos.append((room, sid, p))\n"
     "                if p.presence.purge_due(now):\n",
     "tests/test_presence_machine.py"),

    ("el estado de presencia no viaja en el game_update",
     "models/player.py",
     "            'presence': self.presence.to_dict(),\n",
     "",
     "tests/test_presence_machine.py"),

    # --- Límites de salas -------------------------------------------------
    ("sin techo de salas: join_game crea las que le pidan",
     "models/game_manager.py",
     "            if len(self.rooms) >= self.MAX_ROOMS:\n",
     "            if False and len(self.rooms) >= self.MAX_ROOMS:\n",
     "tests/test_limites_de_sala.py"),

    ("el techo se comprueba ANTES de saber si la sala es nueva (cierra mesas vivas)",
     "models/game_manager.py",
     "        if room_id not in self.rooms:\n",
     "        if len(self.rooms) >= self.MAX_ROOMS:\n"
     "            self.salas_rechazadas += 1\n"
     "            self._outbox.add_direct(room_id, 'join_error', {'message': 'lleno'}, to=sid)\n"
     "            return False\n"
     "        if room_id not in self.rooms:\n",
     "tests/test_limites_de_sala.py"),

    ("el rechazo no avisa al cliente (se traga el error)",
     "models/game_manager.py",
     "                self._outbox.add_direct(room_id, 'join_error', {\n"
     "                    'message': 'El servidor est\u00e1 lleno de salas ahora mismo. '\n"
     "                               'Prueba con otro c\u00f3digo en un momento.'}, to=sid)\n"
     "                return False\n",
     "                return False\n",
     "tests/test_limites_de_sala.py"),

    ("un solo plazo para todas las salas (el de la partida, para las nuevas)",
     "models/game_manager.py",
     "            politica = (self.ROOM_LIFECYCLE if room.ha_empezado\n"
     "                        else self.ROOM_LIFECYCLE_NUEVA)\n",
     "            politica = self.ROOM_LIFECYCLE\n",
     "tests/test_limites_de_sala.py tests/test_reaper_defenses.py"),

    ("un solo plazo para todas las salas (el corto, para las partidas)",
     "models/game_manager.py",
     "            politica = (self.ROOM_LIFECYCLE if room.ha_empezado\n"
     "                        else self.ROOM_LIFECYCLE_NUEVA)\n",
     "            politica = self.ROOM_LIFECYCLE_NUEVA\n",
     "tests/test_limites_de_sala.py"),

    ("la marca de 'ya empezó' se pierde al revertir a waiting",
     "models/game_manager.py",
     "                room.state = 'playing'\n                room.ha_empezado = True\n",
     "                room.state = 'playing'\n",
     "tests/test_limites_de_sala.py"),
]


def suite(objetivos):
    return subprocess.run(
        [sys.executable, "-m", "pytest"] + objetivos.split()
        + ["-q", "--no-header", "-p", "no:cacheprovider"],
        capture_output=True, text=True).stdout


for nombre, fichero, viejo, nuevo, objetivos in CAMBIOS:
    ruta = pathlib.Path(fichero)
    original = ruta.read_text(encoding="utf-8")
    if viejo not in original:
        print(f"??  {nombre}: el patrón a revertir no está en {fichero}")
        continue
    ruta.write_text(original.replace(viejo, nuevo, 1), encoding="utf-8")
    try:
        salida = suite(objetivos)
        fallos = re.findall(r"^FAILED (\S+)", salida, re.M)
        resumen = re.findall(r"(\d+ (?:failed|passed))", salida)
        if fallos:
            print(f"OK  {nombre}")
            for f in fallos[:4]:
                print(f"      - {f}")
        else:
            print(f"!!  {nombre}: NADIE LO DETECTA ({resumen})")
    finally:
        ruta.write_text(original, encoding="utf-8")
