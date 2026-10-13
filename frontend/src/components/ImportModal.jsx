import { motion } from 'framer-motion'
import { FileJson, Upload, CheckCircle } from 'lucide-react'

// Modal de confirmación del archivo .json seleccionado. Solo pinta el preview
// que devuelve validateImportData; la decisión final la toma el servidor.
export default function ImportModal({ pendingImport, importing, onCancel, onConfirm }) {
  if (!pendingImport) return null
  const check = pendingImport.check
  const newTotal = check && !check.fatal ? check.summary.newWhite + check.summary.newBlack : 0

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="workshop-modal-backdrop"
      role="button"
      tabIndex={0}
      aria-label={`Cancelar importación (pulsa Escape o haz clic fuera)`}
      onClick={() => { if (!importing) onCancel() }}
      onKeyDown={(e) => { if (e.key === 'Escape' && !importing) onCancel() }}
    >
      <motion.div
        initial={{ scale: 0.95, y: 20 }}
        animate={{ scale: 1, y: 0 }}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => e.stopPropagation()}
        className="glass-panel workshop-modal-panel"
        role="dialog"
        aria-modal="true"
        aria-label={`Confirmar importación de ${pendingImport.fileName}`}
      >
        <h3 className="workshop-modal-title">
          <FileJson size={20} /> {pendingImport.fileName}
        </h3>
        {check.fatal ? (
          <p className="workshop-error-text">{check.message}</p>
        ) : (
          <>
            <div className="workshop-chips">
              <span className="workshop-chip workshop-chip-ok">
                + {check.summary.newWhite} blancas nuevas
              </span>
              <span className="workshop-chip workshop-chip-ok">
                + {check.summary.newBlack} negras nuevas
              </span>
              {check.summary.dupes > 0 && (
                <span className="workshop-chip workshop-chip-warn">
                  {check.summary.dupes} duplicadas se omitirán
                </span>
              )}
              {check.summary.invalid > 0 && (
                <span className="workshop-chip workshop-chip-err">
                  {check.summary.invalid} inválidas se descartarán
                </span>
              )}
            </div>
            {check.issues.length > 0 && (
              <ul className="workshop-issue-list">
                {check.issues.map((iss, i) => <li key={i}>{iss}</li>)}
              </ul>
            )}
            {check.warnings && check.warnings.length > 0 && (
              <ul className="workshop-issue-list workshop-warning-list" aria-label="Avisos de similitud">
                {check.warnings.map((w, i) => <li key={`w-${i}`}>~ {w}</li>)}
              </ul>
            )}
            <p className="workshop-modal-note">
              Las cartas nuevas se guardarán en <code>cartasCustom.json</code> y estarán
              disponibles de inmediato en todas las salas. El servidor re-valida cada
              carta al confirmar.
            </p>
            <div className="workshop-modal-actions">
              <button type="button" className="btn-secondary workshop-btn-auto" onClick={onCancel} disabled={importing}>
                Cancelar
              </button>
              <button
                type="button"
                className="btn-primary workshop-btn-auto workshop-btn-confirm"
                onClick={onConfirm}
                disabled={importing}
              >
                <Upload size={16} /> {importing ? 'Importando...' : `Importar ${newTotal} cartas`}
              </button>
            </div>
          </>
        )}
      </motion.div>
    </motion.div>
  )
}

export function ImportResultBanner({ importResult }) {
  if (!importResult) return null
  return (
    <motion.div
      initial={{ opacity: 0, y: -6 }}
      animate={{ opacity: 1, y: 0 }}
      className="workshop-import-result"
      role="status"
    >
      <div className="workshop-import-result-title">
        <CheckCircle size={18} />
        Importación completada: {importResult.imported?.white || 0} blancas y {importResult.imported?.black || 0} negras añadidas.
        {(importResult.rejected_count || importResult.ignored_count) ? (
          <span className="workshop-import-result-sub">
            ({importResult.rejected_count || 0} inválidas, {importResult.ignored_count || 0} duplicadas omitidas)
          </span>
        ) : null}
      </div>
      {(importResult.rejected?.length > 0 || importResult.ignored?.length > 0) && (
        <ul className="workshop-issue-list">
          {importResult.rejected?.map((r, i) => <li key={`r-${i}`}>✕ {r}</li>)}
          {importResult.ignored?.map((g, i) => <li key={`g-${i}`}>= {g}</li>)}
        </ul>
      )}
    </motion.div>
  )
}
