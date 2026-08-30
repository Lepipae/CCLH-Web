import React, { useState, useEffect, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Send } from 'lucide-react'

export default function Chat({ socket, room_id }) {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const chatMessagesRef = useRef(null)

  useEffect(() => {
    const handleChat = (data) => {
      setMessages(prev => [...prev, data])
    }
    
    socket.on('chat_message', handleChat)
    return () => socket.off('chat_message', handleChat)
  }, [socket])

  useEffect(() => {
    if (chatMessagesRef.current) {
      chatMessagesRef.current.scrollTop = chatMessagesRef.current.scrollHeight
    }
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
      <div className="chat-messages" ref={chatMessagesRef}>
        {messages.map((m, i) => (
          <div 
            key={i}
            className={`chat-message ${m.system ? 'system' : ''}`}
          >
            {!m.system && <strong>{m.sender}: </strong>}
            {m.msg}
          </div>
        ))}
      </div>
      <form className="chat-input-area" onSubmit={sendChat}>
        <input 
          type="text" 
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Escribe algo..." 
          maxLength="100" 
        />
        <button type="submit" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <Send size={18} />
        </button>
      </form>
    </div>
  )
}
