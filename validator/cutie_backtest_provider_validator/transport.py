"""HTTP transport for the validator.

Two modes:
  - live: talk to a running provider over http(s) via a sync httpx.Client;
  - asgi: import a provider's FastAPI ``app`` and drive it in-process via
    httpx's ASGITransport. ASGITransport is async-only, so requests run through
    an httpx.AsyncClient inside a per-request asyncio loop, exposing the same
    sync interface as the live transport.

Both expose: ``get(path, token)`` and ``post_json(path, body, token)`` returning
a ``Response`` dataclass.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx


@dataclass
class Response:
    status_code: int
    json_body: Any
    json_error: Optional[str]  # set when body was not valid JSON
    text: str


def _to_response(resp: httpx.Response) -> Response:
    text = resp.text
    json_body: Any = None
    json_error: Optional[str] = None
    try:
        json_body = resp.json()
    except Exception as exc:  # noqa: BLE001 - any parse failure is reportable
        json_error = str(exc)
    return Response(
        status_code=resp.status_code,
        json_body=json_body,
        json_error=json_error,
        text=text,
    )


class ValidatorTransport:
    """Abstract base: subclasses implement get/post_json/close."""

    def get(self, path: str, token: Optional[str] = None) -> Response:
        raise NotImplementedError

    def post_json(self, path: str, body: Any, token: Optional[str] = None) -> Response:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    @staticmethod
    def _headers(token: Optional[str]) -> dict:
        return {"Authorization": f"Bearer {token}"} if token else {}

    # -- factories --------------------------------------------------------

    @classmethod
    def for_live(cls, base_url: str, timeout: float) -> "ValidatorTransport":
        return _LiveTransport(base_url, timeout)

    @classmethod
    def for_asgi(
        cls, module_path: str, app_attr: str, timeout: float
    ) -> "ValidatorTransport":
        app = _load_app(module_path, app_attr)
        return _AsgiTransport(app, timeout)


class _LiveTransport(ValidatorTransport):
    def __init__(self, base_url: str, timeout: float) -> None:
        self._client = httpx.Client(timeout=timeout)
        self._base_url = base_url.rstrip("/")

    def get(self, path: str, token: Optional[str] = None) -> Response:
        resp = self._client.get(self._base_url + path, headers=self._headers(token))
        return _to_response(resp)

    def post_json(self, path: str, body: Any, token: Optional[str] = None) -> Response:
        headers = self._headers(token)
        headers["Content-Type"] = "application/json"
        resp = self._client.post(self._base_url + path, json=body, headers=headers)
        return _to_response(resp)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


class _AsgiTransport(ValidatorTransport):
    def __init__(self, app: Any, timeout: float) -> None:
        self._app = app
        self._timeout = timeout
        self._base_url = "http://provider.local"

    def _run(self, coro_fn) -> Response:
        async def _runner() -> Response:
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(
                transport=transport, base_url=self._base_url, timeout=self._timeout
            ) as client:
                resp = await coro_fn(client)
                return _to_response(resp)

        return asyncio.run(_runner())

    def get(self, path: str, token: Optional[str] = None) -> Response:
        headers = self._headers(token)

        async def _do(client: httpx.AsyncClient):
            return await client.get(path, headers=headers)

        return self._run(_do)

    def post_json(self, path: str, body: Any, token: Optional[str] = None) -> Response:
        headers = self._headers(token)
        headers["Content-Type"] = "application/json"

        async def _do(client: httpx.AsyncClient):
            return await client.post(path, json=body, headers=headers)

        return self._run(_do)

    def close(self) -> None:
        return None


def _load_app(module_path: str, app_attr: str) -> Any:
    candidate = Path(module_path)
    if candidate.exists() and candidate.suffix == ".py":
        module_dir = str(candidate.resolve().parent)
        if module_dir not in sys.path:
            sys.path.insert(0, module_dir)
        module_name = candidate.stem
        module = importlib.import_module(module_name)
    else:
        module = importlib.import_module(module_path)
    app = getattr(module, app_attr, None)
    if app is None:
        raise RuntimeError(f"Module '{module_path}' has no attribute '{app_attr}'")
    return app
