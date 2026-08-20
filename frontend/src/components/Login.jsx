import React, { useState } from 'react'
import { motion } from 'framer-motion'

export default function Login({ onJoin }) {
  const [name, setName] = useState('')
  const [room, setRoom] = useState('')

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
        <h1>Juego de Cartas</h1>
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
      </motion.div>
    </div>
  )
}
