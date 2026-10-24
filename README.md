# Is This Us? — A Dog's Memory

[![tests](../../actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)

*An interactive installation in which a Unitree robot dog learns how to be a dog from historical
paintings of humans and dogs. Python · MediaPipe · aiogram 3 · ElevenLabs · Unitree SDK2.
Developer docs are in English in [`docs/`](docs/); the setup guide below is in Russian.*

Интерактивная инсталляция: робот-собака Unitree учится быть собакой по историческим картинам с людьми и собаками.
Человек делает жест → робот вспоминает картину с похожими *отношениями* человека и собаки → пробует повторить позу
собаки своим телом → присылает картину в Telegram и тихо спрашивает: **«Is this us?»**

> The machine does not learn to imitate commands. It learns to imitate relationships.

Документация для разработчиков (на английском): [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md),
[`COMPATIBILITY`](docs/COMPATIBILITY.md), [`HARDWARE_SETUP`](docs/HARDWARE_SETUP.md),
[`SAFETY_CHECKLIST`](docs/SAFETY_CHECKLIST.md), [`DEMO_SCRIPT`](docs/DEMO_SCRIPT.md),
[`TROUBLESHOOTING`](docs/TROUBLESHOOTING.md).

---

## Что уже работает (без робота)

- 24 настоящие картины (The Met — public domain, Cleveland Museum of Art — CC0), метаданные и права проверены через музейные API, каждая картина размечена вручную: поза человека, поза собаки, пространственное отношение, тип взаимодействия, выполнимость для робота, короткое стихотворение.
- Распознавание позы через MediaPipe (фото, видео, вебкамера, камера Go2) + прозрачные геометрические правила: стоит / сидит / на корточках / на коленях / наклон / полулёжа / лежит, жесты «держит», «тянет руку», «смотрит вниз», «повернулся к роботу», расстояние.
- Движок сопоставления **отношений**, а не визуального сходства, с настраиваемыми весами и ограниченной случайностью.
- Безопасный планировщик движений → mock-робот (или Go2 после подтверждения модели).
- Характер робота: детерминированные шаблоны на EN / RU / DE.
- Голос ElevenLabs с кэшем, OGG-голосовыми для Telegram и тихим откатом в текст.
- Telegram-бот: QR-сопряжение, `/observe`, фото-режим без робота, кнопки ♡ / ↻ / ✧, галерея воспоминаний, карточки-воспоминания, `/forget`, команды оператора.
- 99 тестов (включая адаптер Go2 против имитации SDK), CI на GitHub Actions.

---

## 1. Установка

Нужны Python 3.11–3.14 и FFmpeg (для голосовых сообщений).

```bash
cd is-this-us
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[vision,voice,dev]"

cp .env.example .env               # потом заполни токены (см. ниже)
itu setup                          # скачает модель позы, закэширует картины, создаст базу
pytest                             # все тесты должны пройти
```

FFmpeg: macOS — `brew install ffmpeg`, Ubuntu — `sudo apt install ffmpeg`.

## 2. Первая симуляция в консоли (без Telegram, без робота)

```bash
# синтетический скелет (не реальный человек) — сидит
itu simulate --posture sitting --another --card card.jpg

# на коленях и тянет руку, по-русски
itu simulate --posture kneeling --gesture extending --lang ru

# твоё фото (режим A) или один кадр с вебкамеры (режим B)
itu simulate --image me_sitting.jpg
itu simulate --webcam

# живое окно отладки: точки скелета, поза, жесты; ПРОБЕЛ — взаимодействие, Q — выход
itu webcam
```

В выводе видно всё, что требует отладка: распознанная поза и уверенность, уверенность ориентиров,
топ-3 картин с разбивкой баллов, выбранное поведение робота, статус выполнения, диалог, статус доставки
и путь по состояниям.

## 3. Создание Telegram-бота через BotFather

1. В Telegram открой **@BotFather** → `/newbot`.
2. Имя: `Is This Us? — A Dog's Memory`. Username: например `IsThisUsDogBot` (должен заканчиваться на `bot`).
3. BotFather пришлёт токен вида `123456:ABC...` → вставь в `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_BOT_USERNAME=IsThisUsDogBot
   ```
4. Узнай свой числовой Telegram ID (например, у **@userinfobot**) и впиши как оператора:
   ```
   TELEGRAM_OPERATOR_IDS=123456789
   ```
   Можно несколько через запятую. Только эти аккаунты могут `/arm`, `/disarm`, `/qr`, `/status`, `/open`, `/close`.
5. По желанию в BotFather: `/setdescription`, `/setuserpic` (например, фрагмент одной из картин), `/setabouttext`.
   Список команд бот выставит сам при запуске.
6. Запуск:
   ```bash
   itu bot
   ```
7. С телефона: открой бота → *Start*. Отправь своё фото (сидя, стоя, на коленях…) — придёт картина,
   симулированная поза робота, кнопки ♡ / ↻ / ✧. *Keep* сохраняет карточку в `/memory`.
8. Режим инсталляции: оператор пишет боту `/qr` → QR-код (deep link `?start=pair_…`, живёт
   `PAIRING_TOKEN_TTL_MINUTES`). Посетитель сканирует → сопряжён → `/observe` снимает кадр с
   `CAMERA_SOURCE` и запускает весь цикл. QR можно также сделать командой `itu qr --out qr.png`.

Токен никогда не коммить: `.env` уже в `.gitignore`.

## 4. Голос ElevenLabs (по желанию)

```
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=...            # выбери мягкий, тихий голос в Voice Library
ELEVENLABS_MODEL_ID=eleven_multilingual_v2
LOCAL_AUDIO_PLAYBACK=true          # проигрывать на динамике ноутбука / Bluetooth-колонке
```

В ElevenLabs уходит **только текст реплики** — никаких изображений и данных о позе. Аудио кэшируется
в `data/runtime/audio_cache`. Без ключа или при ошибке API всё продолжает работать текстом.

## 5. Настоящий робот

Пошагово (на английском, с командами) — [`docs/HARDWARE_SETUP.md`](docs/HARDWARE_SETUP.md), и обязательно
[`docs/SAFETY_CHECKLIST.md`](docs/SAFETY_CHECKLIST.md). Коротко:

```bash
pip install -e ../unitree_sdk2_python                       # официальный SDK (один раз)
itu robot-check --save-frame test_frame.jpg                 # только чтение, без движения
itu robot-test --sequence --i-have-physical-estop           # каждое движение по Enter
itu live --source unitree --no-motion                       # камера + картины, робот стоит
itu live --source unitree --i-have-physical-estop --lang ru # всё целиком, в терминале
```

Адаптер Go2 написан по исходникам официального SDK: только Sit, RiseSit, StandUp, StandDown,
BalanceStand, Hello, Stretch, StopMove — никакой ходьбы и трюков. Пока он не проверен на твоём роботе,
он помечен **HARDWARE-UNVERIFIED**. Python SDK обычно работает с **Go2 EDU** по Ethernet.

В Telegram-режиме робот двигается, только если одновременно: `APP_MODE=installation`, `ROBOT_MODEL=go2`,
`ROBOT_MOTION_ENABLED=true` **и** оператор отправил `/arm`. Фото из Telegram никогда не двигают
настоящего робота. `/stop`, `s` и Ctrl+C — программная остановка, **не** аварийная кнопка.

## 6. Пополнение архива картин

```bash
itu artworks search "dog portrait" --museum met     # кандидаты (только public domain / CC0)
itu artworks draft met:436217                       # заготовка разметки
# → допиши разметку в data/artworks/curation_notes.yaml
itu artworks build                                  # подтянет метаданные из API и проверит права
itu artworks validate
itu artworks cache
itu artworks list
```

Метаданные музеев не пишутся руками — только через `build`, чтобы не было выдуманных данных.

## Структура

```
is-this-us/
├── data/artworks/        curation_notes.yaml (ручная разметка) · annotations.yaml (сгенерировано) · images/ (кэш)
├── docs/                 архитектура, совместимость, железо, безопасность, сценарий, неполадки
├── models/               модель MediaPipe (скачивается `itu setup`)
├── src/is_this_us/
│   ├── vision/           frame_sources · pose (MediaPipe) · interpreter (правила) · synthetic (тестовые скелеты)
│   ├── artworks/         museum_clients (Met v1.1, Cleveland) · repository · curation
│   ├── matching/         matcher — сопоставление отношений
│   ├── motion/           planner (картина → разрешённое поведение) · guard (безопасность)
│   ├── robot/            base (последовательности SDK) · mock · unitree_go2
│   ├── dialogue/         engine + locales/en, ru, de
│   ├── speech/           ElevenLabs, кэш, FFmpeg, локальный динамик
│   ├── bot/              flows (логика без сети) · app (aiogram 3)
│   ├── orchestrator.py   машина состояний
│   ├── storage.py        SQLite: сопряжение, сессии, воспоминания, /forget
│   ├── cards.py          карточки-воспоминания (Pillow)
│   ├── hardware_cli.py   robot-check · robot-test · live
│   └── cli.py            команда `itu`
├── .github/workflows/    CI: тесты на Python 3.11–3.13
└── tests/                99 тестов
```

## Приватность

Без распознавания лиц и идентификации. Кадры и фото обрабатываются в памяти и не сохраняются.
В базе — только грубые метки (поза, id картины, поведение), они истекают через `SESSION_TTL_MINUTES`.
Сохранённые воспоминания содержат только картину и время. `/forget` удаляет всё сразу.
Один посетитель никогда не видит моменты другого.
