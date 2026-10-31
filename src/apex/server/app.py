"""The collection server: roll entries from phones, watch files from the CLI."""

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from apex.config import clear_server_info, write_server_info

from . import doomscroll
from .auth import Auth, PasswordGate, client_key, safe_next, set_session
from .db import Conflict, NotFound, Store, local_day

WEB_DIR = Path(__file__).parent / "web"
MAX_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_NAME = 80


def _clean_name(value: str | None) -> str | None:
    """Collapse runs of whitespace so "Mia  " and "Mia" are one roster entry."""
    if value is None:
        return None
    return " ".join(value.split()) or None


class RollCreate(BaseModel):
    buggy: str = Field(min_length=1, max_length=MAX_NAME)
    driver: str | None = Field(default=None, max_length=MAX_NAME)
    notes: str = Field(default="", max_length=4000)

    @field_validator("buggy", "driver", mode="before")
    @classmethod
    def _names(cls, v):
        return _clean_name(v) if isinstance(v, str) else v

    @field_validator("notes")
    @classmethod
    def _notes(cls, v: str) -> str:
        return v.strip()


class Assignment(BaseModel):
    roll_id: int


def create_app(*, password: str, data_dir: Path, advertise_url: str | None = None) -> FastAPI:
    """Build the app. `password` is required; there is no unprotected mode.

    With `advertise_url`, the server records its URL and local token in the data
    dir while running, so `apex collect-rolls` on this machine needs no password.
    """
    auth = Auth(password)
    store = Store(data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if advertise_url:
            write_server_info(data_dir, advertise_url, auth.local_token)
        try:
            yield
        finally:
            if advertise_url:
                clear_server_info(data_dir)

    app = FastAPI(
        title="APEX", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.auth = auth
    app.state.store = store
    app.add_middleware(PasswordGate, auth=auth)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    # ── pages ────────────────────────────────────────────────────────────────

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/login", include_in_schema=False)
    def login_page() -> FileResponse:
        return FileResponse(WEB_DIR / "login.html", headers={"Cache-Control": "no-store"})

    @app.post("/login", include_in_schema=False)
    def login(
        request: Request, password: Annotated[str, Form()], next: Annotated[str, Form()] = "/"
    ) -> Response:
        client = client_key(request)
        if (delay := auth.lockout_remaining(client)) > 0:
            return RedirectResponse(f"/login?locked={max(1, int(delay))}", status_code=303)
        if not auth.password_matches(password):
            auth.record_failure(client)
            return RedirectResponse("/login?error=1", status_code=303)
        auth.clear_failures(client)
        return set_session(RedirectResponse(safe_next(next), status_code=303), auth)

    @app.post("/logout", include_in_schema=False)
    def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("apex_session", path="/")
        return response

    # ── collection API ───────────────────────────────────────────────────────

    @app.get("/api/ping")
    def ping() -> dict:
        """Lets a client check its server URL and password without side effects."""
        return {"ok": True}

    @app.get("/api/collection")
    def collection(all: bool = False) -> dict:
        """Everything the collection tab shows, in one read it can poll."""
        today = local_day(datetime.now().astimezone())
        day = None if all else today
        return {
            "today": today,
            "server_time_utc": datetime.now().astimezone().isoformat(),
            "rolls": store.list_rolls(day=day),
            "files": store.list_files(day=day),
            "roster": store.roster(),
        }

    @app.post("/api/rolls", status_code=201)
    def create_roll(body: RollCreate) -> dict:
        return store.create_roll(body.buggy, body.driver, body.notes)

    @app.put("/api/rolls/{roll_id}")
    def update_roll(roll_id: int, body: RollCreate) -> dict:
        """Replace buggy, driver and notes; a blank driver or notes clears it."""
        try:
            return store.update_roll(roll_id, body.buggy, body.driver, body.notes)
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.delete("/api/rolls/{roll_id}", status_code=204)
    def delete_roll(roll_id: int) -> Response:
        try:
            store.delete_roll(roll_id)
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        return Response(status_code=204)

    # ── doomscROLL (read-only) ────────────────────────────────────────────────

    @app.get("/api/course")
    def course() -> dict:
        return doomscroll.course_info(store.course)

    @app.get("/api/feed")
    def feed(
        buggy: str | None = None,
        driver: str | None = None,
        day: str | None = None,
        all: bool = False,
        cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=doomscroll.MAX_PAGE_SIZE)] = doomscroll.PAGE_SIZE,
    ) -> dict:
        """Roll cards, newest first; the first page also carries the leaderboard."""
        return doomscroll.feed(
            store, buggy=buggy, driver=driver, day=day,
            include_no_data=all, cursor=cursor, limit=limit,
        )

    @app.get("/api/rolls/{roll_id}/analysis")
    def roll_analysis(roll_id: int) -> dict:
        try:
            return doomscroll.roll_analysis(store, roll_id)
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/files")
    async def upload_file(
        request: Request,
        filename: Annotated[str, Query(min_length=1, max_length=255)],
        buggy: Annotated[str | None, Query(max_length=MAX_NAME)] = None,
    ) -> dict:
        """Raw FIT bytes in the body. Idempotent: the same bytes are stored once."""
        content = await request.body()
        if not content:
            raise HTTPException(400, "empty upload")
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file too large to be a watch recording")
        status, file = await run_in_threadpool(
            store.ingest_file, content, Path(filename).name, _clean_name(buggy)
        )
        return {"status": status, "file": file}

    @app.put("/api/files/{file_id}/roll")
    def assign(file_id: int, body: Assignment) -> dict:
        try:
            return store.assign_file(file_id, body.roll_id)
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete("/api/files/{file_id}/roll")
    def unassign(file_id: int) -> dict:
        try:
            return store.unassign_file(file_id)
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    return app
