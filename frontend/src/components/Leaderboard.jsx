import React from 'react'
import { motion, AnimatePresence } from 'framer-motion'

export default function Leaderboard({ players, gameState }) {
  // Sort players by points
  const sortedPlayers = [...players].sort((a, b) => b.points - a.points)

  return (
    <div className="sidebar-top">
      <h3>Jugadores</h3>
      <ul id="leaderboard">
        <AnimatePresence>
          {sortedPlayers.map((p) => {
            let status = ''
            if (p.is_czar) {
              status = ' 🔨'
            } else if (p.waiting_next_round) {
              status = ' 🕒'
            } else if (p.has_played) {
              status = ' ✅'
            } else if (gameState === 'playing') {
              status = ' ⏳'
            }

            return (
              <motion.li 
                key={p.id}
                layout
                initial={{ opacity: 0, x: -20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -20 }}
                className="player-item"
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span>{p.name}{status}</span>
                  <span className="badge">{p.points}</span>
                </div>
              </motion.li>
            )
          })}
        </AnimatePresence>
      </ul>
    </div>
  )
}
