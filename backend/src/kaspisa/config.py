"""Настройки приложения из переменных окружения."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KASPISA_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://kaspisa:kaspisa@localhost:5432/kaspisa"
    redis_url: str = "redis://localhost:6379/0"
    # Режим «без записи»: бот считает цены, но не отправляет их в Kaspi.
    dry_run: bool = True
