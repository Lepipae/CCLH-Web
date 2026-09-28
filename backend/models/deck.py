"""Mazo de cartas blancas: el único dueño de las cartas de una sala.

Antes el mazo era una `list` suelta (`room.available_whites`) más un contador
paralelo (`room.deck_size`) que alguien tenía que acordarse de incrementar en
cada carta nueva. Con dos fuentes de verdad, cualquier camino que se olvidara
de actualizar el contador producía un WARNING de "deriva" que no correspondía a
ninguna carta perdida, y la fuga real (una carta destruida de verdad) se
mezclaba entre el ruido.

Aquí el mazo es un objeto con una única lista interna. No existe ningún otro
sitio donde vivan las cartas disponibles, así que no puede desincronizarse nada:
`deck_size` pasa a ser un dato derivado. Las manos de los jugadores y las
submissions de la ronda son solo "dónde están ahora", y `audit()` reconcilia las
tres ubicaciones **por identidad de carta**, no por cantidad: con dos cartas de
texto idéntico, contar no puede decir cuál se perdió, pero los ids sí.
"""
import random
import uuid


class Card(str):
    """Carta blanca con identidad estable (`card_id`).

    Es subclase de `str` a propósito: el texto se sigue comparando, formateando
    y serializando a JSON exactamente igual que antes (el frontend no se entera
    de nada), pero cada carta lleva además un id que no se confunde con su
    texto. Dos cartas con el mismo texto son cartas distintas, y `Deck.audit()`
    puede distinguirlas.
    """

    def __new__(cls, text, card_id=None):
        card = super().__new__(cls, text)
        card.card_id = card_id or uuid.uuid4().hex[:12]
        return card

    def __repr__(self):
        return f"Card({str.__repr__(self)}, id={self.card_id})"


class Deck:
    """Dueño único de las cartas blancas de una sala.

    El mazo reparte (`deal`) y recoge (`return_`). Nadie más lo muta. `audit()`
    devuelve la lista de problemas de conservación que encuentre (vacía = todo
    cuadra) y es lo que la suite de tests corre después de cada acción.
    """

    def __init__(self, texts):
        # card_id -> Card: TODAS las cartas que existen en esta sala.
        self._cards = {}
        # card_ids de las que siguen disponibles para repartir.
        self._pool = []
        for text in texts:
            self._register(text)
        self.shuffle()

    # --- estado observable ---------------------------------------------------

    def _register(self, text, card_id=None):
        """Añade una carta nueva al mazo y devuelve su id."""
        card = text if isinstance(text, Card) else Card(text, card_id)
        self._cards[card.card_id] = card
        self._pool.append(card.card_id)
        return card.card_id

    @property
    def size(self):
        """Total de cartas que existen en la sala (repartidas + en el mazo)."""
        return len(self._cards)

    def count(self):
        """Cartas disponibles ahora mismo en el mazo."""
        return len(self._pool)

    def pool(self):
        """Copia de las cartas disponibles. Es una vista: mutarla no cambia el
        mazo. Para mover cartas hay que usar `deal`/`return_`/`inject`."""
        return [self._cards[card_id] for card_id in self._pool]

    # --- mutaciones (solo por aquí) -------------------------------------------

    def deal(self, count):
        """Reparte `count` cartas del mazo. Si no hay suficientes devuelve `[]`:
        es mejor repartir de menos que perder cartas."""
        if count <= 0 or len(self._pool) < count:
            return []
        drawn = random.sample(self._pool, count)
        for card_id in drawn:
            # Por identidad, no por texto: `list.remove(texto)` podía borrar
            # otra carta con el mismo texto que la muestreada.
            self._pool.remove(card_id)
        return [self._cards[card_id] for card_id in drawn]

    def return_(self, cards):
        """Devuelve al mazo cartas que estaban repartidas (manos, submissions)."""
        for card in cards:
            card_id = getattr(card, 'card_id', None)
            if card_id is None:
                # Carta suelta que no pasó por el mazo (p. ej. creada a mano en
                # un test). Se registra para que `audit` pueda seguirla.
                card_id = self._register(card)
            elif card_id not in self._cards:
                # Vuelve una carta que el mazo no conocía: la damos de alta en
                # vez de perderla, y `audit` seguirá viéndola.
                self._cards[card_id] = card
            self._pool.append(card_id)

    def inject(self, texts):
        """Añade cartas nuevas al mazo en caliente (custom cards, importación).
        Devuelve cuántas se han añadido."""
        added = 0
        for text in texts:
            if text is None or (isinstance(text, str) and not str(text).strip()):
                continue
            self._register(text)
            added += 1
        if added:
            self.shuffle()
        return added

    def shuffle(self):
        random.shuffle(self._pool)

    def clear(self):
        """Vacía el mazo (borrado de la sala). Rompe la conservación a propósito."""
        self._pool = []
        self._cards = {}

    # --- conservación ---------------------------------------------------------

    def audit(self, held=()):
        """Reconcilia mazo + repartidas y devuelve la lista de problemas.

        `held` es una lista de pares `(dónde, carta)`, donde `dónde` es una
        descripción legible ("mano de Ana", "jugada por Beto"). Devolver una
        lista de strings en vez de un booleano permite que un test falle
        mostrando exactamente qué se perdió o se duplicó.
        """
        problems = []
        seen = {}

        for card_id in self._pool:
            seen.setdefault(card_id, []).append("el mazo")

        for where, card in held:
            card_id = getattr(card, 'card_id', None)
            if card_id is None:
                problems.append(f"Carta sin identidad en {where}: {str(card)!r}")
                continue
            seen.setdefault(card_id, []).append(where)

        # Una misma carta en dos sitios a la vez.
        for card_id, places in seen.items():
            if len(places) > 1:
                problems.append(
                    f"Carta {card_id} ({str(self._cards.get(card_id, '?'))!r}) "
                    f"está duplicada en: {', '.join(places)}"
                )

        # Cartas que han desaparecido de donde estaban.
        for card_id, card in self._cards.items():
            if card_id not in seen:
                problems.append(f"Carta {card_id} ({str(card)!r}) ha desaparecido")

        # Cartas repartidas que el mazo no conoce (nadie las registró).
        for card_id in seen:
            if card_id not in self._cards:
                problems.append(f"Carta {card_id} está repartida pero no es de este mazo")

        return problems

    def summary(self):
        """Una línea con el reparto actual. Para logs."""
        return (f"{self.size} cartas, {self.count()} en el mazo, "
                f"{self.size - self.count()} repartidas")
