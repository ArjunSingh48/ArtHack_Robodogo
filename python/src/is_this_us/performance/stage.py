"""What the audience sees: the current picture, its museum label, and subtitles.

* `TerminalStage` — rehearsal / operator console.
* `WebStage` — a projector page served locally (stdlib only), polling `/state`.
  Only files the show is currently displaying can be fetched; the participant's
  composition is served only while it is on screen.
* `RecordingStage` — keeps every event (tests, rehearsal transcripts).
"""

from __future__ import annotations

import asyncio
import hmac
import html
import json
import secrets
import logging
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlparse

from .catalog import ShowArtifact

log = logging.getLogger(__name__)

CONTROL_COMMANDS = {"next", "pause", "resume", "repeat", "skip", "pose", "ready", "decline", "consent_yes",
                    "consent_no", "send_yes", "send_no", "cancel", "listen", "sample", "camera", "samples"}
AI_VOICE_NOTICE = "The robot's voice is AI-generated. Its words are written by the artists."


@dataclass
class View:
    kind: str  # title | blank | artifact | flash | consider (a grid the dog looks through; focus moves) | memes | composition
    artifact: ShowArtifact | None = None
    image: Path | None = None
    memes: list[ShowArtifact] = field(default_factory=list)
    heading: str = ""


class Stage(Protocol):
    def chapter(self, chapter_id: str, title: str) -> None: ...
    def show(self, view: View) -> None: ...
    def subtitle(self, text: str | None) -> None: ...
    def status(self, state: str, detail: str = "") -> None: ...
    def close(self) -> None: ...
    # optional: def alert(self, text: str | None) -> None — a persistent operator-only problem banner


class TerminalStage:
    def __init__(self, out=None, color: bool = True):
        import sys

        self.out = out or sys.stdout
        self.dim = "\033[2m" if color else ""
        self.bold = "\033[1m" if color else ""
        self.reset = "\033[0m" if color else ""

    def _p(self, text: str) -> None:
        print(text, file=self.out, flush=True)

    def chapter(self, chapter_id: str, title: str) -> None:
        self._p(f"\n{self.bold}── {title.upper()} ──{self.reset}")

    def show(self, view: View) -> None:
        if view.kind == "flash" and view.artifact:
            self._p(f"{self.dim}[screen] … {view.artifact.title}, {view.artifact.date}{self.reset}")
        elif view.kind == "consider":
            self._p(f"{self.dim}[screen] remembering: {len(view.memes)} pictures{self.reset}")
        elif view.kind == "artifact" and view.artifact:
            a = view.artifact
            pic = "" if a.displayable else "  (reference only — no image shown)"
            self._p(f"{self.dim}[screen] {a.title} — {a.artist}{', ' + a.date if a.date else ''} · {a.museum}{pic}{self.reset}")
        elif view.kind == "composition":
            self._p(f"{self.dim}[screen] THEN / NOW — IS THIS US?  {view.image}{self.reset}")
        elif view.kind == "memes":
            self._p(f"{self.dim}[screen] " + " · ".join(m.title for m in view.memes) + self.reset)
        else:
            self._p(f"{self.dim}[screen] {view.kind}{self.reset}")

    def subtitle(self, text: str | None) -> None:
        if text:
            for line in text.splitlines():
                self._p(f"   🐾 {line}")

    def status(self, state: str, detail: str = "") -> None:
        self._p(f"{self.dim}   · {state}{': ' + detail if detail else ''}{self.reset}")

    def focus(self, artifact_id: str) -> None:
        pass

    def alert(self, text: str | None) -> None:
        if text:
            self._p(f"\n{self.bold}⚠ OPERATOR: {text}{self.reset}\n")

    def close(self) -> None:
        pass


