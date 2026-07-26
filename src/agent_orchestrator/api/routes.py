"""FastAPI routes for run lifecycle, auth, and human-in-the-loop."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field

from agent_orchestrator.api.auth import (
    auth_enabled,
    clear_session_cookie,
    is_authenticated,
    password_ok,
    require_auth,
    set_session_cookie,
)
from agent_orchestrator.api.idempotency import fingerprint_payload
from agent_orchestrator.api.metrics import build_run_metrics
from agent_orchestrator.api.cache_util import cache_stats, cached_get, graph_meta_cache, health_cache
from agent_orchestrator.api.rate_limit import limiter
from agent_orchestrator.api import runners as runner_registry
from agent_orchestrator.api.settings import get_settings
from agent_orchestrator.core.runner import RunRecord
from agent_orchestrator.core.state import RunStatus
from agent_orchestrator.examples.research_report_pipeline import (
    REPORT_TYPES,
    default_initial_state,
)
from agent_orchestrator.examples.support_resolution_pipeline import default_support_state
from agent_orchestrator.integrations import get_zendesk_sim
from agent_orchestrator.knowledge import extract_text_from_bytes, get_knowledge_store

public_router = APIRouter(prefix="/api")
router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

PIPELINES = [
    {
        "id": "research_report",
        "label": "Research report",
        "description": "Topic → cited, critically reviewed report with human sign-off.",
        "input_mode": "research",
    },
    {
        "id": "support_resolution",
        "label": "Customer Support Resolution Network",
        "description": "Frontline → sentiment → FAQ / Technical / Billing specialists → escalate if needed.",
        "input_mode": "support",
    },
]


class StartRunRequest(BaseModel):
    graph: str = "research_report"
    # Research fields
    topic: str | None = Field(default=None, max_length=500)
    report_type: str = "general"
    # Support fields
    subject: str | None = Field(default=None, max_length=300)
    message: str | None = Field(default=None, max_length=5000)
    customer_name: str = "Customer"
    customer_plan: str = "Pro"
    customer_email: str | None = None
    source: str = "manual"
    external_ticket_id: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)
    initial_state: dict[str, Any] | None = None


class ZendeskConnectRequest(BaseModel):
    subdomain: str = Field(default="acme-demo", max_length=80)


class ZendeskWebhookRequest(BaseModel):
    external_ticket_id: str | None = None
    subject: str = Field(..., min_length=2, max_length=300)
    message: str = Field(..., min_length=5, max_length=5000)
    customer_name: str = "Customer"
    customer_plan: str = "Pro"
    customer_email: str | None = None
    priority: str = "normal"


class ApproveRequest(BaseModel):
    decision: str = Field(default="approve", pattern="^(approve|reject|revise)$")
    comment: str | None = None
    edited_state: dict[str, Any] | None = None


class ResumeRequest(BaseModel):
    state_updates: dict[str, Any] | None = None


class LoginRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=256)


def _serialize(run: RunRecord) -> dict[str, Any]:
    return run.to_dict()


async def _run_in_background(run_id: str) -> None:
    try:
        runner, _ = await runner_registry.get_runner_for_run(run_id)
        await runner.execute(run_id=run_id)
    except Exception as exc:  # noqa: BLE001
        store = runner_registry.get_store()
        run = await store.load_run(run_id)
        if run and run.status not in (RunStatus.FAILED, RunStatus.COMPLETED, RunStatus.PAUSED):
            run.status = RunStatus.FAILED
            run.error = str(exc)
            await store.save_run(run)


def _build_state(body: StartRunRequest) -> dict[str, Any]:
    if body.initial_state:
        return dict(body.initial_state)

    if body.graph == "research_report":
        topic = (body.topic or "").strip()
        if len(topic) < 2:
            raise HTTPException(400, "topic is required for research_report (min 2 chars)")
        return default_initial_state(topic, body.report_type)

    if body.graph == "support_resolution":
        subject = (body.subject or body.topic or "").strip()
        message = (body.message or "").strip()
        if len(subject) < 2:
            raise HTTPException(400, "subject is required for support tickets")
        if len(message) < 5:
            raise HTTPException(400, "message is required for support tickets (min 5 chars)")
        return default_support_state(
            subject=subject,
            message=message,
            customer_name=body.customer_name,
            customer_plan=body.customer_plan,
            customer_email=body.customer_email,
            source=body.source or "manual",
            external_ticket_id=body.external_ticket_id,
        )

    raise HTTPException(400, f"Unknown graph: {body.graph}")


def _idempotency_key_from_request(request: Request, body: StartRunRequest) -> str | None:
    header = (request.headers.get("Idempotency-Key") or "").strip()
    if header:
        return header[:200]
    if body.idempotency_key:
        return body.idempotency_key.strip()[:200] or None
    return None


async def _create_run_idempotent(
    *,
    request: Request,
    body: StartRunRequest,
    background: BackgroundTasks,
    default_key: str | None = None,
    extra_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        runner = runner_registry.get_runner(body.graph)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc

    store = runner_registry.get_store()
    state = _build_state(body)
    req_hash = fingerprint_payload(body.model_dump())
    idem_key = _idempotency_key_from_request(request, body) or default_key

    if idem_key and hasattr(store, "get_idempotency"):
        existing = await store.get_idempotency(idem_key)  # type: ignore[attr-defined]
        if existing:
            if existing["request_hash"] != req_hash:
                raise HTTPException(
                    409,
                    "Idempotency-Key reused with a different request body",
                )
            run = await store.load_run(existing["run_id"])
            if run is None:
                raise HTTPException(500, "Idempotent run missing from store")
            payload = {
                "run_id": run.run_id,
                "status": run.status.value,
                "graph": body.graph,
                "run": _serialize(run),
                "idempotent_replay": True,
            }
            if extra_response:
                payload.update(extra_response)
            return payload

    run = await runner.create_run(state)
    run.state.set("orchestrator_run_id", run.run_id)
    if idem_key:
        run.state.set("idempotency_key", idem_key)
    await store.save_run(run)

    if idem_key and hasattr(store, "put_idempotency"):
        try:
            await store.put_idempotency(idem_key, req_hash, run.run_id)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            # Unique race: another request won — return that run.
            existing = await store.get_idempotency(idem_key)  # type: ignore[attr-defined]
            if existing and existing["request_hash"] == req_hash:
                winner = await store.load_run(existing["run_id"])
                if winner:
                    payload = {
                        "run_id": winner.run_id,
                        "status": winner.status.value,
                        "graph": body.graph,
                        "run": _serialize(winner),
                        "idempotent_replay": True,
                    }
                    if extra_response:
                        payload.update(extra_response)
                    return payload
            raise HTTPException(409, f"Idempotency conflict: {exc}") from exc

    background.add_task(_run_in_background, run.run_id)
    payload = {
        "run_id": run.run_id,
        "status": run.status.value,
        "graph": body.graph,
        "run": _serialize(run),
        "idempotent_replay": False,
    }
    if extra_response:
        payload.update(extra_response)
    return payload


# --- Public routes ---


@public_router.get("/health")
@limiter.limit("60/minute")
async def health(request: Request) -> dict[str, Any]:
    def _payload() -> dict[str, Any]:
        return {"status": "ok", "auth_required": auth_enabled(), "cache": cache_stats()}

    return cached_get(health_cache, "health", _payload)


@public_router.get("/pipelines")
async def pipelines(request: Request) -> dict[str, Any]:
    return {"pipelines": PIPELINES}


@public_router.get("/report-types")
async def report_types(request: Request) -> dict[str, Any]:
    return {
        "report_types": [
            {"id": key, "label": val["label"]} for key, val in REPORT_TYPES.items()
        ]
    }


@public_router.get("/auth/status")
async def auth_status(request: Request) -> dict[str, Any]:
    settings = get_settings()
    return {
        "auth_required": auth_enabled(settings),
        "authenticated": is_authenticated(request, settings),
    }


@public_router.post("/auth/login")
@limiter.limit("10/minute")
async def login(request: Request, body: LoginRequest, response: Response) -> dict[str, Any]:
    settings = get_settings()
    if not auth_enabled(settings):
        return {"ok": True, "auth_required": False, "message": "Auth is disabled (no APP_PASSWORD set)."}
    if not password_ok(body.password, settings):
        raise HTTPException(401, "Invalid password")
    set_session_cookie(response, settings)
    return {"ok": True, "auth_required": True, "authenticated": True}


@public_router.post("/auth/logout")
async def logout(response: Response) -> dict[str, str]:
    clear_session_cookie(response)
    return {"ok": "true"}


# --- Protected routes ---


@router.post("/runs")
@limiter.limit("5/hour")
async def start_run(
    request: Request,
    body: StartRunRequest,
    background: BackgroundTasks,
) -> dict[str, Any]:
    return await _create_run_idempotent(request=request, body=body, background=background)


@router.get("/metrics")
@limiter.limit("60/minute")
async def metrics(request: Request) -> dict[str, Any]:
    """Lightweight ops metrics: run counts, failures, HITL wait."""
    store = runner_registry.get_store()
    if hasattr(store, "list_runs_for_metrics"):
        runs = await store.list_runs_for_metrics(limit=500)  # type: ignore[attr-defined]
    elif hasattr(store, "list_runs"):
        runs = await store.list_runs(limit=500)  # type: ignore[attr-defined]
    else:
        runs = []
    return {"ok": True, **build_run_metrics(runs)}


@router.get("/runs")
@limiter.limit("60/minute")
async def list_runs(request: Request, limit: int = 50) -> dict[str, Any]:
    store = runner_registry.get_store()
    if hasattr(store, "list_runs"):
        runs = await store.list_runs(limit=limit)  # type: ignore[attr-defined]
    else:
        runs = []
    return {"runs": runs}


@router.get("/runs/{run_id}")
@limiter.limit("120/minute")
async def get_run(request: Request, run_id: str) -> dict[str, Any]:
    run = await runner_registry.get_store().load_run(run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return _serialize(run)


@router.get("/runs/{run_id}/trace")
@limiter.limit("60/minute")
async def get_trace(request: Request, run_id: str) -> dict[str, Any]:
    run = await runner_registry.get_store().load_run(run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return {
        "run_id": run_id,
        "status": run.status.value,
        "graph_name": run.graph_name,
        "current_node": run.current_node,
        "trace": [t.to_dict() for t in run.trace],
        "error": run.error,
    }


@router.post("/runs/{run_id}/approve")
@limiter.limit("30/minute")
async def approve_run(
    request: Request,
    run_id: str,
    body: ApproveRequest,
    background: BackgroundTasks,
) -> dict[str, Any]:
    try:
        runner, run = await runner_registry.get_runner_for_run(run_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    if run.status != RunStatus.PAUSED:
        raise HTTPException(400, f"Run is not paused (status={run.status.value})")

    async def _approve() -> None:
        try:
            await runner.approve(
                run_id,
                decision=body.decision,
                edited_state=body.edited_state,
                comment=body.comment,
            )
        except Exception as exc:  # noqa: BLE001
            loaded = await runner.store.load_run(run_id)
            if loaded:
                loaded.error = str(exc)
                if loaded.status == RunStatus.RUNNING:
                    loaded.status = RunStatus.FAILED
                await runner.store.save_run(loaded)

    background.add_task(_approve)
    return {
        "run_id": run_id,
        "message": f"Decision '{body.decision}' accepted; resuming.",
        "status": run.status.value,
    }


@router.post("/runs/{run_id}/resume")
@limiter.limit("30/minute")
async def resume_run(
    request: Request,
    run_id: str,
    body: ResumeRequest,
    background: BackgroundTasks,
) -> dict[str, Any]:
    try:
        runner, run = await runner_registry.get_runner_for_run(run_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    if run.status not in (RunStatus.PAUSED, RunStatus.FAILED, RunStatus.PENDING, RunStatus.RETRYING):
        if run.status != RunStatus.RUNNING:
            raise HTTPException(400, f"Cannot resume run in status {run.status.value}")

    async def _resume() -> None:
        await runner.resume(run_id, body.state_updates)

    background.add_task(_resume)
    return {"run_id": run_id, "message": "Resume started", "status": run.status.value}


@router.get("/graphs/{graph_name}")
@limiter.limit("60/minute")
async def graph_meta(request: Request, graph_name: str) -> dict[str, Any]:
    def _build() -> dict[str, Any]:
        try:
            graph = runner_registry.get_runner(graph_name).graph
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {
            "name": graph.name,
            "entry_point": graph.entry_point,
            "nodes": [
                {
                    "name": n.name,
                    "type": n.node_type,
                    "retry": n.retry_policy.model_dump(),
                }
                for n in graph.nodes.values()
            ],
            "edges": [
                {
                    "source": e.source,
                    "target": e.target,
                    "label": e.label,
                    "conditional": e.condition is not None,
                    "parallel": e.parallel,
                    "fallback": e.is_fallback,
                }
                for e in graph.edges
            ],
            "terminals": sorted(graph.terminal_nodes),
            "cached": True,
        }

    return cached_get(graph_meta_cache, graph_name, _build)


# --- Knowledge base ---


@router.get("/knowledge/documents")
@limiter.limit("60/minute")
async def list_knowledge_docs(request: Request) -> dict[str, Any]:
    store = get_knowledge_store()
    return {"documents": store.list_documents()}


@router.post("/knowledge/documents")
@limiter.limit("20/minute")
async def upload_knowledge_doc(
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    filename = file.filename or "upload.txt"
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > 2_000_000:
        raise HTTPException(400, "File too large (max 2MB)")
    try:
        text = extract_text_from_bytes(filename, data)
        doc = get_knowledge_store().add_document(filename=filename, content=text)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True,
        "document": {
            "doc_id": doc.doc_id,
            "filename": doc.filename,
            "title": doc.title,
            "char_count": doc.char_count,
            "chunk_count": doc.chunk_count,
            "created_at": doc.created_at,
        },
    }


@router.delete("/knowledge/documents/{doc_id}")
@limiter.limit("30/minute")
async def delete_knowledge_doc(request: Request, doc_id: str) -> dict[str, Any]:
    ok = get_knowledge_store().delete_document(doc_id)
    if not ok:
        raise HTTPException(404, "Document not found")
    return {"ok": True, "deleted": doc_id}


# --- Integrations (Zendesk simulator) ---


@router.get("/integrations/zendesk")
@limiter.limit("60/minute")
async def zendesk_status(request: Request) -> dict[str, Any]:
    return get_zendesk_sim().status()


@router.post("/integrations/zendesk/connect")
@limiter.limit("20/minute")
async def zendesk_connect(request: Request, body: ZendeskConnectRequest) -> dict[str, Any]:
    return get_zendesk_sim().connect(subdomain=body.subdomain.strip() or "acme-demo")


@router.post("/integrations/zendesk/disconnect")
@limiter.limit("20/minute")
async def zendesk_disconnect(request: Request) -> dict[str, Any]:
    return get_zendesk_sim().disconnect()


@router.get("/integrations/zendesk/tickets")
@limiter.limit("60/minute")
async def zendesk_list_tickets(request: Request, open_only: bool = True) -> dict[str, Any]:
    sim = get_zendesk_sim()
    return {"tickets": sim.list_tickets(open_only=open_only), "status": sim.status()}


@router.get("/integrations/zendesk/tickets/{ticket_id}")
@limiter.limit("60/minute")
async def zendesk_get_ticket(request: Request, ticket_id: str) -> dict[str, Any]:
    ticket = get_zendesk_sim().get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(404, "Ticket not found")
    return {"ticket": ticket}


@router.post("/integrations/zendesk/webhook")
@limiter.limit("30/minute")
async def zendesk_webhook(request: Request, body: ZendeskWebhookRequest) -> dict[str, Any]:
    """Simulate Zendesk pushing a new inbound ticket."""
    ticket = get_zendesk_sim().ingest_webhook(body.model_dump())
    return {"ok": True, "ticket": ticket}


@router.get("/integrations/zendesk/deliveries")
@limiter.limit("60/minute")
async def zendesk_deliveries(request: Request, limit: int = 50) -> dict[str, Any]:
    return {"deliveries": get_zendesk_sim().list_deliveries(limit=limit)}


@router.post("/integrations/zendesk/tickets/{ticket_id}/start-run")
@limiter.limit("5/hour")
async def zendesk_start_run(
    request: Request,
    ticket_id: str,
    background: BackgroundTasks,
) -> dict[str, Any]:
    """Pull an inbound Zendesk ticket into the support resolution pipeline."""
    sim = get_zendesk_sim()
    if not sim.status()["connected"]:
        raise HTTPException(400, "Connect Zendesk (simulator) first")
    ticket = sim.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(404, "Ticket not found")

    body = StartRunRequest(
        graph="support_resolution",
        subject=ticket["subject"],
        message=ticket["message"],
        customer_name=ticket.get("customer_name") or "Customer",
        customer_plan=ticket.get("customer_plan") or "Pro",
        customer_email=ticket.get("customer_email"),
        source="zendesk",
        external_ticket_id=ticket["external_ticket_id"],
    )
    return await _create_run_idempotent(
        request=request,
        body=body,
        background=background,
        default_key=f"zendesk:{ticket['external_ticket_id']}",
        extra_response={"ticket": ticket},
    )
