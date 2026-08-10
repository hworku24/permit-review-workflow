"""Configuration, loaded from the environment with .env as a fallback.

Defaults point at the local docker-compose stack and the offline AI path so a fresh
clone runs with no credentials at all, which is NFR-06.
"""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

# The department's business day is a local calendar day. Every date cast in this codebase
# and in sql/001_schema.sql resolves against this zone.
DEPARTMENT_TZ = ZoneInfo("America/New_York")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    permitflow_db_url: str = "postgresql://permitflow:permitflow@localhost:5433/permitflow"

    licensing_db_url: str = (
        "postgresql://permitflow_ro:readonly@localhost:5434/licensing_replica"
    )
    licensing_db_timeout_seconds: float = 10.0

    property_records_wsdl: str = "http://localhost:8081/property-records?wsdl"
    property_records_timeout_seconds: float = 10.0

    integration_max_attempts: int = 3
    integration_backoff_base_seconds: float = 1.0
    circuit_failure_threshold: int = 5
    circuit_reset_seconds: float = 60.0

    ai_provider: str = "offline"
    ai_model: str = "offline-rules-v1"
    anthropic_api_key: str | None = None

    # AI-02: a field scoring below this is presented blank rather than as a guess.
    ai_field_confidence_threshold: float = 0.70
    # AI-06: retrieval below this is treated as no support and the answer is withheld.
    ai_grounding_threshold: float = 0.35


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
