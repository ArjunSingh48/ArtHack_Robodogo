"""Application configuration loaded from environment variables / `.env`."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from typing import Annotated, Literal


class AppMode(StrEnum):
    SIMULATION = "simulation"
    INSTALLATION = "installation"


class RobotModel(StrEnum):
    MOCK = "mock"
    GO2 = "go2"  # direct Unitree SDK2 (DDS) from this process
    ZHAW_BRIDGE = "zhaw_bridge"  # via the ZHAW go2_main bridge (python -m go2_bridge)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_mode: AppMode = AppMode.SIMULATION
    default_language: str = "en"

    robot_model: RobotModel = RobotModel.MOCK
    robot_network_interface: str = ""
    robot_motion_enabled: bool = False
    motion_cooldown_seconds: float = 8.0
    motion_timeout_seconds: float = 12.0
    min_battery_percent: int = 25
    go2_state_topic: str = "rt/sportmodestate"
    go2_lowstate_topic: str = "rt/lowstate"
    zhaw_bridge_host: str = "localhost"
    zhaw_bridge_cmd_port: int = 5555
    zhaw_bridge_pub_port: int = 5556
    # Must match the bridge's GO2_SPORT_TIMEOUT_S (go2_main). We wait for each action this long + a margin, so
    # our side never gives up before the robot's own SDK call can answer.
    zhaw_bridge_sdk_timeout_s: float = 10.0

    telegram_bot_token: str = ""
    telegram_operator_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    telegram_bot_username: str = ""
    pairing_token_ttl_minutes: int = 10
    session_ttl_minutes: int = 30

    # English scripted speech; explicit legacy settings remain supported.
    voice_provider: Literal["elevenlabs", "openai", "none"] | None = None
    voice_cache_enabled: bool | None = None
    elevenlabs_api_key: str = Field(default="", repr=False)
    elevenlabs_voice_id: str = ""
    elevenlabs_model_id: str = "eleven_multilingual_v2"
    elevenlabs_output_format: Literal["pcm_24000"] = "pcm_24000"
    elevenlabs_stability: float = Field(default=0.5, ge=0, le=1)
    elevenlabs_similarity_boost: float = Field(default=0.75, ge=0, le=1)
    elevenlabs_style: float = Field(default=0.0, ge=0, le=1)
    elevenlabs_timeout_seconds: float = Field(default=18.0, gt=0, le=60)

    # Azure OpenAI experiments. Model settings below are Azure deployment names.
    azure_openai_api_key: str = Field(default="", repr=False)
    azure_openai_endpoint: str = ""
    # openai = OpenAI voice; none = text-only everywhere (no audio requests at all)
    openai_voice_provider: str = "openai"
    # Free-play lines and `itu talk` (live speech-to-speech)
    openai_realtime_model: str = "gpt-realtime-2.1"
    openai_realtime_voice: str = "marin"
    # Performance (`itu show`): scripted narration, pre-generated and cached per cue
    openai_narration_model: str = "gpt-realtime-2.1-mini"
    # Performance conversation windows (GPT-Live). Off by default; push-to-talk only.
    openai_live_model: str = "gpt-live-1"
    openai_enable_live_conversation: bool = False
    # ONE primary voice for the whole character. Empty = OPENAI_REALTIME_VOICE.
    # Must exist for both Realtime and GPT-Live (alloy, ash, ballad, coral, echo, sage, shimmer, verse, marin, cedar).
    openai_voice: str = ""
    openai_voice_language: str = "en"
    openai_audio_cache_enabled: bool = True
    local_audio_playback: bool = False
    # Voice character (docs/VOICE.md): off (default) | hertz_deadpan | hertz_buzzy | hertz_tin_can
    # A preset = base voice + delivery instructions + local audio processing + sound cues.
    # "off" = the current voice exactly (OPENAI_VOICE, original instructions, no processing).
    voice_preset: str = "off"
    voice_base_voice: str = ""  # override the preset's base voice (empty = preset default); also used live
    voice_fx_enabled: bool = True  # local post-processing (needs ffmpeg; falls back to the raw voice)
    voice_pitch_semitones: float | None = None  # override the preset (e.g. 3, 5, 7)
    voice_formant_semitones: float | None = None  # override the preset (keep well below the pitch)
    voice_rate: float | None = None  # override the preset speaking rate (1.0–1.12 is the useful range)
    voice_coloration: float | None = None  # multiplies the preset's synthetic coloration (0 = none, 1 = preset)
    voice_chirps: bool = True  # occasional tiny chirp before a clip (preset decides how often)
    voice_cues: bool = True  # short procedural character sounds at a few key moments
    voice_cue_volume: float | None = None  # 0..1; empty = preset default

    # Performance mode (`itu show`)
    show_data_dir: Path = Path("./data/performance")
    show_mode: str = "guided"  # guided (operator advances) | automatic
    show_display: str = "terminal"  # terminal | web (projector page on SHOW_WEB_HOST:SHOW_WEB_PORT)
    show_web_host: str = "127.0.0.1"
    show_web_port: int = 8765
    show_camera_source: str = ""  # empty = CAMERA_SOURCE
    show_camera_mirrored: bool = False  # true if the camera image is mirrored (selfie view)
    show_pose_preference: str = "sitting"  # sitting | standing | any — is there a chair for the participant?
    show_telegram_chat_id: int | None = None  # where a confirmed picture is sent (private chat or group)
    show_photo_retention_minutes: int = 60  # participant photos + compositions are deleted after this
    show_output_dir: Path = Path("./data/runtime/performance")
    show_motion_enabled: bool = False  # robot cues in the script; still needs all motion gates + /arm
    # The finale: "encounter" = the good boy encounter (docs/UX.md); "imitate" = copy a chosen painting
    show_finale: str = "encounter"
    show_stable_seconds: float = 3.0  # a pose held this long starts the encounter by itself
    show_final_hold_seconds: float = 10.0  # the robot holds its final pose this long AFTER settling
    show_encounter_photo: bool = True  # ask for a consented local THEN/NOW photo during the hold
    # presentation (beamer + robot): intro → one pose round per era → rescue finale. "show" = narrated show.
    show_programme: str = "presentation"
    show_camera_fallback: str = ""  # e.g. zhaw_bridge: if the laptop camera fails, read poses with the robot's
    # camera instead (no photo of person + robot then — the robot cannot photograph itself)
    # which camera reads the participant's pose: robot (the dog's own camera, via the bridge; falls back to the
    # laptop camera if it has none) | laptop. The laptop camera always takes the photo of the two together.
    show_pose_camera: str = "robot"
    show_live_always: bool = True  # the camera view stays beside the picture for the whole show
    show_operator_key: str = ""  # fixed key for the operator page link (empty = one kept in data/runtime/)
    show_round_hold_seconds: float = 6.0  # each round: the robot holds its pose this long after settling
    show_rescue_wait_seconds: float = 60.0  # finale: how long the robot waits for someone to lie down
    show_montage_flash_seconds: float = 2.8  # each remembered picture while the robot is busy
    # Camera-guided walking to the participant (`itu show --walk`). Off by default.
    show_walk_enabled: bool = False
    # set to true only after `itu show walk-test --calibrate <m>` (sitting AND standing) wrote SHOW_WALK_K_* here
    show_walk_calibrated: bool = False
    show_walk_camera: str = "zhaw_bridge"  # the ROBOT's camera (it must see the person from the robot)
    show_walk_speed: float = 0.22  # m/s, max (the adapter caps at 0.3)
    show_walk_max_distance_m: float = 3.5
    show_walk_hfov_deg: float = 90.0  # robot camera field of view
    show_walk_k_sitting: float = 0.8  # distance calibration: `itu show walk-test --calibrate 2`
    show_walk_k_standing: float = 1.1
    show_walk_beside_m: float = 0.65  # how far beside the person it stops
    show_walk_face: str = "person"  # at the end face: person | start

    # Talk mode (`itu talk`): live conversation with the dog via OpenAI Realtime
    talk_model: str = ""  # empty = OPENAI_REALTIME_MODEL
    talk_voice: str = ""  # empty = OPENAI_REALTIME_VOICE
    talk_half_duplex: bool = True  # true for an open speaker; false only with headset / echo-cancelling speakerphone
    talk_idle_seconds: float = 45
    talk_max_session_seconds: float = 300
    talk_input_device: str = ""  # sounddevice name or index; empty = system default
    talk_output_device: str = ""
    story_path: Path = Path("./data/persona/story.md")

    camera_source: str = "webcam"
    pose_model_path: Path = Path("./models/pose_landmarker_lite.task")

    artwork_data_dir: Path = Path("./data/artworks")
    meme_data_dir: Path = Path("./data/memes")
    # art | meme | both | surprise — what a visitor gets until they choose with /art /meme /surprise
    default_archive: str = "both"
    # A camera that can SEE the robot (not the robot's own camera), for consented imitation photos.
    # Empty = feature off. Same syntax as CAMERA_SOURCE (webcam:1, image:<path>, ...).
    imitation_camera_source: str = ""
    # Comma-separated behaviors allowed at start-up. Empty = defaults (everything except
    # look_aside and peer, which an operator must /approve after testing them on the robot).
    motion_allowed_behaviors: str = ""
    database_path: Path = Path("./data/runtime/is_this_us.sqlite3")
    audio_cache_dir: Path = Path("./data/runtime/audio_cache")
    card_font_path: str = ""

    @model_validator(mode="after")
    def _resolve_voice_settings(self):
        if self.voice_provider is None:
            legacy = self.openai_voice_provider.strip().lower()
            self.voice_provider = legacy if "openai_voice_provider" in self.model_fields_set else "elevenlabs"
            if self.voice_provider not in ("elevenlabs", "openai", "none"):
                raise ValueError("Voice provider must be elevenlabs, openai, or none")
        if self.voice_cache_enabled is None:
            self.voice_cache_enabled = self.openai_audio_cache_enabled
        return self

    @field_validator("telegram_operator_ids", mode="before")
    @classmethod
    def _parse_ids(cls, value: object) -> list[int]:
        if value in (None, ""):
            return []
        if isinstance(value, (list, tuple)):
            return [int(v) for v in value]
        return [int(part) for part in str(value).replace(" ", "").split(",") if part]

    @field_validator("show_telegram_chat_id", mode="before")
    @classmethod
    def _empty_none(cls, value: object):
        return None if value in ("", None) else value

    @field_validator("voice_pitch_semitones", "voice_formant_semitones", "voice_rate", "voice_coloration",
                     "voice_cue_volume", mode="before")
    @classmethod
    def _empty_float_none(cls, value: object):
        return None if value in ("", None) else value

    @property
    def character_voice(self) -> str:
        """The base voice of the active VOICE_PRESET (OPENAI_VOICE when the preset is off)."""
        from .speech.character import voice_for

        return voice_for(self)

    @property
    def voice_configured(self) -> bool:
        if self.voice_provider == "elevenlabs":
            return bool(self.elevenlabs_api_key.strip() and self.elevenlabs_voice_id.strip())
        return self.voice_provider == "openai" and self.azure_voice_configured

    @property
    def azure_voice_configured(self) -> bool:
        """Credentials for the separate Azure conversation experiments."""
        return bool(self.azure_openai_api_key.strip())

    def openai_client(self):
        from openai import AsyncOpenAI

        return AsyncOpenAI(api_key=self.azure_openai_api_key,
                           base_url=self.azure_openai_endpoint.rstrip("/") + "/openai/v1/")

    @property
    def primary_voice(self) -> str:
        return (self.openai_voice or self.openai_realtime_voice or "marin").strip()

    @property
    def real_robot_possible(self) -> bool:
        """True only when every static gate for real hardware is open.

        An operator must still arm the robot at runtime before any motion.
        """
        return (
            self.app_mode is AppMode.INSTALLATION
            and self.robot_model in (RobotModel.GO2, RobotModel.ZHAW_BRIDGE)
            and self.robot_motion_enabled
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
