"""Cartas propias: crear, borrar, listar e importar sets, y vivirlas en caliente.

Todo lo que el administrador añade al mazo (el botón "añadir carta" del
taller, el import de un .json entero) vivía dentro de `GameManager`, en el
mismo archivo que la máquina de presencia, el reaper y el reparto de manos.
Con 1440 líneas, el dominio de las cartas propias era el único que nose parecía a nada de lo demás: ni el presence ni el outbox lo encapsulan, sólo
once métodos pegados al final, justo debajo de `_change_black_card_locked`,
que es donde ya no tocaba nadie.

Aquí viven. `GameManager` los mezcla con `CustomCardsMixin`, así que la API
pública no se mueve: `app.py` sigue llamando a `manager.add_custom_card(...)`
y los tests siguen viendo exactamente el mismo objeto.

Dos detalles que el mixin respeta a propósito, porque son el comportamiento
que había y no una decisión que tomar aquí:

1. `_delete_custom_card_locked` REASIGNA `self.global_black_cards` a una
   lista nueva en vez de mutarla en sitio (las blancas sí se quitan con
   `remove`). Como el mixin se mezcla en `GameManager` y no la envuelve, ese
   `self` es el `GameManager`: la reasignación sigue recolocando el
   atributo en la instancia, igual que antes. Si algún día esto se convierte
   en una clase de verdad con un `__init__` propio, esa línea pasa a ser un
   bug silencioso.

2. Estas operaciones son del REGISTRO, no de una mesa: mutan el mazo global y
   el archivo, y para entregar la carta en vivo tienen que tocar TODAS las
   salas. Por eso el punto de entrada es `_tx_registro()` y no `_tx_sala()`: el
   orden de cerrojos es registro -> sala, y la inyección se hace sala a sala
   con `_sala(room)` desde dentro. El lote se drena al salir, ya sin ningún
   cerrojo de juego tomado, así que el `respond_cb` del cliente que pregunta
   sigue sin escribir en el socket con el registro encima.

Y una nota sobre las rutas, que son dos y no una: `_get_custom_file_path()`
(la interna, `DataScraping/cartasCustom.json`) manda sobre
`_get_external_custom_dir()` + `cartasCustom.json` (la que ve el usuario).
Se lee la que exista, prefiriendo la interna, y se ESCRIBE en las dos, aunque
la interna falle en silencio porque la externa es la que puede vivir en un
volumen montado. Ese "silencio" es intencionado y lo cubre
`tests/test_export_deck.py`.
"""
import difflib
import json
import os
import random

# Límites de una carta, los MISMOS para el alta manual y para la importación de
# un set entero. Estaban solo en el importador y por eso el alta manual podía
# colar una carta que el importador habría rechazado.
MAX_TEXT_LEN = 200
MAX_PICK = 3


