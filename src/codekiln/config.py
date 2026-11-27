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
    billing_ledger: str = "billing_ledger.csv"
    # USD per 1k tokens; defaults are gpt-4o-mini rates, override per provider.
    llm_input_usd_per_1k: float = 0.00015
    llm_output_usd_per_1k: float = 0.0006
