/**
 * Tedi Browser Room — room.js
 *
 * WebSocket, audio playback, VAD, session timer.
 *
 * URL params:
 *   call_id    — required, session identifier
 *   server_url — optional override for WS host (e.g. https://tedi.bonecho.ai)
 *   debug      — set to "1" to show debug log panel
 *
 * WebSocket protocol:
 *   Browser → Server: ready | barge_in | playback_finished
 *   Server → Browser: thinking_start | response_start | audio_chunk |
 *                     response_complete | stop_playback | session_end
 */

'use strict';

// ── URL params ────────────────────────────────────────────
const params    = new URLSearchParams(window.location.search);
const callId    = params.get('call_id');
const serverUrl = params.get('server_url');

if (params.get('debug') === '1') {
  document.body.classList.add('debug');
}

// ── DOM refs ──────────────────────────────────────────────
const orbEl      = document.getElementById('orb');
const orbGlowEl  = document.getElementById('orb-glow');
const statusEl   = document.getElementById('status');
const timerEl    = document.getElementById('session-timer');
const roomUiEl   = document.getElementById('room-ui');
const endScreenEl= document.getElementById('end-screen');
const micDeniedEl= document.getElementById('mic-denied');
const logEl      = document.getElementById('debug-log');

// ── Logging ───────────────────────────────────────────────
function log(msg) {
  const ts   = new Date().toISOString().slice(11, 23);
  const line = `[${ts}] ${msg}`;
  console.log(line);
  if (logEl) {
    logEl.textContent += line + '\n';
    logEl.scrollTop    = logEl.scrollHeight;
  }
}

// ── State ─────────────────────────────────────────────────
let ws              = null;
let audioContext    = null;
let isPlaying       = false;
let currentRequestId= null;
let mp3Chunks       = [];
let activeSource    = null;
let vadStream       = null;
let vadAnalyser     = null;
let vadInterval     = null;
let sessionEnded    = false;
let reconnectCount  = 0;
const MAX_RECONNECTS = 3;
const RECONNECT_DELAY= 2000;

const VAD_THRESHOLD  = 0.015;
const VAD_POLL_MS    = 50;
const VAD_DEBOUNCE_MS= 300;
let lastSpeechTime   = 0;

// ── Session timer ─────────────────────────────────────────
let timerInterval = null;
let timerSeconds  = 0;

function startTimer() {
  if (timerInterval) return;
  timerInterval = setInterval(() => {
    timerSeconds++;
    const m = String(Math.floor(timerSeconds / 60)).padStart(2, '0');
    const s = String(timerSeconds % 60).padStart(2, '0');
    timerEl.textContent = `${m}:${s}`;
  }, 1000);
}

function stopTimer() {
  clearInterval(timerInterval);
  timerInterval = null;
}

// ── Orb / status helpers ──────────────────────────────────
function setStatus(msg, mode) {
  statusEl.textContent = msg;
  statusEl.classList.toggle('thinking-dots', mode === 'thinking');
  orbEl.className = `orb ${mode || 'listening'}`;
  orbGlowEl.className = `orb-glow ${mode || 'listening'}`;
}

// ── End screen ────────────────────────────────────────────
function showEndScreen() {
  if (sessionEnded) return;
  sessionEnded = true;
  stopTimer();
  stopPlayback();

  log('Session ended — showing end screen');

  // Fade out room UI, show end screen
  roomUiEl.classList.add('hidden');
  timerEl.classList.add('hidden');
  endScreenEl.classList.remove('hidden');
}

// ── WebSocket ─────────────────────────────────────────────
function buildWsUrl() {
  if (serverUrl) {
    const url      = new URL(serverUrl);
    const protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${url.host}/ws/bot/${callId}`;
  }
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/ws/bot/${callId}`;
}

function connectWebSocket() {
  if (sessionEnded) return;

  const wsUrl = buildWsUrl();
  log(`Connecting to ${wsUrl} (attempt ${reconnectCount + 1})`);

  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    log('WebSocket connected');
    reconnectCount = 0;
    startTimer();
    setStatus('Listening...', 'listening');
    ws.send(JSON.stringify({ type: 'ready' }));
  };

  ws.onmessage = (event) => {
    try {
      handleServerMessage(JSON.parse(event.data));
    } catch (e) {
      log(`WS parse error: ${e.message}`);
    }
  };

  ws.onclose = (event) => {
    log(`WebSocket closed: ${event.code} ${event.reason}`);
    if (sessionEnded) return;

    if (event.code === 1000) {
      // Normal closure — treat as session end
      showEndScreen();
      return;
    }

    if (reconnectCount < MAX_RECONNECTS) {
      reconnectCount++;
      setStatus('Reconnecting...', 'listening');
      setTimeout(connectWebSocket, RECONNECT_DELAY);
    } else {
      log('Max reconnects reached — showing end screen');
      showEndScreen();
    }
  };

  ws.onerror = () => {
    log('WebSocket error');
  };
}

