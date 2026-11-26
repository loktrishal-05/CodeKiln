from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CODEKILN_", env_file=".env", extra="ignore")

    max_budget_usd: float = 5.0
    db_path: str = "codekiln.sqlite"
    sandbox_memory: str = "4g"
    sandbox_cpus: int = 2
    sandbox_timeout_s: int = 120
    flake_runs: int = 3
    mutant_kill_threshold: float = 0.8