class RecordingStage:
    def __init__(self):
        self.events: list[tuple[str, object]] = []

    def chapter(self, chapter_id: str, title: str) -> None:
        self.events.append(("chapter", chapter_id))

    def show(self, view: View) -> None:
        self.events.append(("show", view.artifact.id if view.artifact else view.kind))

    def subtitle(self, text: str | None) -> None:
        if text:
            self.events.append(("subtitle", text))

    def status(self, state: str, detail: str = "") -> None:
        self.events.append(("status", state))

    def alert(self, text: str | None) -> None:
        self.events.append(("alert", text))

    def focus(self, artifact_id: str) -> None:
        self.events.append(("focus", artifact_id))

    def close(self) -> None:
        pass

    def live(self, jpeg: bytes | None, checks: list, feed: str = "laptop") -> None:
        self.events.append(("live" if feed == "laptop" else f"live_{feed}", bool(jpeg)))

    def of(self, kind: str) -> list:
        return [v for k, v in self.events if k == kind]


class MultiStage:
    def __init__(self, *stages: Stage):
        self.stages = [s for s in stages if s is not None]
        self.web = None

    @property
    def live(self):
        """Live camera view: only when a projector page exists."""
        return self.web.live if self.web is not None else None

    def __getattr__(self, name):
        def call(*a, **kw):
            for s in self.stages:
                fn = getattr(s, name, None)
                if fn is None:  # optional display feature (sim, live …) this stage doesn't have
                    continue
                try:
                    fn(*a, **kw)
                except Exception as exc:  # a broken display must never stop the show
                    log.warning("stage %s.%s failed: %s", type(s).__name__, name, exc)
        return call


