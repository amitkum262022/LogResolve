"""
FastAPI entrypoint for LogResolve — OpenPages log analysis UI and API.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Optional

from fastapi import (
    FastAPI,
    File,
    Form,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from llm_factory import (
    DEFAULT_MODELS,
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_WATSONX_URL,
    LLMConfig,
    PROVIDER_LABELS,
    WATSONX_MODEL_SUGGESTIONS,
    validate_llm_config,
)
from services import (
    BundleState,
    analyze_one_chunk,
    build_work_items,
    explore_lines,
    load_bundle_bytes,
    round_robin_chunks,
)
from settings_store import (
    clear_llm_settings,
    load_llm_settings,
    save_llm_settings,
    settings_file_exists,
)


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# session_id -> BundleState (in-memory; fine for local single-user tool)
_BUNDLES: dict[str, BundleState] = {}

app = FastAPI(title="LogResolve", version="1.0.0")
app.add_middleware(
    SessionMiddleware,
    secret_key=secrets.token_hex(32),
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _sid(request: Request) -> str:
    sid = request.session.get("sid")
    if not sid:
        sid = str(uuid.uuid4())
        request.session["sid"] = sid
    return sid


def _get_bundle(request: Request) -> Optional[BundleState]:
    return _BUNDLES.get(_sid(request))


def _llm_config_from_payload(data: dict[str, Any]) -> LLMConfig:
    provider = (data.get("provider") or "watsonx").strip().lower()
    return LLMConfig(
        provider=provider,  # type: ignore[arg-type]
        model=(data.get("model") or DEFAULT_MODELS.get(provider, "")).strip(),
        api_key=(data.get("api_key") or "").strip(),
        project_id=(data.get("project_id") or "").strip(),
        url=(data.get("url") or DEFAULT_WATSONX_URL).strip(),
        base_url=(data.get("base_url") or DEFAULT_OLLAMA_BASE_URL).strip(),
    )


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    _sid(request)
    saved = load_llm_settings()
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "providers": PROVIDER_LABELS,
            "default_models": DEFAULT_MODELS,
            "watsonx_suggestions": WATSONX_MODEL_SUGGESTIONS,
            "default_watsonx_url": DEFAULT_WATSONX_URL,
            "default_ollama_url": DEFAULT_OLLAMA_BASE_URL,
            "saved_settings": saved,
            "settings_exist": settings_file_exists(),
        },
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@app.get("/api/settings")
async def get_settings() -> JSONResponse:
    return JSONResponse(
        {
            "settings": load_llm_settings(),
            "exists": settings_file_exists(),
            "defaults": {
                "models": DEFAULT_MODELS,
                "watsonx_url": DEFAULT_WATSONX_URL,
                "ollama_url": DEFAULT_OLLAMA_BASE_URL,
                "watsonx_suggestions": WATSONX_MODEL_SUGGESTIONS,
            },
        }
    )


class SettingsPayload(BaseModel):
    provider: str = "watsonx"
    openai_api_key: str = ""
    openai_model: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = ""
    gemini_api_key: str = ""
    gemini_model: str = ""
    watsonx_api_key: str = ""
    watsonx_project_id: str = ""
    watsonx_url: str = DEFAULT_WATSONX_URL
    watsonx_model: str = ""
    watsonx_model_suggestion: str = ""
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL
    ollama_model: str = ""


@app.post("/api/settings")
async def post_settings(payload: SettingsPayload) -> JSONResponse:
    path = save_llm_settings(payload.model_dump())
    return JSONResponse({"ok": True, "path": str(path.name)})


@app.delete("/api/settings")
async def delete_settings() -> JSONResponse:
    clear_llm_settings()
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Load bundle
# ---------------------------------------------------------------------------
@app.post("/api/load")
async def load_bundle(
    request: Request,
    files: list[UploadFile] = File(...),
    do_mask: str = Form("true"),
) -> JSONResponse:
    if not files:
        return JSONResponse({"ok": False, "error": "No files uploaded"}, status_code=400)
    try:
        payloads: list[tuple[str, bytes]] = []
        for f in files:
            data = await f.read()
            payloads.append((f.filename or "upload.log", data))
        mask_flag = str(do_mask).lower() in ("1", "true", "yes", "on")
        loop = asyncio.get_running_loop()
        state = await loop.run_in_executor(
            None, partial(load_bundle_bytes, payloads, do_mask=mask_flag)
        )
        sid = _sid(request)
        _BUNDLES[sid] = state
        return JSONResponse(
            {
                "ok": True,
                "categories": state.selected_categories,
                "count": len(state.selected_categories),
                "do_mask": mask_flag,
            }
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/bundle")
async def bundle_status(request: Request) -> JSONResponse:
    state = _get_bundle(request)
    if not state:
        return JSONResponse({"loaded": False})
    return JSONResponse(
        {
            "loaded": True,
            "categories": sorted(state.categories.keys()),
            "selected_categories": state.selected_categories,
            "max_chunks": state.max_chunks,
            "results_count": len(state.analysis_results),
            "last_llm_label": state.last_llm_label,
            "analysis_partial": state.analysis_partial,
            "analysis_total_found": state.analysis_total_found,
        }
    )


class SelectionPayload(BaseModel):
    selected_categories: list[str] = Field(default_factory=list)
    max_chunks: int = 10


@app.post("/api/selection")
async def update_selection(request: Request, payload: SelectionPayload) -> JSONResponse:
    state = _get_bundle(request)
    if not state:
        return JSONResponse({"ok": False, "error": "No bundle loaded"}, status_code=400)
    valid = set(state.categories.keys())
    state.selected_categories = [c for c in payload.selected_categories if c in valid]
    state.max_chunks = max(1, min(2000, int(payload.max_chunks)))
    return JSONResponse(
        {
            "ok": True,
            "selected_categories": state.selected_categories,
            "max_chunks": state.max_chunks,
        }
    )


# ---------------------------------------------------------------------------
# Explore
# ---------------------------------------------------------------------------
class ExplorePayload(BaseModel):
    selected_categories: list[str] = Field(default_factory=list)
    category_filter: str = "(all selected)"
    keyword: str = ""
    start: Optional[str] = None
    end: Optional[str] = None
    reveal: bool = False
    limit: int = 5000


@app.post("/api/explore")
async def explore(request: Request, payload: ExplorePayload) -> JSONResponse:
    state = _get_bundle(request)
    if not state:
        return JSONResponse({"ok": False, "error": "No bundle loaded"}, status_code=400)

    def _parse_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    selected = payload.selected_categories or state.selected_categories
    rows = explore_lines(
        state,
        selected_categories=selected,
        category_filter=payload.category_filter,
        keyword=payload.keyword,
        start=_parse_dt(payload.start),
        end=_parse_dt(payload.end),
        reveal=payload.reveal,
        limit=payload.limit,
    )
    return JSONResponse({"ok": True, "rows": rows, "count": len(rows)})


# ---------------------------------------------------------------------------
# Analyze (WebSocket for progress)
# ---------------------------------------------------------------------------
@app.websocket("/ws/analyze")
async def ws_analyze(websocket: WebSocket) -> None:
    await websocket.accept()
    # SessionMiddleware does not apply to websockets the same way; client sends sid.
    try:
        init = await websocket.receive_json()
        sid = init.get("sid") or ""
        state = _BUNDLES.get(sid)
        if not state:
            await websocket.send_json(
                {"type": "error", "error": "No bundle loaded (missing session)."}
            )
            await websocket.close()
            return

        selected = init.get("selected_categories") or state.selected_categories
        max_chunks = int(init.get("max_chunks") or state.max_chunks or 10)
        do_mask = bool(init.get("do_mask", state.do_mask))
        state.do_mask = do_mask
        state.selected_categories = selected
        state.max_chunks = max_chunks

        llm_config = _llm_config_from_payload(init.get("llm") or {})
        missing = validate_llm_config(llm_config)
        if missing:
            await websocket.send_json(
                {
                    "type": "error",
                    "error": f"Incomplete credentials: {', '.join(missing)}",
                }
            )
            await websocket.close()
            return

        llm_run_label = f"{llm_config.display_name()} / {llm_config.resolved_model()}"
        work_by_cat = build_work_items(state, selected)
        total_found = sum(len(v) for v in work_by_cat.values())
        if total_found == 0:
            await websocket.send_json(
                {
                    "type": "error",
                    "error": "No ERROR / FATAL / Exception / CRITICAL chunks in selection.",
                }
            )
            await websocket.close()
            return

        planned = round_robin_chunks(work_by_cat, max_chunks)
        skipped = max(0, total_found - len(planned))
        state.analysis_results = []
        state.last_llm_label = llm_run_label
        state.analysis_total_found = total_found
        state.analysis_partial = skipped > 0

        await websocket.send_json(
            {
                "type": "start",
                "total": len(planned),
                "total_found": total_found,
                "skipped": skipped,
                "categories": sorted({p[0] for p in planned}),
                "llm_label": llm_run_label,
            }
        )

        results: list[dict] = []
        for i, (cat, chunk, token_map) in enumerate(planned):
            result = analyze_one_chunk(
                llm_config, cat, chunk, token_map, state, llm_run_label
            )
            results.append(result)
            state.analysis_results = list(results)
            state.analysis_partial = (i + 1) < len(planned) or skipped > 0
            await websocket.send_json(
                {
                    "type": "progress",
                    "index": i + 1,
                    "total": len(planned),
                    "label": result.get("label"),
                    "result": result,
                }
            )

        state.analysis_partial = skipped > 0
        await websocket.send_json(
            {
                "type": "done",
                "results": results,
                "partial": skipped > 0,
                "total_found": total_found,
                "llm_label": llm_run_label,
            }
        )
    except WebSocketDisconnect:
        return
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "error": str(exc)})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.get("/api/session")
async def session_info(request: Request) -> JSONResponse:
    return JSONResponse({"sid": _sid(request)})


@app.get("/api/results")
async def get_results(request: Request) -> JSONResponse:
    state = _get_bundle(request)
    if not state:
        return JSONResponse({"results": []})
    return JSONResponse(
        {
            "results": state.analysis_results,
            "last_llm_label": state.last_llm_label,
            "partial": state.analysis_partial,
            "total_found": state.analysis_total_found,
        }
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8005, reload=True)
