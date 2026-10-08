"""Runtime settings, read from environment variables or a local .env file."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Upstream LLM: any OpenAI-compatible endpoint. Default is Gemini's compatibility layer.
    upstream_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    upstream_api_key: SecretStr = SecretStr("")
    upstream_model: str = "gemini-2.5-flash"
    # Single-tenant: ignore the model the client asks for and always use upstream_model.
    override_model: bool = True
    upstream_timeout_s: float = 60.0

    # Policies
    policy_dir: Path = Path("policies")
    enabled_policies: str = "all"  # "all" or a comma-separated list of policy names

    # Optional auth for clients of the proxy. Empty = open (fine for local dev).
    api_keys: str = ""

    # Include a "dwarpal" field with guard results in every response body.
    expose_trace: bool = True
    refusal_message: str = "Sorry, I can't help with that request."

    # PR-04: the Gradio demo at /demo (needs the `demo` extra). It calls this proxy through
    # the OpenAI SDK; empty demo_proxy_url = this process, http://127.0.0.1:$PORT/v1.
    demo_enabled: bool = True
    demo_proxy_url: str = ""

    # Baked into the Docker image at build time so /healthz can prove deployed == main.
    git_sha: str = "dev"

    # PR-05: budget protection for the public demo (dwarpal/limits.py). 0 = off; the Docker
    # image turns both on. Trust X-Forwarded-For only behind a proxy that sets it (HF Spaces).
    rate_limit_per_minute: int = 0
    daily_request_cap: int = 0
    trust_forwarded_for: bool = False

    @property
    def enabled_policy_names(self) -> set[str] | None:
        if self.enabled_policies.strip().lower() == "all":
            return None
        return {p.strip() for p in self.enabled_policies.split(",") if p.strip()}

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
