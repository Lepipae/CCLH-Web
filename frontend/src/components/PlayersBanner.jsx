import React from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { MessageCircle } from 'lucide-react'

export default function PlayersBanner({ players, onOpenChat, unreadCount }) {
  const sortedPlayers = [...players].sort((a, b) => b.points - a.points)
  const top3 = sortedPlayers.slice(0, 3)

  return (
    <div className="players-banner">
      <div className="players-banner-list">
        <AnimatePresence>
          {top3.map((p, i) => (
            <motion.div
              key={p.id}
              layout
              initial={{ opacity: 0, y: -8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              className="player-chip"
            >
              {i === 0 && <span className="player-chip-medal">👑</span>}
              <span className="player-chip-name">{p.name}</span>
              {p.is_czar && <span title="Juez">🔨</span>}
              {p.has_played && !p.is_czar && <span title="Ya jugó">✅</span>}
              {p.waiting_next_round && !p.is_czar && <span title="Espectador">🕒</span>}
              <span className="player-chip-points">{p.points}</span>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
      <button
        type="button"
        className="chat-fab"
        onClick={onOpenChat}
        aria-label="Abrir chat"
        title="Abrir chat"
      >
        <MessageCircle size={20} />
        {unreadCount > 0 && <span className="chat-fab-badge">{unreadCount > 99 ? '99+' : unreadCount}</span>}
      </button>
    </div>
  )
}
