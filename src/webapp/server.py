"""Authenticated local dashboard for the persistent PAPER runtime."""
import asyncio
import hashlib
import hmac
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator

from src.runtime import ROOT, PAIRS, PaperEngine, StateStore


class PairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pair: str
    side: Literal[-1, 1] = -1

    @field_validator('side', mode='before')
    @classmethod
    def strict_side(cls, value):
        if type(value) is not int or value not in (-1, 1):
            raise ValueError('side must be integer -1 or 1')
        return value


class CloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pair: str | None = None


def create_app(bybit_client=None, order_manager=None, risk_manager=None, screener=None,
               tg_reporter=None, *, engine=None, data_dir=None, token=None, start_workers=True):
    # Legacy dependencies are deliberately not used: the tested application is PAPER only.
    data_dir = Path(data_dir or os.environ.get("TBOT_DATA_DIR", ROOT / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    token_path = data_dir / "dashboard.token"
    if token is None:
        token = os.environ.get('TBOT_DASHBOARD_TOKEN')
    if token is None:
        if not token_path.exists():
            try:
                with token_path.open("x", encoding="ascii") as f:
                    f.write(secrets.token_urlsafe(32))
            except FileExistsError:
                pass
        token = token_path.read_text(encoding="ascii").strip()
    if len(token) < 32:
        raise ValueError("Dashboard token must contain at least 32 characters")
    cookie_key = hmac.new(token.encode(), b"tbot-session-v2", hashlib.sha256).digest()
    holder = {"engine": engine}

    @asynccontextmanager
    async def lifespan(app):
        if holder["engine"] is None:
            database_url = os.environ.get('DATABASE_URL')
            if database_url:
                from src.storage.postgres_state import PostgresStateStore
                store = PostgresStateStore(database_url)
            else:
                if os.environ.get('RENDER') == 'true':
                    raise RuntimeError('Render deployment requires DATABASE_URL; ephemeral journal refused')
                store = StateStore(data_dir / "paper.sqlite3")
            try:
                holder["engine"] = PaperEngine(store)
            except Exception:
                store.close()
                raise
        runtime = holder["engine"]
        app.state.engine = runtime
        if tg_reporter:
            tg_reporter.app_state_getter = runtime.snapshot
            tg_reporter.runtime = runtime
        polling = None
        if start_workers:
            runtime.start()
            if tg_reporter and tg_reporter.enabled:
                polling = asyncio.create_task(tg_reporter.dp.start_polling(tg_reporter.bot, handle_signals=False))
        try:
            yield
        finally:
            if polling:
                polling.cancel()
                try:
                    await polling
                except asyncio.CancelledError:
                    pass
                await tg_reporter.bot.session.close()
            await asyncio.to_thread(runtime.shutdown)

    app = FastAPI(title="Tbot PAPER", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def runtime():
        if holder["engine"] is None:
            raise HTTPException(503, "Runtime has not started")
        return holder["engine"]

    def valid_cookie(value):
        try:
            stamp, signature = value.split(".", 1)
            if not 0 <= time.time() - int(stamp) < 12 * 3600:
                return False
            expected = hmac.new(cookie_key, stamp.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(signature, expected)
        except (ValueError, AttributeError, TypeError):
            return False

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        public = request.url.path in {"/", "/healthz", "/favicon.ico"}
        bearer = request.headers.get("authorization", "")
        authorized = hmac.compare_digest(bearer.encode(), ("Bearer " + token).encode()) or valid_cookie(request.cookies.get("tbot_session"))
        if not public and not authorized:
            return JSONResponse({"detail": "Authenticate using your local dashboard token"}, status_code=401)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if origin and origin != expected:
                return JSONResponse({"detail": "Cross-origin writes are forbidden"}, status_code=403)
            if request.url.path.startswith('/api/') and request.headers.get('content-length', '0').isdigit() and int(request.headers.get('content-length', '0')) > 8192:
                return JSONResponse({"detail": "Request too large"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        return response

    @app.exception_handler(ValueError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/")
    async def index():
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "mode": "PAPER", "version": "2.1", "commit": os.environ.get('RENDER_GIT_COMMIT', 'local')[:12]}

    @app.post("/session")
    async def login(request: Request):
        auth = request.headers.get("authorization", "")
        if not hmac.compare_digest(auth.encode(), ("Bearer " + token).encode()):
            raise HTTPException(401, "Invalid dashboard token")
        stamp = str(int(time.time()))
        signature = hmac.new(cookie_key, stamp.encode(), hashlib.sha256).hexdigest()
        response = JSONResponse({"status": "ok"})
        response.set_cookie("tbot_session", stamp + "." + signature, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", max_age=12 * 3600)
        return response

    @app.get("/readyz")
    async def ready():
        state = runtime().snapshot()
        return JSONResponse({"ready": state["ready"], "last_error": state["last_error"]}, status_code=200 if state["ready"] else 503)

    @app.get("/api/status")
    async def status():
        return runtime().snapshot()

    @app.get("/api/pairs")
    async def pairs(type: str | None = None):
        return {"pairs": [p for p in runtime().pairs() if type is None or p['type'] == type]}

    @app.post("/api/pairs/toggle")
    async def toggle(payload: PairRequest):
        r = runtime()
        if payload.pair not in {'/'.join(p) for p in PAIRS}:
            raise ValueError("Unknown pair")
        with r.lock:
            r.require_owner()
            disabled = r.state['disabled']
            if payload.pair in disabled:
                disabled.remove(payload.pair)
            else:
                disabled.append(payload.pair)
            r._persist()
            return {"enabled": payload.pair not in disabled}

    @app.get("/api/logs")
    async def logs():
        with runtime().lock:
            return {"logs": list(runtime().events)}

    @app.get("/api/trades")
    async def trades():
        state = runtime().snapshot()
        return {"trades": state["recent_trades"], "stats": state["stats"]}

    @app.get("/api/chart")
    async def chart(pair: str = 'BTCUSDT/ETHUSDT'):
        r = runtime()
        with r.lock:
            model = r.models.get(pair)
            if model is None:
                return {"pair": pair, "labels": [], "z_score": [], "current_z": None}
            return {"pair": pair, "labels": model['labels'], "z_score": model['z_score'],
                    "current_z": r.zscore(model, pair) if r.fresh(pair) else None,
                    "upper_band": [r.settings.entry_z_score] * len(model['labels']),
                    "lower_band": [-r.settings.entry_z_score] * len(model['labels'])}

    @app.get("/api/analytics")
    async def analytics():
        r = runtime()
        with r.lock:
            state = r.snapshot()
            curve = [r.settings.initial_capital] + [v for _, v in r.state['equity']]
            peak, drawdown = curve[0], 0.
            for value in curve:
                peak = max(peak, value)
                drawdown = max(drawdown, (peak - value) / peak * 100)
            return dict(state['stats'], equity_curve=curve, max_drawdown_pct=drawdown,
                        total_return_pct=(curve[-1] / curve[0] - 1) * 100, sharpe_ratio=None)

    @app.get("/api/settings")
    async def settings():
        return runtime().settings.model_dump()

    @app.post("/api/settings")
    async def settings_update(payload: dict):
        runtime().update_settings(payload)
        return {"status": "ok"}

    @app.post("/api/demo/open_trade")
    async def open_trade(payload: PairRequest):
        return {"status": "ok", "position": runtime().open(payload.pair, payload.side)}

    @app.post("/api/positions/close")
    @app.post("/api/demo/close_trade")
    async def close_trade(payload: CloseRequest):
        return runtime().close(payload.pair)

    @app.post("/api/kill")
    async def pause():
        return runtime().stop(liquidate=False)

    @app.post("/api/stop")
    async def stop():
        return runtime().stop(liquidate=True)

    @app.post("/api/resume")
    async def resume():
        runtime().resume()
        return {"status": "ok", "autopilot": True}

    app.get_state = lambda: runtime().snapshot()
    return app
