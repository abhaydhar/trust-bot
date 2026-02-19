from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"

    litellm_api_base: str = ""
    litellm_api_key: str = ""
    litellm_model: str = "gpt-4o"
    litellm_embedding_model: str = "text-embedding-3-small"

    codebase_root: Path = Path("./sample_codebase")
    chroma_persist_dir: Path = Path("./data/chromadb")

    log_level: str = "INFO"
    server_port: int = 7860

    # Validation tuning
    max_concurrent_llm_calls: int = 5
    function_context_buffer_lines: int = 10
    max_function_lines_for_llm: int = 500

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
