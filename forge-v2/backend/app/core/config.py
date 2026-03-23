"""Application configuration."""
from __future__ import annotations

import json
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_SECRET_KEY = "CHANGE-IN-PRODUCTION-32-CHAR-SECRET"


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://forge:forge@localhost/forgeims"
    SECRET_KEY: str = DEFAULT_SECRET_KEY
    ALLOWED_ORIGINS: List[str] = ["http://localhost:3000"]
    ALLOWED_HOSTS: List[str] = ["*"]
    SESSION_TTL_SECONDS: int = 28800
    LOGIN_MAX_ATTEMPTS: int = 10
    LOGIN_WINDOW_SECONDS: int = 300
    LOGIN_LOCKOUT_SECONDS: int = 900
    LOGIN_FAILURE_DELAY_MS: int = 350
    TOKEN_REVOKE_CACHE_MAX: int = 10000
    API_DOCS_ENABLED: bool = True
    APP_ENV: str = "development"
    FORCE_HTTPS_HEADERS: bool = False

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    @field_validator("ALLOWED_ORIGINS", "ALLOWED_HOSTS", mode="before")
    @classmethod
    def _parse_list(cls, value):
        if isinstance(value, list):
            return value
        if value is None:
            return []
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return parsed
            return [item.strip() for item in stripped.split(",") if item.strip()]
        raise TypeError("Expected a list or comma-separated string")

    def validate_runtime(self) -> None:
        env = (self.APP_ENV or "development").lower()
        if env in {"prod", "production"}:
            if self.SECRET_KEY == DEFAULT_SECRET_KEY or len(self.SECRET_KEY) < 32:
                raise RuntimeError(
                    "SECRET_KEY must be set to a unique value with at least 32 characters in production."
                )
        elif len(self.SECRET_KEY) < 16:
            raise RuntimeError("SECRET_KEY must be at least 16 characters long.")


settings = Settings()
