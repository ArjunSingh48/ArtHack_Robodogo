// Is This Us? — the presentation, running entirely in the browser (static site; data from scripts/export_web.py).
// A port of src/is_this_us/performance/director.py (presentation programme) with the simulated dog:
// intro → one round per era (pose → closest picture → the dog walks over and takes the painted dog's pose →
// THEN/NOW) → the rescue finale. Every step has a hash (#prehistory.04) to come back to it.

const $ = (id) => document.getElementById(id);
const show = await (await fetch('data/show.json')).json();
const PERSON = [2.2, 0.45];  // where the participant stands in the simulated room (m; x forward, y left)
const POSE_GROUP = { standing: 'standing', leaning_forward: 'standing', sitting: 'sitting', kneeling: 'kneeling',
  crouching: 'kneeling', reclining: 'lying', lying: 'lying' };
const NEAR = [['sitting', 'kneeling'], ['kneeling', 'lying'], ['sitting', 'lying'], ['kneeling', 'standing']];
const MEME_BEHAVIOR = { standing: 'greet', sitting: 'sit', kneeling: 'stretch', lying: 'lie_down' };
const pick = (a) => a[Math.floor(Math.random() * a.length)];

// ---------------------------------------------------------------- run control
// `gen` changes on End / a jump: every wait checks it, so the old run unwinds at once.
let gen = 0, paused = false, lastSaid = null;
class Stop extends Error {}
const alive = (g) => { if (g !== gen) throw new Stop(); };
const bus = new EventTarget();
function send(cmd) {
  if (cmd === 'pause') { paused = true; audio.pause(); }
  if (cmd === 'resume') { paused = false; if (audio.src && !audio.ended) audio.play().catch(() => {}); }
  if (cmd === 'cancel') return toLobby();
  if (cmd === 'camera') return useCamera(true);
  if (cmd === 'samples') return useSamples(true);
  bus.dispatchEvent(new CustomEvent('cmd', { detail: cmd }));
  controls(state);
}
function nextCmd(accept, ms) {  // → the command, or null on timeout
  return new Promise((res) => {
    const h = (e) => { if (accept.includes(e.detail)) { done(); res(e.detail); } };
    const t = ms ? setTimeout(() => { done(); res(null); }, ms) : null;
    const done = () => { bus.removeEventListener('cmd', h); clearTimeout(t); };
    bus.addEventListener('cmd', h);
  });
}
async function sleep(ms, g) {  // pause-aware
  let left = ms;
  while (left > 0) { await new Promise((r) => setTimeout(r, 100)); alive(g); if (!paused) left -= 100; }
}
async function whilePaused(g) { while (paused) { await new Promise((r) => setTimeout(r, 150)); alive(g); } }

// ---------------------------------------------------------------- operator buttons (only what works now)
let state = 'lobby';
const ASK = {
  readiness: [['ready', '▶ Start the round now'], ['skip', '⏭ Skip this round']],
  rescue_down: [['ready', '✓ Someone is lying down'], ['skip', 'Skip']],
  rescue_up: [['ready', '✓ They are up again']],
  photo: [['next', 'Next ▸']],
};
function controls(st) {
  state = st;
  const b = [...(ASK[st] || [])];
  if (['narration', 'readiness', 'photo'].includes(st)) b.push(['repeat', '↺ Repeat']);
  if (st === 'narration') b.push(['next', 'Next ▸']);
  if (st !== 'lobby') b.push(paused ? ['resume', 'Resume'] : ['pause', 'Pause']);
  if (st !== 'lobby') b.push(source === 'photos' ? ['camera', '📷 Switch to my camera'] : ['samples', '🖼 Switch to sample photos']);
  if (st !== 'lobby') b.push(['cancel', 'End']);
  $('controls').innerHTML = b.map(([k, t]) => `<button data-c="${k}" class="${ASK[st]?.some(([a]) => a === k) ? 'ask' : ''}">${t}</button>`).join('');
}
$('controls').addEventListener('click', (e) => { const c = e.target.dataset?.c; if (c) send(c); });
const msg = (t) => { $('msg').textContent = t || ''; };

