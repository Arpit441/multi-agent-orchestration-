"""Registry of named graph runners sharing one persistence store."""

from __future__ import annotations

from agent_orchestrator.core.runner import GraphRunner, PersistenceStore, RunRecord

_runners: dict[str, GraphRunner] = {}
_store: PersistenceStore | None = None


def set_runners(runners: dict[str, GraphRunner], store: PersistenceStore) -> None:
    global _runners, _store
    _runners = dict(runners)
    _store = store


def list_pipeline_ids() -> list[str]:
    return sorted(_runners)


def get_runner(graph_name: str) -> GraphRunner:
    if graph_name not in _runners:
        raise KeyError(
            f"Unknown graph '{graph_name}'. Available: {sorted(_runners)}"
        )
    return _runners[graph_name]


def get_store() -> PersistenceStore:
    if _store is None:
        raise RuntimeError("Store not initialized")
    return _store


async def get_runner_for_run(run_id: str) -> tuple[GraphRunner, RunRecord]:
    store = get_store()
    run = await store.load_run(run_id)
    if run is None:
        raise KeyError(f"Unknown run_id: {run_id}")
    return get_runner(run.graph_name), run
