from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import ai, auth, intel, learning, local, mt5_agent, system, trading
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.services import monitoring

configure_logging()
monitoring.install_error_counter("api")
log = logging.getLogger("api")
settings = get_settings()

app = FastAPI(title="RR Trading Engine API", version="1.0.0",
              docs_url="/api/docs" if settings.environment != "production" else None, openapi_url="/api/openapi.json")
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
                   allow_credentials=True, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                   allow_headers=["Content-Type", "X-CSRF-Token"])


@app.middleware("http")
async def security_headers(request: Request, call_next):
    t0 = time.perf_counter()
    resp = await call_next(request)
    # Parts 27-30 Phase 7: per-minute request count, latency and 5xx rate (best effort)
    monitoring.record_request((time.perf_counter() - t0) * 1000, resp.status_code)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    try:                                   # master upgrade: unhandled errors become (de-duplicated) incidents
        from app.db.session import session_scope
        from app.services import incidents
        route = getattr(request.scope.get("route"), "path", request.url.path)
        with session_scope() as db:
            incidents.record(db, "api", "error", f"{request.method} {route}: {type(exc).__name__}: {str(exc)[:300]}",
                             {"path": route, "method": request.method})
    except Exception:  # noqa: BLE001 - reporting must never hide the original error response
        log.warning("incident for the unhandled error not recorded", exc_info=True)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


for r in (auth.router, auth.users, trading.router, intel.router, system.router, learning.learning, learning.intel,
          mt5_agent.router, local.router, ai.router):
    app.include_router(r)
