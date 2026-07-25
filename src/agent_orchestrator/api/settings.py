"""Application settings."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-lite-latest"
    database_path: str = "data/orchestrator.db"
    knowledge_db_path: str = "data/knowledge.db"
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: str = "*"

    # Auth: when APP_PASSWORD is set, login/API key is required for protected routes.
    app_password: str = ""
    api_key: str = ""
    session_secret: str = "change-me-in-production"
    cookie_secure: bool = False  # set True behind HTTPS

    # Rate limits (SlowAPI strings)
    rate_limit_runs: str = "5/hour"
    rate_limit_login: str = "10/minute"
    rate_limit_mutate: str = "30/minute"

    @property
    def db_path(self) -> Path:
        return Path(self.database_path)


def get_settings() -> Settings:
    return Settings()
