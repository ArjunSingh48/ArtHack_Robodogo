// The real Go2, when the page is opened with ?robot=ws://127.0.0.1:8770 and `itu web-link` runs on this laptop
// (python/README → the real robot). Without the parameter nothing here does anything and the show is simulated only.
// The relay passes everything through the Python MotionGuard: calm poses only, one at a time. WALKING is decided by
// the Python backend, not here: the page asks for `approach {side, posture, facing}`, the robot's own camera finds the
// real participant, and the page only draws the state that comes back (robot.live). Every walk ends with
// Move 0 → stop → stop_move; the relay stops the dog if this page goes away.
const url = new URLSearchParams(location.search).get('robot');
const badge = document.getElementById('robot');
let ws = null, mode = '', armed = false, line = '', note = '', nextId = 1, walkingInfo = '';
let live = null;  // the backend's last walk_state: {phase, detail, robot: [x, y, yaw], person: [x, y] | null}
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
    if (m.type === 'hello') { mode = m.mode; walkingInfo = m.walking || ''; }
    if (m.type === 'walk_state') { live = m; note = `${m.phase.replace('_', ' ')}${m.detail ? ' — ' + m.detail : ''}`; }
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

const connected = () => !!(ws && ws.readyState === WebSocket.OPEN);

async function movement(m, check) {  // → the relay's final answer; End / a jump on the page cancels the walk
  const p = request(m);
  let finished = false;
  p.then(() => { finished = true; });
  try {
    while (!finished) await check();  // throws when the show is ended or jumps: the walk is cancelled below
  } catch (e) { send({ cmd: 'cancel_walk' }); await p; throw e; }
  return p;
}

export const robot = {
  on: !!url,
  get connected() { return connected(); },
  get live() { return live; },
  get walkingInfo() { return walkingInfo; },
  // the camera-guided walk on the real robot (the page never sends velocities for it)
  approach: (side, posture, facing, check) => movement({ cmd: 'approach', side, posture, facing }, check),
  goHome: (check) => movement({ cmd: 'go_home' }, check),
  cancelWalk: () => { if (url) send({ cmd: 'cancel_walk' }); },
  pose: (name) => (url ? request({ cmd: 'pose', name }) : Promise.resolve({ ok: true })),  // sit, lie_down, greet, stretch, stand
  // open-loop velocity: only the relay's --mock accepts it (the real robot refuses: it walks by its own camera)
  walk: (vx, vy, vyaw) => { if (url && !connected()) return; if (url && mode === 'mock') send({ cmd: 'walk', vx, vy, vyaw }); },
  walkEnd: () => (url ? request({ cmd: 'walk_end' }) : Promise.resolve({ ok: true })),
  stop: () => { if (url) send({ cmd: 'stop' }); },  // software stop, latched until Arm
};
