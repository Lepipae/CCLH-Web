const socket = io(); // Conecta dinámicamente al host actual

let mi_sid = null;
let current_room = null;
let current_pick = 1;
let selected_cards = [];

// Elementos DOM
const loginScreen = document.getElementById('login-screen');
const gameScreen = document.getElementById('game-screen');
const joinBtn = document.getElementById('join-btn');
const startBtn = document.getElementById('start-btn');
const renewBtn = document.getElementById('renew-btn');
const inviteBtn = document.getElementById('invite-btn');
const changeBlackBtn = document.getElementById('change-black-btn');
const usernameInput = document.getElementById('username');
const roomIdInput = document.getElementById('room_id');
const roomCustomCards = document.getElementById('room-custom-cards');
const customCardsInput = document.getElementById('custom-cards-input');
const addCustomCardsBtn = document.getElementById('add-custom-cards-btn');
const chatMessages = document.getElementById('chat-messages');
const chatInput = document.getElementById('chat-input');
const chatSendBtn = document.getElementById('chat-send-btn');

const displayRoom = document.getElementById('display-room');
const playerName = document.getElementById('player-name');
const playerPoints = document.getElementById('player-points');
const leaderboard = document.getElementById('leaderboard');
const statusBar = document.getElementById('status-bar');

const blackCardSlot = document.getElementById('black-card-slot');
const blackCardText = blackCardSlot.querySelector('.card-text');
const playedCardsArea = document.getElementById('played-cards-area');
const playerHandArea = document.getElementById('player-hand');

socket.on('connect', () => {
    mi_sid = socket.id;
});

// Auto-fill room if in URL
window.addEventListener('DOMContentLoaded', () => {
    const params = new URLSearchParams(window.location.search);
    const roomParam = params.get('room');
    if (roomParam) {
        roomIdInput.value = roomParam;
        usernameInput.focus();
    }
});

joinBtn.addEventListener('click', () => {
    const name = usernameInput.value.trim();
    const room = roomIdInput.value.trim().toUpperCase();
    if (name && room) {
        socket.emit('join_game', { name: name, room_id: room });
        current_room = room;
        playerName.textContent = name;
        displayRoom.textContent = room;
        
        loginScreen.classList.remove('active');
        gameScreen.classList.add('active');
    }
});

startBtn.addEventListener('click', () => {
    socket.emit('start_game', { room_id: current_room });
});

renewBtn.addEventListener('click', () => {
    socket.emit('vote_renew', { room_id: current_room });
});

inviteBtn.addEventListener('click', () => {
    const url = window.location.origin + window.location.pathname + '?room=' + current_room;
    navigator.clipboard.writeText(url).then(() => {
        const originalText = inviteBtn.textContent;
        inviteBtn.textContent = '¡Enlace Copiado!';
        setTimeout(() => {
            inviteBtn.textContent = originalText;
        }, 2000);
    });
});

changeBlackBtn.addEventListener('click', () => {
    socket.emit('change_black_card', { room_id: current_room });
});

addCustomCardsBtn.addEventListener('click', () => {
    const val = customCardsInput.value.trim();
    if (val) {
        socket.emit('add_room_cards', { room_id: current_room, cards: val });
        customCardsInput.value = '';
    }
});

function sendChatMessage() {
    const msg = chatInput.value.trim();
    if (msg && current_room) {
        socket.emit('send_chat', { room_id: current_room, msg: msg });
        chatInput.value = '';
    }
}

chatSendBtn.addEventListener('click', sendChatMessage);
chatInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') sendChatMessage();
});

socket.on('chat_message', (data) => {
    const div = document.createElement('div');
    div.className = 'chat-message';
    if (data.system) {
        div.classList.add('system');
        div.textContent = data.msg;
    } else {
        // Escaping HTML
        const safeMsg = document.createElement('div');
        safeMsg.textContent = data.msg;
        const safeSender = document.createElement('div');
        safeSender.textContent = data.sender;
        
        div.innerHTML = `<strong>${safeSender.innerHTML}:</strong> ${safeMsg.innerHTML}`;
    }
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
});

