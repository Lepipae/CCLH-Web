// Web Audio API Sound Synthesizer for Card Game Effects
class SoundManager {
  constructor() {
    this.ctx = null
    this.muted = false
    try {
      this.muted = localStorage.getItem('cch_muted') === 'true'
    } catch (e) {}
  }

  init() {
    if (!this.ctx) {
      const AudioContext = window.AudioContext || window.webkitAudioContext
      if (AudioContext) {
        this.ctx = new AudioContext()
      }
    }
    if (this.ctx && this.ctx.state === 'suspended') {
      this.ctx.resume()
    }
  }

  toggleMute() {
    this.muted = !this.muted
    try {
      localStorage.setItem('cch_muted', this.muted)
    } catch (e) {}
    return this.muted
  }

  isMuted() {
    return this.muted
  }

  // 1. Play Card (Paper slide and snap on table)
  playCard() {
    if (this.muted) return
    this.init()
    if (!this.ctx) return

    const now = this.ctx.currentTime

    // White noise for card paper slide
    const bufferSize = this.ctx.sampleRate * 0.08
    const buffer = this.ctx.createBuffer(1, bufferSize, this.ctx.sampleRate)
    const data = buffer.getChannelData(0)
    for (let i = 0; i < bufferSize; i++) {
      data[i] = (Math.random() * 2 - 1) * Math.exp(-i / (bufferSize * 0.3))
    }

    const noise = this.ctx.createBufferSource()
    noise.buffer = buffer

    const filter = this.ctx.createBiquadFilter()
    filter.type = 'lowpass'
    filter.frequency.setValueAtTime(1200, now)
    filter.frequency.exponentialRampToValueAtTime(300, now + 0.08)

    const gain = this.ctx.createGain()
    gain.gain.setValueAtTime(0.35, now)
    gain.gain.exponentialRampToValueAtTime(0.01, now + 0.08)

    noise.connect(filter)
    filter.connect(gain)
    gain.connect(this.ctx.destination)

    noise.start(now)

    // Snap impact tone
    const osc = this.ctx.createOscillator()
    const oscGain = this.ctx.createGain()

    osc.type = 'triangle'
    osc.frequency.setValueAtTime(180, now)
    osc.frequency.exponentialRampToValueAtTime(60, now + 0.05)

    oscGain.gain.setValueAtTime(0.25, now)
    oscGain.gain.exponentialRampToValueAtTime(0.01, now + 0.05)

    osc.connect(oscGain)
    oscGain.connect(this.ctx.destination)

    osc.start(now)
    osc.stop(now + 0.06)
  }

  // 2. Select Card (Soft tap in hand)
  selectCard() {
    if (this.muted) return
    this.init()
    if (!this.ctx) return

    const now = this.ctx.currentTime
    const osc = this.ctx.createOscillator()
    const gain = this.ctx.createGain()

    osc.type = 'sine'
    osc.frequency.setValueAtTime(450, now)
    osc.frequency.exponentialRampToValueAtTime(900, now + 0.035)

    gain.gain.setValueAtTime(0.2, now)
    gain.gain.exponentialRampToValueAtTime(0.001, now + 0.035)

    osc.connect(gain)
    gain.connect(this.ctx.destination)

    osc.start(now)
    osc.stop(now + 0.04)
  }

  // 3. Reveal Card (Card flip swoosh + snap)
  revealCard() {
    if (this.muted) return
    this.init()
    if (!this.ctx) return

    const now = this.ctx.currentTime

    // Swoosh noise
    const bufferSize = this.ctx.sampleRate * 0.12
    const buffer = this.ctx.createBuffer(1, bufferSize, this.ctx.sampleRate)
    const data = buffer.getChannelData(0)
    for (let i = 0; i < bufferSize; i++) {
      data[i] = (Math.random() * 2 - 1)
    }

    const noise = this.ctx.createBufferSource()
    noise.buffer = buffer

    const filter = this.ctx.createBiquadFilter()
    filter.type = 'bandpass'
    filter.frequency.setValueAtTime(350, now)
    filter.frequency.exponentialRampToValueAtTime(1600, now + 0.07)
    filter.frequency.exponentialRampToValueAtTime(350, now + 0.12)
    filter.Q.value = 2.5

    const gain = this.ctx.createGain()
    gain.gain.setValueAtTime(0.01, now)
    gain.gain.linearRampToValueAtTime(0.25, now + 0.06)
    gain.gain.exponentialRampToValueAtTime(0.01, now + 0.12)

    noise.connect(filter)
    filter.connect(gain)
    gain.connect(this.ctx.destination)

    noise.start(now)

    // Flip snap tone
    const osc = this.ctx.createOscillator()
    const oscGain = this.ctx.createGain()
    osc.type = 'sine'
    osc.frequency.setValueAtTime(260, now + 0.06)
    osc.frequency.exponentialRampToValueAtTime(95, now + 0.1)

    oscGain.gain.setValueAtTime(0.2, now + 0.06)
    oscGain.gain.exponentialRampToValueAtTime(0.001, now + 0.11)

    osc.connect(oscGain)
    oscGain.connect(this.ctx.destination)

    osc.start(now + 0.06)
    osc.stop(now + 0.12)
  }

  // 4. Win Round (Victory Fanfare / Gold Chime)
  winRound() {
    if (this.muted) return
    this.init()
    if (!this.ctx) return

    const now = this.ctx.currentTime
    const notes = [523.25, 659.25, 783.99, 1046.50] // C5, E5, G5, C6 (Major chord)

    notes.forEach((freq, idx) => {
      const startTime = now + idx * 0.07
      const osc = this.ctx.createOscillator()
      const gain = this.ctx.createGain()

      osc.type = 'triangle'
      osc.frequency.setValueAtTime(freq, startTime)

      gain.gain.setValueAtTime(0.22, startTime)
      gain.gain.exponentialRampToValueAtTime(0.001, startTime + 0.45)

      osc.connect(gain)
      gain.connect(this.ctx.destination)

      osc.start(startTime)
      osc.stop(startTime + 0.5)
    })
  }

  // 5. Vote Card (Heart popping chime)
  voteCard() {
    if (this.muted) return
    this.init()
    if (!this.ctx) return

    const now = this.ctx.currentTime
    const osc = this.ctx.createOscillator()
    const gain = this.ctx.createGain()

    osc.type = 'sine'
    osc.frequency.setValueAtTime(587.33, now) // D5
    osc.frequency.exponentialRampToValueAtTime(880, now + 0.08) // A5

    gain.gain.setValueAtTime(0.2, now)
    gain.gain.exponentialRampToValueAtTime(0.001, now + 0.15)

    osc.connect(gain)
    gain.connect(this.ctx.destination)

    osc.start(now)
    osc.stop(now + 0.16)
  }
}

export const soundManager = new SoundManager()
