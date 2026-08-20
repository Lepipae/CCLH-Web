import React from 'react'
import { motion } from 'framer-motion'

export default function Card({ 
  text, 
  type = 'white', 
  pick = null,
  onClick, 
  isPlayable = false, 
  isSelected = false,
  isFaceDown = false,
  isWinner = false,
  isLoser = false,
  selectionOrder = null,
  author = null
}) {
  
  let className = `card ${type}`
  if (isPlayable) className += ' playable'
  if (isSelected) className += ' selected'
  if (isFaceDown) className += ' face-down'
  if (isWinner) className += ' winner-card winner-animation'
  if (isLoser) className += ' loser-fade'

  return (
    <motion.div 
      className={className}
      onClick={isPlayable ? onClick : undefined}
      data-selection-order={selectionOrder}
      initial={{ scale: 0.8, opacity: 0, y: 20 }}
      animate={{ scale: 1, opacity: 1, y: 0 }}
      exit={{ scale: 0.8, opacity: 0 }}
      whileHover={isPlayable ? { y: -10, scale: 1.05 } : {}}
      transition={{ type: 'spring', stiffness: 300, damping: 20 }}
      layout
    >
      <div className="card-text">
        {!isFaceDown && text}
      </div>
      
      {!isFaceDown && author && (
        <div className="card-author">de {author}</div>
      )}
      
      {!isFaceDown && (
        <div className="card-footer">
          Cartas Contra Miedo Y Hambre {pick > 1 && `(ROBA ${pick})`}
        </div>
      )}
    </motion.div>
  )
}
