import React, { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Copy } from 'lucide-react'
import Leaderboard from './Leaderboard'
import Chat from './Chat'
import Card from './Card'
import LobbyOptions from './LobbyOptions'

export default function GameScreen({ gameState, mySid, socket }) {
  const { 
    room_id, state, black_card, players, czar, leader, 
    hand, played_cards, winner_sid, renew_votes, 
    has_voted_renew, total_active, options 
  } = gameState

  const [selectedCards, setSelectedCards] = useState([])

  const me = players.find(p => p.id === mySid)
  const isCzar = mySid === czar
  const isLeader = mySid === leader
  const canPlay = state === 'playing' && !isCzar && me && !me.has_played && !me.waiting_next_round

  const handleStart = () => {
    // If the inputs lost focus, options were sent.
    // We just emit start.
    socket.emit('start_game', { room_id })
  }

  const handleCardClick = (index) => {
    if (!canPlay) return
    const maxPicks = black_card?.pick || 1

    if (selectedCards.includes(index)) {
      setSelectedCards(selectedCards.filter(i => i !== index))
    } else {
      if (selectedCards.length < maxPicks) {
        const newSel = [...selectedCards, index]
        setSelectedCards(newSel)
        if (newSel.length === maxPicks) {
          socket.emit('play_card', { room_id, card_index: newSel })
          setSelectedCards([])
        }
      }
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
          {isCzar && <span className="badge">👑 Juez Actual</span>}
          <button 
            className="btn-secondary" 
            style={{ padding: '0.4rem 0.8rem', display: 'flex', alignItems: 'center', gap: '0.5rem', marginTop: 0 }} 
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
        <aside className="sidebar">
          <Leaderboard players={players} gameState={state} />
          <Chat socket={socket} room_id={room_id} />
        </aside>

        <main className="board">
          {state === 'waiting' ? (
            <div className="glass-panel" style={{ margin: 'auto' }}>
              <h2>Esperando jugadores...</h2>
              <p>Mínimo 2 jugadores para empezar.</p>
              {isLeader && (
                <>
                  <LobbyOptions socket={socket} roomId={room_id} options={options} />
                  <button 
                    className="btn-primary" 
                    onClick={handleStart} 
                    disabled={players.length < 2}
                    style={{ marginTop: '1rem', opacity: players.length < 2 ? 0.5 : 1, cursor: players.length < 2 ? 'not-allowed' : 'pointer' }}
                  >
                    {players.length < 2 ? 'Faltan jugadores' : 'Iniciar Partida'}
                  </button>
                </>
              )}
            </div>
          ) : (
            <>
              <div id="status-bar">
                {isCzar ? "Eres el Juez 👑. Espera a que los demás jueguen." :
                 me?.waiting_next_round ? "Partida en curso. Entrarás a jugar en la siguiente ronda." :
                 me?.has_played ? "Has jugado tus cartas. Esperando a los demás..." :
                 "Es tu turno. Elige cartas para jugar."}
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
                              socket.emit('reveal_card', { room_id, sub_id: pc.id })
                            } else {
                              const allRevealed = played_cards.every(c => c.revealed);
                              if (allRevealed) {
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

              <div id="game-controls" style={{ display: 'flex', gap: '1rem', justifyContent: 'center', marginBottom: '2rem' }}>
                <button 
                  className="btn-secondary" 
                  onClick={handleRenew} 
                  disabled={has_voted_renew}
                  style={{ opacity: has_voted_renew ? 0.5 : 1 }}
                >
                  {has_voted_renew ? `Votado (${renew_votes}/${neededVotes})` : `Renovar Cartas (${renew_votes}/${neededVotes})`}
                </button>
                {isCzar && state === 'playing' && (
                  <button className="btn-secondary" onClick={handleChangeBlack}>
                    Cambiar Carta Negra
                  </button>
                )}
                {isCzar && state === 'round_end' && (
                  <button className="btn-primary" onClick={() => socket.emit('next_round', { room_id })}>
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
                <AnimatePresence>
                  {hand.map((txt, idx) => (
                    <Card 
                      key={txt + idx} // simple unique key for now
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
    </div>
  )
}
