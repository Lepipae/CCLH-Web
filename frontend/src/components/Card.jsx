import React, { useRef, useLayoutEffect } from 'react'
import { motion } from 'framer-motion'
import { Heart } from 'lucide-react'

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
  author = null,
  votesCount = 0,
  hasVoted = false,
  isVotable = false,
  isMine = false,
  onVote = null
}) {
  const cardRef = useRef(null)
  const textRef = useRef(null)

  useLayoutEffect(() => {
    const textEl = textRef.current
    const cardEl = cardRef.current
    if (!textEl || !cardEl || isFaceDown) return

    let animationFrameId = null

    const fitText = () => {
      if (!textEl || !cardEl) return

      textEl.style.fontSize = ''

      const cardHeight = cardEl.clientHeight
      const textHeight = textEl.clientHeight
      if (cardHeight <= 0 || textHeight <= 0) return

      const computed = window.getComputedStyle(textEl)
      let currentPx = parseFloat(computed.fontSize) || 20
      const minPx = 8

      textEl.style.fontSize = `${currentPx}px`

      const isOverflowing = () => {
        const textOverflow = textEl.scrollHeight > textEl.clientHeight + 1 || textEl.scrollWidth > textEl.clientWidth + 1
        const cardOverflow = cardEl.scrollHeight > cardEl.clientHeight + 1
        return textOverflow || cardOverflow
      }

      while (isOverflowing() && currentPx > minPx) {
        currentPx -= 0.5
        textEl.style.fontSize = `${currentPx}px`
      }
    }

    fitText()
    animationFrameId = requestAnimationFrame(fitText)

    const ro = new ResizeObserver(() => {
      fitText()
    })
    ro.observe(cardEl)
    ro.observe(textEl)

    return () => {
      if (animationFrameId) cancelAnimationFrame(animationFrameId)
      ro.disconnect()
    }
  }, [text, isFaceDown])

  let className = `card ${type}`
  if (isPlayable) className += ' playable'
  if (isSelected) className += ' selected'
  if (isFaceDown) className += ' face-down'
  if (isWinner) className += ' winner-card winner-animation'
  if (isLoser) className += ' loser-fade'

  const showVoteBadge = !isFaceDown && (votesCount > 0 || isVotable || isMine)

  return (
    <motion.div 
      ref={cardRef}
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
      <div className="card-text" ref={textRef}>
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

      {showVoteBadge && (
        <button
          type="button"
          className={`card-vote-badge ${hasVoted ? 'voted' : ''} ${isMine ? 'mine' : ''}`}
          onClick={(e) => {
            e.stopPropagation()
            if (isVotable && onVote) {
              onVote()
            }
          }}
          disabled={!isVotable || isMine}
          title={
            isMine 
              ? 'Esta es tu carta (no puedes votarla)' 
              : isVotable 
                ? (hasVoted ? 'Quitar voto favorito' : 'Votar como tu favorita ❤️') 
                : `${votesCount} voto${votesCount !== 1 ? 's' : ''}`
          }
        >
          <Heart size={13} fill={hasVoted ? "#ffffff" : "currentColor"} color={hasVoted ? "#ffffff" : "currentColor"} />
          {votesCount > 0 && <span className="vote-count">{votesCount}</span>}
        </button>
      )}
    </motion.div>
  )
}

