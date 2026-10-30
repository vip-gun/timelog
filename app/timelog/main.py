"""Application factory. Run with:  uvicorn --factory timelog.main:create_app"""
import logging
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import backup
from .config import Settings, get_settings, set_settings
from .db import Base, SessionLocal, ensure_columns, init_engine
from .routes import admin, approvals, auth, home, oncall, overtime, payroll, submission, team
from .security import LoginRequired
from .seed import seed
from .web import BASE_DIR, render

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'; base-uri 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = settings or get_settings()
    set_settings(s)
    engine = init_engine(s.database_url)
    Base.metadata.create_all(engine)
    for added in ensure_columns(engine):
        logging.getLogger(__name__).info("Schema upgrade: added column %s", added)
    with SessionLocal() as db:
        seed(db, s)

    if s.backup_scheduler:
        backup.encrypt_legacy(s.backup_dir, backup.passphrase_of(s))

        def current_schedule() -> backup.Schedule:
            with SessionLocal() as sdb:
                return backup.load_schedule(sdb)

        backup.start_scheduler(engine, s.backup_dir, current_schedule, backup.passphrase_of(s))

    app = FastAPI(title=s.app_name, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            if k == "Cache-Control" and request.url.path.startswith("/static/"):
                continue
            response.headers.setdefault(k, v)
        return response

    app.add_middleware(
        SessionMiddleware,
        secret_key=s.secret_key,
        session_cookie="timelog_session",
        max_age=s.session_max_age,
        same_site="lax",
        https_only=s.session_cookie_secure,
    )
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    for module in (auth, home, oncall, overtime, approvals, submission, payroll, team, admin):
        app.include_router(module.router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        return render(request, "error.html", {"status": exc.status_code, "detail": exc.detail}, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        return render(request, "error.html", {"status": 400, "detail": "Invalid request parameters."}, 400)

    return app
