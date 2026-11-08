import time
from uuid import uuid4

from fastapi import FastAPI, Request
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from app.core.logging import configure_http_logger


def install_request_logging(app: FastAPI) -> None:
    logger = configure_http_logger()

    @app.middleware("http")
    async def log_http_request(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = str(uuid4())
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            route = getattr(request.scope.get("route"), "path", None) or "unmatched"
            logger.info(
                "http_request_completed",
                extra={
                    "http_fields": {
                        "request_id": request_id,
                        "method": request.method,
                        "route": route,
                        "status_code": status_code,
                        "duration_ms": max(0.0, (time.perf_counter() - started) * 1000),
                    }
                },
            )
