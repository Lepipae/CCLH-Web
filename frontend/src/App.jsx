import { useEffect, useRef, useState } from 'react'
import { io } from 'socket.io-client'
import Login from './components/Login'
import GameScreen from './components/GameScreen'
import CardWorkshop from './components/CardWorkshop'

const socket = io()

// Avisos de presencia que se muestran en la cabecera. Se guardan los últimos
// pocos porque interesa el orden de llegada y se caen solos tras un rato:
// el estado de verdad NO vive aquí, vive en el `game_update` (cada jugador
// lleva su `presence`), así que perder un aviso es solo no ver una línea.
const MAX_AVISOS = 4
const VIDA_AVISO_MS = 9000

function App() {
  const [inGame, setInGame] = useState(false)
  const [gameState, setGameState] = useState(null)
  const [mySid, setMySid] = useState(null)
  const [view, setView] = useState('login') // 'login' | 'workshop' | 'game'
  // Último join enviado, para poder reentrar solo tras una reconexión.
  const lastJoin = useRef(null)
  // sid -> estado de presencia, rebuilding desde la foto de la sala.
  const [presencia, setPresencia] = useState({})
  // Avisos recientes de la máquina de dos fases (sospecha, recuperación, purga).
  const [avisos, setAvisos] = useState([])
  // Si el aviso va conmigo, el plazo de gracia que me han dado. Se guarda tal
  // cual lo envía el reto: el estado de MI presencia está en la foto, pero el
  // plazo (que es una promesa con fecha) solo llega en el evento.
  const [miGracia, setMiGracia] = useState(null)

  useEffect(() => {
    socket.on('connect', () => {
      setMySid(socket.id)
      // socket.io reconecta solo, y al hacerlo el sid cambia: para el servidor
      // este jugador es uno nuevo. Si no reentramos, el asiento anterior se
      // queda fantasma y el jugador se queda mirando una partida muerta.
      // El backend reconoce el asiento por el mismo nombre y lo recupera.
      if (lastJoin.current) {
        socket.emit('join_game', lastJoin.current)
      }
    })

    socket.on('game_update', (data) => {
      setGameState(data)
      setInGame(true)
      setView('game')
      // El estado de presencia se reconstruye desde la foto: es lo que garantiza
      // que el aviso de "fulano está sospechoso" no se queda pegado si a ese
      // jugador se le ve durante un `game_update` en el que ya ha vuelto.
      const siguiente = {}
      for (const p of data.players || []) {
        if (p.presence) siguiente[p.id] = { ...p.presence, name: p.name }
      }
      setPresencia(siguiente)
    })

    socket.on('join_error', (data) => {
      alert(data.message)
      setInGame(false)
      setView('login')
    })

    // El servidor emite 'heartbeat_ping' cada ROOM_REAP_INTERVAL s y espera
    // esta respuesta como prueba de que hay alguien leyendo al otro lado. Sin
    // ella, un socket que se cae sin 'disconnect' (típico detrás de ngrok)
    // deja el asiento ocupado para siempre.
    socket.on('heartbeat_ping', () => {
      socket.emit('heartbeat_pong')
    })

    // --- Presencia en dos fases (vivo / sospechoso / purgado) -------------
    // `presence_alert` es el aviso a la mesa: entra alguien en periodo de
    // gracia, vuelve, o se libera su asiento.
    //
    // Además de pintar la línea de aviso, ACTUALIZA el mapa de presencia. La
    // foto de la sala también lo trae, pero la foto solo llega cuando cambia
    // algo del juego, y una sospecha no cambia nada del juego: sin esto, en un
    // lobby en calma el ⚠️ no aparecería hasta que alguien jugara una carta.
    socket.on('presence_alert', (data) => {
      setPresencia(prev => {
        if (data.state === 'purgado') {
          // Se va de la sala: fuera del mapa, que la foto ya no listará.
          const copia = { ...prev }
          delete copia[data.sid]
          return copia
        }
        return {
          ...prev,
          [data.sid]: {
            state: data.state,
            since: data.since,
            deadline: data.deadline,
            grace: data.grace,
            left: data.left,
            name: data.name,
          },
        }
      })
      setAvisos(prev => [
        { ...data, id: `${data.sid}-${data.state}-${Date.now()}` },
        ...prev,
      ].slice(0, MAX_AVISOS))
      if (data.state === 'vivo') setMiGracia(null)
      // Si el liberado soy YO, el servidor ya me ha quitado el asiento: la        // partida que se está pintando ya no es la mía y todos sus botones van a
        // ser rechazados. Sin esto, el cliente se queda mirando una mesa muerta sin
      // salida, porque no hay ningún botón de "salir" en la partida. Se vuelve
      // al login, que es donde se puede volver a entrar.
      //
      // Se compara con `socket.id` y no con el estado `mySid` porque este
      // listener se registra una vez (deps []) y cualquier `mySid` capturado
      // aquí se quedaría en su valor inicial para siempre.
      if (data.state === 'purgado' && data.sid === socket.id) {
        alert(data.name
          ? `Se ha liberado tu asiento (${data.name} dejó de responder). Vuelve a entrar para seguir jugando.`
          : 'Se ha liberado tu asiento. Vuelve a entrar para seguir jugando.')
        lastJoin.current = null
        setInGame(false)
        setView('login')
      }
    })

    // `presence_challenge` es el reto explícito: "si estás ahí, contesta". Se
    // contesta con el mismo pong de siempre, pero aquí con una intención clara
    // (es lo que devuelve el asiento dentro del periodo de gracia), así que
    // además se guarda el plazo para poder mostrar la cuenta atrás.
    socket.on('presence_challenge', (data) => {
      socket.emit('heartbeat_pong')
      setMiGracia({ grace: data.grace, left: data.left })
    })

    return () => {
      socket.off('connect')
      socket.off('game_update')
      socket.off('join_error')
      socket.off('heartbeat_ping')
      socket.off('presence_alert')
      socket.off('presence_challenge')
    }
  }, [])

  // Los avisos se caen solos: sin esto, la cabecera acumularía líneas de hace
  // media hora de gente que ya se fue.
  useEffect(() => {
    if (avisos.length === 0) return
    const t = setTimeout(() => setAvisos(prev => prev.slice(0, -1)), VIDA_AVISO_MS)
    return () => clearTimeout(t)
  }, [avisos])

  const handleJoin = (name, room) => {
    lastJoin.current = { name, room_id: room }
    socket.emit('join_game', lastJoin.current)
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
        presencia={presencia}
        avisos={avisos}
        miGracia={miGracia}
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