// ---------------------------------------------------------------- the screen
const art = (id) => show.art[id];
const label = (a) => `<div class="label"><i>${a.title}</i>${a.artist}${a.date ? ', ' + a.date : ''}<br><span>${a.museum} · ${a.rights}</span></div>`;
function screen(kind, x) {
  const v = $('view');
  if (kind === 'title') v.innerHTML = `<div class="title">Is this us?</div>`;
  else if (kind === 'blank') v.innerHTML = '';
  else if (kind === 'art') { const a = art(x); v.innerHTML = a ? `<div class="figure"><img src="${a.image}" alt="">${label(a)}</div>` : ''; }
  else if (kind === 'memes') v.innerHTML = `<div class="memes">${show.memes.map((m) => `<img src="${art(m).image}" alt="">`).join('')}</div>`;
  else if (kind === 'grid') v.innerHTML = `<div class="consider"><b>remembering…</b><div class="cgrid">${x.map((id) => `<img data-id="${id}" src="${art(id).image}" alt="">`).join('')}</div></div>`;
  else if (kind === 'html') v.innerHTML = x;
}
function showCue(s) {
  if (!s) return;
  if (s === 'title' || s === 'blank' || s === 'memes') screen(s);
  else if (art(s)) screen('art', s);
}
const subtitle = (t) => { $('subtitle').textContent = t || ''; };

// ---------------------------------------------------------------- voice (pre-generated clips)
const audio = new Audio();
async function say(text, url, g) {
  lastSaid = [text, url];
  for (;;) {
    subtitle(text);
    const ended = url ? new Promise((r) => { audio.onended = r; audio.onerror = r; }) : sleep(Math.max(1500, text.split(/\s+/).length * 380), g);
    if (url) { audio.src = url; audio.play().catch(() => {}); }
    const cmd = await Promise.race([ended.then(() => 'done'), nextCmd(['next', 'repeat', 'skip'])]);
    alive(g);
    await whilePaused(g);
    if (cmd === 'repeat') continue;
    audio.pause(); subtitle('');
    return cmd;
  }
}
const line = (sec, key, g) => { const v = pick(show.lines[sec][key]); return say(v.text, v.audio, g); };
const sayAgain = (g) => (lastSaid ? say(lastSaid[0], lastSaid[1], g) : null);

// ---------------------------------------------------------------- where the participant comes from
let source = null, stream = null, sample = 0, reading = { visible: false }, poseMod = null;
const used = new Set();
const video = $('video');
async function useCamera(switching) {
  try {
    stream = stream || await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 }, audio: false });
    video.srcObject = stream; await video.play();
    poseMod = poseMod || await import('./pose.js');
    await poseMod.loadPose();
    source = 'camera'; msg(switching ? 'using your camera' : '');
  } catch (e) {
    msg('no camera (' + (e.name || e) + ') — using the sample photos');
    useSamples();
  }
  controls(state);
}
function useSamples(switching) {
  source = 'photos';
  if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
  if (!switching) { sample = Math.floor(Math.random() * show.samples.length); used.clear(); used.add(sample); }
  controls(state);
}
const sampleImgs = show.samples.map((s) => Object.assign(new Image(), { src: s.image }));
function readNow() {
  if (source === 'photos') {
    const s = show.samples[sample];
    reading = { visible: !!s.points, posture: s.posture, framing: 'ok', points: s.points, skeleton: s.skeleton };
  } else if (source === 'camera' && poseMod) {
    reading = poseMod.read(video, show.swap_lr);
  } else reading = { visible: false };
  return reading;
}
// the camera panel: the (mirrored) camera or the sample photo, with the pose drawn on it
const cv = $('camcanvas'), cx = cv.getContext('2d');
const BONES = [[11, 12], [11, 13], [13, 15], [12, 14], [14, 16], [11, 23], [12, 24], [23, 24], [23, 25], [25, 27], [24, 26], [26, 28]];
function drawCam() {
  const box = $('camview'); cv.width = box.clientWidth; cv.height = box.clientHeight;
  const img = source === 'photos' ? sampleImgs[sample] : source === 'camera' ? video : null;
  const w = img ? (img.videoWidth || img.naturalWidth) : 0, h = img ? (img.videoHeight || img.naturalHeight) : 0;
  cx.fillStyle = '#151417'; cx.fillRect(0, 0, cv.width, cv.height);
  if (!w) return;
  const k = Math.min(cv.width / w, cv.height / h), dw = w * k, dh = h * k, ox = (cv.width - dw) / 2, oy = (cv.height - dh) / 2;
  cx.save();
  if (source === 'camera') { cx.translate(cv.width, 0); cx.scale(-1, 1); }  // like a mirror
  cx.drawImage(img, source === 'camera' ? cv.width - ox - dw : ox, oy, dw, dh);
  cx.restore();
  const p = reading.points;
  if (p) {
    cx.strokeStyle = '#a0d6aa'; cx.lineWidth = 2;
    for (const [a, b] of BONES) if (p[a] && p[b] && p[a][2] > 0.5 && p[b][2] > 0.5) {
      cx.beginPath(); cx.moveTo(ox + p[a][0] * dw, oy + p[a][1] * dh); cx.lineTo(ox + p[b][0] * dw, oy + p[b][1] * dh); cx.stroke();
    }
  }
}
function checks(extra = []) {
  const r = reading, c = [['I can see you', r.visible], ...(r.visible ? [['whole body in the picture', r.framing === 'ok']] : []), ...extra];
  $('checks').innerHTML = c.map(([t, ok]) => `<span class="${ok ? 'ok' : ''}">${ok ? '✓' : '○'} ${t}</span>`).join('');
}
function photo() {  // what the photo camera shows now, as an image URL (stays in this browser)
  const c = document.createElement('canvas'), img = source === 'photos' ? sampleImgs[sample] : video;
  c.width = img.videoWidth || img.naturalWidth; c.height = img.videoHeight || img.naturalHeight;
  const g = c.getContext('2d');
  if (source === 'camera') { g.translate(c.width, 0); g.scale(-1, 1); }
  g.drawImage(img, 0, 0);
  return c.toDataURL('image/jpeg', 0.9);
}
setInterval(() => { if (source) { if (source === 'camera') readNow(); drawCam(); } }, 200);

