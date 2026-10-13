import { motion } from 'framer-motion'
import { ArrowLeft, Plus, FileCode, CheckCircle, AlertCircle, Upload, Download, FileJson } from 'lucide-react'
import Card from './Card'
import { useCustomCards } from '../hooks/useCustomCards'
import { buildExportPayload } from '../utils/cardsValidation'
import ImportModal, { ImportResultBanner } from './ImportModal'
import CardsGallery from './CardsGallery'
import './CardWorkshop.css'

// Taller de cartas propias: el componente solo pinta. El estado + socket vive
// en useCustomCards, la validación pura en utils/cardsValidation (con tests).
// La verdad la tiene el servidor: aquí solo previews; el servidor re-valida.
export default function CardWorkshop({ socket, onBack }) {
  const {
    cardType, setCardType, cardText, setCardText, cardPick, setCardPick,
    loading, alertInfo, filterTab, setFilterTab,
    fileInputRef, importing, pendingImport, setPendingImport,
    importResult, whiteList, blackList, totalCount,
    confirmDelete, requestDeleteCard, cancelDeleteCard, confirmDeleteCard,
    showAlert, handleAddCard, openFilePicker, handleFileSelected, confirmImport,
  } = useCustomCards(socket)

  const handleExportDeck = () => {
    if (totalCount === 0) return
    const blob = new Blob([JSON.stringify(buildExportPayload(whiteList, blackList), null, 2)], { type: 'application/json' })
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

  return (
    <div className="screen active workshop-screen">
      <div className="workshop-wrap">

        {/* Input de archivo oculto: se dispara desde el botón "Importar" */}
        <input
          ref={fileInputRef}
          type="file"
          accept=".json,application/json"
          className="workshop-file-hidden"
          onChange={handleFileSelected}
          aria-hidden="true"
          tabIndex={-1}
        />

        {/* Header */}
        <div className="workshop-header">
          <button
            type="button"
            className="btn-secondary workshop-btn-auto workshop-btn-row"
            onClick={onBack}
          >
            <ArrowLeft size={18} /> Volver al Inicio
          </button>
          <div className="workshop-header-meta">
            <FileCode size={16} /> Guardando en <code>cartasCustom.json</code>
          </div>
        </div>

        {/* Title */}
        <div className="workshop-title">
          <h1>Taller de Cartas Custom</h1>
          <p>
            Crea tus propias cartas blancas y negras. Se guardarán localmente en <code>cartasCustom.json</code> y estarán disponibles en todas tus partidas.
          </p>
        </div>

        {/* Alert notification (toast no bloqueante, sustituye a alert()) */}
        {alertInfo && (
          <motion.div
            initial={{ opacity: 0, y: -10 }}
            animate={{ opacity: 1, y: 0 }}
            role={alertInfo.isError ? 'alert' : 'status'}
            className={`workshop-alert ${alertInfo.isError ? 'workshop-alert-error' : 'workshop-alert-ok'}`}
          >
            {alertInfo.isError ? <AlertCircle size={20} /> : <CheckCircle size={20} />}
            <span className="workshop-alert-msg">{alertInfo.message}</span>
          </motion.div>
        )}

        {/* Importar / exportar archivos .json */}
        <div className="glass-panel workshop-panel-wide">
          <h2 className="workshop-panel-title">
            <FileJson size={22} /> Importar y exportar cartas (.json)
          </h2>
          <p className="workshop-panel-desc">
            Sube un archivo con el mismo formato que los sets base: <code>{'{"whiteCards": ["..."], "blackCards": [{"text": "...", "pick": 2}]}'}</code>.
            El archivo se valida carta a carta: las duplicadas o inválidas se omiten y el resto se añade al mazo.
          </p>
          <div className="workshop-actions">
            <button
              type="button"
              className="btn-primary workshop-btn-auto workshop-btn-pick"
              onClick={openFilePicker}
              disabled={importing}
            >
              <Upload size={18} /> {importing ? 'Importando...' : 'Seleccionar archivo .json'}
            </button>
            <button
              type="button"
              className="btn-secondary workshop-btn-auto workshop-btn-pick workshop-btn-export"
              onClick={handleExportDeck}
              disabled={totalCount === 0}
              title={totalCount === 0 ? 'No hay cartas que exportar' : `Descargar las ${totalCount} cartas como cartasCustom.json`}
            >
              <Download size={18} /> Exportar mazo ({totalCount})
            </button>
          </div>

          <ImportResultBanner importResult={importResult} />
        </div>

        <ImportModal
          pendingImport={pendingImport}
          importing={importing}
          onCancel={() => setPendingImport(null)}
          onConfirm={confirmImport}
        />

        {/* Creator Section */}
        <div className="glass-panel workshop-creator">
          <h2 className="workshop-creator-title">
            ✨ Crear Nueva Carta
          </h2>

          <div className="workshop-creator-grid">
            {/* Form */}
            <form onSubmit={handleAddCard}>
              <div className="form-group">
                <label htmlFor="workshop-card-type">Tipo de Carta</label>
                <select
                  id="workshop-card-type"
                  value={cardType}
                  onChange={(e) => setCardType(e.target.value)}
                  className="workshop-select"
                >
                  <option value="white">Carta Blanca (Respuesta)</option>
                  <option value="black">Carta Negra (Pregunta)</option>
                </select>
              </div>

              <div className="form-group">
                <label htmlFor="workshop-card-text">Texto de la Carta</label>
                <textarea
                  id="workshop-card-text"
                  value={cardText}
                  onChange={(e) => setCardText(e.target.value)}
                  placeholder={cardType === 'black' ? 'Ej: La razón por la que llegué tarde hoy fue _.' : 'Ej: Un gato ninja jugando con fuego.'}
                  rows={4}
                  maxLength={200}
                  className="workshop-textarea"
                />
                {cardType === 'black' && (
                  <span className="workshop-hint">
                    💡 Usa un guión bajo <code>_</code> para representar el espacio en blanco.
                  </span>
                )}
              </div>

              {cardType === 'black' && (
                <div className="form-group">
                  <label htmlFor="workshop-card-pick">Cartas Necesarias (Pick)</label>
                  <input
                    id="workshop-card-pick"
                    type="number"
                    min="1"
                    max="3"
                    value={cardPick}
                    onChange={(e) => setCardPick(e.target.value)}
                    className="workshop-input"
                  />
                </div>
              )}

              <button
                type="submit"
                className="btn-primary workshop-submit"
                disabled={loading || !cardText.trim()}
              >
                <Plus size={18} /> {loading ? 'Guardando...' : 'Guardar en cartasCustom.json'}
              </button>
            </form>

            {/* Live Preview */}
            <div className="workshop-preview">
              <span className="workshop-preview-label" id="workshop-preview-label">
                Vista Previa en Vivo
              </span>
              <div className="workshop-preview-scale" role="img" aria-labelledby="workshop-preview-label">
                <Card
                  text={cardText || (cardType === 'black' ? 'Tu pregunta aparecerá aquí con _.' : 'Tu respuesta aparecerá aquí...')}
                  type={cardType}
                  pick={cardType === 'black' ? cardPick : 1}
                />
              </div>
            </div>
          </div>
        </div>

        <CardsGallery
          whiteList={whiteList}
          blackList={blackList}
          totalCount={totalCount}
          filterTab={filterTab}
          onFilterChange={setFilterTab}
          onDelete={requestDeleteCard}
          confirmDelete={confirmDelete}
          onCancelDelete={cancelDeleteCard}
          onConfirmDelete={confirmDeleteCard}
        />

      </div>
    </div>
  )
}
