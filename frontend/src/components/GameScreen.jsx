import React, { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Copy, Sparkles, X, Volume2, VolumeX } from 'lucide-react'
import Leaderboard from './Leaderboard'
import PlayersBanner from './PlayersBanner'
import Chat from './Chat'
import Card from './Card'
import LobbyOptions from './LobbyOptions'
import CustomCards from './CustomCards'
import { soundManager } from '../utils/soundManager'

export default function GameScreen({ gameState, mySid, socket }) {
  const { 
    room_id, state, black_card, players, czar, leader, 
    hand, played_cards, winner_sid, renew_votes, 
    has_voted_renew, total_active, options 
  } = gameState

  const [selectedCards, setSelectedCards] = useState([])
  const [showCustomModal, setShowCustomModal] = useState(false)
  const [isMuted, setIsMuted] = useState(soundManager.isMuted())
  const [chatMessages, setChatMessages] = useState([])
  const [chatOpen, setChatOpen] = useState(false)
  const [unreadChat, setUnreadChat] = useState(0)

  const me = players.find(p => p.id === mySid)
  const isCzar = mySid === czar
  const isLeader = mySid === leader
  const canPlay = state === 'playing' && !isCzar && me && !me.has_played && !me.waiting_next_round

  useEffect(() => {
    if (state === 'round_end') {
      soundManager.winRound()
    }
  }, [state])

  // Single socket listener owns chat history so the drawer keeps it while closed
  const MAX_CHAT = 200
  useEffect(() => {
    const handleChat = (data) => {
      setChatMessages(prev => {
        const next = [...prev, data]
        return next.length > MAX_CHAT ? next.slice(next.length - MAX_CHAT) : next
      })
      if (!chatOpen) setUnreadChat(c => c + 1)
    }
    socket.on('chat_message', handleChat)
    return () => socket.off('chat_message', handleChat)
  }, [socket, chatOpen])

  const handleSendChat = (msg) => {
    socket.emit('send_chat', { room_id, msg })
  }

  const handleStart = () => {
    socket.emit('start_game', { room_id })
  }

  const handleToggleMute = () => {
    const muted = soundManager.toggleMute()
    setIsMuted(muted)
  }

  const handleCardClick = (index) => {
    if (!canPlay) return
    const maxPicks = black_card?.pick || 1

    if (selectedCards.includes(index)) {
      soundManager.selectCard()
      setSelectedCards(selectedCards.filter(i => i !== index))
    } else {
      if (selectedCards.length < maxPicks) {
        soundManager.selectCard()
        const newSel = [...selectedCards, index]
        setSelectedCards(newSel)
        if (newSel.length === maxPicks) {
          soundManager.playCard()
          socket.emit('play_card', { room_id, card_index: newSel })
          setSelectedCards([])
        }
      }
    }
  }

  const handleVoteCard = (cardId) => {
    if (!isCzar && (state === 'judging' || state === 'round_end')) {
      soundManager.voteCard()
      socket.emit('vote_card', { room_id, card_id: cardId })
    }
  }

  const handleRenew = () => {
    socket.emit('vote_renew', { room_id })
  }

  const handleChangeBlack = () => {
    socket.emit('change_black_card', { room_id })
  }

  const currentThreshold = options?.renew_threshold || 0.75
  const neededVotes = Math.ceil(total_active * currentThreshold)

  return (
    <div className="screen active" style={{ display: 'flex', flexDirection: 'column' }}>
      <header className="game-header">
        <div className="header-left">
          <h2>Sala: {room_id}</h2>
        </div>
        <div className="header-right" style={{ display: 'flex', alignItems: 'center', gap: '1rem' }}>
          <span id="player-name">{me?.name || 'Espectador'}</span>
          {isCzar && <span className="badge">🔨 Juez</span>}
          <button 
            className="btn-secondary"
            style={{ padding: '0.4rem 0.6rem', display: 'flex', alignItems: 'center', gap: '0.4rem', marginTop: 0 }}
            onClick={handleToggleMute}
            title={isMuted ? 'Activar sonido' : 'Silenciar sonido'}
          >
            {isMuted ? <VolumeX size={18} color="#ef4444" /> : <Volume2 size={18} color="#10b981" />}
          </button>
          <button 
            className="btn-secondary" 
            style={{ padding: '0.4rem 0.8rem', display: 'flex', alignItems: 'center', gap: '0.4rem', marginTop: 0 }} 
            onClick={() => {
              const inviteUrl = `${window.location.origin}${window.location.pathname}?room=${room_id}`
              navigator.clipboard.writeText(inviteUrl)
              alert(`¡Enlace de invitación copiado al portapapeles!\n\n${inviteUrl}`)
            }}
          >
            <Copy size={16} /> Invitar
          </button>
        </div>
      </header>

      <div className="game-layout">
        <PlayersBanner
          players={players}
          onOpenChat={() => { setChatOpen(true); setUnreadChat(0) }}
          unreadCount={unreadChat}
        />
        <aside className="sidebar">
          <Leaderboard players={players} gameState={state} />
          <Chat variant="sidebar" messages={chatMessages} onSend={handleSendChat} />
        </aside>

        <main className="board">
          {state === 'waiting' ? (
            <div className="glass-panel" style={{ margin: 'auto', maxWidth: '550px', width: '90%' }}>
              <h2>
                {players.length < 2 
                  ? 'Esperando jugadores...' 
                  : isLeader 
                    ? '¡Todo listo para empezar!' 
                    : 'Esperando al líder...'}
              </h2>
              <p>
                {players.length < 2 
                  ? 'Mínimo 2 jugadores para empezar.' 
                  : isLeader 
                    ? 'Configura las opciones y dale a iniciar.' 
                    : 'El líder está configurando la sala y pronto iniciará la partida.'}
              </p>
              {isLeader && (
                <LobbyOptions socket={socket} roomId={room_id} options={options} />
              )}
              
              <CustomCards socket={socket} roomId={room_id} />

              {isLeader && (
                <button 
                  className="btn-primary" 
                  onClick={handleStart} 
                  disabled={players.length < 2}
                  style={{ marginTop: '1.5rem', opacity: players.length < 2 ? 0.5 : 1, cursor: players.length < 2 ? 'not-allowed' : 'pointer' }}
                >
                  {players.length < 2 ? 'Faltan jugadores' : 'Iniciar Partida'}
                </button>
              )}
            </div>
          ) : (
            <>
              <div id="status-bar">
                {state === 'judging' ? (
                  isCzar ? (
                    played_cards.every(c => c.revealed) 
                      ? "¡Todas las respuestas reveladas! Haz clic en la que más te guste para elegir al ganador 🏆." 
                      : "¡Todos han jugado! Haz clic en cada carta boca abajo para revelarla 🔨."
                  ) : (
                    "El Juez 🔨 está revelando y evaluando las respuestas... ⚖️"
                  )
                ) : state === 'round_end' ? (
                  isCzar ? (
                    "¡Ganador seleccionado! Pulsa 'Siguiente Ronda' cuando queráis continuar."
                  ) : (
                    "Ronda finalizada. Esperando a que el Juez inicie la siguiente ronda."
                  )
                ) : isCzar ? (
                  "Eres el Juez 🔨. Espera a que los demás jugadores elijan su respuesta."
                ) : me?.waiting_next_round ? (
                  "Partida en curso. Entrarás a jugar en la siguiente ronda."
                ) : me?.has_played ? (
                  "Has jugado tu carta ✅. Esperando a que los demás jugadores terminen..."
                ) : (
                  "Es tu turno. Elige tu carta para jugar."
                )}
              </div>

              <div className="center-area">
                {black_card && (
                  <Card 
                    text={black_card.text} 
                    type="black" 
                    pick={black_card.pick} 
                  />
                )}
                
                <div className="played-cards">
                  <AnimatePresence>
                    {played_cards.map((pc, i) => (
                      <div 
                        key={pc.id} 
                        className={`card-stack ${!pc.revealed && state === 'judging' ? 'face-down' : ''}`}
                        onClick={() => {
                          if (state === 'judging' && isCzar) {
                            if (!pc.revealed) {
                              soundManager.revealCard()
                              socket.emit('reveal_card', { room_id, sub_id: pc.id })
                            } else {
                              const allRevealed = played_cards.every(c => c.revealed);
                              if (allRevealed) {
                                soundManager.winRound()
                                socket.emit('choose_winner', { room_id, sub_id: pc.id })
                              } else {
                                alert('¡Debes revelar todas las cartas antes de elegir un ganador!');
                              }
                            }
                          }
                        }}
                      >
                        {pc.revealed || state === 'round_end' ? (
                          pc.cards.map((txt, idx) => (
                            <Card 
                              key={idx} 
                              text={txt} 
                              type="white" 
                              isWinner={state === 'round_end' && pc.sid === winner_sid}
                              isLoser={state === 'round_end' && pc.sid !== winner_sid}
                              isPlayable={state === 'judging' && isCzar}
                              votesCount={idx === pc.cards.length - 1 ? (pc.votes_count || 0) : 0}
                              hasVoted={idx === pc.cards.length - 1 ? Boolean(pc.has_voted) : false}
                              isVotable={!isCzar && (state === 'judging' || state === 'round_end') && !pc.is_mine && idx === pc.cards.length - 1}
                              isMine={pc.is_mine}
                              onVote={() => handleVoteCard(pc.id)}
                            />
                          ))
                        ) : (
                          // Dummy cards for face down
                          Array(black_card?.pick || 1).fill(0).map((_, idx) => (
                            <Card key={idx} isFaceDown={true} isPlayable={isCzar} />
                          ))
                        )}
                      </div>
                    ))}
                  </AnimatePresence>
                </div>
              </div>

              <div id="game-controls" style={{ display: 'flex', gap: '1rem', justifyContent: 'center', marginBottom: '2rem', flexWrap: 'wrap' }}>
                <button 
                  className="btn-secondary" 
                  onClick={() => setShowCustomModal(true)}
                  style={{ width: 'auto', marginTop: 0 }}
                >
                  <Sparkles size={16} style={{ display: 'inline', marginRight: '0.4rem', verticalAlign: 'middle' }} />
                  Cartas Custom
                </button>
                {!isCzar && (
                  <button 
                    className="btn-secondary" 
                    onClick={handleRenew} 
                    style={{ 
                      width: 'auto',
                      marginTop: 0,
                      background: has_voted_renew ? 'rgba(59, 130, 246, 0.3)' : undefined,
                      borderColor: has_voted_renew ? 'var(--accent-primary)' : undefined
                    }}
                    title={has_voted_renew ? 'Haz clic para cancelar tu voto' : 'Vota para cambiar las cartas de tu mano'}
                  >
                    {has_voted_renew 
                      ? `✓ Votado para renovar (${renew_votes}/${neededVotes})` 
                      : `Renovar Cartas (${renew_votes}/${neededVotes})`}
                  </button>
                )}
                {isCzar && state === 'playing' && (
                  <button className="btn-secondary" onClick={handleChangeBlack} style={{ width: 'auto', marginTop: 0 }}>
                    Cambiar Carta Negra
                  </button>
                )}
                {isCzar && state === 'round_end' && (
                  <button className="btn-primary" onClick={() => socket.emit('next_round', { room_id })} style={{ width: 'auto', marginTop: 0 }}>
                    Siguiente Ronda
                  </button>
                )}
              </div>
            </>
          )}

          {state !== 'waiting' && hand && (
            <div className="player-hand-container">
              <h3>Tu Mano</h3>
              <div className="player-hand">
                <AnimatePresence mode="popLayout">
                  {hand.map((txt, idx) => (
                    <Card
                      key={txt} // Usar solo el texto para que la key sea estable al desplazar índices
                      text={txt}
                      type="white"
                      isPlayable={canPlay}
                      isSelected={selectedCards.includes(idx)}
                      selectionOrder={selectedCards.indexOf(idx) !== -1 ? selectedCards.indexOf(idx) + 1 : null}
                      onClick={() => handleCardClick(idx)}
                    />
                  ))}
                </AnimatePresence>
              </div>
            </div>
          )}
        </main>
      </div>

      {/* Mobile chat drawer (opened from the banner FAB) */}
      <Chat
        variant="drawer"
        messages={chatMessages}
        open={chatOpen}
        onClose={() => setChatOpen(false)}
        onSend={handleSendChat}
      />

      {showCustomModal && (
        <div style={{
          position: 'fixed',
          top: 0, left: 0, right: 0, bottom: 0,
          background: 'rgba(0, 0, 0, 0.7)',
          backdropFilter: 'blur(8px)',
          zIndex: 1000,
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'center',
          padding: '1rem'
        }}>
          <div className="glass-panel" style={{ maxWidth: '500px', width: '100%', position: 'relative' }}>
            <button 
              onClick={() => setShowCustomModal(false)}
              style={{
                position: 'absolute',
                top: '1rem',
                right: '1rem',
                background: 'transparent',
                border: 'none',
                color: 'var(--text-secondary)',
                cursor: 'pointer'
              }}
            >
              <X size={20} />
            </button>
            <CustomCards socket={socket} roomId={room_id} />
          </div>
        </div>
      )}
    </div>
  )
}