// ---------------------------------------------------------------- the simulated dog (SimWorld + Approacher, simplified)
const dog = { x: 0, y: 0, yaw: 0, walking: false, action: '', body: 'standing' };
let sim3d = null;
import('./sim3d.js').then((m) => { sim3d = m.start($('sim3d')); }).catch((e) => console.warn('3-D view unavailable', e));
setInterval(() => {
  if (!sim3d) return;
  sim3d.update({ robot: [dog.x, dog.y, dog.yaw], person: PERSON, posture: reading.posture || 'standing', body: dog.body,
    action: dog.action, walking: dog.walking, skeleton: reading.skeleton || null, trail: [] });
  const doing = { Sit: 'sitting down', StandDown: 'lying down', Hello: 'giving a paw', Stretch: 'stretching', StandUp: 'standing up' };
  $('simdoing').textContent = (dog.walking ? 'walking' : doing[dog.action] || dog.body).toUpperCase();
}, 100);
const wrap = (a) => Math.atan2(Math.sin(a), Math.cos(a));
async function turnTo(heading, g) {
  dog.walking = true;
  for (;;) { const e = wrap(heading - dog.yaw); if (Math.abs(e) < 0.03) break; dog.yaw += Math.sign(e) * Math.min(Math.abs(e), 0.05); await sleep(60, g); }
  dog.walking = false;
}
async function walkTo(tx, ty, g) {
  if (Math.hypot(tx - dog.x, ty - dog.y) < 0.05) return;
  await turnTo(Math.atan2(ty - dog.y, tx - dog.x), g);
  dog.walking = true;
  for (;;) {
    const d = Math.hypot(tx - dog.x, ty - dog.y); if (d < 0.02) break;
    const s = Math.min(d, 0.3 * 0.06);  // 0.3 m/s
    dog.yaw = Math.atan2(ty - dog.y, tx - dog.x); dog.x += Math.cos(dog.yaw) * s; dog.y += Math.sin(dog.yaw) * s;
    await sleep(60, g);
  }
  dog.walking = false;
}
const FACING = { away: 0, viewer: Math.PI, left: Math.PI / 2, right: -Math.PI / 2 };
async function standUp(g) {
  if (dog.body === 'standing') return;
  dog.action = dog.body === 'sitting' ? 'RiseSit' : 'StandUp'; await sleep(1500, g); dog.action = ''; dog.body = 'standing';
}
async function goBeside(side, facing, g) {  // like Approacher.approach: next to the person as the audience sees it
  await standUp(g);
  const [px, py] = PERSON, gap = 0.75;
  const goal = side === 'right' ? [px, py - gap] : side === 'front' ? [px - 0.85, py] : [px, py + gap];
  await walkTo(goal[0], goal[1], g);
  await turnTo(facing in FACING ? FACING[facing] : Math.atan2(py - dog.y, px - dog.x), g);
}
async function takePose(b, g) {
  const steps = { sit: ['Sit', 'sitting', 3000], lie_down: ['StandDown', 'lying', 2500], greet: ['Hello', 'standing', 4000],
    stretch: ['Stretch', 'standing', 4000], stand: ['', 'standing', 500] }[b] || ['', 'standing', 500];
  dog.action = steps[0]; await sleep(steps[2], g);
  if (b === 'sit' || b === 'lie_down') { dog.body = steps[1]; dog.action = steps[0]; } else { dog.action = ''; dog.body = 'standing'; }
}
async function goHome(g) { await standUp(g); await walkTo(0, 0, g); await turnTo(0, g); }

