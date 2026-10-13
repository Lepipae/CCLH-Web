import React, { useState, useEffect } from 'react'
import { AnimatePresence } from 'framer-motion'
import { Copy, Sparkles, X, Volume2, VolumeX } from 'lucide-react'
import Leaderboard from './Leaderboard'
import PlayersBanner from './PlayersBanner'
import Chat from './Chat'
import Card from './Card'
import LobbyOptions from './LobbyOptions'
import CustomCards from './CustomCards'
import { soundManager } from '../utils/soundManager'
import { calcNeededVotes } from '../utils/cardsValidation'
import './CardWorkshop.css'

export default function GameScreen({ gameState, mySid, socket, presencia = {}, avisos = [], miGracia = null }) {
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
  // Toast no bloqueante (sustituye a alert()): copiar enlace, orden del juez.
  const [notice, setNotice] = useState(null) // { message, kind }

  const me = players.find(p => p.id === mySid)
  const isCzar = mySid === czar
  const isLeader = mySid === leader
  const canPlay = state === 'playing' && !isCzar && me && !me.has_played && !me.waiting_next_round

  useEffect(() => {
    if (state === 'round_end') {
      soundManager.winRound()
    }
  }, [state])

  useEffect(() => {
    if (!notice) return
    const t = setTimeout(() => setNotice(null), 4000)
    return () => clearTimeout(t)
  }, [notice])

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

  const handleInvite = async () => {
    const inviteUrl = `${window.location.origin}${window.location.pathname}?room=${room_id}`
    try {
      await navigator.clipboard.writeText(inviteUrl)
      setNotice({ message: `¡Enlace de invitación copiado! ${inviteUrl}`, kind: 'ok' })
    } catch {
      setNotice({ message: `Copia este enlace: ${inviteUrl}`, kind: 'info' })
    }
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

  // Acción del juez sobre una pila: revelar boca abajo o elegir ganadora.
  // Extraída para compartirla entre click y teclado (misma lógica, sin duplicar).
  const handleJudgeAction = (pc) => {
    if (!(state === 'judging' && isCzar)) return
    if (!pc.revealed) {
      soundManager.revealCard()
      socket.emit('reveal_card', { room_id, sub_id: pc.id })
    } else {
      const allRevealed = played_cards.every(c => c.revealed)
      if (allRevealed) {
        soundManager.winRound()
        socket.emit('choose_winner', { room_id, sub_id: pc.id })
      } else {
        setNotice({ message: 'Revela todas las cartas antes de elegir un ganador.', kind: 'info' })
      }
    }
  }

  const handleJudgeKeyDown = (e, pc) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      handleJudgeAction(pc)
    }
  }

  // Umbral de renovación: el servidor cuenta los votos de verdad; aquí solo
  // se pinta cuántos faltan (función pura testeada en cardsValidation).
  const currentThreshold = options?.renew_threshold || 0.75
  const neededVotes = calcNeededVotes(total_active, currentThreshold)

  // --- Presencia en dos fases ---------------------------------------------
  // El estado de cada jugador (vivo / sospechoso / purgado) llega en la propia
  // foto de la sala, así que el badge de PlayersBanner no depende de que este
  // cliente hubiera visto el evento. Aquí solo se pinta el aviso y, si el
  // sospechoso soy yo, la cuenta atrás de lo que me queda de asiento.
  // El plazo de gracia lo envía el reto (`presence_challenge`) y se muestra tal
  // cual. Una cuenta atrás al segundo necesitaría un temporizador en el cliente
  // para compensar el desfase entre los relojes de aquí y del servidor, y aquí
  // no compensa: lo que le importa a quien está leyendo es cuánto le queda de
  // margen, no qué punto del segundo le queda.
  const meSospechosa = presencia[mySid]?.state === 'sospechoso'

  return (
    <div className="screen active game-screen-col">
      {notice && (
        <div className={`app-toast app-toast-${notice.kind}`} role="status">
          <span>{notice.message}</span>
          <button type="button" onClick={() => setNotice(null)} aria-label="Cerrar aviso">
            <X size={16} />
          </button>
        </div>
      )}
      <header className="game-header">
        <div className="header-left">
          <h2>Sala: {room_id}</h2>
          {meSospechosa && (
            <div className="presence-warning" role="status">
              Te hemos perdido el hilo. Contestamos el reto por ti, pero si no
              vuelve a hablar en {miGracia?.grace ?? '—'}s se libera tu asiento ⚠️
            </div>
          )}
          {avisos.length > 0 && (
            <ul className="presence-notices" aria-live="polite">
              {avisos.map(a => (
                <li key={a.id} className={`presence-notice presence-${a.state}`}>
                  {a.state === 'sospechoso' && <>⚠️ <b>{a.name}</b> lleva {a.left ?? '—'}s sin responder. Si vuelve, recupera su asiento.</>}
                  {a.state === 'purgado' && <>🚪 Se ha liberado el asiento de <b>{a.name}</b> (sin respuesta).</>}
                  {a.state === 'vivo' && <>✅ <b>{a.name}</b> ha vuelto. Su asiento se conserva.</>}
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="header-right game-header-right">
          <span id="player-name">{me?.name || 'Espectador'}</span>
          {isCzar && <span className="badge">🔨 Juez</span>}
          <button
            type="button"
            className="btn-secondary game-header-btn"
            onClick={handleToggleMute}
            title={isMuted ? 'Activar sonido' : 'Silenciar sonido'}
            aria-label={isMuted ? 'Activar sonido' : 'Silenciar sonido'}
            aria-pressed={!isMuted}
          >
            {isMuted ? <VolumeX size={18} color="#ef4444" /> : <Volume2 size={18} color="#10b981" />}
          </button>
          <button
            type="button"
            className="btn-secondary game-header-btn game-header-btn-wide"
            onClick={handleInvite}
          >
            <Copy size={16} /> Invitar
          </button>
        </div>
      </header>

      <div className="game-layout">
        <PlayersBanner
          players={players}
          presencia={presencia}
          onOpenChat={() => { setChatOpen(true); setUnreadChat(0) }}
          unreadCount={unreadChat}
        />
        <aside className="sidebar">
          <Leaderboard players={players} gameState={state} presencia={presencia} />
          <Chat variant="sidebar" messages={chatMessages} onSend={handleSendChat} />
        </aside>

        <main className="board">
          {state === 'waiting' ? (
            <div className="glass-panel game-waiting-panel">
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
                  type="button"
                  className="btn-primary game-start-btn"
                  onClick={handleStart}
                  disabled={players.length < 2}
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
                    {played_cards.map((pc) => {
                      // Solo la pila del juez es accionable: el div se anuncia
                      // como botón únicamente cuando el juez puede actuar.
                      const judgeActive = state === 'judging' && isCzar
                      return (
                        <div
                          key={pc.id}
                          className={`card-stack ${!pc.revealed && state === 'judging' ? 'face-down' : ''} ${judgeActive ? 'card-stack-judge' : ''}`}
                          {...(judgeActive
                            ? {
                                role: 'button',
                                tabIndex: 0,
                                'aria-label': pc.revealed
                                  ? 'Elegir esta respuesta como ganadora'
                                  : 'Revelar esta respuesta',
                                onClick: () => handleJudgeAction(pc),
                                onKeyDown: (e) => handleJudgeKeyDown(e, pc),
                              }
                            : {})}
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
                      )
                    })}
                  </AnimatePresence>
                </div>
              </div>

              <div id="game-controls" className="game-controls">
                <button
                  type="button"
                  className="btn-secondary game-controls-btn"
                  onClick={() => setShowCustomModal(true)}
                >
                  <Sparkles size={16} className="game-inline-icon" />
                  Cartas Custom
                </button>
                {!isCzar && (
                  <button
                    type="button"
                    className={`btn-secondary game-controls-btn ${has_voted_renew ? 'game-controls-btn-voted' : ''}`}
                    onClick={handleRenew}
                    title={has_voted_renew ? 'Haz clic para cancelar tu voto' : 'Vota para cambiar las cartas de tu mano'}
                  >
                    {has_voted_renew
                      ? `✓ Votado para renovar (${renew_votes}/${neededVotes})`
                      : `Renovar Cartas (${renew_votes}/${neededVotes})`}
                  </button>
                )}
                {isCzar && state === 'playing' && (
                  <button type="button" className="btn-secondary game-controls-btn" onClick={handleChangeBlack}>
                    Cambiar Carta Negra
                  </button>
                )}
                {isCzar && state === 'round_end' && (
                  <button type="button" className="btn-primary game-controls-btn" onClick={() => socket.emit('next_round', { room_id })}>
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
                      // La key NO puede ser el texto: dos cartas iguales en la
                      // misma mano (el mazo no garantiza que sean únicas) daban
                      // dos elementos con la misma key, y React avisa y recyclea
                      // el nodo equivocado al reorderar. El índice es lo único
                      // único que hay aquí, porque al serializar la mano se
                      // pierde el `card_id` que lleva la carta en el servidor.
                      key={`${idx}:${txt}`}
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
        <div
          className="game-modal-backdrop"
          role="button"
          tabIndex={0}
          aria-label="Cerrar cartas personalizadas (pulsa Escape o haz clic fuera)"
          onClick={() => setShowCustomModal(false)}
          onKeyDown={(e) => { if (e.key === 'Escape') setShowCustomModal(false) }}
        >
          <div
            className="glass-panel game-modal-panel"
            role="dialog"
            aria-modal="true"
            aria-label="Añadir cartas personalizadas a la sala"
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => e.stopPropagation()}
          >
            <button
              type="button"
              onClick={() => setShowCustomModal(false)}
              className="game-modal-close"
              aria-label="Cerrar cartas personalizadas"
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
