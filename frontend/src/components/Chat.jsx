import React, { useState, useEffect, useRef } from 'react'
import { X, Send } from 'lucide-react'

export default function Chat({ messages, variant = 'sidebar', open = true, onClose = () => {}, onSend = () => {} }) {
  const [input, setInput] = useState('')
  const chatMessagesRef = useRef(null)

  useEffect(() => {
    if (chatMessagesRef.current) {
      chatMessagesRef.current.scrollTop = chatMessagesRef.current.scrollHeight
    }
  }, [messages, open])

  const sendChat = (e) => {
    e.preventDefault()
    if (input.trim()) {
      onSend(input.trim())
      setInput('')
    }
  }

  const messageList = (
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
  )

  const inputArea = (
    <form className="chat-input-area" onSubmit={sendChat}>
      <input
        type="text"
        value={input}
        onChange={(e) => setInput(e.target.value)}
        placeholder="Escribe algo..."
        maxLength="100"
        aria-label="Mensaje de chat"
      />
      <button type="submit" aria-label="Enviar mensaje" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Send size={18} />
      </button>
    </form>
  )

  // Mobile: fixed bottom drawer, opened from the banner FAB
  if (variant === 'drawer') {
    return (
      <>
        <div
          className={`chat-backdrop ${open ? 'visible' : ''}`}
          onClick={onClose}
          aria-hidden="true"
        />
        <div
          className={`chat-drawer ${open ? 'open' : ''}`}
          role="dialog"
          aria-label="Chat de la sala"
          aria-hidden={!open}
        >
          <h3>Chat</h3>
          <button type="button" className="chat-close-btn" onClick={onClose} aria-label="Cerrar chat">
            <X size={20} />
          </button>
          {messageList}
          {inputArea}
        </div>
      </>
    )
  }

  // Desktop: inline panel in the sidebar (always visible)
  return (
    <div className="sidebar-bottom chat-inline">
      <h3>Chat</h3>
      {messageList}
      {inputArea}
    </div>
  )
}
