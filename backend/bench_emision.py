"""Banco de medición de la emisión: cuánto se para el resto del servidor por un
cliente lento.

La sonda es `manager.heartbeat(sid)`: una operación que solo necesita el cerrojo
de una sala y no hace NADA de E/S (renueva un `last_seen` y sale). Lo que tarda
es, exactamente, el tiempo que ese cerrojo estuvo retenido. En el hilo
principal, en cambio, se simula un socket de 5 ms por mensaje, que es lo que
costaba un `emit` de verdad.

    python bench_emision.py

Compara el comportamiento actual con el anterior al cambio (el drenaje por
dentro del `with self._lock`, que se reproduce parcheando `_tx_sala` para que
escriba el lote sin haber soltado el cerrojo de la sala).
"""
import os
import sys
import threading
import time
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import models.game_manager as gm_module  # noqa: E402

BLANCAS = [f"blanca {i}" for i in range(60)]
NEGRAS = [{"text": f"negra {i} _", "pick": 1} for i in range(20)]
EMIT_MS = 5.0     # lo que cuesta un emit en un socket de verdad (medido)
SALAS = 41
CONECTADOS = 49


class SocketLento:
    def __init__(self, ms):
        self.pausa = ms / 1000.0

    def emit(self, event, payload=None, to=None):
        time.sleep(self.pausa)

    def start_background_task(self, fn, *a, **k):
        return None

    def sleep(self, s):
        return None


def manager_nuevo():
    gm = gm_module.GameManager(SocketLento(EMIT_MS), list(BLANCAS), list(NEGRAS))
    gm.rooms.clear()
    gm._outbox._channels.clear()
    return gm


def drenar_dentro_del_cerrojo(gm):
    """Reproduce el comportamiento de antes del cambio: el lote se escribe en el
    socket con el cerrojo de la sala todavía tomado.

    Solo afecta a la transacción MÁS EXTERNA (las anidadas se funden con la de
    fuera, como siempre): es la que en el modelo antiguo sostenía el cerrojo
    global mientras escribía. Vaciar el lote después de escribirlo evita que el
    drenaje "correcto" de la transacción emita todo otra vez."""
    original = gm_module.GameManager._tx_sala

    @contextmanager
    def tx_sala_ingenua(self, room_id, room=None):
        if self._outbox._stack():          # anidada: normal
            with original(self, room_id, room) as sala:
                yield sala
            return
        with original(self, room_id, room) as sala:
            try:
                yield sala
            finally:
                if self._outbox._stack():
                    lote = self._outbox._stack()[-1]
                    self._outbox.flush(lote)     # <- aquí, con el cerrojo tomado
                    lote.updates.clear()
                    lote.directs.clear()
                    lote.replies.clear()

    gm_module.GameManager._tx_sala = tx_sala_ingenua
    return lambda: setattr(gm_module.GameManager, "_tx_sala", original)


def sonda(gm, trabajo, esperar_ms=EMIT_MS * 2):
    """Cuánto se para un hilo que solo necesita el cerrojo mientras `trabajo`
    (que sí hace E/S lenta) retiene el servidor."""
    paradas = []
    listo = threading.Event()

    def rival():
        listo.wait(5)
        time.sleep(esperar_ms / 1000.0)      # situarse a mitad del trabajo
        t0 = time.perf_counter()
        gm.heartbeat(gm.sondas[0])           # sin E/S: solo pide el cerrojo
        paradas.append((time.perf_counter() - t0) * 1000)

    hilo = threading.Thread(target=rival)
    listo.set()
    hilo.start()
    t0 = time.perf_counter()
    trabajo()
    trabajo_tarda = (time.perf_counter() - t0) * 1000
    hilo.join(10)
    return (max(paradas) if paradas else 0.0), trabajo_tarda


def difusion(gm):
    # Repetible a propósito (jugar una carta solo se puede una vez): cada voto
    # difunde la sala entera, 8 emisiones lentas, y eso es lo que se mide.
    gm.vote_renew(gm.sondas[2], "S0")


def preparar_difusion(gm, jugadores=8):
    for i in range(jugadores):
        gm.join_game(f"S{i}", f"J{i}", "S0")
        gm.heartbeat(f"S{i}")
    gm.sondas = list(gm.rooms["S0"].players)
    gm.rooms["S0"].options['turn_order'] = 'sequential'
    gm.start_game(gm.sondas[0], "S0")


def preparar_servidor(gm):
    """41 salas, 49 jugadores conectados: el escenario que se midió en el
    despliegue real (el reaper recorre todas las salas cada 15 s)."""
    n = 0
    for s in range(SALAS):
        sala = f"S{s}"
        for _ in range(2):
            gm.join_game(f"S{s}-{n}", f"J{n}", sala)
            gm.heartbeat(f"S{s}-{n}")
            n += 1
            if n >= CONECTADOS:
                break
        if n >= CONECTADOS:
            break
    # La sonda mira el cerrojo de la sala S0, que es la primera que recorre el
    # reaper: es donde antes se notaba el atasco y donde ahora no debe notarse.
    gm.sondas = ["S0-0"]


def main():
    print(f"socket simulado: {EMIT_MS:.0f} ms por mensaje\n")
    escenarios = [("difusión a 8 jugadores", preparar_difusion, difusion),
                  (f"pasada del reaper ({SALAS} salas, {CONECTADOS} conectados)",
                   preparar_servidor, lambda gm: gm.reap_empty_rooms())]
    print(f"{'escenario':<44}{'antes':>12}{'ahora':>12}{'ganancia':>10}")
    print("-" * 78)
    for titulo, preparar, trabajo in escenarios:
        measured = []
        for dentro in (True, False):
            gm = manager_nuevo()
            preparar(gm)
            # `dentro` parchea la CLASE, así que hay que devolverla a su sitio
            # antes de medir el otro extremo (si no, los dos serían "antes").
            restaurar = drenar_dentro_del_cerrojo(gm) if dentro else None
            try:
                peor = max(sonda(gm, lambda: trabajo(gm))[0] for _ in range(3))
            finally:
                if restaurar:
                    restaurar()
            measured.append(peor)
        antes, ahora = measured
        print(f"{titulo:<44}{antes:>9.1f} ms{ahora:>9.1f} ms"
              f"{antes / max(ahora, 0.001):>9.0f}x")


if __name__ == '__main__':
    main()
