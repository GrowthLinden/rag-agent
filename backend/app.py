from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import os
import sys

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Keep these imports after the path setup so both `python backend/app.py`
# and `uvicorn backend.app:app` work from a fresh checkout.
import api as api_module  # noqa: E402
from auth import get_password_hash  # noqa: E402
from database import SessionLocal, init_db  # noqa: E402
from models import User  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"


def _is_production() -> bool:
    return os.getenv("ENVIRONMENT", os.getenv("APP_ENV", "development")).strip().lower() in {"prod", "production"}


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _list_env(name: str, default: str = "") -> list[str]:
    value = os.getenv(name, default)
    return [item.strip() for item in value.split(",") if item.strip()]


def _cors_origins() -> list[str]:
    origins = _list_env("CORS_ALLOW_ORIGINS", "" if _is_production() else "*")
    if _is_production() and (not origins or "*" in origins):
        raise RuntimeError("生产环境必须配置 CORS_ALLOW_ORIGINS，且不能使用通配符 *")
    return origins or ["*"]


def _ensure_bootstrap_admin() -> None:
    username = os.getenv("BOOTSTRAP_ADMIN_USERNAME", "").strip()
    password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "").strip()
    if not username and not password:
        return
    if not username or not password:
        raise RuntimeError("BOOTSTRAP_ADMIN_USERNAME 和 BOOTSTRAP_ADMIN_PASSWORD 必须同时配置")

    db = SessionLocal()
    try:
        if db.query(User).filter(User.role == "admin").first():
            return

        user = db.query(User).filter(User.username == username).first()
        if user:
            user.role = "admin"
            user.password_hash = get_password_hash(password)
        else:
            user = User(username=username, password_hash=get_password_hash(password), role="admin")
            db.add(user)
        db.commit()
    finally:
        db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="SuperService AI Customer Service API")
    cors_origins = _cors_origins()

    @app.on_event("startup")
    async def _startup_init_db():
        init_db()
        _ensure_bootstrap_admin()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=_bool_env("CORS_ALLOW_CREDENTIALS", False) and "*" not in cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # No-cache middleware for development
    @app.middleware("http")
    async def _no_cache(request, call_next):
        response = await call_next(request)
        path = request.url.path or ""
        if path == "/" or path.endswith((".html", ".js", ".css")):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    app.include_router(api_module.router)

    data_dir = BASE_DIR / "data"
    if data_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(data_dir)), name="assets")

    # serve frontend static files at root
    if FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", 8000)))
