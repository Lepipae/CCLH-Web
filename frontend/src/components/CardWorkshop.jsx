import React, { useState, useEffect } from 'react'
import { motion } from 'framer-motion'
import { ArrowLeft, Plus, Trash2, FileCode, CheckCircle, AlertCircle } from 'lucide-react'
import Card from './Card'

export default function CardWorkshop({ socket, onBack }) {
  const [cardType, setCardType] = useState('white')
  const [cardText, setCardText] = useState('')
  const [cardPick, setCardPick] = useState(1)
  const [loading, setLoading] = useState(false)
  const [alertInfo, setAlertInfo] = useState(null) // { message, isError }
  const [customCards, setCustomCards] = useState({ whiteCards: [], blackCards: [] })
  const [filterTab, setFilterTab] = useState('all') // 'all' | 'white' | 'black'

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

    return () => {
      socket.off('custom_cards_list')
      socket.off('custom_card_result')
      socket.off('custom_card_deleted')
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

  const whiteList = customCards.whiteCards || []
  const blackList = customCards.blackCards || []
  const totalCount = whiteList.length + blackList.length

  return (
    <div className="screen active" style={{ overflowY: 'auto', padding: '2rem 1rem' }}>
      <div style={{ maxWidth: '900px', margin: '0 auto', width: '100%' }}>
        
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
