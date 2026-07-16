from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass




def _int_env(env: dict[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer") from exc
    if value <= 0:
        raise ConfigError(f"{key} must be positive")
    return value


@dataclass(frozen=True)
class AppConfig:
    deepseek_api_key: str | None
    deepseek_model: str
    spotify_client_id: str | None
    spotify_client_secret: str | None
    spotify_refresh_token: str | None
    spotify_device_name: str
    public_base_url: str
    icecast_url: str
    icecast_status_url: str | None
    liquidsoap_control_socket: Path
    db_path: Path
    media_dir: Path
    recordings_dir: Path
    podcast_window_sec: int
    music_window_sec: int

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "AppConfig":
        source = os.environ if env is None else env
        podcast_minutes = _int_env(source, "PODCAST_WINDOW_MINUTES", 20)
        music_minutes = _int_env(source, "MUSIC_WINDOW_MINUTES", 10)
        public_base = source.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
        return cls(
            deepseek_api_key=source.get("DEEPSEEK_API_KEY") or None,
            deepseek_model=source.get("DEEPSEEK_MODEL", "deepseek-chat"),
            spotify_client_id=source.get("SPOTIFY_CLIENT_ID") or None,
            spotify_client_secret=source.get("SPOTIFY_CLIENT_SECRET") or None,
            spotify_refresh_token=source.get("SPOTIFY_REFRESH_TOKEN") or None,
            spotify_device_name=source.get("SPOTIFY_DEVICE_NAME", "Radai VPS"),
            public_base_url=public_base,
            icecast_url=source.get("ICECAST_URL", f"{public_base}/radio.mp3"),
            icecast_status_url=source.get("ICECAST_STATUS_URL") or None,
            liquidsoap_control_socket=Path(source.get("LIQUIDSOAP_CONTROL_SOCKET", "/tmp/radai-liquidsoap.sock")),
            db_path=Path(source.get("RADAI_DB_PATH", "./data/radai.sqlite3")),
            media_dir=Path(source.get("RADAI_MEDIA_DIR", "./data/media")),
            recordings_dir=Path(source.get("RADAI_RECORDINGS_DIR", "./data/recordings")),
            podcast_window_sec=podcast_minutes * 60,
            music_window_sec=music_minutes * 60,
        )

    def require_deepseek(self) -> str:
        if not self.deepseek_api_key:
            raise ConfigError("DEEPSEEK_API_KEY is required")
        return self.deepseek_api_key


    def require_spotify(self) -> tuple[str, str, str]:
        missing = [
            name
            for name, value in (
                ("SPOTIFY_CLIENT_ID", self.spotify_client_id),
                ("SPOTIFY_CLIENT_SECRET", self.spotify_client_secret),
                ("SPOTIFY_REFRESH_TOKEN", self.spotify_refresh_token),
            )
            if not value
        ]
        if missing:
            raise ConfigError(", ".join(missing) + " required")
        return self.spotify_client_id or "", self.spotify_client_secret or "", self.spotify_refresh_token or ""
