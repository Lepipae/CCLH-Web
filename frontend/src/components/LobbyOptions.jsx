import React, { useState, useEffect } from 'react'

export default function LobbyOptions({ socket, roomId, options }) {
  const [handSize, setHandSize] = useState(options?.hand_size || 10)
  const [renewThreshold, setRenewThreshold] = useState((options?.renew_threshold || 0.75) * 100)
  const [turnOrder, setTurnOrder] = useState(options?.turn_order || 'winner')

  useEffect(() => {
    if (options) {
      setHandSize(options.hand_size)
      setRenewThreshold(options.renew_threshold * 100)
      setTurnOrder(options.turn_order)
    }
  }, [options])

  const sendUpdate = (updates) => {
    socket.emit('update_room_options', {
      room_id: roomId,
      hand_size: updates.handSize !== undefined ? parseInt(updates.handSize) : handSize,
      renew_threshold: updates.renewThreshold !== undefined ? parseInt(updates.renewThreshold) / 100 : renewThreshold / 100,
      turn_order: updates.turnOrder !== undefined ? updates.turnOrder : turnOrder
    })
  }

  return (
    <div style={{ marginTop: '2rem', padding: '1.5rem', textAlign: 'left', background: 'rgba(0,0,0,0.2)', borderRadius: '12px', border: '1px solid rgba(255,255,255,0.05)' }}>
      <h3 style={{ marginBottom: '1rem', fontSize: '0.9rem', color: 'var(--text-secondary)' }}>⚙️ Opciones de la Sala</h3>
      
      <div className="form-group">
        <label>Cartas en la mano</label>
        <input 
          type="number" 
          min="5" max="20"
          value={handSize}
          onChange={(e) => setHandSize(e.target.value)}
          onBlur={(e) => sendUpdate({ handSize: e.target.value })}
        />
      </div>

      <div className="form-group">
        <label>Votos para renovar mano (%)</label>
        <input 
          type="number" 
          min="10" max="100" step="5"
          value={renewThreshold}
          onChange={(e) => setRenewThreshold(e.target.value)}
          onBlur={(e) => sendUpdate({ renewThreshold: e.target.value })}
        />
      </div>

      <div className="form-group">
        <label>Orden de turnos (Juez)</label>
        <select 
          value={turnOrder}
          onChange={(e) => {
            setTurnOrder(e.target.value)
            sendUpdate({ turnOrder: e.target.value })
          }}
          style={{ width: '100%', padding: '0.75rem', background: 'rgba(0,0,0,0.2)', color: 'white', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.1)' }}
        >
          <option value="winner">Gana el juez anterior</option>
          <option value="random">Aleatorio</option>
          <option value="sequential">Por orden de entrada</option>
        </select>
      </div>
    </div>
  )
}
