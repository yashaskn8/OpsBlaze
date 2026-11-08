from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "postgres"
    postgres_password: str
    postgres_db: str = "opsblaze"

    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_db_file: str = ".local/temporal.db"

    @property
    def database_url(self) -> URL:
        # URL.create escapes special characters in the password (e.g. '@').
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