# ---------------------------------------------------------------------- projector page
SIM3D_JS = Path(__file__).with_name("sim3d.js")  # the 3-D simulated room (three.js); falls back to drawSim
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Is This Us?</title>
<style>
:root{--bg:#1f1d21;--ink:#efe9de;--muted:#9c958c;--accent:#b7a6d6}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);
font-family:Georgia,'Times New Roman',serif;overflow:hidden}
#chapter{position:fixed;top:3vh;left:4vw;letter-spacing:.35em;font:12px/1 system-ui,sans-serif;color:var(--accent);text-transform:uppercase}
#stage{position:fixed;inset:9vh 4vw 26vh 4vw;display:flex;align-items:center;justify-content:center;gap:3vw}
#view{height:100%;display:flex;align-items:center;justify-content:center;gap:4vw;min-width:0}
#live{display:none;height:100%;flex:0 0 30vw;max-width:30vw;gap:1vh;grid-template-rows:repeat(var(--n,1),minmax(0,1fr)) auto}
#live .cam{display:none;flex-direction:column;align-items:center;justify-content:center;min-height:0;width:100%}
#live .cam img,#simsvg{flex:1 1 auto;min-height:0;max-height:none;width:100%;object-fit:contain;border-radius:6px}
#live #cam-laptop img{box-shadow:0 0 0 2px var(--accent)}
#live #cam-robot img{box-shadow:0 0 0 2px #8fc7d9}
#simsvg{background:#26232a;box-shadow:0 0 0 2px #8fc7d9}
#sim3d{display:none;flex:1 1 auto;min-height:0;width:100%;border-radius:6px;overflow:hidden;box-shadow:0 0 0 2px #8fc7d9}
#cam-sim.three #sim3d{display:block}#cam-sim.three #simsvg{display:none}
#live .cap{margin-top:.5vh;font:11px system-ui,sans-serif;color:var(--muted);letter-spacing:.15em;white-space:nowrap}
body.live #view{flex:1 1 auto;max-width:60vw}
#checks{margin-top:.8vh;display:flex;gap:1.2vw;flex-wrap:wrap;justify-content:center;font:clamp(13px,1.3vw,19px) system-ui,sans-serif}
#checks span{color:var(--muted)}#checks span.ok{color:#a0d6aa}
body.live #view .figure{max-width:42vw}
#stage img{max-width:100%;max-height:calc(100% - 6.5em);min-height:0;object-fit:contain;box-shadow:0 0 0 1px #3a363d}
.figure{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;max-width:62vw}
.label{margin-top:1.6vh;text-align:center;font-size:clamp(13px,1.6vw,22px)}
.label i{display:block;font-size:1.1em}.label span{color:var(--muted);font:clamp(11px,1vw,15px) system-ui,sans-serif}
.card{border:1px solid var(--accent);padding:4vh 2vw;text-align:center;max-width:60vw;overflow-wrap:anywhere}
.card i{font-size:clamp(22px,3vw,44px);display:block}.card span{color:var(--muted);font:14px system-ui,sans-serif}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(16vw,1fr));gap:2vw;width:100%}
.title{font-size:clamp(36px,7vw,110px);font-style:italic;letter-spacing:.02em}
.flash{animation:flash .9s ease-out both}.flash img{filter:sepia(.25) saturate(.85)}
.flash .label{color:var(--muted)}.flash .label b{display:block;color:var(--accent);letter-spacing:.35em;
font:600 11px system-ui,sans-serif;margin-bottom:.6vh}
.consider{width:100%;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center}
.consider b{color:var(--accent);letter-spacing:.35em;font:600 12px system-ui,sans-serif;margin-bottom:1.4vh}
.cgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(9vw,1fr));gap:1.4vw;width:100%;align-items:center;justify-items:center}
.cgrid figure{margin:0;opacity:.38;filter:grayscale(.55);transition:opacity 1.2s ease,filter 1.2s ease,transform 1.2s ease}
.cgrid figure img{max-width:100%;max-height:24vh;object-fit:contain;box-shadow:none!important}
.cgrid figure.on{opacity:1;filter:none;transform:scale(1.08)}
.cgrid figure.on img{box-shadow:0 0 0 2px var(--accent)!important}
.cgrid figcaption{font:clamp(10px,.9vw,13px) system-ui,sans-serif;color:var(--muted);text-align:center;margin-top:.6vh;opacity:0;transition:opacity 1.2s}
.cgrid figure.on figcaption{opacity:1}
@keyframes flash{from{opacity:0;transform:scale(1.035);filter:blur(3px)}to{opacity:1;transform:none;filter:none}}
#subtitle{position:fixed;left:6vw;right:6vw;bottom:9vh;text-align:center;font-size:clamp(20px,2.8vw,40px);
line-height:1.35;min-height:2.7em;white-space:pre-line}
#notice{position:fixed;bottom:2.4vh;left:0;right:0;text-align:center;color:var(--muted);font:12px system-ui,sans-serif}
#controls{position:fixed;top:1.6vh;right:2vw;display:none;gap:6px;flex-wrap:wrap;justify-content:flex-end;max-width:70vw;
font:13px system-ui,sans-serif;z-index:5}
#controls button{background:#2b282e;color:var(--ink);border:1px solid #4a4550;border-radius:999px;padding:7px 13px;cursor:pointer}
#controls button:hover{border-color:var(--accent)}
#controls button.main{background:var(--accent);color:#1f1d21;border-color:var(--accent);font-weight:600}
#controls button.ask{border-color:var(--accent);color:var(--accent)}
#state{color:var(--muted);align-self:center;margin-right:6px}
#alert{position:fixed;top:11vh;right:2vw;max-width:46vw;display:none;background:#5a1f24;color:#ffd9d9;
border:1px solid #c0525c;border-radius:8px;padding:10px 14px;font:14px/1.35 system-ui,sans-serif;z-index:6}
#msg{position:fixed;top:6.5vh;right:2vw;max-width:46vw;text-align:right;color:var(--accent);font:13px system-ui,sans-serif}
</style></head><body>
<div id="chapter"></div><div id="controls"></div><div id="msg"></div><div id="alert"></div>
<div id="stage"><div id="view"></div><div id="live">
<div class="cam" id="cam-sim"><div id="sim3d"></div><svg id="simsvg" viewBox="0 0 400 260"></svg><div class="cap">SIMULATED DOG · <span id="simdoing"></span></div></div>
<div class="cam" id="cam-robot"><img id="liveimg-robot" alt="what the robot dog sees"><div class="cap">WHAT THE DOG SEES</div></div>
<div class="cam" id="cam-laptop"><img id="liveimg" alt="the photo camera"><div class="cap">THE PHOTO CAMERA</div></div>
<div id="checks"></div></div></div><div id="subtitle"></div><div id="notice"></div>
<script type="importmap">{"imports":{
 "three":"https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.js",
 "three/examples/jsm/":"https://cdn.jsdelivr.net/npm/three@0.170.0/examples/jsm/",
 "urdf-loader":"https://cdn.jsdelivr.net/npm/urdf-loader@0.13.1/src/URDFLoader.js"}}</script>
