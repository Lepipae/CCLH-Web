import React, { useState, useEffect, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'

export default function Chat({ socket, room_id }) {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const messagesEndRef = useRef(null)

  useEffect(() => {
    const handleChat = (data) => {
      setMessages(prev => [...prev, data])
    }
    
    socket.on('chat_message', handleChat)
    return () => socket.off('chat_message', handleChat)
  }, [socket])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const sendChat = (e) => {
    e.preventDefault()
    if (input.trim()) {
      socket.emit('send_chat', { room_id, msg: input.trim() })
      setInput('')
    }
  }

  return (
    <div className="sidebar-bottom">
      <h3>Chat</h3>
      <div className="chat-messages">
        <AnimatePresence>
          {messages.map((m, i) => (
            <motion.div 
              key={i}
              initial={{ opacity: 0, x: -10 }}
              animate={{ opacity: 1, x: 0 }}
              className={`chat-message ${m.system ? 'system' : ''}`}
            >
              {!m.system && <strong>{m.sender}: </strong>}
              {m.msg}
            </motion.div>
          ))}
        </AnimatePresence>
        <div ref={messagesEndRef} />
      </div>
      <form className="chat-input-area" onSubmit={sendChat}>
        <input 
          type="text" 
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Escribe algo..." 
          maxLength="100" 
        />
        <button type="submit">En</button>
      </form>
    </div>
  )
}
