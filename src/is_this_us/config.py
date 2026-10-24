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
    GO2 = "go2"


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

    telegram_bot_token: str = ""
    telegram_operator_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    telegram_bot_username: str = ""
    pairing_token_ttl_minutes: int = 10
    session_ttl_minutes: int = 30

    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model_id: str = "eleven_multilingual_v2"
    local_audio_playback: bool = False

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

    @property
    def elevenlabs_configured(self) -> bool:
        return bool(self.elevenlabs_api_key and self.elevenlabs_voice_id)

    @property
    def real_robot_possible(self) -> bool:
        """True only when every static gate for real hardware is open.

        An operator must still arm the robot at runtime before any motion.
        """
        return (
            self.app_mode is AppMode.INSTALLATION
            and self.robot_model is RobotModel.GO2
            and self.robot_motion_enabled
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
