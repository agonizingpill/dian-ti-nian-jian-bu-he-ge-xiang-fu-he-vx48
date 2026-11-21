import time
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from ..db import connection

class AuditRequestMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        started = time.time()
        response = await call_next(request)
        response.headers["X-Process-Time-ms"] = str(round((time.time()-started)*1000, 2))
        return response
