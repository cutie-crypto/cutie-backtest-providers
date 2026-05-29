"""FastAPI app exposing the Cutie BYO backtest provider contract.

Endpoints (IMPL §4.1):
    GET  /health          - no auth; must not leak secrets / local paths
    GET  /catalog         - Bearer auth; cutie.backtest_provider_catalog.v1
    POST /cutie/backtest  - Bearer auth; cutie.external_backtest.request/response.v1
    GET  /reports/{name}  - serves a local report file (local_machine_only)

You normally only edit ``adapter.py``; this module wires the contract together.

Run:
    CUTIE_BACKTEST_PROVIDER_TOKEN="local-dev-token" \
      python -m cutie_byo_provider.app
    # or
    CUTIE_BACKTEST_PROVIDER_TOKEN="local-dev-token" \
      uvicorn cutie_byo_provider.app:app --host 127.0.0.1 --port 8767
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import adapter, reports, security, settings
from .contract import (
    CATALOG_SCHEMA,
    RESPONSE_SCHEMA,
    BacktestRequest,
    BacktestResult,
    CatalogResponse,
    ProviderInfo,
    business_failure,
    success_response,
)

logger = logging.getLogger("cutie_byo_provider")

app = FastAPI(title="Cutie BYO Backtest Provider", version="1.0.0")


@app.on_event("startup")
async def _startup_warning() -> None:
    if not settings.PROVIDER_TOKEN:
        logger.warning(
            "CUTIE_BACKTEST_PROVIDER_TOKEN not set — running without authentication "
            "(dev mode). Set the token before exposing this provider."
        )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _verify_bearer(authorization: Optional[str]) -> None:
    """Validate the Bearer token. Raises 401 on mismatch (IMPL §4.1)."""
    if not settings.PROVIDER_TOKEN:
        return  # dev mode: accept anything
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    parts = authorization.split(" ", 1)
    if (
        len(parts) != 2
        or parts[0].lower() != "bearer"
        or parts[1] != settings.PROVIDER_TOKEN
    ):
        raise HTTPException(status_code=401, detail="Invalid token")


# ---------------------------------------------------------------------------
# GET /health  (no auth; must not leak secrets / paths — IMPL §4.1, §8.1)
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> JSONResponse:
    payload = {
        "ok": True,
        "provider_id": adapter.PROVIDER_ID,
        "engine_name": adapter.ENGINE_NAME,
        "engine_version": adapter.engine_version(),
        "data_ready": True,
        "checked_at": int(time.time()),
    }
    # Defense in depth: never let an adapter leak a secret/path through health.
    return JSONResponse(security.scrub(payload))


# ---------------------------------------------------------------------------
# GET /catalog  (Bearer auth — IMPL §5.1)
# ---------------------------------------------------------------------------


@app.get("/catalog")
async def catalog(authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    _verify_bearer(authorization)
    response = CatalogResponse(
        schema=CATALOG_SCHEMA,
        provider=ProviderInfo(
            provider_id=adapter.PROVIDER_ID,
            provider_name=adapter.PROVIDER_NAME,
            provider_version=adapter.PROVIDER_VERSION,
            homepage_url=adapter.PROVIDER_HOMEPAGE_URL,
            maintainer=adapter.PROVIDER_MAINTAINER,
        ),
        tools=adapter.list_tools(),
    )
    payload = response.model_dump(by_alias=True, exclude_none=True)
    # Final scrub before it leaves the process (IMPL §8.4).
    return JSONResponse(security.scrub(payload))


# ---------------------------------------------------------------------------
# POST /cutie/backtest  (Bearer auth — IMPL §6)
# ---------------------------------------------------------------------------


@app.post("/cutie/backtest")
async def cutie_backtest(
    request: Request, authorization: Optional[str] = Header(default=None)
) -> JSONResponse:
    _verify_bearer(authorization)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "schema": RESPONSE_SCHEMA,
                "result_status": "failed",
                "provider_name": adapter.PROVIDER_NAME,
                "error_type": "INVALID_REQUEST",
                "error_message": "Request body must be valid JSON",
            },
        )

    try:
        bt_request = BacktestRequest.model_validate(body)
    except Exception as exc:
        return JSONResponse(
            status_code=200,
            content={
                "schema": RESPONSE_SCHEMA,
                "result_status": "failed",
                "provider_name": adapter.PROVIDER_NAME,
                "error_type": "INVALID_REQUEST",
                "error_message": f"Request does not match schema: {exc}",
            },
        )

    try:
        result = adapter.run_backtest(bt_request)
    except Exception as exc:  # adapter raised -> ENGINE_ERROR (IMPL §6.3)
        logger.exception("Adapter run_backtest failed")
        return JSONResponse(
            content=business_failure(
                error_type="ENGINE_ERROR",
                error_message=str(exc),
                provider_name=adapter.PROVIDER_NAME,
                engine_name=adapter.ENGINE_NAME,
                engine_version=adapter.engine_version(),
                data_source=adapter.DATA_SOURCE,
            )
        )

    # Adapter returned a business-failure dict.
    if isinstance(result, dict):
        return JSONResponse(content=security.scrub(result))

    if not isinstance(result, BacktestResult):
        return JSONResponse(
            content=business_failure(
                error_type="ENGINE_ERROR",
                error_message="adapter returned an unexpected result type",
                provider_name=adapter.PROVIDER_NAME,
            )
        )

    # Normalize report_url to a relative path/ref (IMPL §7).
    if result.report_url is not None:
        result.report_url = security.scrub_report_url(result.report_url)

    payload = success_response(
        provider_name=adapter.PROVIDER_NAME,
        engine_name=adapter.ENGINE_NAME,
        engine_version=adapter.engine_version(),
        data_source=adapter.DATA_SOURCE,
        result=result,
    )
    return JSONResponse(content=security.scrub(payload))


# ---------------------------------------------------------------------------
# GET /reports/{filename}  (local report serving — IMPL §7)
# ---------------------------------------------------------------------------


@app.get("/reports/{filename}")
async def serve_report(filename: str):
    # Reject path traversal.
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = reports.report_path(filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    media_type = "application/json" if path.suffix == ".json" else "text/html"
    return FileResponse(str(path), media_type=media_type)


# ---------------------------------------------------------------------------
# Exception handlers — every response must be JSON in the v1 envelope.
# ---------------------------------------------------------------------------


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    # 401/403 from Bearer auth -> AUTH_FAILED; other HTTP errors -> INVALID_REQUEST.
    error_type = "AUTH_FAILED" if exc.status_code in (401, 403) else "INVALID_REQUEST"
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "schema": RESPONSE_SCHEMA,
            "result_status": "failed",
            "provider_name": adapter.PROVIDER_NAME,
            "error_type": error_type,
            "error_message": exc.detail,
        },
    )


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception")
    return JSONResponse(
        status_code=500,
        content={
            "schema": RESPONSE_SCHEMA,
            "result_status": "failed",
            "provider_name": adapter.PROVIDER_NAME,
            "error_type": "ENGINE_ERROR",
            "error_message": str(exc),
        },
    )


def main() -> None:
    import uvicorn

    uvicorn.run(
        "cutie_byo_provider.app:app",
        host=settings.HOST,
        port=settings.PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
