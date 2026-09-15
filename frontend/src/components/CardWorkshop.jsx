import React, { useState, useEffect, useRef } from 'react'
import { motion } from 'framer-motion'
import { ArrowLeft, Plus, Trash2, FileCode, CheckCircle, AlertCircle, Upload, Download, FileJson } from 'lucide-react'
import Card from './Card'
import { soundManager } from '../utils/soundManager'

// Misma validación básica que aplica el backend al importar
const IMPORT_RULES = { maxTextLen: 200, maxPick: 3 }

/** Pre-valida el contenido de un archivo .json de cartas sin tocar el servidor.
 *  Devuelve { valid, fatal, message, summary: {newWhite, newBlack, dupes, invalid}, issues[] } */
function validateImportData(parsed) {
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed) ||
      (!('whiteCards' in parsed) && !('blackCards' in parsed))) {
    return { valid: false, fatal: true, message: 'El archivo no tiene el formato esperado: se necesita un objeto con "whiteCards" y/o "blackCards".' }
  }

  const summary = { newWhite: 0, newBlack: 0, dupes: 0, invalid: 0 }
  const issues = []
  const seenWhite = new Set()
  const seenBlack = new Set()

  const whites = Array.isArray(parsed.whiteCards) ? parsed.whiteCards : []
  const blacks = Array.isArray(parsed.blackCards) ? parsed.blackCards : []

  whites.forEach((item, i) => {
    const text = typeof item === 'string' ? item.trim() : null
    if (!text) {
      summary.invalid++
      if (issues.length < 10) issues.push(`Blanca #${i + 1}: no es un texto válido`)
      return
    }
    if (text.length > IMPORT_RULES.maxTextLen) {
      summary.invalid++
      if (issues.length < 10) issues.push(`Blanca #${i + 1}: supera ${IMPORT_RULES.maxTextLen} caracteres`)
      return
    }
    if (seenWhite.has(text)) { summary.dupes++; return }
    seenWhite.add(text)
    summary.newWhite++
  })

  blacks.forEach((item, i) => {
    const text = item && typeof item === 'object' && typeof item.text === 'string' ? item.text.trim()
      : typeof item === 'string' ? item.trim() : null
    if (!text) {
      summary.invalid++
      if (issues.length < 10) issues.push(`Negra #${i + 1}: falta el texto`)
      return
    }
    let pick = item && typeof item === 'object' ? item.pick : 1
    if (typeof pick === 'string') pick = parseInt(pick, 10)
    if (!Number.isInteger(pick) || pick < 1 || pick > IMPORT_RULES.maxPick) {
      summary.invalid++
      if (issues.length < 10) issues.push(`Negra #${i + 1}: "pick" inválido (${String(item?.pick)})`)
      return
    }
    if (pick >= 2 && (text.match(/_/g) || []).length < pick) {
      summary.invalid++
      if (issues.length < 10) issues.push(`Negra #${i + 1}: pick=${pick} pero tiene menos de ${pick} guiones "_"`)
      return
    }
    if (seenBlack.has(text)) { summary.dupes++; return }
    seenBlack.add(text)
    summary.newBlack++
  })

  return { valid: true, fatal: false, summary, issues }
}

