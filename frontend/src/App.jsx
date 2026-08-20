import { useEffect, useState } from 'react'
import { io } from 'socket.io-client'
import Login from './components/Login'
import GameScreen from './components/GameScreen'

const socket = io()

function App() {
  const [inGame, setInGame] = useState(false)
  const [gameState, setGameState] = useState(null)
  const [mySid, setMySid] = useState(null)

  useEffect(() => {
    socket.on('connect', () => {
      setMySid(socket.id)
    })

    socket.on('game_update', (data) => {
      setGameState(data)
    })

    socket.on('join_error', (data) => {
      alert(data.message)
      setInGame(false)
    })

    return () => {
      socket.off('connect')
      socket.off('game_update')
      socket.off('join_error')
    }
  }, [])

  const handleJoin = (name, room) => {
    socket.emit('join_game', { name, room_id: room })
    setInGame(true)
  }

  if (!inGame || !gameState) {
    return <Login onJoin={handleJoin} />
  }

  return (
    <GameScreen 
      gameState={gameState} 
      mySid={mySid} 
      socket={socket} 
    />
  )
}

export default App
