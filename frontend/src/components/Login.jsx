import React, { useState } from 'react'
import { motion } from 'framer-motion'
import { Palette } from 'lucide-react'

export default function Login({ onJoin, onOpenWorkshop }) {
  const [name, setName] = useState('')
  const [room, setRoom] = useState(() => {
    const params = new URLSearchParams(window.location.search)
    return params.get('room') || ''
  })

  const handleSubmit = (e) => {
    e.preventDefault()
    if (name.trim() && room.trim()) {
      onJoin(name.trim(), room.trim().toUpperCase())
    }
  }

  return (
    <div className="screen active" id="login-screen">
      <motion.div 
        className="glass-panel"
        initial={{ y: 50, opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
        transition={{ duration: 0.6 }}
      >
        <h1 style={{ fontSize: '2rem' }}>Cartas Contra Miedo Y Hambre</h1>
        <p className="subtitle">Únete a una sala para empezar a jugar</p>
        
        <form onSubmit={handleSubmit} id="login-form">
          <div className="form-group">
            <label htmlFor="nickname">Tu Nombre</label>
            <input 
              type="text" 
              id="nickname" 
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Ej. Juan" 
              required 
              maxLength="15" 
            />
          </div>
          
          <div className="form-group">
            <label htmlFor="room">Código de Sala</label>
            <input 
              type="text" 
              id="room" 
              value={room}
              onChange={(e) => setRoom(e.target.value)}
              placeholder="Ej. SALA1" 
              required 
              maxLength="10" 
              style={{ textTransform: 'uppercase' }} 
            />
          </div>
          
          <motion.button 
            type="submit" 
            className="btn-primary"
            whileHover={{ scale: 1.05 }}
            whileTap={{ scale: 0.95 }}
          >
            Entrar a la Sala
          </motion.button>
        </form>

        <div style={{ marginTop: '1.5rem', paddingTop: '1.25rem', borderTop: '1px solid rgba(255, 255, 255, 0.1)' }}>
          <button 
            type="button" 
            className="btn-secondary"
            onClick={onOpenWorkshop}
            style={{ 
              marginTop: 0, 
              display: 'flex', 
              alignItems: 'center', 
              justifyContent: 'center', 
              gap: '0.5rem',
              background: 'rgba(167, 139, 250, 0.1)',
              borderColor: 'rgba(167, 139, 250, 0.3)'
            }}
          >
            <Palette size={18} color="#a78bfa" />
            Taller de Cartas Custom
          </button>
        </div>
      </motion.div>
    </div>
  )
}