class CustomCardsMixin:
    """Cartas propias del mazo global: alta, baja, listado e importación.

    Es un mixin y no una clase aparte porque estas cartas no son un subsistema
    aparte: mutan `self.global_white_cards` / `self.global_black_cards` y se
    inyectan en los mazos de las salas vivas (`room.deck`, `room.available_blacks`),
    es decir, dependen del estado y de las salas que posee el GameManager. Sin
    el GameManager no hay nada que hacer aquí; con él, el estado de una carta
    propia es el de una carta normal y no el de una copia aparte.
    """

    def _get_custom_file_path(self):
        # Sobrescribible por entorno: los tests la aíslan para no tocar datos reales
        env_path = os.environ.get("INTERNAL_CUSTOM_PATH")
        if env_path:
            return os.path.abspath(env_path)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.abspath(os.path.join(base_dir, "..", "DataScraping", "cartasCustom.json"))

    def _get_external_custom_dir(self):
        env_dir = os.environ.get("EXTERNAL_CARDS_DIR")
        if env_dir:
            return os.path.abspath(env_dir)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.abspath(os.path.join(base_dir, "..", "..", "custom_cards"))

    def _save_custom_data(self, custom_data):
        internal_path = self._get_custom_file_path()
        os.makedirs(os.path.dirname(internal_path), exist_ok=True)
        with open(internal_path, 'w', encoding='utf-8') as f:
            json.dump(custom_data, f, ensure_ascii=False, indent=2)

        try:
            external_dir = self._get_external_custom_dir()
            os.makedirs(external_dir, exist_ok=True)
            external_path = os.path.join(external_dir, "cartasCustom.json")
            with open(external_path, 'w', encoding='utf-8') as f:
                json.dump(custom_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("Error exportando cartas a carpeta externa:", e)

    def add_custom_card(self, card_type, text, pick, respond_cb):
        # Transacción completa: muta el mazo global, el archivo y las salas.
        # El callback se difiere: escribirle al cliente que pregunta es E/S, y
        # el drena sale al terminar, ya sin ningún cerrojo de juego.
        with self._tx_registro():
            respond_cb = self._outbox.deferred(respond_cb)
            self._add_custom_card_locked(card_type, text, pick, respond_cb)

    def _add_custom_card_locked(self, card_type, text, pick, respond_cb):
        if not isinstance(text, str):
            respond_cb({'success': False, 'message': 'El texto no es un texto.'})
            return
        text = text.strip()
        if not text:
            respond_cb({'success': False, 'message': 'El texto está vacío.'})
            return
        if len(text) > MAX_TEXT_LEN:
            respond_cb({'success': False,
                        'message': f'El texto es demasiado largo (máximo {MAX_TEXT_LEN} caracteres).'})
            return
        if card_type not in ('white', 'black'):
            respond_cb({'success': False, 'message': 'Tipo de carta inválido.'})
            return

        def check_sim(new_txt, lst, thresh=0.85):
            n_l = new_txt.lower()
            for c in lst:
                tc = c if isinstance(c, str) else c.get('text', '')
                if difflib.SequenceMatcher(None, n_l, tc.lower().strip()).ratio() >= thresh:
                    return True, tc
            return False, None
            
        custom_path = self._get_custom_file_path()
        external_path = os.path.join(self._get_external_custom_dir(), "cartasCustom.json")
        custom_data = {"whiteCards": [], "blackCards": []}
        
        target_path = custom_path if os.path.exists(custom_path) else (external_path if os.path.exists(external_path) else None)
        if target_path:
            try:
                with open(target_path, 'r', encoding='utf-8') as f:
                    custom_data = json.load(f)
            except Exception as e:
                print("Error leyendo json custom:", e)

        if card_type == 'white':
            is_sim, match = check_sim(text, self.global_white_cards)
            if is_sim:
                respond_cb({'success': False, 'message': f'Muy similar a una carta existente: "{match}"'})
                return
            self.global_white_cards.append(text)
            if 'whiteCards' not in custom_data:
                custom_data['whiteCards'] = []
            if text not in custom_data['whiteCards']:
                custom_data['whiteCards'].append(text)
            for room in self.rooms.values():
                with self._sala(room):
                    room.deck.inject([text])  # el mazo crece solo, sin contador paralelo
        elif card_type == 'black':
            # El `pick` (cuántas cartas hay que jugar para poder ganar la ronda)
            # lo envía el cliente y NO es inocuo: con un `pick` por encima del
            # tamaño de la mano, la ronda no la puede completar nadie y la mesa
            # se queda en 'playing' para siempre, sin purga ni salida. Se valida
            # aquí, con el mismo tope que usa el importador.
            try:
                pick = int(pick)
            except (TypeError, ValueError):
                respond_cb({'success': False,
                            'message': 'El número de cartas a elegir no es un número.'})
                return
            if not 1 <= pick <= MAX_PICK:
                respond_cb({'success': False,
                            'message': f'El número de cartas a elegir debe estar entre 1 y {MAX_PICK}.'})
                return
            is_sim, match = check_sim(text, self.global_black_cards)
            if is_sim:
                respond_cb({'success': False, 'message': f'Muy similar a una carta existente: "{match}"'})
                return
            new_c = {'text': text, 'pick': pick}
            self.global_black_cards.append(new_c)
            if 'blackCards' not in custom_data:
                custom_data['blackCards'] = []
            custom_data['blackCards'].append(new_c)
            for room in self.rooms.values():
                with self._sala(room):
                    room.available_blacks.append(new_c)
                    random.shuffle(room.available_blacks)
        else:
            respond_cb({'success': False, 'message': 'Tipo de carta inválido.'})
            return

        try:
            self._save_custom_data(custom_data)
        except Exception as e:
            respond_cb({'success': False, 'message': f'Error guardando cartas custom: {str(e)}'})
            return
            
        respond_cb({'success': True, 'whiteCards': custom_data.get('whiteCards', []), 'blackCards': custom_data.get('blackCards', [])})

    def get_custom_cards(self, respond_cb):
        with self._tx_registro():
            respond_cb = self._outbox.deferred(respond_cb)
            custom_path = self._get_custom_file_path()
            external_path = os.path.join(self._get_external_custom_dir(), "cartasCustom.json")
            target_path = custom_path if os.path.exists(custom_path) else (external_path if os.path.exists(external_path) else None)

            if target_path:
                try:
                    with open(target_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        respond_cb({'success': True, 'whiteCards': data.get('whiteCards', []), 'blackCards': data.get('blackCards', [])})
                        return
                except Exception as e:
                    respond_cb({'success': False, 'message': str(e)})
                    return
            respond_cb({'success': True, 'whiteCards': [], 'blackCards': []})

    def delete_custom_card(self, card_type, text, respond_cb):
        with self._tx_registro():
            respond_cb = self._outbox.deferred(respond_cb)
            self._delete_custom_card_locked(card_type, text, respond_cb)

    def _delete_custom_card_locked(self, card_type, text, respond_cb):
        custom_path = self._get_custom_file_path()
        external_path = os.path.join(self._get_external_custom_dir(), "cartasCustom.json")
        target_path = custom_path if os.path.exists(custom_path) else (external_path if os.path.exists(external_path) else None)
        
        if not target_path:
            respond_cb({'success': False, 'message': 'Archivo no encontrado.'})
            return
        try:
            with open(target_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            if card_type == 'white':
                w_list = data.get('whiteCards', [])
                data['whiteCards'] = [c for c in w_list if (c if isinstance(c, str) else c.get('text')) != text]
                if text in self.global_white_cards:
                    self.global_white_cards.remove(text)
            elif card_type == 'black':
                b_list = data.get('blackCards', [])
                data['blackCards'] = [c for c in b_list if (c if isinstance(c, str) else c.get('text')) != text]
                self.global_black_cards = [c for c in self.global_black_cards if (c if isinstance(c, str) else c.get('text')) != text]
            
            self._save_custom_data(data)
            respond_cb({'success': True, 'whiteCards': data.get('whiteCards', []), 'blackCards': data.get('blackCards', [])})
        except Exception as e:
            respond_cb({'success': False, 'message': str(e)})

    @staticmethod
    def _card_texts(cards):
        """Textos normalizados de una lista de cartas (acepta str o {text, pick})."""
        out = set()
        for c in cards:
            t = c if isinstance(c, str) else (c.get('text') if isinstance(c, dict) else None)
            if isinstance(t, str) and t.strip():
                out.add(t.strip())
        return out

    def import_custom_cards(self, raw_json, respond_cb):
        """
        Importa un set completo de cartas desde el contenido de un archivo .json
        con el mismo formato que los sets base (p. ej. CAH-es-set.json):

            { "whiteCards": ["texto", ...],
              "blackCards": [ { "text": "...", "pick": 2 }, ... ] }

        Valida carta a carta: las inválidas (tipo/longitud/pick/huecos) y las
        duplicadas (dentro del archivo, en cartasCustom.json o en el mazo base)
        se descartan individualmente; el resto se guarda y se inyecta en el
        mazo global y en las salas activas, igual que add_custom_card.
        """
        with self._tx_registro():
            respond_cb = self._outbox.deferred(respond_cb)
            self._import_custom_cards_locked(raw_json, respond_cb)

    def _import_custom_cards_locked(self, raw_json, respond_cb):

        MAX_DETAILS = 10     # ejemplos de rechazo/omisión devueltos al cliente
        # MAX_TEXT_LEN y MAX_PICK son de módulo: los comparte con el alta manual.

        # 1) Parsear el JSON crudo y exigir la estructura del formato base
        if isinstance(raw_json, (dict, list)):
            data = raw_json  # el cliente también puede enviar el objeto ya parseado
        else:
            try:
                data = json.loads(raw_json)
            except (TypeError, ValueError) as e:
                respond_cb({'success': False, 'message': f'JSON inválido: {e}'})
                return

        if not isinstance(data, dict) or ('whiteCards' not in data and 'blackCards' not in data):
            respond_cb({'success': False,
                        'message': 'Estructura no reconocida: se esperaba un objeto con "whiteCards" y/o "blackCards".'})
            return

        def clean_text(value):
            """Texto listo para guardar, o None si no es válido como carta."""
            if not isinstance(value, str):
                return None
            text = value.strip()
            if not text or len(text) > MAX_TEXT_LEN:
                return None
            return text

        rejected, ignored = [], []
        n_rejected = n_ignored = 0

        def reject(reason):
            nonlocal n_rejected
            n_rejected += 1
            if len(rejected) < MAX_DETAILS:
                rejected.append(reason)

        def ignore(reason):
            nonlocal n_ignored
            n_ignored += 1
            if len(ignored) < MAX_DETAILS:
                ignored.append(reason)

        # Cartas que ya están en juego (base + customs cargadas al arrancar)
        existing_whites = self._card_texts(self.global_white_cards)
        existing_blacks = self._card_texts(self.global_black_cards)

        # 2) Blancas: cadenas de texto no vacías y no duplicadas
        new_whites = []
        raw_whites = data.get('whiteCards', [])
        if not isinstance(raw_whites, list):
            reject('whiteCards: no es una lista')
            raw_whites = []
        for i, item in enumerate(raw_whites):
            text = clean_text(item)
            if text is None:
                if not isinstance(item, str):
                    reject(f'Blanca #{i + 1}: debe ser texto, no {type(item).__name__}')
                elif len(item.strip()) > MAX_TEXT_LEN:
                    reject(f'Blanca #{i + 1}: demasiado larga (máx. {MAX_TEXT_LEN} caracteres)')
                else:
                    reject(f'Blanca #{i + 1}: vacía')
                continue
            if text in new_whites:
                ignore(f'Blanca duplicada en el archivo: "{text[:40]}"')
                continue
            if text in existing_whites:
                ignore(f'Ya existe en el mazo: "{text[:40]}"')
                continue
            new_whites.append(text)

        # 3) Negras: {text, pick}; se tolera texto plano como pick=1
        new_blacks = []
        new_black_texts = set()
        raw_blacks = data.get('blackCards', [])
        if not isinstance(raw_blacks, list):
            reject('blackCards: no es una lista')
            raw_blacks = []
        for i, item in enumerate(raw_blacks):
            if isinstance(item, dict):
                text = clean_text(item.get('text'))
                raw_pick = item.get('pick', 1)
            elif isinstance(item, str):
                text = clean_text(item)
                raw_pick = 1
            else:
                text, raw_pick = None, 1

            if text is None:
                reject(f'Negra #{i + 1}: falta o es inválido el texto')
                continue

            try:
                pick = int(raw_pick)  # se tolera "2" o 2.0
            except (TypeError, ValueError):
                reject(f'Negra #{i + 1}: "pick" no es un número ({raw_pick!r})')
                continue
            if not 1 <= pick <= MAX_PICK:
                reject(f'Negra #{i + 1}: "pick" fuera de rango (1-{MAX_PICK}): {pick}')
                continue
            # Una negra pick>=2 necesita al menos `pick` huecos (_) para ser jugable
            if pick >= 2 and text.count('_') < pick:
                reject(f'Negra #{i + 1}: necesita al menos {pick} guion(es) bajo(s) "_" para pick={pick}')
                continue
            if text in new_black_texts:
                ignore(f'Negra duplicada en el archivo: "{text[:40]}"')
                continue
            if text in existing_blacks:
                ignore(f'Ya existe en el mazo: "{text[:40]}"')
                continue

            new_black_texts.add(text)
            new_blacks.append({'text': text, 'pick': pick})

        if not new_whites and not new_blacks:
            respond_cb({'success': False,
                        'message': f'Ninguna carta válida para importar ({n_rejected} inválidas, {n_ignored} duplicadas).',
                        'rejected': rejected,
                        'ignored': ignored})
            return

        # 4) Fusionar con cartasCustom.json y guardar (ruta interna + externa)
        custom_path = self._get_custom_file_path()
        external_path = os.path.join(self._get_external_custom_dir(), "cartasCustom.json")
        target_path = custom_path if os.path.exists(custom_path) else (external_path if os.path.exists(external_path) else None)
        custom_data = {"whiteCards": [], "blackCards": []}
        if target_path:
            try:
                with open(target_path, 'r', encoding='utf-8') as f:
                    custom_data = json.load(f)
            except Exception as e:
                print("Error leyendo json custom:", e)
        if not isinstance(custom_data, dict):
            custom_data = {"whiteCards": [], "blackCards": []}

        file_whites = self._card_texts(custom_data.get('whiteCards', []))
        file_blacks = self._card_texts(custom_data.get('blackCards', []))
        for w in new_whites:
            if w not in file_whites:
                custom_data.setdefault('whiteCards', []).append(w)
        for b in new_blacks:
            if b['text'] not in file_blacks:
                custom_data.setdefault('blackCards', []).append(b)

        try:
            self._save_custom_data(custom_data)
        except Exception as e:
            respond_cb({'success': False, 'message': f'Error guardando cartas custom: {e}'})
            return

        # 5) Inyectar en el mazo global y en las salas activas (en directo)
        self.global_white_cards.extend(new_whites)
        self.global_black_cards.extend(new_blacks)
        for room in self.rooms.values():
            with self._sala(room):
                if new_whites:
                    room.deck.inject(new_whites)
                if new_blacks:
                    room.available_blacks.extend(new_blacks)
                    random.shuffle(room.available_blacks)

        respond_cb({'success': True,
                    'imported': {'white': len(new_whites), 'black': len(new_blacks)},
                    'rejected_count': n_rejected,
                    'ignored_count': n_ignored,
                    'rejected': rejected,
                    'ignored': ignored,
                    'whiteCards': custom_data.get('whiteCards', []),
                    'blackCards': custom_data.get('blackCards', [])})