// ---------------------------------------------------------------- rounds
function score(a, group) {
  if (!group) return 0;
  if (a.human === group) return 3;
  return NEAR.some(([x, y]) => (x === group && y === a.human) || (y === group && x === a.human)) ? 1.5 : 0;
}
const recent = new Set();
function choosePicture(era, group) {
  const pool = show.eras[era].filter((id) => !recent.has(id));
  const list = pool.length ? pool : show.eras[era];
  const best = Math.max(...list.map((id) => score(art(id), group)));
  return pick(list.filter((id) => score(art(id), group) === best));
}
function fitSample(era) {  // sample photos: the unused person whose pose fits this era's pictures best
  if (used.size >= show.samples.length) used.clear();
  const fit = (i) => Math.max(0, ...show.eras[era].map((id) => score(art(id), POSE_GROUP[show.samples[i].posture]))) - (used.has(i) ? 100 : 0);
  const sc = show.samples.map((_, i) => fit(i)), top = Math.max(...sc);
  sample = pick(sc.map((s, i) => [s, i]).filter(([s]) => s === top).map(([, i]) => i)); used.add(sample);
  readNow();
}
async function waitForPose(g, targets, label) {  // steady pose (or a target posture) held 2 s, or Ready
  let since = null, last = null;
  for (;;) {
    alive(g); await whilePaused(g);
    const r = readNow(), ok = r.visible && (targets ? targets.includes(r.posture) : !!r.posture);
    if (ok && r.posture === last) since = since ?? Date.now(); else since = ok ? Date.now() : null;
    last = r.posture;
    const held = since !== null && Date.now() - since > 2000;
    checks([[label, held]]);
    if (held) return r;
    const c = await nextCmd(['ready', 'skip', 'repeat'], 250);
    if (c === 'ready') return readNow();
    if (c === 'skip') return null;
    if (c === 'repeat') await sayAgain(g);
  }
}
function card(a, img, heading) {
  const when = new Date().toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric' });
  return `<div class="card">${a ? `<div class="row"><div class="col"><b>THEN</b><img src="${a.image}" alt=""><i>${a.title}</i><span>${a.artist}${a.date ? ', ' + a.date : ''} · ${a.museum}</span></div>
    <div class="col"><b>NOW</b><img src="${img}" alt=""><i>A participant and a robot dog</i><span>Made together, ${when}</span></div></div>`
    : `<div class="col" style="align-items:center"><img src="${img}" alt="" style="max-height:52vh"></div>`}
    <h2>${heading}</h2><p>is this us? · a collaboration</p></div>`;
}
function meme(img, group) {
  return new Promise((res) => {
    const im = new Image();
    im.onload = () => {
      const c = document.createElement('canvas'); c.width = 1200; c.height = Math.round(im.height * 1200 / im.width);
      const g = c.getContext('2d'); g.drawImage(im, 0, 0, c.width, c.height);
      const mp = show.meme_phrases, words = [...[...(mp[group] || [])].sort(() => Math.random() - 0.5).slice(0, 2),
        ...[...(mp.any || [])].sort(() => Math.random() - 0.5).slice(0, 2), mp.wow || 'wow'];
      const colors = ['#ff40a0', '#5ae65a', '#3cc8ff', '#ffdc28', '#ff8c1e', '#be78ff'];
      g.font = `bold ${Math.round(c.width / 16)}px "Comic Sans MS", system-ui`; g.lineWidth = 6; g.strokeStyle = '#141414';
      words.forEach((t, i) => {
        const y = c.height * (0.12 + 0.18 * i), x = i % 2 ? c.width - g.measureText(t).width - 40 : 40;
        g.strokeText(t, x, y); g.fillStyle = colors[i % colors.length]; g.fillText(t, x, y);
      });
      res(c.toDataURL('image/jpeg', 0.9));
    };
    im.src = img;
  });
}
async function round(cue, g) {
  const era = cue.target;
  state = 'readiness'; controls(state);
  let r;
  if (source === 'photos') { fitSample(era); r = reading; checks(); }  // the sample photo IS the pose
  else {
    if ((await say(cue.text, cue.audio, g)) === 'skip') return;
    r = await waitForPose(g, null, 'holding still');
  }
  if (!r || !r.visible) { await line('round', 'nobody', g); return; }
  const group = POSE_GROUP[r.posture] || '';
  const id = era === 'internet' ? null : choosePicture(era, group);
  const a = id ? art(id) : null;
  if (id) recent.add(id);
  // remembering: the era's pictures, attention moving across them, while the dog walks over
  state = 'remembering'; controls(state);
  const grid = id ? [...show.eras[era]] : show.memes;
  if (grid.length) screen('grid', grid);
  let focus = 0;
  const flash = setInterval(() => { document.querySelectorAll('.cgrid img').forEach((im, i) => im.classList.toggle('on', i === focus % grid.length)); focus++; }, 1400);
  try {
    await Promise.all([line('round', 'thinking', g),
      goBeside(a ? a.side : 'left', a ? a.facing : 'person', g).then(() => takePose(a ? a.dog : MEME_BEHAVIOR[group] || 'sit', g))]);
  } finally { clearInterval(flash); }
  if (a) { screen('art', id); await line('round', 'found', g); } else await line('round', 'meme_found', g);
  await line('round', 'hold', g);
  await sleep(2500, g);
  const shot = photo();
  const img = a ? shot : await meme(shot, group);
  screen('html', card(a, img, a ? 'IS THIS US?' : 'MUCH US. WOW.'));
  state = 'photo'; controls(state);
  await line('round', 'admire', g);
  for (;;) { const c = await nextCmd(['next', 'repeat']); alive(g); if (c === 'next') break; await sayAgain(g); }
  state = 'narration'; controls(state);
  await goHome(g);
}
async function rescue(cue, g) {
  await say(cue.text, cue.audio, g);
  state = 'rescue_down'; controls(state);
  const down = await waitForPose(g, ['lying', 'reclining'], 'someone lying down');
  if (!down) { await line('rescue', 'nobody', g); return; }
  state = 'narration'; controls(state);
  await line('rescue', 'alarm', g);
  await Promise.all([line('rescue', 'coming', g), goBeside('front', 'person', g)]);
  await line('rescue', 'arrived', g);
  await takePose('greet', g);
  await line('rescue', 'wake', g);
  state = 'rescue_up'; controls(state);
  await waitForPose(g, ['sitting', 'standing', 'kneeling', 'crouching', 'leaning_forward'], 'up again');
  state = 'narration'; controls(state);
  await line('rescue', 'saved', g);
  await takePose('sit', g);
  screen('html', card(null, photo(), 'A really good boy.'));
  await sleep(4000, g);
  await goHome(g);
}

