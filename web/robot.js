// The real Go2, when the page is opened with ?robot=ws://127.0.0.1:8770 and `itu web-link` runs on this laptop
// (python/README → the real robot). Without the parameter nothing here does anything and the show is simulated only.
// The relay passes everything through the Python MotionGuard: calm poses only, one at a time, walking clamped and
// streamed, every walk ended with Move 0 → stop → stop_move; it stops the dog if this page goes away.
const url = new URLSearchParams(location.search).get('robot');
const badge = document.getElementById('robot');
let ws = null, mode = '', armed = false, line = '', note = '', nextId = 1;
const waiting = new Map();  // id → resolve

function show() {
  if (!badge) return;
  const up = ws && ws.readyState === WebSocket.OPEN;
  badge.hidden = !url;
  badge.className = !up ? 'off' : armed ? 'on' : 'warn';
  badge.innerHTML = !up ? `REAL GO2 · not connected (${url}) — is <code>itu web-link</code> running?`
    : `REAL GO2${mode === 'mock' ? ' (mock)' : ''} · ${armed ? 'armed' : line}${note ? ' · ' + note : ''}`
      + (armed ? ' · <b>Esc = stop</b>' : ' <button id="robotarm">Arm (dog checked)</button>');
  const b = document.getElementById('robotarm');
  if (b) b.onclick = () => send({ cmd: 'arm' });
}
function send(m) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(m)); }
function connect() {
  ws = new WebSocket(url);
  ws.onopen = show;
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === 'hello') mode = m.mode;
    if (m.type === 'state') { armed = m.armed; line = m.line; note = m.msg && !m.armed ? m.msg : ''; }
    if (m.type === 'walk_refused') note = 'walking refused: ' + m.msg;
    if (m.type === 'done') {
      if (!m.ok) note = m.msg;
      waiting.get(m.id)?.(m); waiting.delete(m.id);
    }
    show();
  };
  ws.onclose = () => {
    for (const r of waiting.values()) r({ ok: false, msg: 'disconnected' });
    waiting.clear(); show(); setTimeout(connect, 2000);
  };
}
if (url) { connect(); show(); }

function request(m) {  // → the relay's answer; at once when not connected (the simulated show goes on)
  if (!ws || ws.readyState !== WebSocket.OPEN) return Promise.resolve({ ok: false, msg: 'not connected' });
  const id = nextId++;
  return new Promise((res) => { waiting.set(id, res); send({ ...m, id }); });
}

export const robot = {
  on: !!url,
  pose: (name) => (url ? request({ cmd: 'pose', name }) : Promise.resolve({ ok: true })),  // sit, lie_down, greet, stretch, stand
  walk: (vx, vy, vyaw) => { if (url) send({ cmd: 'walk', vx, vy, vyaw }); },  // renew ≥ 2×/s, else the dog stops
  walkEnd: () => (url ? request({ cmd: 'walk_end' }) : Promise.resolve({ ok: true })),
  stop: () => { if (url) send({ cmd: 'stop' }); },  // software stop, latched until Arm
};
