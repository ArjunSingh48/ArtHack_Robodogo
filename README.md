# Is This Us?

A tiny robot dog looks for us in 5,000 years of pictures of humans and dogs. Take any pose — it finds the
closest picture of that era, walks over and takes the painted dog's pose next to you, and makes a THEN / NOW
picture of the two of you.

**Live: https://isthisus.vercel.app**

Everything runs in the browser (a static site in `web/`):

- the pose is read on the visitor's device (MediaPipe Pose Landmarker); the camera picture never leaves it
- the dog is a simulated Unitree Go2 (official URDF, three.js) next to an avatar posed from the camera
- the voice is pre-generated (`web/audio/`); no API keys online
- start with your camera or with sample photos (Unsplash); every step has a hash (`#paintings.04`) to come back to it

```bash
cd web && python3 -m http.server 8790      # local: http://127.0.0.1:8790/
cd web && vercel deploy --prod             # deploy
```

## Content and the archived Python show

`web/data/show.json` (script, pictures with each painted dog's pose/side/facing, lines, sample poses) and
`web/audio/` were exported from the original Python installation, which drove a real Go2 through a Telegram
bot and a projector page. It is archived, unchanged, in [`archive/python/`](archive/python/README.md).
To change the content, edit it there and re-export:

```bash
cd archive/python && .venv/bin/python scripts/export_web.py
```

Picture credits: museum pictures are public domain / open access (labels on screen); sample photos and the
meme stand-ins are from Unsplash (Unsplash License) — see `archive/python/data/standins/README.md`.
