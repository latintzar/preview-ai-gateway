from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import math
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Config
from .ledger import Ledger, LedgerError
from .provider import ProviderFailure, complete, rates_current, reservation_ns
from .sessions import Sessions, SessionError
from .tasks import Registry, default_registry

COOKIE = "__Host-atelier-preview"
EXHAUSTED_MESSAGE = "Today’s demo budget is used. Use your own key or run it from GitHub."
MAX_BODY = 16_384


class GatewayError(Exception):
    def __init__(self, code, message, status=400, actions=None):
        self.code, self.message, self.status, self.actions = code, message, status, actions


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    request_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    mode: str = Field(pattern=r"^(shared|own)$")
    input: dict


class KeyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    key: str = Field(min_length=32, max_length=512, pattern=r"^sk-[A-Za-z0-9_-]+$", repr=False)


def _shape(value, depth=0):
    if depth > 8:
        raise GatewayError("invalid_input", "Check the feature input.")
    if isinstance(value, dict):
        if len(value) > 32:
            raise GatewayError("invalid_input", "Check the feature input.")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 80:
                raise GatewayError("invalid_input", "Check the feature input.")
            _shape(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 32:
            raise GatewayError("invalid_input", "Check the feature input.")
        for item in value:
            _shape(item, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise GatewayError("invalid_input", "Check the feature input.")


async def body(request):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise GatewayError("json_required", "Send JSON input.", 415)
    try:
        content_length = int(request.headers.get("content-length", "0"))
        if not 0 <= content_length <= MAX_BODY:
            raise GatewayError("input_too_large", "Use a smaller input.", 413)
        raw = bytearray()
        async with asyncio.timeout(5):
            async for part in request.stream():
                if len(raw) + len(part) > MAX_BODY:
                    raise GatewayError("input_too_large", "Use a smaller input.", 413)
                raw.extend(part)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError()
        _shape(data)
        return data
    except GatewayError:
        raise
    except Exception:
        raise GatewayError("invalid_json", "Check the JSON input.") from None


def create_app(config=None, registry=None, *, transport=None, clock=time.time, session_clock=time.monotonic):
    config = config or Config.from_env()
    registry = registry or default_registry(config)
    if not isinstance(registry, Registry):
        raise ValueError("Use a reviewed server-side task registry.")
    ledger = Ledger(config.ledger_path)
    sessions = Sessions(config, session_clock)
    allowed_origins = registry.origins | {config.public_origin}

    @asynccontextmanager
    async def lifespan(app):
        async def expire_keys():
            while True:
                await asyncio.sleep(1)
                sessions.prune()
        async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(30, connect=5), limits=httpx.Limits(max_connections=4, max_keepalive_connections=4)) as client:
            app.state.provider = client
            cleaner = asyncio.create_task(expire_keys())
            try:
                yield
            finally:
                cleaner.cancel()
                try:
                    await cleaner
                except asyncio.CancelledError:
                    pass
                sessions.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sessions, app.state.ledger, app.state.registry = sessions, ledger, registry
    app.add_middleware(CORSMiddleware, allow_origins=sorted(allowed_origins), allow_credentials=True,
                       allow_methods=["GET", "POST", "DELETE"], allow_headers=["Content-Type", "X-CSRF-Token"])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(config.public_origin).hostname])

    @app.middleware("http")
    async def secure_headers(request, call_next):
        if request.url.scheme != "https":
            response = JSONResponse({"code": "tls_required", "message": "Use the HTTPS gateway."}, status_code=400)
        else:
            response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                 "Cross-Origin-Opener-Policy": "same-origin",
                                 "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                                 "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error(request, exc):
        result = {"code": exc.code, "message": exc.message}
        if exc.actions:
            result["actions"] = exc.actions
        return JSONResponse(result, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse({"code": "invalid_input", "message": "Check the input."}, status_code=422)

    def trusted_origin(request, *, popup=False):
        value = request.headers.get("origin")
        if value not in ({config.public_origin} if popup else allowed_origins) or request.headers.get("sec-fetch-site") == "cross-site":
            raise GatewayError("origin_denied", "Open this feature from its preview.", 403)
        return value

    def session(request, *, popup=False, csrf=True):
        trusted_origin(request, popup=popup)
        try:
            item = sessions.get(request.cookies.get(COOKIE))
        except SessionError:
            raise GatewayError("session_required", "Open the preview again.", 401) from None
        try:
            sessions.control_rate("api-global", 120)
            sessions.control_rate("api-session:" + sessions.digest(item.id), 30)
        except SessionError:
            raise GatewayError("rate_limited", "Please try again shortly.", 429) from None
        if csrf and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), item.csrf):
            raise GatewayError("csrf_denied", "Refresh this preview session.", 403)
        return item

    def actions(project):
        return [{"label": "Use my key", "url": config.public_origin + "/key?project=" + project.id},
                {"label": "Get the code", "url": project.code_url}]

    @app.get("/health")
    async def health():
        return {"status": "ok", "shared_enabled": config.shared_enabled, "registered_tasks": len(registry.tasks)}

    @app.post("/api/session")
    async def bootstrap(request: Request):
        trusted_origin(request)
        await body(request)
        try:
            item, created = sessions.bootstrap(request.cookies.get(COOKIE), request.client.host)
        except SessionError:
            raise GatewayError("rate_limited", "Please try again shortly.", 429) from None
        response = JSONResponse({"csrf": item.csrf, "has_key": bool(item.key),
                                 "key_seconds": max(0, int(item.key_expires - sessions.clock())) if item.key else 0})
        if created:
            response.set_cookie(COOKIE, item.id, secure=True, httponly=True, samesite="strict", path="/", max_age=config.session_seconds)
        return response

    @app.post("/api/key")
    async def put_key(request: Request):
        item = session(request, popup=True)
        try:
            value = KeyInput.model_validate(await body(request)).key
            sessions.put_key(item.id, value)
        except ValidationError:
            raise GatewayError("invalid_key", "Check the OpenAI API key.", 422) from None
        except SessionError:
            raise GatewayError("rate_limited", "Please try again shortly.", 429) from None
        return {"stored": True, "expires_seconds": max(0, int(item.key_expires - sessions.clock()))}

    @app.delete("/api/key")
    async def forget_key(request: Request):
        item = session(request, popup=True); sessions.forget_key(item.id)
        return {"stored": False}

    @app.post("/api/run")
    async def run(request: Request):
        item = session(request)
        try:
            incoming = RunInput.model_validate(await body(request))
            task = registry.tasks.get(incoming.task_id)
            if not task:
                raise GatewayError("unknown_task", "Feature unavailable.", 404)
            project = registry.projects[task.project]
            if request.headers.get("origin") != project.origin:
                raise GatewayError("origin_denied", "Open this feature from its preview.", 403)
            value = task.input_model.model_validate(incoming.input)
            text = task.render_input(value)
            if not isinstance(text, str) or len(text.encode()) > task.max_input_bytes:
                raise GatewayError("invalid_input", "Use a smaller feature input.", 422)
        except ValidationError:
            raise GatewayError("invalid_input", "Check the feature input.", 422) from None
        except GatewayError:
            raise
        except Exception:
            raise GatewayError("invalid_input", "Check the feature input.", 422) from None
        if incoming.mode == "shared":
            if not config.shared_enabled:
                raise GatewayError("shared_disabled", "Use your own key or run it from GitHub.", 503, actions(project))
            if not rates_current(clock()):
                raise GatewayError("rate_review_required", "Use your own key or run it from GitHub.", 503, actions(project))
            key = config.shared_key
        else:
            try:
                key = sessions.key_for(item.id)
            except SessionError:
                raise GatewayError("key_required", "Use your OpenAI key for this preview.", 401, actions(project)) from None
        try:
            ticket = ledger.admit(project=project.id, task=task.id, mode="hosted" if incoming.mode == "shared" else "own",
                                  session_hash=sessions.digest(item.id), ip_hash=sessions.ip_digest(request.client.host),
                                  request_id=incoming.request_id, reserved_ns=reservation_ns(task.max_output_tokens) if incoming.mode == "shared" else 0,
                                  daily_limit_ns=config.daily_limit_ns, now=clock())
        except LedgerError as error:
            if error.code == "budget_exceeded":
                raise GatewayError("daily_budget_used", EXHAUSTED_MESSAGE, 429, actions(project)) from None
            raise GatewayError(error.code, "Please try again shortly.", 409 if error.code == "duplicate_request" else 429) from None
        submitted = False; settled = False
        try:
            ledger.submitted(ticket, now=clock()); submitted = True
            receipt = await complete(request.app.state.provider, key, task.instructions, text, task.max_output_tokens, ticket)
            ledger.settle(ticket, receipt.charge_ns if incoming.mode == "shared" else 0, now=clock())
            settled = True
            if not receipt.completed or not receipt.text:
                raise GatewayError("output_unavailable", "Try this feature again.", 502)
            result = task.parse_output(receipt.text)
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
            if len(encoded) > 65_536:
                raise GatewayError("output_unavailable", "Try this feature again.", 502)
            return {"result": result, "request_id": incoming.request_id, "mode": incoming.mode}
        except GatewayError:
            raise
        except BaseException as error:
            if not settled:
                if submitted:
                    ledger.uncertain(ticket, now=clock())
                else:
                    ledger.release_unsent(ticket, now=clock())
            if isinstance(error, asyncio.CancelledError):
                raise
            if settled:
                raise GatewayError("output_unavailable", "Try this feature again.", 502) from None
            raise GatewayError("provider_uncertain", "This request needs review. Use your own key or run it from GitHub.", 502, actions(project)) from None
        finally:
            key = ""

    @app.get("/key", response_class=HTMLResponse)
    async def key_page(project: str):
        if project not in registry.projects:
            raise GatewayError("unknown_project", "Preview unavailable.", 404)
        return HTMLResponse("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                            "<title>Use my key</title><link rel='stylesheet' href='/static/key.css'><script defer src='/static/key.js'></script></head>"
                            "<body><main><h1>Use my key</h1><form id='key-form'><label for='key'>OpenAI API key</label>"
                            "<input id='key' type='password' autocomplete='off' minlength='32' maxlength='512' required>"
                            "<p>Held for up to ten minutes. Your OpenAI account pays for these requests.</p><button>Use my key</button></form>"
                            "<button id='forget' type='button'>Forget my key</button><p id='status' role='status'></p></main></body></html>")

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    return app


def factory():
    return create_app()
