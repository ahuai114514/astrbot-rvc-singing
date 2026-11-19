from __future__ import annotations

import hmac
import re
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .config import Settings
from .jobs import Jobs


class CreateRequest(BaseModel):
    source: str = Field(min_length=3, max_length=2000)
    start: int = Field(default=0, ge=0, le=3600)
    duration: int = Field(default=60, ge=60, le=60)
    pitch: int = Field(default=0, ge=-12, le=12)
    request_id: str = Field(min_length=1, max_length=200)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.jobs = Jobs(settings)
        await app.state.jobs.start()
        try:
            yield
        finally:
            await app.state.jobs.close()

    app = FastAPI(
        title="角色唱歌网关", lifespan=lifespan, docs_url=None, redoc_url=None
    )

    def authorize(authorization: str | None = Header(default=None)):
        if not hmac.compare_digest(
            (authorization or "").encode(), f"Bearer {settings.token}".encode()
        ):
            raise HTTPException(401, "网关令牌错误")

    @app.get("/health")
    async def health():
        return {
            "status": "ok", "backend": f"runninghub-{settings.conversion_backend}", "version": "1.5.0",
            "separator": "melband-roformer-fp32", "harmony": bool(settings.harmony_workflow_id),
            **({"auto_f0_adjust": False} if settings.conversion_backend == "seedvc"
               else {"index_rate": settings.index_rate}),
        }

    @app.get("/v1/search", dependencies=[Depends(authorize)])
    async def search(q: str = Query(min_length=1, max_length=120)):
        try:
            return {"results": await app.state.jobs.bili.search(q)}
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @app.post("/v1/jobs", dependencies=[Depends(authorize)])
    async def submit(body: CreateRequest):
        try:
            source = await app.state.jobs.bili.resolve(body.source)
            job, cached = app.state.jobs.create(
                source, body.start, body.duration, body.pitch, body.request_id
            )
            return {**app.state.jobs.public(job), "reused": cached}
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    def find(job_id: str):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise HTTPException(404, "任务不存在")
        job = app.state.jobs.get(job_id)
        if not job:
            raise HTTPException(404, "任务不存在或已过期")
        return job

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(authorize)])
    async def status(job_id: str):
        return app.state.jobs.public(find(job_id))

    @app.get("/v1/jobs/{job_id}/audio/{kind}", dependencies=[Depends(authorize)])
    async def audio(job_id: str, kind: Literal["mixed", "vocals"]):
        job = find(job_id)
        path = app.state.jobs.directory(job) / f"{kind}.mp3"
        if job["status"] != "success" or not path.is_file():
            raise HTTPException(409, "歌曲尚未完成或缓存已过期")
        return FileResponse(path, media_type="audio/mpeg", filename=f"{kind}.mp3")

    return app