// ---------------------------------------------------------------- the show
async function run(from) {
  const g = ++gen;
  paused = false; recent.clear();
  let [ci, qi] = [0, 0];
  if (from) {
    const [ch] = from.split('.');
    ci = Math.max(0, show.chapters.findIndex((c) => c.id === ch));
    qi = Math.max(0, show.chapters[ci].cues.findIndex((c) => c.id === from));
  }
  try {
    for (; ci < show.chapters.length; ci++, qi = 0) {
      const ch = show.chapters[ci];
      $('chapter').textContent = ch.title;
      for (; qi < ch.cues.length; qi++) {
        const cue = ch.cues[qi];
        if (cue.branch === 'alone') continue;  // the ending: "together" (a photo was made)
        if (cue.show || cue.kind === 'round' || cue.kind === 'rescue') history.replaceState(null, '', '#' + cue.id);
        state = 'narration'; controls(state);
        showCue(cue.show);
        if (cue.kind === 'consent') continue;  // decided on the start screen
        if (cue.kind === 'pause') await sleep(cue.seconds * 1000, g);
        else if (cue.kind === 'round') await round(cue, g);
        else if (cue.kind === 'rescue') await rescue(cue, g);
        else if (cue.text) await say(cue.text, cue.audio, g);
      }
    }
    await sleep(3000, g);
    toLobby();
  } catch (e) { if (!(e instanceof Stop)) { console.error(e); msg('error: ' + e.message); } }
}
function toLobby() {
  gen++; paused = false; audio.pause(); subtitle(''); screen('title'); $('chapter').textContent = '';
  Object.assign(dog, { x: 0, y: 0, yaw: 0, walking: false, action: '', body: 'standing' });
  $('lobby').style.display = 'flex'; state = 'lobby'; controls(state);
}
async function start(withCamera) {
  $('lobby').style.display = 'none';
  if (withCamera) await useCamera(); else useSamples();
  const from = location.hash.slice(1);
  run(from && from.includes('.') ? from : null);
}
$('startCam').onclick = () => start(true);
$('startSamples').onclick = () => start(false);
// a hash typed (or Back) replays that step
window.addEventListener('hashchange', () => { const id = location.hash.slice(1); if (source && id) { audio.pause(); run(id); } });
toLobby();
