import React, { useState } from 'react'
import { Plus, Sparkles, Check } from 'lucide-react'

export default function CustomCards({ socket, roomId }) {
  const [customText, setCustomText] = useState('')
  const [addedCount, setAddedCount] = useState(null)

  const handleAddCards = (e) => {
    e.preventDefault()
    const val = customText.trim()
    if (!val) return

    const cardList = val.split(',').map(c => c.trim()).filter(Boolean)
    if (cardList.length === 0) return

    socket.emit('add_room_cards', { room_id: roomId, cards: val })
    setAddedCount(cardList.length)
    setCustomText('')

    setTimeout(() => {
      setAddedCount(null)
    }, 3000)
  }

  return (
    <div 
      className="custom-cards-container" 
      style={{ 
        marginTop: '1.5rem', 
        padding: '1.25rem', 
        textAlign: 'left', 
        background: 'rgba(0, 0, 0, 0.25)', 
        borderRadius: '12px', 
        border: '1px solid rgba(255, 255, 255, 0.08)' 
      }}
    >
      <h3 style={{ 
        marginBottom: '0.5rem', 
        fontSize: '0.9rem', 
        color: 'var(--text-primary)', 
        display: 'flex', 
        alignItems: 'center', 
        gap: '0.5rem' 
      }}>
        <Sparkles size={16} color="var(--accent-primary)" />
        Añadir Cartas Blancas Custom
      </h3>
      <p style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: '0.75rem' }}>
        Añade cartas personalizadas para esta partida (separadas por coma).
      </p>

      <form onSubmit={handleAddCards} style={{ display: 'flex', gap: '0.5rem' }}>
        <input 
          type="text" 
          placeholder="Ej: Mi suegra, Un gato ninja, Patatas bravas" 
          value={customText} 
          onChange={(e) => setCustomText(e.target.value)} 
          style={{ 
            flex: 1, 
            padding: '0.75rem 1rem', 
            background: 'rgba(0, 0, 0, 0.3)', 
            border: '1px solid rgba(255, 255, 255, 0.1)', 
            borderRadius: '8px', 
            color: 'white', 
            fontSize: '0.9rem', 
            outline: 'none' 
          }} 
        />
        <button 
          type="submit" 
          className="btn-secondary" 
          disabled={!customText.trim()}
          style={{ 
            marginTop: 0, 
            width: 'auto', 
            whiteSpace: 'nowrap', 
            display: 'flex', 
            alignItems: 'center', 
            gap: '0.4rem', 
            padding: '0.75rem 1.25rem',
            opacity: !customText.trim() ? 0.5 : 1,
            cursor: !customText.trim() ? 'not-allowed' : 'pointer'
          }}
        >
          <Plus size={16} /> Añadir
        </button>
      </form>

      {addedCount !== null && (
        <div style={{ 
          marginTop: '0.75rem', 
          fontSize: '0.85rem', 
          color: '#4ade80', 
          display: 'flex', 
          alignItems: 'center', 
          gap: '0.4rem' 
        }}>
          <Check size={16} />
          {addedCount === 1 ? '¡1 carta añadida a la sala!' : `¡${addedCount} cartas añadidas a la sala!`}
        </div>
      )}
    </div>
  )
}
