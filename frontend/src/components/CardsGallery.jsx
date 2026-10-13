import { Trash2 } from 'lucide-react'
import Card from './Card'

// Galería de cartas guardadas + modal de confirmación de borrado (sustituye
// window.confirm). Solo pinta; el borrado real lo emite el hook.
export default function CardsGallery({
  whiteList, blackList, totalCount, filterTab, onFilterChange,
  onDelete, confirmDelete, onCancelDelete, onConfirmDelete,
}) {
  const showWhite = filterTab === 'all' || filterTab === 'white'
  const showBlack = filterTab === 'all' || filterTab === 'black'

  return (
    <div className="workshop-gallery">
      <div className="workshop-gallery-head">
        <h2>🗂️ Cartas Guardadas ({totalCount})</h2>
        <div className="workshop-filters" role="group" aria-label="Filtrar cartas">
          <button
            type="button"
            className="btn-secondary workshop-filter-btn"
            aria-pressed={filterTab === 'all'}
            onClick={() => onFilterChange('all')}
          >
            Todas ({totalCount})
          </button>
          <button
            type="button"
            className="btn-secondary workshop-filter-btn"
            aria-pressed={filterTab === 'white'}
            onClick={() => onFilterChange('white')}
          >
            Blancas ({whiteList.length})
          </button>
          <button
            type="button"
            className="btn-secondary workshop-filter-btn"
            aria-pressed={filterTab === 'black'}
            onClick={() => onFilterChange('black')}
          >
            Negras ({blackList.length})
          </button>
        </div>
      </div>

      {totalCount === 0 ? (
        <div className="glass-panel workshop-empty">
          <p>Aún no hay cartas guardadas en <code>cartasCustom.json</code>.</p>
          <p className="workshop-empty-sub">Utiliza el formulario de arriba para añadir tu primera carta.</p>
        </div>
      ) : (
        <div className="workshop-grid">
          {showWhite && whiteList.map((item, idx) => {
            const text = typeof item === 'string' ? item : item.text
            return (
              <div key={`w-${idx}`} className="workshop-card-wrap">
                <Card text={text} type="white" />
                <button
                  type="button"
                  onClick={() => onDelete('white', text)}
                  title="Eliminar de cartasCustom.json"
                  aria-label={`Eliminar carta blanca: ${text.slice(0, 60)}`}
                  className="workshop-delete-btn"
                >
                  <Trash2 size={16} />
                </button>
              </div>
            )
          })}

          {showBlack && blackList.map((item, idx) => {
            const text = typeof item === 'string' ? item : item.text
            const pick = typeof item === 'object' ? item.pick : 1
            return (
              <div key={`b-${idx}`} className="workshop-card-wrap">
                <Card text={text} type="black" pick={pick} />
                <button
                  type="button"
                  onClick={() => onDelete('black', text)}
                  title="Eliminar de cartasCustom.json"
                  aria-label={`Eliminar carta negra: ${text.slice(0, 60)}`}
                  className="workshop-delete-btn"
                >
                  <Trash2 size={16} />
                </button>
              </div>
            )
          })}
        </div>
      )}

      {confirmDelete && (
        <div
          className="workshop-modal-backdrop"
          role="button"
          tabIndex={0}
          aria-label="Cancelar eliminación (pulsa Escape o haz clic fuera)"
          onClick={onCancelDelete}
          onKeyDown={(e) => { if (e.key === 'Escape') onCancelDelete() }}
        >
          <div
            className="glass-panel workshop-modal-panel workshop-confirm-panel"
            role="dialog"
            aria-modal="true"
            aria-label="Confirmar eliminación de carta"
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => e.stopPropagation()}
          >
            <h3>Eliminar carta</h3>
            <p className="workshop-modal-note">
              ¿Eliminar de <code>cartasCustom.json</code> la carta «{confirmDelete.text.slice(0, 120)}»?
              Esta acción no se puede deshacer.
            </p>
            <div className="workshop-modal-actions">
              <button type="button" className="btn-secondary workshop-btn-auto" onClick={onCancelDelete}>
                Cancelar
              </button>
              <button
                type="button"
                className="btn-primary workshop-btn-auto workshop-btn-danger"
                onClick={onConfirmDelete}
                autoFocus
              >
                Eliminar
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