socket.on('game_update', (data) => {
    console.log("Game update:", data);
    
    // Actualizar Leaderboard
    leaderboard.innerHTML = '';
    let is_my_turn_to_judge = data.czar === mi_sid;
    let i_am_czar_and_waiting = false;
    
    data.players.forEach(p => {
        const li = document.createElement('li');
        li.className = 'player-item';
        
        let status = '';
        if (p.is_czar) {
            status = ' 👑 (Juez)';
        } else if (p.has_played) {
            status = ' ✅ (Jugó)';
        } else if (data.state === 'playing') {
            status = ' ⏳ (Pensando...)';
        }
        
        li.textContent = `${p.name} - ${p.points} pts${status}`;
        if (p.id === mi_sid) {
            li.style.fontWeight = 'bold';
            playerPoints.textContent = p.points;
        }
        leaderboard.appendChild(li);
    });

    // Actualizar controles y barra de estado
    startBtn.classList.add('hidden');
    
    if (data.state !== 'waiting') {
        renewBtn.classList.remove('hidden');
        const threshold = Math.ceil(data.total_active * 0.75);
        renewBtn.textContent = `Renovar Cartas (${data.renew_votes}/${threshold})`;
        if (data.has_voted_renew) {
            renewBtn.disabled = true;
            renewBtn.style.opacity = '0.5';
            renewBtn.textContent = `Votado (${data.renew_votes}/${threshold})`;
        } else {
            renewBtn.disabled = false;
            renewBtn.style.opacity = '1';
            renewBtn.textContent = `Renovar Cartas (${data.renew_votes}/${threshold})`;
        }
    } else {
        renewBtn.classList.add('hidden');
    }
    
    if (data.state === 'waiting') {
        roomCustomCards.classList.remove('hidden');
        if (data.players.length < 2) {
            statusBar.textContent = "Esperando a que se unan más jugadores...";
        } else {
            statusBar.textContent = "Listos para empezar.";
        }
        
        if (data.leader === mi_sid && data.players.length >= 2) {
            startBtn.classList.remove('hidden');
            statusBar.textContent = "Listos para empezar. ¡Haz clic en Iniciar Partida!";
        } else if (data.leader !== mi_sid && data.players.length >= 2) {
            statusBar.textContent = "Esperando a que el líder inicie la partida...";
        }
        
        blackCardSlot.classList.add('hidden');
        playedCardsArea.classList.add('hidden');
        changeBlackBtn.classList.add('hidden');
    }
    else if (data.state === 'playing') {
        roomCustomCards.classList.add('hidden');
        blackCardSlot.classList.remove('hidden');
        if (typeof data.black_card === 'string') {
            blackCardText.innerHTML = data.black_card.replace(/_/g, '______');
            current_pick = 1;
        } else if (data.black_card) {
            blackCardText.innerHTML = data.black_card.text.replace(/_/g, '______');
            current_pick = data.black_card.pick || 1;
        }
        
        if (current_pick > 1) {
            blackCardText.innerHTML += `<div style="margin-top: 15px; font-weight: bold; color: var(--accent); font-size: 0.9rem;">ELIGE ${current_pick} CARTAS</div>`;
        }
        
        // Renderizar cartas boca abajo de los que han jugado
        playedCardsArea.innerHTML = '';
        playedCardsArea.classList.remove('hidden');
        
        const playedCount = data.players.filter(p => p.has_played && !p.is_czar).length;
        for (let i = 0; i < playedCount; i++) {
            const stack = document.createElement('div');
            stack.className = 'card-stack face-down';
            for (let j = 0; j < current_pick; j++) {
                const card = document.createElement('div');
                card.className = 'card face-down';
                stack.appendChild(card);
            }
            playedCardsArea.appendChild(stack);
        }
        
        if (is_my_turn_to_judge) {
            statusBar.textContent = "Eres el Juez 👑. Espera a que los demás jueguen sus cartas.";
            i_am_czar_and_waiting = true;
            changeBlackBtn.classList.remove('hidden');
        } else {
            changeBlackBtn.classList.add('hidden');
            const me = data.players.find(p => p.id === mi_sid);
            if (me && me.has_played) {
                statusBar.textContent = "Has jugado tus cartas. Esperando a los demás...";
            } else {
                statusBar.textContent = "Es tu turno. Elige cartas para jugar.";
            }
        }
    }
    else if (data.state === 'judging') {
        blackCardSlot.classList.remove('hidden');
        playedCardsArea.classList.remove('hidden');
        changeBlackBtn.classList.add('hidden');
        
        if (typeof data.black_card === 'string') {
            blackCardText.innerHTML = data.black_card.replace(/_/g, '______');
            current_pick = 1;
        } else if (data.black_card) {
            blackCardText.innerHTML = data.black_card.text.replace(/_/g, '______');
            current_pick = data.black_card.pick || 1;
        }
        
        if (is_my_turn_to_judge) {
            statusBar.textContent = "Eres el Juez 👑. Haz clic en las cartas para darles la vuelta.";
        } else {
            statusBar.textContent = "El Juez está eligiendo al ganador...";
        }
        
        renderPlayedCards(data.played_cards, is_my_turn_to_judge, data.state);
    }
    else if (data.state === 'round_end') {
        blackCardSlot.classList.remove('hidden');
        playedCardsArea.classList.remove('hidden');
        changeBlackBtn.classList.add('hidden');
        
        if (typeof data.black_card === 'string') {
            blackCardText.innerHTML = data.black_card.replace(/_/g, '______');
        } else if (data.black_card) {
            blackCardText.innerHTML = data.black_card.text.replace(/_/g, '______');
        }
        
        const winner = data.players.find(p => p.id === data.winner_sid);
        const winnerName = winner ? winner.name : "Alguien";
        statusBar.innerHTML = `¡🌟 <strong>${winnerName}</strong> ha ganado la ronda! 🌟<br>Siguiente ronda en 5 segundos...`;
        
        renderPlayedCards(data.played_cards, false, data.state, data.players, data.winner_sid);
    }

    // Limpiar selección al final de ronda o al actualizar mano si no hay jugada
    const me = data.players.find(p => p.id === mi_sid);
    if (!me || !me.has_played || data.state !== 'playing') {
        selected_cards = [];
    }

    // Renderizar mano del jugador
    renderHand(data.hand, data.state, is_my_turn_to_judge, data.players);
});