// ── Server message handler ────────────────────────────────
function handleServerMessage(msg) {
  switch (msg.type) {
    case 'thinking_start':
      log('Tedi is thinking...');
      setStatus('Thinking', 'thinking');
      break;

    case 'response_start':
      log(`Response start: "${(msg.spoken_text || '').slice(0, 80)}"`);
      currentRequestId = msg.request_id;
      mp3Chunks        = [];
      setStatus('Speaking...', 'speaking');
      break;

    case 'audio_chunk':
      if (msg.request_id === currentRequestId) {
        const bytes = Uint8Array.from(atob(msg.audio_base64), c => c.charCodeAt(0));
        mp3Chunks.push(bytes);
      }
      break;

    case 'response_complete':
      log(`Response complete: ${msg.request_id}`);
      playBufferedAudio();
      break;

    case 'stop_playback':
      log('Server requested stop');
      stopPlayback();
      break;

    case 'session_end':
      log('Server sent session_end');
      showEndScreen();
      break;

    default:
      log(`Unknown message type: ${msg.type}`);
  }
}

// ── Audio ─────────────────────────────────────────────────
async function initAudio() {
  if (audioContext) return;
  audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 44100 });
  log(`AudioContext created (state: ${audioContext.state})`);
  if (audioContext.state === 'suspended') {
    await audioContext.resume();
    log('AudioContext resumed');
  }
}

async function playBufferedAudio() {
  if (!audioContext) await initAudio();

  if (mp3Chunks.length === 0) {
    finishPlayback();
    return;
  }

  const totalLength = mp3Chunks.reduce((s, c) => s + c.length, 0);
  const combined    = new Uint8Array(totalLength);
  let offset        = 0;
  for (const chunk of mp3Chunks) {
    combined.set(chunk, offset);
    offset += chunk.length;
  }
  mp3Chunks = [];

  log(`Decoding ${totalLength} bytes of MP3`);

  try {
    const audioBuffer = await audioContext.decodeAudioData(combined.buffer);
    isPlaying = true;

    activeSource          = audioContext.createBufferSource();
    activeSource.buffer   = audioBuffer;
    activeSource.connect(audioContext.destination);
    activeSource.onended  = () => {
      activeSource = null;
      finishPlayback();
    };
    activeSource.start(0);
    log(`Playing ${audioBuffer.duration.toFixed(2)}s`);
  } catch (e) {
    log(`Audio decode error: ${e.message}`);
    finishPlayback();
  }
}

function finishPlayback() {
  isPlaying = false;
  setStatus('Listening...', 'listening');
  if (ws && ws.readyState === WebSocket.OPEN && currentRequestId) {
    ws.send(JSON.stringify({ type: 'playback_finished', request_id: currentRequestId }));
  }
}

function stopPlayback() {
  if (activeSource) {
    try {
      activeSource.onended = null;
      activeSource.stop();
    } catch (_) { /* already stopped */ }
    activeSource = null;
  }
  mp3Chunks        = [];
  isPlaying        = false;
  currentRequestId = null;
  if (!sessionEnded) setStatus('Listening...', 'listening');
  log('Playback stopped');
}

// ── VAD (Voice Activity Detection) ───────────────────────
async function initVAD() {
  try {
    vadStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    log('Microphone access granted');

    if (!audioContext) await initAudio();

    const source  = audioContext.createMediaStreamSource(vadStream);
    vadAnalyser   = audioContext.createAnalyser();
    vadAnalyser.fftSize = 2048;
    source.connect(vadAnalyser);

    const dataArray = new Float32Array(vadAnalyser.fftSize);

    vadInterval = setInterval(() => {
      vadAnalyser.getFloatTimeDomainData(dataArray);

      let sum = 0;
      for (let i = 0; i < dataArray.length; i++) sum += dataArray[i] * dataArray[i];
      const rms = Math.sqrt(sum / dataArray.length);

      if (rms > VAD_THRESHOLD) {
        const now = Date.now();
        if (isPlaying && (now - lastSpeechTime) > VAD_DEBOUNCE_MS) {
          log(`BARGE-IN (RMS: ${rms.toFixed(4)})`);
          stopPlayback();
          if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'barge_in' }));
          }
        }
        lastSpeechTime = now;
      }
    }, VAD_POLL_MS);

    log('VAD initialized');
  } catch (e) {
    log(`VAD/mic error: ${e.message}`);
    if (e.name === 'NotAllowedError' || e.name === 'PermissionDeniedError') {
      micDeniedEl.classList.remove('hidden');
    }
  }
}

// ── Bootstrap ─────────────────────────────────────────────
async function main() {
  if (!callId) {
    log('ERROR: No call_id in URL');
    setStatus('Error — no session ID', 'listening');
    return;
  }

  log(`call_id: ${callId}`);
  if (serverUrl) log(`server_url: ${serverUrl}`);

  setStatus('Connecting...', 'listening');

  // Connect WebSocket first — don't let audio/VAD failures block it
  connectWebSocket();

  try {
    await initAudio();
  } catch (e) {
    log(`Audio init (non-fatal): ${e.message}`);
  }

  initVAD().catch(e => log(`VAD init (non-fatal): ${e.message}`));
}

main().catch(e => log(`Fatal: ${e.message}`));
