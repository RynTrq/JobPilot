from __future__ import annotations

import os
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.api import routes_button_memory, routes_config, routes_control, routes_runtime, routes_stream
from backend.config import DATA_DIR, GENERATOR_DISABLED, OUTPUT_DIR, validate_config
from backend.form.field_answerer import load_candidate_data, precompute_corpus_embeddings
from backend.logging_setup import configure_logging
from backend.llm.providers import build_daily_caps_from_env, build_provider_clients_from_env
from backend.llm.router import ModelRouter
from backend.models.classifier import Classifier
from backend.models.encoder import Encoder
from backend.conductor import Conductor as Orchestrator
from backend.models.generator import Generator
from backend.alarm.notifier import AlarmNotifier
from backend.alarm.approval import ApprovalGate, ClassifierReviewGate, ManualTakeoverGate
from backend.scraping.browser import Browser
from backend.storage.mongo_db import MongoStore
from backend.storage.sqlite_db import SQLiteStore

log = structlog.get_logger()


def _init_gate(factory, stream, store):
    try:
        return factory(stream, store)
    except TypeError:
        return factory(stream)


async def _create_generator(router: ModelRouter):
    try:
        return await Generator.create(router=router)
    except TypeError:
        return await Generator.create()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log.info("logging_initialized")
    # parents=True so nested DATA_DIR configurations don't crash startup.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    validate_config()
    log.info("config_loaded", data_dir=str(DATA_DIR), output_dir=str(OUTPUT_DIR))

    app.state.sqlite = SQLiteStore()
    if hasattr(app.state.sqlite, "maintenance"):
        app.state.sqlite.maintenance()
    log.info("sqlite_initialized")
    # Heal any run rows the previous process left in an in-flight state (crashed /
    # Ctrl+C'd / SIGKILLed). Without this, the menubar UI boots thinking a run is
    # live when the backend process is actually dead, and the user sees a spinning
    # "running" indicator forever.
    try:
        reconciled = app.state.sqlite.reconcile_orphan_runs()
        if reconciled:
            log.info("reconciled_orphan_runs", count=reconciled)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("reconcile_orphan_runs_failed", error=str(exc))

    app.state.encoder = Encoder()
    log.info(
        "encoder_initialized",
        model_name=getattr(app.state.encoder, "model_name", "<unknown>"),
        device=getattr(app.state.encoder, "device", "<unknown>"),
    )
    try:
        app.state.candidate_data = load_candidate_data()
        app.state.field_answer_corpus_embeddings, app.state.field_answer_corpus = precompute_corpus_embeddings(
            app.state.encoder,
            app.state.candidate_data,
        )
    except Exception as exc:
        log.exception("field_answerer_corpus_prepare_failed", error=str(exc))
        raise
    log.info(
        "field_answerer_corpus_ready",
        corpus_size=len(app.state.field_answer_corpus),
        embedding_rows=int(getattr(app.state.field_answer_corpus_embeddings, "shape", (0, 0))[0]),
    )
    app.state.classifier = Classifier.load()
    log.info("classifier_initialized")
    app.state.router = ModelRouter(
        allow_paid=os.getenv("JOBPILOT_ALLOW_PAID") == "1",
        local_enabled=not GENERATOR_DISABLED,
        provider_clients=build_provider_clients_from_env(),
        daily_caps=build_daily_caps_from_env(),
    )
    log.info("llm_router_initialized", local_enabled=not GENERATOR_DISABLED)
    app.state.generator = await _create_generator(app.state.router)
    log.info("generator_initialized")
    app.state.browser = Browser.lazy()
    log.info("browser_manager_initialized", startup="lazy")
    app.state.mongo = MongoStore.from_env()
    app.state.stream = routes_stream.stream
    app.state.alarm = _init_gate(AlarmNotifier, app.state.stream, app.state.sqlite)
    app.state.approval = _init_gate(ApprovalGate, app.state.stream, app.state.sqlite)
    app.state.classifier_review = _init_gate(ClassifierReviewGate, app.state.stream, app.state.sqlite)
    app.state.manual_takeover = _init_gate(ManualTakeoverGate, app.state.stream, app.state.sqlite)
    log.info("gates_initialized")
    app.state.orch = Orchestrator(app.state)
    log.info("orchestrator_initialized")
    try:
        yield
    finally:
        # Shut every subsystem down independently — one failing cleanup should not
        # prevent others from releasing resources (browser windows, SQLite handles).
        orch = getattr(app.state, "orch", None)
        if orch is not None:
            with suppress(Exception):
                await orch.stop()
        for gate_name in ("approval", "classifier_review", "manual_takeover"):
            gate = getattr(app.state, gate_name, None)
            if gate is not None:
                with suppress(Exception):
                    gate.cancel_all(reason="shutdown")
        browser = getattr(app.state, "browser", None)
        if browser is not None:
            with suppress(Exception):
                await browser.close()
        generator = getattr(app.state, "generator", None)
        if generator is not None:
            with suppress(Exception):
                await generator.shutdown()
            with suppress(Exception):
                generator.unload()
        sqlite = getattr(app.state, "sqlite", None)
        if sqlite is not None:
            with suppress(Exception):
                sqlite.close()


app = FastAPI(title="JobPilot", version="0.1.0", lifespan=lifespan)
app.include_router(routes_control.router)
app.include_router(routes_config.router)
app.include_router(routes_stream.router)
app.include_router(routes_button_memory.router)
app.include_router(routes_runtime.router)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-Id") or str(uuid4())
    request.state.correlation_id = correlation_id
    response = await call_next(request)
    response.headers["X-Correlation-Id"] = correlation_id
    return response


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": "http_error",
                "message": str(exc.detail),
                "status_code": exc.status_code,
                "correlation_id": getattr(request.state, "correlation_id", None),
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request validation failed",
                "details": exc.errors(),
                "status_code": 422,
                "correlation_id": getattr(request.state, "correlation_id", None),
            }
        },
    )


@app.get("/health")
async def health(request: Request) -> dict:
    """Cheap liveness probe for the menubar app to confirm the server is up."""
    return {"ok": True, "service": "jobpilot", "correlation_id": getattr(request.state, "correlation_id", None)}
