import { useEffect, useState } from 'react'
import { io } from 'socket.io-client'
import Login from './components/Login'
import GameScreen from './components/GameScreen'
import CardWorkshop from './components/CardWorkshop'

const socket = io()

function App() {
  const [inGame, setInGame] = useState(false)
  const [gameState, setGameState] = useState(null)
  const [mySid, setMySid] = useState(null)
  const [view, setView] = useState('login') // 'login' | 'workshop' | 'game'

  useEffect(() => {
    socket.on('connect', () => {
      setMySid(socket.id)
    })

    socket.on('game_update', (data) => {
      setGameState(data)
      setInGame(true)
      setView('game')
    })

    socket.on('join_error', (data) => {
      alert(data.message)
      setInGame(false)
      setView('login')
    })

    return () => {
      socket.off('connect')
      socket.off('game_update')
      socket.off('join_error')
    }
  }, [])

  const handleJoin = (name, room) => {
    socket.emit('join_game', { name, room_id: room })
  }

  if (view === 'workshop') {
    return <CardWorkshop socket={socket} onBack={() => setView('login')} />
  }

  if (view === 'game' && inGame && gameState) {
    return (
      <GameScreen 
        gameState={gameState} 
        mySid={mySid} 
        socket={socket} 
      />
    )
  }

  return (
    <Login 
      onJoin={handleJoin} 
      onOpenWorkshop={() => setView('workshop')} 
    />
  )
}

export default App

