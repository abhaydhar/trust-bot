from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"
    neo4j_max_connection_lifetime: int = 3600
    neo4j_max_connection_pool_size: int = 50
    neo4j_connection_acquisition_timeout: int = 60
    neo4j_connection_timeout: int = 30
    neo4j_max_transaction_retry_time: int = 30
    neo4j_keep_alive: bool = True

    litellm_api_base: str = ""
    litellm_api_key: str = ""
    litellm_model: str = "gpt-4o"
    litellm_embedding_model: str = "text-embedding-3-small"

    codebase_root: Path = Path("./sample_codebase")
    chroma_persist_dir: Path = Path("./data/chromadb")

    log_level: str = "INFO"
    server_port: int = 7860

    # Session / state persistence — env vars: TRUSTBOT_STORAGE_SECRET, TRUSTBOT_SESSION_MAX_AGE_DAYS
    trustbot_storage_secret: str = ""
    trustbot_session_max_age_days: int = 7

    @property
    def storage_secret(self) -> str:
        return self.trustbot_storage_secret

    @property
    def session_max_age_days(self) -> int:
        return self.trustbot_session_max_age_days

    # Validation tuning
    max_concurrent_llm_calls: int = 5
    function_context_buffer_lines: int = 10
    max_function_lines_for_llm: int = 500

    # Agentic mode — env var: TRUSTBOT_AGENTIC_MODE
    trustbot_agentic_mode: str = "llm"
    trustbot_llm_temperature: float = 0.1
    trustbot_llm_max_tokens: int = 4096

    @property
    def agentic_mode(self) -> str:
        return self.trustbot_agentic_mode

    @property
    def llm_temperature(self) -> float:
        return self.trustbot_llm_temperature

    @property
    def llm_max_tokens(self) -> int:
        return self.trustbot_llm_max_tokens

    # Multi-agent / job queue
    redis_url: str = "redis://localhost:6379/0"
    enable_celery: bool = False
    enable_browser_tool: bool = False

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    def get_litellm_kwargs(self) -> dict:
        """Return common kwargs for all LiteLLM calls (api_base, api_key)."""
        kwargs: dict = {}
        if self.litellm_api_base:
            kwargs["api_base"] = self.litellm_api_base
        if self.litellm_api_key:
            kwargs["api_key"] = self.litellm_api_key
        return kwargs


settings = Settings()