<script type="module">import('/sim3d.js').then(m=>{window.sim3d=m.start(document.getElementById('sim3d'))})
 .catch(e=>console.warn('3-D view unavailable, using the drawn map',e))</script>
<script>
let last="", lastState=null;
const key=new URLSearchParams(location.search).get('key');
const ASK={lobby:[['consent_yes','▶ Start — they agreed to photos (camera)'],
  ['sample','▶ Start with sample photos']],
 participant_readiness:[['ready','▶ Start the round now'],['skip','⏭ Skip this round']],
 final_pose:[['skip','End the pose now']],
 consent:[['consent_yes','✓ They agreed to a photo'],['consent_no','✗ No photo']],
 delivery:[['send_yes','✓ Send to Telegram'],['send_no',"✗ Don't send"]],
 rescue:[['ready','✓ Yes, now'],['skip','Skip']],
 artwork_display:[['next','▸ Continue (photo seen)']],
 participation:[['ready','✓ They did it'],['next','Move on']],
 awaiting_photo:[['consent_no','✗ No photo']]};
const BASE=[['repeat','↺ Repeat'],['pause','Pause'],['resume','Resume'],['next','Next ▸'],['pose','✦ Go to interactive'],['camera','📷 Switch to the webcam'],['samples','🖼 Switch to sample photos'],['cancel','End']];
async function send(cmd){
 const r=await fetch('/control?key='+encodeURIComponent(key)+'&cmd='+cmd,{method:'POST'});
 const j=await r.json().catch(()=>({msg:'error'}));document.getElementById('msg').textContent=j.msg||'';}
let source='', allowed=[];  // source: camera | photos (only the switch to the other is shown); allowed: what works now
function controls(state){
 if(!key)return;const c=document.getElementById('controls');c.style.display='flex';
 const ask=(ASK[state]||[]).map(([k,t])=>`<button class="ask" onclick="send('${k}')">${t}</button>`).join('');
 const base=BASE.filter(([k])=>allowed.includes(k))
  .filter(([k])=>!(k==='camera'&&source!=='photos')&&!(k==='samples'&&source==='photos')).map(([k,t])=>`<button class="${k==='pose'?'main':''}" onclick="send('${k}')">${t}</button>`).join('');
 c.innerHTML=`<span id="state">${state}</span>`+ask+base;}
function drawSim(m){
 // top-down: the dog starts at the bottom centre facing up (x forward, y to its left), 1 m = 70 px
 const P=(x,y)=>[200-y*70,235-x*70];const [rx,ry,ryaw]=m.robot;const [px,py]=m.person;
 const [sx,sy]=P(rx,ry),[qx,qy]=P(px,py);const a=-ryaw*180/Math.PI;
 const trail=m.trail.map(([x,y])=>P(x,y).join(',')).join(' ');
 const grid=[0,1,2,3].map(i=>`<line x1="0" y1="${235-i*70}" x2="400" y2="${235-i*70}" stroke="#34303a"/>`).join('');
 const body={sitting:'SIT',lying:'DOWN',standing:'STAND'}[m.body]||'';
 document.getElementById('simsvg').innerHTML=grid+
  `<polyline points="${trail}" fill="none" stroke="#8fc7d9" stroke-dasharray="4 4" stroke-width="2"/>`+
  `<g transform="translate(${qx},${qy})"><circle r="15" fill="#b7a6d6" opacity=".85"/><text y="32" fill="#efe9de" font-size="12" text-anchor="middle" font-family="system-ui">person · ${m.posture}</text></g>`+
  `<g transform="translate(${sx},${sy}) rotate(${a})"><rect x="-11" y="-17" width="22" height="34" rx="8" fill="${m.walking?'#a0d6aa':'#8fc7d9'}"/><path d="M0,-26 L-6,-16 L6,-16 Z" fill="#efe9de"/></g>`+
  `<text x="${sx+18}" y="${sy+4}" fill="#efe9de" font-size="12" font-family="system-ui">${body}</text>`+
  `<text x="8" y="16" fill="#9c958c" font-size="11" font-family="system-ui">simulated room — nothing real moves</text>`;
 document.getElementById('simdoing').textContent=(m.doing||m.body||'').toUpperCase();}
