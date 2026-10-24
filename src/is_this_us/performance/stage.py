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
                    "consent_no", "send_yes", "send_no", "cancel", "listen"}
AI_VOICE_NOTICE = "The robot's voice is AI-generated (OpenAI). Its words are written by the artists."


@dataclass
class View:
    kind: str  # title | blank | artifact | memes | composition
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
        if view.kind == "artifact" and view.artifact:
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

    def close(self) -> None:
        pass

    def live(self, jpeg: bytes | None, checks: list) -> None:
        self.events.append(("live", bool(jpeg)))

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
                try:
                    getattr(s, name)(*a, **kw)
                except Exception as exc:  # a broken display must never stop the show
                    log.warning("stage %s.%s failed: %s", type(s).__name__, name, exc)
        return call


# ---------------------------------------------------------------------- projector page
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Is This Us?</title>
<style>
:root{--bg:#1f1d21;--ink:#efe9de;--muted:#9c958c;--accent:#b7a6d6}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);
font-family:Georgia,'Times New Roman',serif;overflow:hidden}
#chapter{position:fixed;top:3vh;left:4vw;letter-spacing:.35em;font:12px/1 system-ui,sans-serif;color:var(--accent);text-transform:uppercase}
#stage{position:fixed;inset:9vh 4vw 26vh 4vw;display:flex;align-items:center;justify-content:center;gap:3vw}
#view{height:100%;display:flex;align-items:center;justify-content:center;gap:4vw;min-width:0}
#live{display:none;height:100%;flex-direction:column;align-items:center;justify-content:center;max-width:46vw}
#live img{max-width:100%;max-height:calc(100% - 5.5em);object-fit:contain;border-radius:6px;box-shadow:0 0 0 2px var(--accent)}
#live .cap{margin-top:1.2vh;font:clamp(11px,1vw,15px) system-ui,sans-serif;color:var(--muted);letter-spacing:.2em}
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
#msg{position:fixed;top:6.5vh;right:2vw;max-width:46vw;text-align:right;color:var(--accent);font:13px system-ui,sans-serif}
</style></head><body>
<div id="chapter"></div><div id="controls"></div><div id="msg"></div>
<div id="stage"><div id="view"></div><div id="live"><img id="liveimg" alt="what the robot sees">
<div class="cap">WHAT I SEE</div><div id="checks"></div></div></div><div id="subtitle"></div><div id="notice"></div>
<script>
let last="", lastState=null;
const key=new URLSearchParams(location.search).get('key');
const ASK={participant_readiness:[['ready','✓ Ready'],['decline','✗ Declines']],
 consent:[['consent_yes','✓ They agreed to a photo'],['consent_no','✗ No photo']],
 delivery:[['send_yes','Send the picture'],['send_no',"Don't send"]],
 participation:[['ready','✓ They did it'],['next','Move on']],
 awaiting_photo:[['consent_no','✗ No photo']]};
const BASE=[['repeat','↺ Repeat'],['pause','Pause'],['resume','Resume'],['next','Next ▸'],['pose','✦ Go to interactive'],['cancel','End']];
async function send(cmd){
 const r=await fetch('/control?key='+encodeURIComponent(key)+'&cmd='+cmd,{method:'POST'});
 const j=await r.json().catch(()=>({msg:'error'}));document.getElementById('msg').textContent=j.msg||'';}
function controls(state){
 if(!key)return;const c=document.getElementById('controls');c.style.display='flex';
 const ask=(ASK[state]||[]).map(([k,t])=>`<button class="ask" onclick="send('${k}')">${t}</button>`).join('');
 const base=BASE.map(([k,t])=>`<button class="${k==='pose'?'main':''}" onclick="send('${k}')">${t}</button>`).join('');
 c.innerHTML=`<span id="state">${state}</span>`+ask+base;}
async function tick(){
 try{const r=await fetch('/state',{cache:'no-store'});const s=await r.json();
  document.getElementById('chapter').textContent=s.chapter||'';
  document.getElementById('subtitle').textContent=s.subtitle||'';
  document.getElementById('notice').textContent=s.notice||'';
  if(s.view_html!==last){document.getElementById('view').innerHTML=s.view_html;last=s.view_html}
  const lv=document.getElementById('live');
  if(s.live&&s.live.on){document.body.classList.add('live');lv.style.display='flex';
   document.getElementById('liveimg').src='/live.jpg?v='+s.live.v;
   document.getElementById('checks').innerHTML=s.live.checks.map(([t,ok])=>`<span class="${ok?'ok':''}">${ok?'✓':'○'} ${t}</span>`).join('');}
  else{document.body.classList.remove('live');lv.style.display='none';}
  if(s.state!==lastState){controls(s.state);lastState=s.state;
   if(key&&s.note){document.getElementById('msg').textContent='🐕 '+s.note}}
 }catch(e){}
 setTimeout(tick,document.body.classList.contains('live')?150:350)}
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
    def __init__(self, host: str = "127.0.0.1", port: int = 8765):
        self._lock = threading.Lock()
        self._state = {"chapter": "", "subtitle": "", "view_html": "", "notice": AI_VOICE_NOTICE, "state": "",
                       "updated": 0.0}
        self._files: dict[str, Path] = {}
        self._control = None  # (callback, loop) once enabled
        self._live_jpeg: bytes | None = None  # newest camera frame, in memory only
        self.key = secrets.token_urlsafe(9)  # only the operator URL can press buttons
        stage = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_POST(self):  # noqa: N802
                url = urlparse(self.path)
                q = parse_qs(url.query)
                if url.path != "/control":
                    return self._send(404, "text/plain", b"not found")
                if not stage._control or not hmac.compare_digest(q.get("key", [""])[0], stage.key):
                    return self._send(403, "application/json", b'{"msg": "not allowed"}')
                cmd = q.get("cmd", [""])[0]
                if cmd not in CONTROL_COMMANDS:
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
                if urlparse(self.path).path == "/live.jpg":
                    with stage._lock:
                        frame = stage._live_jpeg
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

    def _img(self, path: Path) -> str:
        token = f"{abs(hash((str(path), time.time()))) % 10**10}.jpg"
        with self._lock:
            self._files = {token: path}  # only what is on screen right now
        return f"/img/{token}"

    def chapter(self, chapter_id: str, title: str) -> None:
        self._set(chapter=title)

    def show(self, view: View) -> None:
        if view.kind == "artifact" and view.artifact:
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

    def live(self, jpeg: bytes | None, checks: list) -> None:
        """Newest camera view (or None to hide it). Kept in memory, replaced by the next frame."""
        with self._lock:
            self._live_jpeg = jpeg
            v = (self._state.get("live") or {}).get("v", 0) + 1
            self._state["live"] = {"on": jpeg is not None, "checks": checks if jpeg else [], "v": v}
            self._state["updated"] = time.time()

    def enable_controls(self, callback, loop) -> None:
        """callback(cmd) -> (ok, msg), run on the show's event loop."""
        self._control = (callback, loop)

    @property
    def control_url(self) -> str:
        return f"{self.url}?key={self.key}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
