// Funciones puras del taller de cartas (sin React, sin socket, sin DOM).
//
// Viven aquí y no en CardWorkshop.jsx para poder probarlas con vitest sin
// montar ningún componente. La verdad del juego sigue estando en el servidor
// (`game_update` / `custom_cards_imported`): todo lo que devuelven estas
// funciones es un PREVIEW para el usuario. El servidor re-valida cada carta
// al importar (longitud, pick, huecos, duplicadas, similitud difflib 0.85).

// Mismos topes que el backend (`backend/models/custom_cards.py`):
// MAX_TEXT_LEN=200, MAX_PICK=3. El umbral 0.85 es el de `check_sim` con
// difflib.SequenceMatcher; aquí solo se usa como AVISO en el preview.
export const IMPORT_RULES = {
  maxTextLen: 200,
  maxPick: 3,
  similarityThreshold: 0.85,
  maxIssues: 10,
  maxFileBytes: 5 * 1024 * 1024,
}

/** true si el texto es solo un número ("12345", "-3,14"): sin valor de juego. */
export function isNumericText(text) {
  if (typeof text !== 'string') return true
  const t = text.trim()
  if (!t) return false
  return /^-?\d+([.,]\d+)?$/.test(t)
}

function bigrams(s) {
  const out = []
  for (let i = 0; i < s.length - 1; i++) out.push(s.slice(i, i + 2))
  return out
}

/** Similitud Dice sobre bigramas (0..1). Aproximación barata a
 *  difflib.SequenceMatcher.ratio() del servidor: basta para avisar en el
 *  preview; el servidor re-valida con difflib de verdad. */
export function textSimilarity(a, b) {
  const na = (a || '').toLowerCase().trim()
  const nb = (b || '').toLowerCase().trim()
  if (na === nb) return 1
  if (na.length < 2 || nb.length < 2) return na === nb ? 1 : 0
  const ba = bigrams(na)
  const bb = bigrams(nb)
  const counts = new Map()
  for (const g of bb) counts.set(g, (counts.get(g) || 0) + 1)
  let inter = 0
  for (const g of ba) {
    const c = counts.get(g) || 0
    if (c > 0) {
      inter++
      counts.set(g, c - 1)
    }
  }
  return (2 * inter) / (ba.length + bb.length)
}

function cardTextOf(item) {
  if (typeof item === 'string') return item
  if (item && typeof item === 'object' && typeof item.text === 'string') return item.text
  return null
}

/** Busca el primer texto de `existing` similar a `text` (>= umbral). */
export function findSimilar(text, existing, threshold = IMPORT_RULES.similarityThreshold) {
  if (!text || !Array.isArray(existing)) return null
  for (const item of existing) {
    const other = cardTextOf(item)
    if (typeof other !== 'string') continue
    if (textSimilarity(text, other) >= threshold) return other.trim()
  }
  return null
}

/** Pre-valida un .json de cartas sin tocar el servidor.
 *  `existing` (opcional) = { whiteCards, blackCards } ya en el mazo: solo
 *  genera avisos `warnings[]` de similitud, nunca rechaza (el servidor decide).
 *  Devuelve { valid, fatal, message, summary, issues, warnings }. */
