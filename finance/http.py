from __future__ import annotations

import httpx

from .config import HTTP_TIMEOUT, USER_AGENT


def client(**kwargs) -> httpx.AsyncClient:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.9"}
    headers.update(kwargs.pop("headers", {}))
    return httpx.AsyncClient(
        headers=headers,
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
        http2=True,
        **kwargs,
    )
