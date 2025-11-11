import os
from pathlib import Path


class Settings:
    """Application configuration with sensible defaults and ENV overrides."""

    # Upstream live stream URL (proxied to clients and used for ingest)
    STREAM_URL: str = os.getenv("STREAM_URL", "http://157.91.66.31:8888/wfyi-hd1")

    # Where to store rolling MP3 segments
    BUFFER_DIR: Path = Path(os.getenv("BUFFER_DIR", "storage/segments")).resolve()

    # Segment/Buffer timing
    SEGMENT_SECONDS: int = int(os.getenv("SEGMENT_SECONDS", "2"))
    BUFFER_MINUTES: int = int(os.getenv("BUFFER_MINUTES", "12"))
    # Keep a little extra to avoid gaps during concat
    CLEANUP_MARGIN_MINUTES: int = int(os.getenv("CLEANUP_MARGIN_MINUTES", "2"))

    # Max minutes allowed for download endpoint
    MAX_DOWNLOAD_MINUTES: int = int(os.getenv("MAX_DOWNLOAD_MINUTES", "30"))

    # FFmpeg path (ensure ffmpeg is installed and accessible on PATH)
    FFMPEG_PATH: str = os.getenv("FFMPEG_PATH", "ffmpeg")
    # Audio bitrate used for MP3 segments (in bits per second)
    AUDIO_BITRATE: int = int(os.getenv("AUDIO_BITRATE", "128000"))

    # Backend server config
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))

    # CORS configuration for the frontend (adjust as needed)
    CORS_ORIGINS: list[str] = (
        os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
        .split(",")
    )


settings = Settings()

# Ensure buffer directory exists
settings.BUFFER_DIR.mkdir(parents=True, exist_ok=True)

