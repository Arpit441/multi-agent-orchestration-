"""HTTP API package."""

__all__ = ["app", "create_app"]


def __getattr__(name: str):
    if name in {"app", "create_app"}:
        from agent_orchestrator.api.app import app, create_app

        return app if name == "app" else create_app
    raise AttributeError(name)
