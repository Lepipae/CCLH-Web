import { describe, expect, it } from 'vitest'
import {
  buildExportPayload,
  calcNeededVotes,
  IMPORT_RULES,
  parseCustomCardsInput,
  textSimilarity,
  validateImportData,
} from './cardsValidation'

describe('validateImportData', () => {
  it('rechaza lo que no es un set (fatal)', () => {
    for (const bad of [null, undefined, [], 'texto', {}, { foo: ['bar'] }]) {
      const r = validateImportData(bad)
      expect(r.valid).toBe(false)
      expect(r.fatal).toBe(true)
    }
  })

  it('acepta un set mínimo válido', () => {
    const r = validateImportData({
      whiteCards: ['Carta importada A'],
      blackCards: [{ text: 'Pregunta _', pick: 1 }],
    })
    expect(r.valid).toBe(true)
    expect(r.summary).toMatchObject({ newWhite: 1, newBlack: 1, dupes: 0, invalid: 0 })
  })

  it('marca vacías, numéricas y largas como inválidas', () => {
    const r = validateImportData({
      whiteCards: ['', '   ', 12345, 'x'.repeat(IMPORT_RULES.maxTextLen + 1), '12345', 'Carta buena'],
    })
    expect(r.valid).toBe(true)
    // '': vacía, 12345: no texto, larga: >200, '12345': numérica
    expect(r.summary.invalid).toBeGreaterThanOrEqual(4)
    expect(r.summary.newWhite).toBe(1)
    expect(r.issues.join('\n')).toMatch(/número/)
  })

  it('valida pick: rango, numérico y huecos "_"', () => {
    const r = validateImportData({
      blackCards: [
        { text: 'Sin hueco', pick: 2 }, // sin _
        { text: 'Pick alto _', pick: 5 }, // fuera de rango
        { text: 'Pick raro _', pick: 'dos' }, // no numérico
        { text: '', pick: 1 }, // vacía
        { text: 'Buena _ y _', pick: 2 },
        'Plano _', // string tolerado como pick=1
      ],
    })
    expect(r.summary.newBlack).toBe(2)
    expect(r.summary.invalid).toBe(4)
    expect(r.issues.join('\n')).toMatch(/guion/)
  })

  it('tolera pick como string numérico ("2") igual que el servidor', () => {
    const r = validateImportData({ blackCards: [{ text: 'Dos huecos _ y _', pick: '2' }] })
    expect(r.summary.newBlack).toBe(1)
  })

  it('cuenta duplicadas dentro del archivo sin invalidarlas', () => {
    const r = validateImportData({ whiteCards: ['Repetida', 'Repetida', 'Repetida'] })
    expect(r.summary.newWhite).toBe(1)
    expect(r.summary.dupes).toBe(2)
  })

  it('la similitud 0.85 es solo aviso preview, nunca rechazo', () => {
    const existing = { whiteCards: ['Carta verdaderamente original'], blackCards: [] }
    const r = validateImportData({ whiteCards: ['Carta verdaderamente original!'] }, existing)
    // Sigue contando como nueva: el servidor decide con difflib de verdad.
    expect(r.summary.newWhite).toBe(1)
    expect(r.summary.invalid).toBe(0)
    expect(r.warnings.length).toBeGreaterThan(0)
    expect(r.warnings.join('\n')).toMatch(/servidor/)
  })

  it('textSimilarity es 1 para idénticas e insensible a mayúsculas', () => {
    expect(textSimilarity('Hola Mundo', 'hola mundo')).toBe(1)
    expect(textSimilarity('abc', 'xyz')).toBeLessThan(0.5)
  })
})

describe('calcNeededVotes (GameScreen)', () => {
  it('techo de total * umbral', () => {
    expect(calcNeededVotes(4, 0.75)).toBe(3)
    expect(calcNeededVotes(3, 0.75)).toBe(3) // ceil(2.25)
    expect(calcNeededVotes(10, 0.5)).toBe(5)
  })

  it('umbral por defecto 0.75', () => {
    expect(calcNeededVotes(4)).toBe(3)
  })

  it('totales inválidos dan 0 en vez de NaN', () => {
    expect(calcNeededVotes(0, 0.75)).toBe(0)
    expect(calcNeededVotes(undefined, 0.75)).toBe(0)
    expect(calcNeededVotes(4, 0)).toBe(0)
  })
})

describe('parseCustomCardsInput (CustomCards coma-separadas)', () => {
  it('parte por comas y limpia', () => {
    expect(parseCustomCardsInput('Mi suegra, Un gato ninja, Patatas bravas')).toEqual([
      'Mi suegra',
      'Un gato ninja',
      'Patatas bravas',
    ])
  })

  it('ignora vacías de comas dobles o bordes', () => {
    expect(parseCustomCardsInput('a,, b, ,c,')).toEqual(['a', 'b', 'c'])
  })

  it('no string da lista vacía', () => {
    expect(parseCustomCardsInput(null)).toEqual([])
    expect(parseCustomCardsInput('   ')).toEqual([])
  })
})

describe('buildExportPayload', () => {
  it('normaliza mixto str/{text,pick} al formato base', () => {
    const out = buildExportPayload(
      ['Blanca suelta', { text: 'Blanca obj' }],
      ['Negra suelta', { text: 'Negra _', pick: 2 }],
    )
    expect(out).toEqual({
      whiteCards: ['Blanca suelta', 'Blanca obj'],
      blackCards: [{ text: 'Negra suelta', pick: 1 }, { text: 'Negra _', pick: 2 }],
    })
  })
})
