"""app/middleware/audit.py — Field-level audit snapshot middleware"""
import json
import time
from typing import Callable
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class AuditMiddleware(BaseHTTPMiddleware):
    """
    Lightweight middleware that tags requests with timing + user context.
    Actual audit writes happen in service layer for field-level granularity.
    """

    SKIP_PATHS = {"/health", "/api/v1/auth/login", "/api/docs"}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path in self.SKIP_PATHS:
            return await call_next(request)

        start = time.time()
        request.state.audit_start = start
        request.state.client_ip = self._get_ip(request)

        response = await call_next(request)
        return response

    def _get_ip(self, request: Request) -> str:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"
