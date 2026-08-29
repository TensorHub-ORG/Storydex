from uuid import uuid4
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api.routes_auth import router as auth_router
from api.routes_file import router as file_router
from api.routes_help import router as help_router
from api.routes_presets import router as presets_router
from api.routes_story import router as story_router
from api.routes_sys import router as sys_router
from api.routes_wiki import router as wiki_router
from api.response import ApiTrace, error_response
from core.config import get_settings
from core.exceptions import StorydexError
from core.logger import get_logger, with_trace
from services.project_service import get_project_service
from services.coomi_version_service import check_coomi_version
from services.execution_coordinator import get_execution_coordinator
from services.content_pipeline_service import (
    bootstrap_content_workspace,
    stop_content_pipeline,
)

settings = get_settings()
app_logger = get_logger(__name__)

app = FastAPI(title=settings.app_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "null",
        "file://",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:5174",
        "http://localhost:5174",
        "http://127.0.0.1:4173",
        "http://localhost:4173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sys_router, prefix="/api/v1")
app.include_router(file_router, prefix="/api/v1")
app.include_router(help_router, prefix="/api/v1")
app.include_router(story_router, prefix="/api/v1")
app.include_router(wiki_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(presets_router, prefix="/api/v1")


@app.on_event("startup")
def bootstrap_workspace() -> None:
    coomi_status = check_coomi_version()
    if not coomi_status["ok"]:
        detail = "; ".join(coomi_status["warnings"])
        app_logger.critical("Coomi runtime identity check failed: %s", detail)
        raise RuntimeError(f"Coomi runtime identity check failed: {detail}")
    app_logger.info(
        "Coomi runtime identity check passed: %s source=%s",
        coomi_status["expected"],
        coomi_status["expectedFingerprint"],
    )

    project = get_project_service().current_project()
    app_logger.info("Workspace bootstrap completed at %s", project["workspaceRoot"])
    try:
        pipeline = bootstrap_content_workspace(Path(project["workspaceRoot"]))
        app_logger.info(
            "Content pipeline published catalog generation %s and updated %s retrieval source(s)",
            pipeline["catalogGeneration"],
            pipeline["retrievalUpdatedFileCount"],
        )
    except Exception as exc:
        app_logger.error("Content pipeline bootstrap failed: %s", exc)
    try:
        reconciled = get_execution_coordinator().reconcile_workspace(Path(project["workspaceRoot"]))
        if reconciled:
            app_logger.warning("Marked %s interrupted execution(s) as unfinished", len(reconciled))
    except Exception as exc:
        app_logger.error("Execution reconciliation failed: %s", exc)


@app.on_event("shutdown")
def shutdown_content_pipeline() -> None:
    stop_content_pipeline()


def _resolve_trace_id(request: Request) -> str:
    return request.headers.get("x-trace-id") or str(uuid4())


@app.exception_handler(StorydexError)
async def handle_storydex_error(request: Request, exc: StorydexError) -> JSONResponse:
    trace_id = _resolve_trace_id(request)
    trace_logger = with_trace(app_logger, trace_id)
    trace_logger.warning("Storydex error code=%s message=%s", exc.code, exc.message)
    audit = []
    tool_audit = exc.details.get("toolAudit") if isinstance(exc.details, dict) else None
    if isinstance(tool_audit, dict):
        audit.append(tool_audit)

    envelope = error_response(
        code=exc.code,
        message=exc.message,
        details=exc.details,
        trace=ApiTrace(traceId=trace_id),
        audit=audit,
    )
    return JSONResponse(status_code=exc.status_code, content=envelope.model_dump(by_alias=True))


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    trace_id = _resolve_trace_id(request)
    trace_logger = with_trace(app_logger, trace_id)
    trace_logger.warning("Request validation failed")

    errors = exc.errors()
    first = errors[0] if errors else {}
    location = ".".join(str(item) for item in first.get("loc", ()) if item != "body") or "请求内容"
    error_type = str(first.get("type") or "")
    context = first.get("ctx") if isinstance(first.get("ctx"), dict) else {}
    if error_type == "string_too_long":
        summary = f"{location} 过长，最多允许 {context.get('max_length', '规定')} 个字符。"
    elif error_type == "string_too_short":
        summary = f"{location} 过短，至少需要 {context.get('min_length', '规定')} 个字符。"
    elif error_type == "missing":
        summary = f"缺少必填字段：{location}。"
    elif error_type == "greater_than_equal":
        summary = f"{location} 不能小于 {context.get('ge', '规定的最小值')}。"
    elif error_type == "less_than_equal":
        summary = f"{location} 不能大于 {context.get('le', '规定的最大值')}。"
    elif error_type == "string_pattern_mismatch":
        summary = f"{location} 格式不符合要求。"
    else:
        summary = f"{location} 参数无效：{first.get('msg') or '请检查输入。'}"
    envelope = error_response(
        code="request_validation_error",
        message=summary,
        details={"errors": errors},
        trace=ApiTrace(traceId=trace_id),
    )
    return JSONResponse(status_code=422, content=envelope.model_dump(by_alias=True))


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    trace_id = _resolve_trace_id(request)
    trace_logger = with_trace(app_logger, trace_id)
    trace_logger.exception("Unhandled exception")

    envelope = error_response(
        code="internal_server_error",
        message="Internal server error.",
        details={"exceptionType": exc.__class__.__name__},
        trace=ApiTrace(traceId=trace_id),
    )
    return JSONResponse(status_code=500, content=envelope.model_dump(by_alias=True))

if settings.serve_frontend_static and settings.frontend_dist_dir.exists():
    app.mount(
        "/",
        StaticFiles(directory=str(settings.frontend_dist_dir), html=True),
        name="frontend",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=settings.api_host, port=settings.api_port, reload=False)
