"""Application configuration loaded from environment variables / `.env`."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from typing import Annotated


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

    telegram_bot_token: str = ""
    telegram_operator_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    telegram_bot_username: str = ""
    pairing_token_ttl_minutes: int = 10
    session_ttl_minutes: int = 30

    # Azure OpenAI (v1 API). Model settings below are Azure deployment names.
    azure_openai_api_key: str = ""
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
    # Camera-guided walking to the participant (`itu show --walk`). Off by default.
    show_walk_enabled: bool = False
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

    @property
    def voice_configured(self) -> bool:
        return bool(self.azure_openai_api_key) and self.openai_voice_provider.strip().lower() == "openai"

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