function renderHand(hand, state, is_czar, players) {
    playerHandArea.innerHTML = '';
    const me = players.find(p => p.id === mi_sid);
    const can_play = state === 'playing' && !is_czar && me && !me.has_played;
    
    hand.forEach((cardText, index) => {
        const card = document.createElement('div');
        card.className = 'card white';
        
        if (selected_cards.includes(index)) {
            card.classList.add('selected');
            card.setAttribute('data-selection-order', selected_cards.indexOf(index) + 1);
        }
        
        if (can_play) {
            card.classList.add('playable');
            card.addEventListener('click', (e) => {
                if (card.classList.contains('flying') || card.classList.contains('selected')) return;
                
                if (current_pick === 1) {
                    flyCard(card);
                    socket.emit('play_card', { room_id: current_room, card_index: index });
                } else {
                    selected_cards.push(index);
                    card.classList.add('selected');
                    card.setAttribute('data-selection-order', selected_cards.length);
                    
                    if (selected_cards.length === current_pick) {
                        const selectedElements = Array.from(playerHandArea.children).filter(c => c.classList.contains('selected'));
                        selectedElements.forEach(c => flyCard(c));
                        socket.emit('play_card', { room_id: current_room, card_index: [...selected_cards] });
                        selected_cards = [];
                    }
                }
            });
        } else {
            card.style.opacity = '0.7';
            card.style.cursor = 'not-allowed';
        }
        
        card.innerHTML = `
            <div class="card-text">${cardText}</div>
            <div class="card-footer">Cartas Contra Miedo y Hambre</div>
        `;
        playerHandArea.appendChild(card);
    });
}

function flyCard(cardElement) {
    cardElement.classList.add('flying');
    const rect = cardElement.getBoundingClientRect();
    cardElement.style.opacity = '0';
    
    const clone = cardElement.cloneNode(true);
    clone.className = 'card white card-flying';
    clone.style.left = rect.left + 'px';
    clone.style.top = rect.top + 'px';
    clone.style.width = rect.width + 'px';
    clone.style.height = rect.height + 'px';
    clone.style.opacity = '1';
    
    // Remove pseudo-element numbering on clone
    clone.removeAttribute('data-selection-order');
    clone.classList.remove('selected');
    
    document.body.appendChild(clone);
    
    let targetX, targetY;
    const areaRect = playedCardsArea.getBoundingClientRect();
    clone.getBoundingClientRect(); // force reflow
    
    targetX = areaRect.left + (areaRect.width / 2) - (rect.width / 2);
    targetY = areaRect.top + (areaRect.height / 2) - (rect.height / 2);
    
    clone.style.transform = `translate(${targetX - rect.left}px, ${targetY - rect.top}px) scale(0.8) rotateY(180deg)`;
    clone.style.opacity = '0';
    
    setTimeout(() => {
        if (clone.parentNode) {
            clone.parentNode.removeChild(clone);
        }
    }, 600);
}

function renderPlayedCards(playedCards, is_czar, state, players_list = null, winner_sid = null) {
    playedCardsArea.innerHTML = '';
    playedCards.forEach((c) => {
        if (!c.revealed && state !== 'round_end') {
            const stack = document.createElement('div');
            stack.className = 'card-stack face-down';
            const count = (c.cards && c.cards.length > 0) ? c.cards.length : current_pick;
            for (let i = 0; i < count; i++) {
                const card = document.createElement('div');
                card.className = 'card face-down';
                stack.appendChild(card);
            }
            if (is_czar && state === 'judging') {
                stack.addEventListener('click', () => {
                    socket.emit('reveal_card', { room_id: current_room, sub_id: c.id });
                });
            }
            playedCardsArea.appendChild(stack);
        } else {
            const stack = document.createElement('div');
            stack.className = 'card-stack';
            
            c.cards.forEach(text => {
                const card = document.createElement('div');
                card.className = 'card white played';
                if (is_czar && state === 'judging') {
                    card.classList.add('playable');
                }
                
                let authorHtml = '';
                if (state === 'round_end' && c.sid && players_list) {
                    const author = players_list.find(p => p.id === c.sid);
                    if (author) {
                        authorHtml = `<div class="card-author">Jugada por: ${author.name}</div>`;
                    }
                    if (c.sid === winner_sid) {
                        card.classList.add('winner-animation');
                    } else {
                        card.classList.add('loser-fade');
                    }
                }
                
                card.innerHTML = `<div class="card-text">${text}</div>${authorHtml}<div class="card-footer">Cartas Contra Miedo y Hambre</div>`;
                stack.appendChild(card);
            });
            
            if (is_czar && state === 'judging') {
                stack.addEventListener('click', () => {
                    socket.emit('choose_winner', { room_id: current_room, sub_id: c.id });
                });
            }
            
            playedCardsArea.appendChild(stack);
        }
    });
}
