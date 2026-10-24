"""What the audience sees: the current picture, its museum label, and subtitles.

* `TerminalStage` — rehearsal / operator console.
* `WebStage` — a projector page served locally (stdlib only), polling `/state`.
  Only files the show is currently displaying can be fetched; the participant's
  composition is served only while it is on screen.
* `RecordingStage` — keeps every event (tests, rehearsal transcripts).
"""

from __future__ import annotations

import html
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol

from .catalog import ShowArtifact

log = logging.getLogger(__name__)

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

    def of(self, kind: str) -> list:
        return [v for k, v in self.events if k == kind]


class MultiStage:
    def __init__(self, *stages: Stage):
        self.stages = [s for s in stages if s is not None]

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
#stage{position:fixed;inset:9vh 4vw 26vh 4vw;display:flex;align-items:center;justify-content:center;gap:4vw}
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
</style></head><body>
<div id="chapter"></div><div id="stage"></div><div id="subtitle"></div><div id="notice"></div>
<script>
let last="";
async function tick(){
 try{const r=await fetch('/state',{cache:'no-store'});const s=await r.json();
  document.getElementById('chapter').textContent=s.chapter||'';
  document.getElementById('subtitle').textContent=s.subtitle||'';
  document.getElementById('notice').textContent=s.notice||'';
  if(s.view_html!==last){document.getElementById('stage').innerHTML=s.view_html;last=s.view_html}
 }catch(e){}
 setTimeout(tick,350)}
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
        self._state = {"chapter": "", "subtitle": "", "view_html": "", "notice": AI_VOICE_NOTICE, "updated": 0.0}
        self._files: dict[str, Path] = {}
        stage = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_GET(self):  # noqa: N802
                if self.path in ("/", "/index.html"):
                    return self._send(200, "text/html; charset=utf-8", PAGE.encode())
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
        pass

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
