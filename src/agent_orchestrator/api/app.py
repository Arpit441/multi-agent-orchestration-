"""FastAPI application entrypoint."""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from agent_orchestrator.api.auth import auth_enabled, is_authenticated
from agent_orchestrator.api.rate_limit import limiter
from agent_orchestrator.api.routes import public_router, router
from agent_orchestrator.api import runners as runner_registry
from agent_orchestrator.api.settings import get_settings
from agent_orchestrator.core.runner import GraphRunner
from agent_orchestrator.examples.research_report_pipeline import build_research_report_graph
from agent_orchestrator.examples.support_resolution_pipeline import (
    build_support_resolution_graph,
)
from agent_orchestrator.llm import GeminiClient, set_gemini_client
from agent_orchestrator.persistence import SQLiteStore
from agent_orchestrator.knowledge import get_knowledge_store, set_knowledge_store, KnowledgeStore

import agent_orchestrator.nodes  # noqa: F401

load_dotenv(override=True)

STATIC_DIR = Path(__file__).resolve().parent.parent / "trace_viewer" / "static"


def _asset_version() -> str:
    """Bust browser cache whenever any static UI file changes."""
    newest = 0
    for name in ("index.html", "app.js", "styles.css"):
        path = STATIC_DIR / name
        if path.exists():
            newest = max(newest, int(path.stat().st_mtime))
    return str(newest or int(os.environ.get("STATIC_ASSET_VERSION", "1")))


def _render_index_html() -> str:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    ver = _asset_version()
    html = re.sub(r"/static/styles\.css(\?v=\d+)?", f"/static/styles.css?v={ver}", html)
    html = re.sub(r"/static/app\.js(\?v=\d+)?", f"/static/app.js?v={ver}", html)
    return html


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.gemini_api_key:
        os.environ["GEMINI_API_KEY"] = settings.gemini_api_key
    if settings.gemini_model:
        os.environ["GEMINI_MODEL"] = settings.gemini_model

    set_gemini_client(
        GeminiClient(api_key=settings.gemini_api_key or None, model=settings.gemini_model)
    )

    set_knowledge_store(KnowledgeStore(settings.knowledge_db_path))
    _ = get_knowledge_store()  # ensure ready

    store = SQLiteStore(settings.db_path)
    runners = {
        "research_report": GraphRunner(build_research_report_graph(), store=store),
        "support_resolution": GraphRunner(build_support_resolution_graph(), store=store),
    }
    runner_registry.set_runners(runners, store)

    app = FastAPI(
        title="Agent Orchestrator",
        description="Multi-agent orchestration framework with Gemini, checkpoints, and traces",
        version="0.2.0",
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins if origins != ["*"] else ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(public_router)
    app.include_router(router)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.middleware("http")
        async def _no_cache_static(request: Request, call_next):
            response = await call_next(request)
            path = request.url.path
            if path == "/" or path.startswith("/static/") or path == "/login":
                response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
            return response

        @app.get("/login")
        async def login_page() -> FileResponse:
            return FileResponse(
                STATIC_DIR / "login.html",
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )

        @app.get("/", response_model=None)
        async def index(request: Request):
            if auth_enabled(settings) and not is_authenticated(request, settings):
                return RedirectResponse(url="/login", status_code=302)
            return HTMLResponse(
                content=_render_index_html(),
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )

    @app.on_event("startup")
    async def _startup() -> None:
        if auth_enabled(settings):
            if settings.session_secret in {"", "change-me-in-production"}:
                print(
                    "WARNING: Set SESSION_SECRET to a long random value for production auth."
                )
        else:
            print(
                "WARNING: APP_PASSWORD is empty — API/UI are open. Set APP_PASSWORD for public deploy."
            )

        if hasattr(store, "list_runs"):
            for row in await store.list_runs(limit=100):  # type: ignore[attr-defined]
                if row.get("status") == "RUNNING":
                    run = await store.load_run(row["run_id"])
                    if run:
                        note = "Process restarted; call resume to continue."
                        run.error = f"{run.error} | {note}" if run.error else note
                        await store.save_run(run)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "agent_orchestrator.api.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
