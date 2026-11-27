from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CODEKILN_", env_file=".env", extra="ignore")

    max_budget_usd: float = 5.0
    db_path: str = "codekiln.sqlite"
    # OpenAI-compatible chat endpoint; the key stays on the host (never in a container).
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    sandbox_memory: str = "4g"
    sandbox_cpus: int = 2
    sandbox_timeout_s: int = 120
    flake_runs: int = 3
    mutant_kill_threshold: float = 0.8
