import React, { useState } from 'react'
import { Plus, Sparkles, Check } from 'lucide-react'
import { parseCustomCardsInput } from '../utils/cardsValidation'
import './CardWorkshop.css'

// Cartas blancas temporales de la sala (coma-separadas, no persisten).
// El parseo vive en utils/cardsValidation para poder probarlo; el servidor
// valida de nuevo al recibir `add_room_cards`.
export default function CustomCards({ socket, roomId }) {
  const [customText, setCustomText] = useState('')
  const [addedCount, setAddedCount] = useState(null)

  const handleAddCards = (e) => {
    e.preventDefault()
    const val = customText.trim()
    if (!val) return

    const cardList = parseCustomCardsInput(val)
    if (cardList.length === 0) return

    socket.emit('add_room_cards', { room_id: roomId, cards: val })
    setAddedCount(cardList.length)
    setCustomText('')

    setTimeout(() => {
      setAddedCount(null)
    }, 3000)
  }

  return (
    <div className="custom-cards-container custom-cards-box">
      <h3 className="custom-cards-title">
        <Sparkles size={16} color="var(--accent-primary)" />
        Añadir Cartas Blancas Custom
      </h3>
      <p className="custom-cards-desc" id="custom-cards-hint">
        Añade cartas personalizadas para esta partida (separadas por coma).
      </p>

      <form onSubmit={handleAddCards} className="custom-cards-form">
        <label htmlFor="custom-cards-input" className="hidden">Cartas personalizadas separadas por coma</label>
        <input
          id="custom-cards-input"
          type="text"
          placeholder="Ej: Mi suegra, Un gato ninja, Patatas bravas"
          value={customText}
          onChange={(e) => setCustomText(e.target.value)}
          aria-describedby="custom-cards-hint"
          maxLength={1000}
          className="custom-cards-input"
        />
        <button
          type="submit"
          className="btn-secondary custom-cards-submit"
          disabled={!customText.trim()}
        >
          <Plus size={16} /> Añadir
        </button>
      </form>

      {addedCount !== null && (
        <div className="custom-cards-ok" role="status">
          <Check size={16} />
          {addedCount === 1 ? '¡1 carta añadida a la sala!' : `¡${addedCount} cartas añadidas a la sala!`}
        </div>
      )}
    </div>
  )
}