async function tick(){
 try{const r=await fetch('/state',{cache:'no-store'});const s=await r.json();
  document.getElementById('chapter').textContent=s.chapter||'';
  const step=s.step||s.chapter_id;
  if(step&&location.hash.slice(1)!==step)history.replaceState(null,'','#'+step);
  document.getElementById('subtitle').textContent=s.subtitle||'';
  document.getElementById('notice').textContent=s.notice||'';
  const al=document.getElementById('alert');if(key&&s.alert){al.style.display='block';al.textContent='⚠ '+s.alert}else{al.style.display='none'}
  if(s.view_html!==last){document.getElementById('view').innerHTML=s.view_html;last=s.view_html}
  document.querySelectorAll('.cgrid figure').forEach(f=>f.classList.toggle('on',f.dataset.id===s.focus));
  const lv=document.getElementById('live');const feeds=s.live||{};let any=false,checks=null;
  for(const f of ['robot','laptop']){const st=feeds[f]||{};const box=document.getElementById('cam-'+f);
   if(st.on){any=true;box.style.display='flex';const im=document.getElementById(f==='robot'?'liveimg-robot':'liveimg');
    const src='/live.jpg?feed='+f+'&v='+st.v;if(im.dataset.v!==String(st.v)){im.src=src;im.dataset.v=st.v}
    if(st.checks&&st.checks.length&&(!checks||f===feeds.pose))checks=st.checks}else{box.style.display='none'}}
  const sb=document.getElementById('cam-sim');
  if(s.sim){any=true;sb.style.display='flex';
   if(window.sim3d&&window.sim3d.ready()){sb.classList.add('three');window.sim3d.update(s.sim)}else{sb.classList.remove('three');drawSim(s.sim)}
   document.getElementById('simdoing').textContent=(s.sim.doing||s.sim.body||'').toUpperCase()}
  else{sb.style.display='none'}
  lv.style.setProperty('--n',['cam-sim','cam-robot','cam-laptop'].filter(id=>document.getElementById(id).style.display==='flex').length||1);
  if(any){document.body.classList.add('live');lv.style.display='grid';
   document.getElementById('checks').innerHTML=(checks||[]).map(([t,ok])=>`<span class="${ok?'ok':''}">${ok?'✓':'○'} ${t}</span>`).join('');}
  else{document.body.classList.remove('live');lv.style.display='none';}
  if(s.state!==lastState||(s.source||'')!==source||String(s.allowed)!==String(allowed)){
   source=s.source||'';allowed=s.allowed||[];controls(s.state);lastState=s.state;
   if(key&&s.note){document.getElementById('msg').textContent='🐕 '+s.note}}
 }catch(e){}
 setTimeout(tick,document.body.classList.contains('live')?150:350)}
