"""Client for the wrapped LLM. Speaks the OpenAI chat-completions protocol."""

from typing import Any

import httpx


class UpstreamError(Exception):
    """The upstream answered with an error, or could not be reached."""

    def __init__(self, status_code: int, body: Any):
        super().__init__(f"upstream error {status_code}")
        self.status_code = status_code
        self.body = body


class UpstreamClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # A trailing slash makes httpx append "chat/completions" instead of replacing the last
        # path segment.
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            timeout=timeout_s,
            transport=transport,
        )

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._client.post("chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise UpstreamError(
                502, {"error": {"message": f"upstream unreachable: {exc}"}}
            ) from exc
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = {"error": {"message": resp.text}}
            raise UpstreamError(resp.status_code, body)
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()