export default function CardWorkshop({ socket, onBack }) {
  const [cardType, setCardType] = useState('white')
  const [cardText, setCardText] = useState('')
  const [cardPick, setCardPick] = useState(1)
  const [loading, setLoading] = useState(false)
  const [alertInfo, setAlertInfo] = useState(null) // { message, isError }
  const [customCards, setCustomCards] = useState({ whiteCards: [], blackCards: [] })
  const [filterTab, setFilterTab] = useState('all') // 'all' | 'white' | 'black'

  // Estado del importador de .json
  const fileInputRef = useRef(null)
  const [importing, setImporting] = useState(false)         // petición en curso
  const [pendingImport, setPendingImport] = useState(null)  // { fileName, data, check } antes de confirmar
  const [importResult, setImportResult] = useState(null)    // resumen devuelto por el servidor

  useEffect(() => {
    fetchCustomCards()

    socket.on('custom_cards_list', (data) => {
      if (data.success) {
        setCustomCards({
          whiteCards: data.whiteCards || [],
          blackCards: data.blackCards || []
        })
      }
    })

    socket.on('custom_card_result', (data) => {
      setLoading(false)
      if (data.success) {
        showAlert('¡Carta guardada con éxito en cartasCustom.json!', false)
        setCardText('')
        if (data.whiteCards || data.blackCards) {
          setCustomCards({
            whiteCards: data.whiteCards || [],
            blackCards: data.blackCards || []
          })
        } else {
          fetchCustomCards()
        }
      } else {
        showAlert('Error: ' + data.message, true)
      }
    })

    socket.on('custom_card_deleted', (data) => {
      if (data.success) {
        setCustomCards({
          whiteCards: data.whiteCards || [],
          blackCards: data.blackCards || []
        })
        showAlert('Carta eliminada de cartasCustom.json', false)
      } else {
        showAlert('Error eliminando carta: ' + data.message, true)
      }
    })

    // Respuesta del servidor a la importación del archivo
    socket.on('custom_cards_imported', (data) => {
      setImporting(false)
      setPendingImport(null)
      if (data.success) {
        setCustomCards({
          whiteCards: data.whiteCards || [],
          blackCards: data.blackCards || []
        })
        setImportResult(data)
      } else {
        showAlert('Error importando: ' + (data.message || 'desconocido'), true)
      }
    })

    return () => {
      socket.off('custom_cards_list')
      socket.off('custom_card_result')
      socket.off('custom_card_deleted')
      socket.off('custom_cards_imported')
    }
  }, [socket])

  const fetchCustomCards = () => {
    socket.emit('get_custom_cards')
  }

  const showAlert = (message, isError) => {
    setAlertInfo({ message, isError })
    setTimeout(() => {
      setAlertInfo(null)
    }, 4000)
  }

  const handleAddCard = (e) => {
    e.preventDefault()
    if (!cardText.trim()) {
      showAlert('El texto de la carta no puede estar vacío.', true)
      return
    }

    soundManager.playCard()
    setLoading(true)
    socket.emit('add_custom_card', {
      type: cardType,
      text: cardText.trim(),
      pick: cardType === 'black' ? parseInt(cardPick) || 1 : 1
    })
  }

  const handleDeleteCard = (type, text) => {
    if (window.confirm(`¿Estás seguro de eliminar esta carta de cartasCustom.json?`)) {
      socket.emit('delete_custom_card', { type, text })
    }
  }

  // ---------- Importación de archivos .json ----------

  const openFilePicker = () => {
    if (fileInputRef.current) fileInputRef.current.click()
  }

  const handleFileSelected = (e) => {
    const file = e.target.files && e.target.files[0]
    e.target.value = '' // permite re-seleccionar el mismo archivo
    if (!file) return

    if (file.size > 5 * 1024 * 1024) {
      showAlert('El archivo es demasiado grande (máximo 5 MB).', true)
      return
    }

    const reader = new FileReader()
    reader.onload = () => {
      let parsed
      try {
        parsed = JSON.parse(reader.result)
      } catch (err) {
        showAlert(`"${file.name}" no es un JSON válido: ${err.message}`, true)
        return
      }
      const check = validateImportData(parsed)
      if (!check.valid) {
        showAlert(check.message, true)
        return
      }
      if (check.summary.newWhite === 0 && check.summary.newBlack === 0) {
        const { dupes, invalid } = check.summary
        showAlert(`Ninguna carta nueva que importar (${dupes} duplicadas, ${invalid} inválidas).`, true)
        return
      }
      setImportResult(null)
      setPendingImport({ fileName: file.name, data: parsed, check })
    }
    reader.onerror = () => showAlert('No se pudo leer el archivo.', true)
    reader.readAsText(file)
  }

  const confirmImport = () => {
    if (!pendingImport || importing) return
    soundManager.playCard()
    setImporting(true)
    // El servidor re-valida todo: el cliente no es fuente de verdad
    socket.emit('import_custom_cards', { json: pendingImport.data, file_name: pendingImport.fileName })
  }

  // ---------- Exportación del mazo a .json ----------

  /** Construye el objeto exportable con el mismo formato que los sets base:
   *  blancas como texto plano y negras como {text, pick} (normalizando sueltos). */
  const buildExportPayload = () => {
    return {
      whiteCards: whiteList.map((w) => (typeof w === 'string' ? w : w.text)),
      blackCards: blackList.map((b) => (
        typeof b === 'string' ? { text: b, pick: 1 } : { text: b.text, pick: b.pick || 1 }
      ))
    }
  }

  const handleExportDeck = () => {
    if (totalCount === 0) return
    const blob = new Blob([JSON.stringify(buildExportPayload(), null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = 'cartasCustom.json'
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
    showAlert('Mazo exportado como cartasCustom.json', false)
  }

  const whiteList = customCards.whiteCards || []
  const blackList = customCards.blackCards || []
  const totalCount = whiteList.length + blackList.length
  const pendingCheck = pendingImport?.check

  return (
    <div className="screen active" style={{ overflowY: 'auto', padding: '2rem 1rem' }}>
      <div style={{ maxWidth: '900px', margin: '0 auto', width: '100%' }}>

        {/* Input de archivo oculto: se dispara desde el botón "Importar" */}
        <input
          ref={fileInputRef}
          type="file"
          accept=".json,application/json"
          style={{ display: 'none' }}
          onChange={handleFileSelected}
        />

        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '2rem' }}>
          <button
            className="btn-secondary"
            onClick={onBack}
            style={{ width: 'auto', marginTop: 0, display: 'flex', alignItems: 'center', gap: '0.5rem' }}
          >
            <ArrowLeft size={18} /> Volver al Inicio
          </button>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
            <FileCode size={16} /> Guardando en <code>cartasCustom.json</code>
          </div>
        </div>

        {/* Title */}
        <div style={{ textAlign: 'center', marginBottom: '2.5rem' }}>
          <h1 style={{
            fontSize: '2.5rem',
            marginBottom: '0.5rem',
            background: 'linear-gradient(to right, #a78bfa, #60a5fa)',
            WebkitBackgroundClip: 'text',
            WebkitTextFillColor: 'transparent'
          }}>
            Taller de Cartas Custom
          </h1>
          <p style={{ color: 'var(--text-secondary)', maxWidth: '600px', margin: '0 auto' }}>
            Crea tus propias cartas blancas y negras. Se guardarán localmente en <code>cartasCustom.json</code> y estarán disponibles en todas tus partidas.
          </p>
        </div>

        {/* Alert notification */}
        {alertInfo && (
          <motion.div
            initial={{ opacity: 0, y: -10 }}
            animate={{ opacity: 1, y: 0 }}
            style={{
              padding: '1rem',
              borderRadius: '12px',
              marginBottom: '1.5rem',
              display: 'flex',
              alignItems: 'center',
              gap: '0.75rem',
              background: alertInfo.isError ? 'rgba(239, 68, 68, 0.2)' : 'rgba(34, 197, 94, 0.2)',
              border: `1px solid ${alertInfo.isError ? 'rgba(239, 68, 68, 0.4)' : 'rgba(34, 197, 94, 0.4)'}`,
              color: alertInfo.isError ? '#fca5a5' : '#86efac'
            }}
          >
            {alertInfo.isError ? <AlertCircle size={20} /> : <CheckCircle size={20} />}
            <span style={{ fontWeight: 500 }}>{alertInfo.message}</span>
          </motion.div>
        )}

        {/* Importar / exportar archivos .json */}
        <div className="glass-panel" style={{ maxWidth: '100%', padding: '1.5rem 2rem', marginBottom: '2rem', textAlign: 'left' }}>
          <h2 style={{ fontSize: '1.3rem', marginBottom: '0.5rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <FileJson size={22} /> Importar y exportar cartas (.json)
          </h2>
          <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem', marginBottom: '1rem' }}>
            Sube un archivo con el mismo formato que los sets base: <code>{'{"whiteCards": ["..."], "blackCards": [{"text": "...", "pick": 2}]}'}</code>.
            El archivo se valida carta a carta: las duplicadas o inválidas se omiten y el resto se añade al mazo.
          </p>
          <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap' }}>
            <button
              className="btn-primary"
              onClick={openFilePicker}
              disabled={importing}
              style={{
                display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '0.5rem',
                opacity: importing ? 0.6 : 1,
                cursor: importing ? 'not-allowed' : 'pointer'
              }}
            >
              <Upload size={18} /> {importing ? 'Importando...' : 'Seleccionar archivo .json'}
            </button>
            <button
              className="btn-secondary"
              onClick={handleExportDeck}
              disabled={totalCount === 0}
              title={totalCount === 0 ? 'No hay cartas que exportar' : `Descargar las ${totalCount} cartas como cartasCustom.json`}
              style={{
                width: 'auto', marginTop: 0,
                display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '0.5rem',
                opacity: totalCount === 0 ? 0.6 : 1,
                cursor: totalCount === 0 ? 'not-allowed' : 'pointer'
              }}
            >
              <Download size={18} /> Exportar mazo ({totalCount})
            </button>
          </div>

          {/* Resumen del último import realizado */}
          {importResult && (
            <motion.div
              initial={{ opacity: 0, y: -6 }}
              animate={{ opacity: 1, y: 0 }}
              style={{
                marginTop: '1rem', padding: '1rem', borderRadius: '12px',
                background: 'rgba(34, 197, 94, 0.15)', border: '1px solid rgba(34, 197, 94, 0.4)'
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', color: '#86efac', fontWeight: 600, marginBottom: '0.25rem' }}>
                <CheckCircle size={18} />
                Importación completada: {importResult.imported?.white || 0} blancas y {importResult.imported?.black || 0} negras añadidas.
                {(importResult.rejected_count || importResult.ignored_count) ? (
                  <span style={{ fontWeight: 400, color: 'var(--text-secondary)' }}>
                    ({importResult.rejected_count || 0} inválidas, {importResult.ignored_count || 0} duplicadas omitidas)
                  </span>
                ) : null}
              </div>
              {(importResult.rejected?.length > 0 || importResult.ignored?.length > 0) && (
                <ul style={{ margin: '0.5rem 0 0 1.2rem', padding: 0, fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
                  {importResult.rejected?.map((r, i) => <li key={`r-${i}`} style={{ marginBottom: '0.15rem' }}>✕ {r}</li>)}
                  {importResult.ignored?.map((g, i) => <li key={`g-${i}`} style={{ marginBottom: '0.15rem' }}>= {g}</li>)}
                </ul>
              )}
            </motion.div>
          )}
        </div>

        {/* Modal de confirmación del archivo seleccionado */}
        {pendingImport && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            style={{
              position: 'fixed', inset: 0, zIndex: 1000,
              background: 'rgba(0, 0, 0, 0.6)', backdropFilter: 'blur(4px)',
              display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '1rem'
            }}
            onClick={() => { if (!importing) setPendingImport(null) }}
          >
            <motion.div
              initial={{ scale: 0.95, y: 20 }}
              animate={{ scale: 1, y: 0 }}
              onClick={(e) => e.stopPropagation()}
              className="glass-panel"
              style={{ maxWidth: '560px', width: '100%', padding: '2rem', textAlign: 'left', maxHeight: '85vh', overflowY: 'auto' }}
            >
              <h3 style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.75rem' }}>
                <FileJson size={20} /> {pendingImport.fileName}
              </h3>
              {pendingCheck.fatal ? (
                <p style={{ color: '#fca5a5' }}>{pendingCheck.message}</p>
              ) : (
                <>
                  <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap', marginBottom: '0.75rem' }}>
                    <span style={{ padding: '0.35rem 0.75rem', borderRadius: '8px', background: 'rgba(34, 197, 94, 0.2)', border: '1px solid rgba(34, 197, 94, 0.4)', fontSize: '0.9rem' }}>
                      + {pendingCheck.summary.newWhite} blancas nuevas
                    </span>
                    <span style={{ padding: '0.35rem 0.75rem', borderRadius: '8px', background: 'rgba(34, 197, 94, 0.2)', border: '1px solid rgba(34, 197, 94, 0.4)', fontSize: '0.9rem' }}>
                      + {pendingCheck.summary.newBlack} negras nuevas
                    </span>
                    {pendingCheck.summary.dupes > 0 && (
                      <span style={{ padding: '0.35rem 0.75rem', borderRadius: '8px', background: 'rgba(234, 179, 8, 0.2)', border: '1px solid rgba(234, 179, 8, 0.4)', fontSize: '0.9rem' }}>
                        {pendingCheck.summary.dupes} duplicadas se omitirán
                      </span>
                    )}
                    {pendingCheck.summary.invalid > 0 && (
                      <span style={{ padding: '0.35rem 0.75rem', borderRadius: '8px', background: 'rgba(239, 68, 68, 0.2)', border: '1px solid rgba(239, 68, 68, 0.4)', fontSize: '0.9rem' }}>
                        {pendingCheck.summary.invalid} inválidas se descartarán
                      </span>
                    )}
                  </div>
                  {pendingCheck.issues.length > 0 && (
                    <ul style={{ margin: '0 0 0.75rem 1.2rem', padding: 0, fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
                      {pendingCheck.issues.map((iss, i) => <li key={i} style={{ marginBottom: '0.15rem' }}>{iss}</li>)}
                    </ul>
                  )}
                  <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginBottom: '1.25rem' }}>
                    Las cartas nuevas se guardarán en <code>cartasCustom.json</code> y estarán disponibles de inmediato en todas las salas.
                  </p>
                  <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'flex-end' }}>
                    <button className="btn-secondary" onClick={() => setPendingImport(null)} disabled={importing} style={{ width: 'auto', marginTop: 0 }}>
                      Cancelar
                    </button>
                    <button className="btn-primary" onClick={confirmImport} disabled={importing} style={{ width: 'auto', marginTop: 0, display: 'flex', alignItems: 'center', gap: '0.5rem', opacity: importing ? 0.6 : 1 }}>
                      <Upload size={16} /> {importing ? 'Importando...' : `Importar ${pendingCheck.summary.newWhite + pendingCheck.summary.newBlack} cartas`}
                    </button>
                  </div>
                </>
              )}
            </motion.div>
          </motion.div>
        )}

        {/* Creator Section */}
        <div className="glass-panel" style={{ maxWidth: '100%', padding: '2rem', marginBottom: '3rem', textAlign: 'left' }}>
          <h2 style={{ fontSize: '1.3rem', marginBottom: '1.5rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            ✨ Crear Nueva Carta
          </h2>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '2rem', alignItems: 'start' }}>
            {/* Form */}
            <form onSubmit={handleAddCard}>
              <div className="form-group">
                <label>Tipo de Carta</label>
                <select
                  value={cardType}
                  onChange={(e) => setCardType(e.target.value)}
                  style={{
                    width: '100%',
                    padding: '0.75rem 1rem',
                    background: 'rgba(0, 0, 0, 0.3)',
                    border: '1px solid rgba(255, 255, 255, 0.1)',
                    borderRadius: '8px',
                    color: 'white',
                    fontSize: '1rem',
                    outline: 'none'
                  }}
                >
                  <option value="white">Carta Blanca (Respuesta)</option>
                  <option value="black">Carta Negra (Pregunta)</option>
                </select>
              </div>

              <div className="form-group">
                <label>Texto de la Carta</label>
                <textarea
                  value={cardText}
                  onChange={(e) => setCardText(e.target.value)}
                  placeholder={cardType === 'black' ? "Ej: La razón por la que llegué tarde hoy fue _." : "Ej: Un gato ninja jugando con fuego."}
                  rows={4}
                  style={{
                    width: '100%',
                    padding: '0.75rem 1rem',
                    background: 'rgba(0, 0, 0, 0.3)',
                    border: '1px solid rgba(255, 255, 255, 0.1)',
                    borderRadius: '8px',
                    color: 'white',
                    fontSize: '1rem',
                    fontFamily: 'inherit',
                    resize: 'vertical',
                    outline: 'none'
                  }}
                />
                {cardType === 'black' && (
                  <span style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', display: 'block', marginTop: '0.3rem' }}>
                    💡 Usa un guión bajo <code>_</code> para representar el espacio en blanco.
                  </span>
                )}
              </div>

              {cardType === 'black' && (
                <div className="form-group">
                  <label>Cartas Necesarias (Pick)</label>
                  <input
                    type="number"
                    min="1"
                    max="3"
                    value={cardPick}
                    onChange={(e) => setCardPick(e.target.value)}
                    style={{
                      width: '100%',
                      padding: '0.75rem 1rem',
                      background: 'rgba(0, 0, 0, 0.3)',
                      border: '1px solid rgba(255, 255, 255, 0.1)',
                      borderRadius: '8px',
                      color: 'white',
                      fontSize: '1rem'
                    }}
                  />
                </div>
              )}

              <button
                type="submit"
                className="btn-primary"
                disabled={loading || !cardText.trim()}
                style={{
                  marginTop: '1rem',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  gap: '0.5rem',
                  opacity: loading || !cardText.trim() ? 0.6 : 1,
                  cursor: loading || !cardText.trim() ? 'not-allowed' : 'pointer'
                }}
              >
                <Plus size={18} /> {loading ? 'Guardando...' : 'Guardar en cartasCustom.json'}
              </button>
            </form>

            {/* Live Preview */}
            <div style={{ textAlign: 'center', display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
              <label style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 600, marginBottom: '1rem' }}>
                Vista Previa en Vivo
              </label>
              <div style={{ transform: 'scale(0.95)', transformOrigin: 'top center' }}>
                <Card
                  text={cardText || (cardType === 'black' ? 'Tu pregunta aparecerá aquí con _.' : 'Tu respuesta aparecerá aquí...')}
                  type={cardType}
                  pick={cardType === 'black' ? cardPick : 1}
                />
              </div>
            </div>
          </div>
        </div>

        {/* Custom Cards Saved Gallery */}
        <div style={{ textAlign: 'left' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.5rem', flexWrap: 'wrap', gap: '1rem' }}>
            <h2>🗂️ Cartas Guardadas ({totalCount})</h2>
            <div style={{ display: 'flex', gap: '0.5rem' }}>
              <button
                className="btn-secondary"
                onClick={() => setFilterTab('all')}
                style={{
                  width: 'auto', marginTop: 0, padding: '0.4rem 0.8rem', fontSize: '0.85rem',
                  background: filterTab === 'all' ? 'var(--accent-primary)' : undefined,
                  borderColor: filterTab === 'all' ? 'var(--accent-primary)' : undefined
                }}
              >
                Todas ({totalCount})
              </button>
              <button
                className="btn-secondary"
                onClick={() => setFilterTab('white')}
                style={{
                  width: 'auto', marginTop: 0, padding: '0.4rem 0.8rem', fontSize: '0.85rem',
                  background: filterTab === 'white' ? 'var(--accent-primary)' : undefined,
                  borderColor: filterTab === 'white' ? 'var(--accent-primary)' : undefined
                }}
              >
                Blancas ({whiteList.length})
              </button>
              <button
                className="btn-secondary"
                onClick={() => setFilterTab('black')}
                style={{
                  width: 'auto', marginTop: 0, padding: '0.4rem 0.8rem', fontSize: '0.85rem',
                  background: filterTab === 'black' ? 'var(--accent-primary)' : undefined,
                  borderColor: filterTab === 'black' ? 'var(--accent-primary)' : undefined
                }}
              >
                Negras ({blackList.length})
              </button>
            </div>
          </div>

          {totalCount === 0 ? (
            <div className="glass-panel" style={{ maxWidth: '100%', padding: '3rem', textAlign: 'center', color: 'var(--text-secondary)' }}>
              <p>Aún no hay cartas guardadas en <code>cartasCustom.json</code>.</p>
              <p style={{ fontSize: '0.9rem', marginTop: '0.5rem' }}>Utiliza el formulario de arriba para añadir tu primera carta.</p>
            </div>
          ) : (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: '1.5rem', justifyContent: 'center' }}>
              {(filterTab === 'all' || filterTab === 'white') && whiteList.map((item, idx) => {
                const text = typeof item === 'string' ? item : item.text
                return (
                  <div key={`w-${idx}`} style={{ position: 'relative', display: 'flex', justifyContent: 'center' }}>
                    <Card text={text} type="white" />
                    <button
                      onClick={() => handleDeleteCard('white', text)}
                      title="Eliminar de cartasCustom.json"
                      style={{
                        position: 'absolute',
                        top: '10px',
                        right: '10px',
                        background: 'rgba(239, 68, 68, 0.85)',
                        color: 'white',
                        border: 'none',
                        borderRadius: '50%',
                        width: '32px',
                        height: '32px',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        cursor: 'pointer',
                        boxShadow: '0 4px 10px rgba(0,0,0,0.4)'
                      }}
                    >
                      <Trash2 size={16} />
                    </button>
                  </div>
                )
              })}

              {(filterTab === 'all' || filterTab === 'black') && blackList.map((item, idx) => {
                const text = typeof item === 'string' ? item : item.text
                const pick = typeof item === 'object' ? item.pick : 1
                return (
                  <div key={`b-${idx}`} style={{ position: 'relative', display: 'flex', justifyContent: 'center' }}>
                    <Card text={text} type="black" pick={pick} />
                    <button
                      onClick={() => handleDeleteCard('black', text)}
                      title="Eliminar de cartasCustom.json"
                      style={{
                        position: 'absolute',
                        top: '10px',
                        right: '10px',
                        background: 'rgba(239, 68, 68, 0.85)',
                        color: 'white',
                        border: 'none',
                        borderRadius: '50%',
                        width: '32px',
                        height: '32px',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        cursor: 'pointer',
                        boxShadow: '0 4px 10px rgba(0,0,0,0.4)'
                      }}
                    >
                      <Trash2 size={16} />
                    </button>
                  </div>
                )
              })}
            </div>
          )}
        </div>

      </div>
    </div>
  )
}