// every step has a hash (#prehistory, #paintings …): the address follows the show; typing one, or Back,
// replays that step
window.addEventListener('hashchange',()=>{const id=location.hash.slice(1);if(key&&id)send('goto:'+id)});
tick();
</script></body></html>"""


def _label(a: ShowArtifact) -> str:
    meta = html.escape(a.artist + (f", {a.date}" if a.date else ""))
    return (f"<div class='label'><i>{html.escape(a.title)}</i>{meta}<br>"
            f"<span>{html.escape(a.museum)} · {html.escape(a.rights_status)}</span></div>")


def _ref_card(a: ShowArtifact) -> str:
    from urllib.parse import urlparse

    host = urlparse(a.page_url).netloc.removeprefix("www.") or a.page_url
    return (f"<div class='card'><i>{html.escape(a.title)}</i>{html.escape(a.date)}<br>"
            f"<span>{html.escape(a.description)}<br><br>reference only — image not reproduced<br>"
            f"source: {html.escape(host)}</span></div>")


class WebStage:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765, key: str | None = None):
        self._lock = threading.Lock()
        self._state = {"chapter": "", "subtitle": "", "view_html": "", "notice": AI_VOICE_NOTICE, "state": "",
                       "updated": 0.0}
        self._files: dict[str, Path] = {}
        self._control = None  # (callback, loop) once enabled
        self._live_jpeg: dict[str, bytes | None] = {}  # newest frame per camera feed, in memory only
        self.key = key or secrets.token_urlsafe(9)  # only the operator URL can press buttons
        stage = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_POST(self):  # noqa: N802
                url = urlparse(self.path)
                q = parse_qs(url.query)
                if url.path != "/control":
                    return self._send(404, "text/plain", b"not found")
                if not stage._control:
                    return self._send(403, "application/json", b'{"msg": "buttons are not active yet: start a performance"}')
                if not hmac.compare_digest(q.get("key", [""])[0], stage.key):
                    return self._send(403, "application/json",
                                      b'{"msg": "old operator link: open the Operator page link printed in the terminal"}')
                cmd = q.get("cmd", [""])[0]
                if cmd not in CONTROL_COMMANDS and not cmd.startswith("goto:"):
                    return self._send(400, "application/json", b'{"msg": "unknown command"}')
                fn, loop = stage._control

                async def call():
                    return fn(cmd)
                try:
                    ok, msg = asyncio.run_coroutine_threadsafe(call(), loop).result(timeout=3)
                except Exception as exc:  # loop gone, timeout
                    ok, msg = False, f"error: {type(exc).__name__}"
                return self._send(200, "application/json", json.dumps({"ok": ok, "msg": msg}).encode())

            def do_GET(self):  # noqa: N802
                if urlparse(self.path).path in ("/", "/index.html"):
                    return self._send(200, "text/html; charset=utf-8", PAGE.encode())
                if urlparse(self.path).path == "/sim3d.js":
                    return self._send(200, "text/javascript; charset=utf-8", SIM3D_JS.read_bytes())
                if urlparse(self.path).path == "/live.jpg":
                    feed = parse_qs(urlparse(self.path).query).get("feed", ["laptop"])[0]
                    with stage._lock:
                        frame = stage._live_jpeg.get(feed)
                    if frame:
                        return self._send(200, "image/jpeg", frame)
                    return self._send(404, "text/plain", b"no live view")
                if self.path.startswith("/state"):
                    with stage._lock:
                        body = json.dumps(stage._state).encode()
                    return self._send(200, "application/json", body)
                if self.path.startswith("/img/"):
                    token = self.path[5:].split("?")[0]
                    with stage._lock:
                        p = stage._files.get(token)
                    if p and p.exists():
                        return self._send(200, "image/jpeg", p.read_bytes())
                return self._send(404, "text/plain", b"not found")

            def _send(self, code, ctype, body):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.url = f"http://{host}:{self.server.server_address[1]}/"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw, updated=time.time())

    def _img(self, path: Path, keep: bool = False) -> str:
        token = f"{abs(hash((str(path), time.time()))) % 10**10}.jpg"
        with self._lock:
            if keep:
                self._files[token] = path  # several pictures on screen at once (the remembering grid)
            else:
                self._files = {token: path}  # only what is on screen right now
        return f"/img/{token}"

    def chapter(self, chapter_id: str, title: str) -> None:
        self._set(chapter=title, chapter_id=chapter_id, step=chapter_id)

    def allowed(self, cmds: list[str]) -> None:
        """The commands that do something right now: the operator page shows only those buttons."""
        self._set(allowed=cmds)

    def source(self, kind: str) -> None:
        """Where the participant comes from: "camera" or "photos" — only the switch to the other one is shown."""
        self._set(source=kind)

    def step(self, step_id: str) -> None:
        """A picture step (cue id): the operator page's address becomes #<step>, to come back to it."""
        self._set(step=step_id)

    def show(self, view: View) -> None:
        if view.kind == "consider" and view.memes:
            with self._lock:
                self._files = {}
            figs = "".join(f"<figure data-id='{html.escape(a.id)}'><img src='{self._img(a.image_path, keep=True)}' alt=''>"
                           f"<figcaption>{html.escape(a.title)}, {html.escape(a.date)}</figcaption></figure>"
                           for a in view.memes if a.displayable)
            body = (f"<div class='consider'><b>{html.escape((view.heading or 'remembering…').upper())}</b>"
                    f"<div class='cgrid'>{figs}</div></div>")
            self._set(view_html=body, focus="")
            return
        if view.kind == "flash" and view.artifact and view.artifact.displayable:
            a = view.artifact
            body = (f"<div class='figure flash'><img src='{self._img(a.image_path)}' alt=''><div class='label'>"
                    f"<b>REMEMBERING…</b>{html.escape(a.title)}, {html.escape(a.date)}</div></div>")
        elif view.kind == "artifact" and view.artifact:
            a = view.artifact
            body = (f"<div class='figure'><img src='{self._img(a.image_path)}' alt=''>{_label(a)}</div>"
                    if a.displayable else _ref_card(a))
        elif view.kind == "composition" and view.image:
            body = f"<div class='figure'><img src='{self._img(view.image)}' alt='THEN and NOW'></div>"
        elif view.kind == "memes":
            body = "<div class='grid'>" + "".join(_ref_card(m) for m in view.memes) + "</div>"
        elif view.kind == "title":
            with self._lock:
                self._files = {}
            body = "<div class='title'>Is this us?</div>"
        else:
            with self._lock:
                self._files = {}
            body = ""
        self._set(view_html=body)

    def subtitle(self, text: str | None) -> None:
        self._set(subtitle=text or "")

    def status(self, state: str, detail: str = "") -> None:
        if state != "listening":
            self._set(state=state, note=detail)

    def sim(self, snapshot: dict | None) -> None:
        """The simulated dog (positions in metres), drawn on the page; None hides it."""
        self._set(sim=snapshot)

    def focus(self, artifact_id: str) -> None:
        """Which picture of the remembering grid the dog is looking at (no re-render: no flicker)."""
        self._set(focus=artifact_id)

    def alert(self, text: str | None) -> None:
        """Operator-only banner (shown on the ?key= page, never on the projector)."""
        self._set(alert=text or "")

    def live(self, jpeg: bytes | None, checks: list, feed: str = "laptop") -> None:
        """Newest frame of one camera feed ("robot" = what the dog sees, "laptop" = the photo camera), or None to
        hide it. Kept in memory, replaced by the next frame."""
        with self._lock:
            self._live_jpeg[feed] = jpeg
            feeds = dict(self._state.get("live") or {})
            v = (feeds.get(feed) or {}).get("v", 0) + 1
            feeds[feed] = {"on": jpeg is not None, "checks": checks if jpeg else [], "v": v}
            if jpeg is not None and checks:
                feeds["pose"] = feed  # whose checklist to show
            self._state["live"] = feeds
            self._state["updated"] = time.time()

    def enable_controls(self, callback, loop) -> None:
        """callback(cmd) -> (ok, msg), run on the show's event loop."""
        self._control = (callback, loop)

    @property
    def control_url(self) -> str:
        return f"{self.url}?key={self.key}"

    def close(self) -> None:
        self.closed = True
        self.server.shutdown()
        self.server.server_close()