export function validateImportData(parsed, existing = null) {
  if (
    !parsed ||
    typeof parsed !== 'object' ||
    Array.isArray(parsed) ||
    !('whiteCards' in parsed) && !('blackCards' in parsed)
  ) {
    return {
      valid: false,
      fatal: true,
      message:
        'El archivo no tiene el formato esperado: se necesita un objeto con "whiteCards" y/o "blackCards".',
      summary: { newWhite: 0, newBlack: 0, dupes: 0, invalid: 0 },
      issues: [],
      warnings: [],
    }
  }

  const summary = { newWhite: 0, newBlack: 0, dupes: 0, invalid: 0 }
  const issues = []
  const warnings = []
  const seenWhite = new Set()
  const seenBlack = new Set()
  const pushIssue = (msg) => {
    if (issues.length < IMPORT_RULES.maxIssues) issues.push(msg)
  }
  const pushWarning = (msg) => {
    if (warnings.length < IMPORT_RULES.maxIssues) warnings.push(msg)
  }

  const existingWhites = Array.isArray(existing?.whiteCards) ? existing.whiteCards : null
  const existingBlacks = Array.isArray(existing?.blackCards) ? existing.blackCards : null

  const whites = Array.isArray(parsed.whiteCards) ? parsed.whiteCards : []
  const blacks = Array.isArray(parsed.blackCards) ? parsed.blackCards : []

  whites.forEach((item, i) => {
    const text = typeof item === 'string' ? item.trim() : null
    if (!text) {
      summary.invalid++
      pushIssue(`Blanca #${i + 1}: no es un texto válido`)
      return
    }
    if (isNumericText(text)) {
      summary.invalid++
      pushIssue(`Blanca #${i + 1}: es solo un número, sin valor de juego`)
      return
    }
    if (text.length > IMPORT_RULES.maxTextLen) {
      summary.invalid++
      pushIssue(`Blanca #${i + 1}: supera ${IMPORT_RULES.maxTextLen} caracteres`)
      return
    }
    if (seenWhite.has(text)) {
      summary.dupes++
      return
    }
    seenWhite.add(text)
    summary.newWhite++
    // Aviso preview de similitud (difflib 0.85 en servidor): no bloquea.
    if (existingWhites) {
      const match = findSimilar(text, existingWhites)
      if (match && match !== text) pushWarning(`Blanca #${i + 1}: muy similar a "${match.slice(0, 40)}" (el servidor la revisará)`)
    }
  })

  blacks.forEach((item, i) => {
    const text =
      item && typeof item === 'object' && typeof item.text === 'string'
        ? item.text.trim()
        : typeof item === 'string'
          ? item.trim()
          : null
    if (!text) {
      summary.invalid++
      pushIssue(`Negra #${i + 1}: falta el texto`)
      return
    }
    if (isNumericText(text)) {
      summary.invalid++
      pushIssue(`Negra #${i + 1}: es solo un número, sin valor de juego`)
      return
    }
    if (text.length > IMPORT_RULES.maxTextLen) {
      summary.invalid++
      pushIssue(`Negra #${i + 1}: supera ${IMPORT_RULES.maxTextLen} caracteres`)
      return
    }
    let pick = item && typeof item === 'object' ? item.pick : 1
    if (typeof pick === 'string') pick = parseInt(pick, 10)
    // Se tolera 2.0 como el servidor (int(2.0) == 2); el resto no es número.
    if (typeof pick === 'number' && !Number.isInteger(pick)) {
      if (!Number.isFinite(pick) || Math.floor(pick) !== pick) {
        summary.invalid++
        pushIssue(`Negra #${i + 1}: "pick" no es un número (${String(item?.pick)})`)
        return
      }
      pick = Math.floor(pick)
    }
    if (!Number.isInteger(pick) || pick < 1 || pick > IMPORT_RULES.maxPick) {
      summary.invalid++
      pushIssue(`Negra #${i + 1}: "pick" inválido (${String(item?.pick)})`)
      return
    }
    if (pick >= 2 && (text.match(/_/g) || []).length < pick) {
      summary.invalid++
      pushIssue(`Negra #${i + 1}: pick=${pick} pero tiene menos de ${pick} guiones "_"`)
      return
    }
    if (seenBlack.has(text)) {
      summary.dupes++
      return
    }
    seenBlack.add(text)
    summary.newBlack++
    if (existingBlacks) {
      const match = findSimilar(text, existingBlacks)
      if (match && match !== text) pushWarning(`Negra #${i + 1}: muy similar a "${match.slice(0, 40)}" (el servidor la revisará)`)
    }
  })

  return { valid: true, fatal: false, summary, issues, warnings }
}

/** Construye el objeto exportable con el formato de los sets base:
 *  blancas como texto plano, negras como {text, pick}. Pura: no toca DOM. */
export function buildExportPayload(whiteList, blackList) {
  const whites = Array.isArray(whiteList) ? whiteList : []
  const blacks = Array.isArray(blackList) ? blackList : []
  return {
    whiteCards: whites.map((w) => (typeof w === 'string' ? w : w.text)),
    blackCards: blacks.map((b) =>
      typeof b === 'string' ? { text: b, pick: 1 } : { text: b.text, pick: b.pick || 1 },
    ),
  }
}

/** Votos necesarios para renovar mano (GameScreen): techo(total * umbral).
 *  Pura para poder probarla; el servidor es quien cuenta los votos de verdad. */
export function calcNeededVotes(totalActive, threshold = 0.75) {
  const total = Number(totalActive)
  const umbral = Number(threshold)
  if (!Number.isFinite(total) || total <= 0) return 0
  if (!Number.isFinite(umbral) || umbral <= 0) return 0
  return Math.ceil(total * umbral)
}

/** Parte el input de CustomCards por comas ("a, b,, c" -> ["a","b","c"]). */
export function parseCustomCardsInput(value) {
  if (typeof value !== 'string') return []
  return value
    .split(',')
    .map((c) => c.trim())
    .filter(Boolean)
}
