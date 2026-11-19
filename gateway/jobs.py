from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

from .cloud import Cloud, CloudError, CloudPending
from .config import Settings
from .media import (
    Bilibili, combine_vocals, encode_vocals, mix, prepare_separation,
    restore_harmony, restore_separation, trim,
)

logger = logging.getLogger(__name__)
ACTIVE = {"queued", "running"}
STAGES = {
    "queued": "排队中",
    "download": "下载歌曲",
    "separation": "分离人声",
    "harmony": "分离主唱与和声",
    "conversion": "转换角色音色",
    "mix": "合成音频",
    "done": "已完成",
}


def cache_key(
    url: str, start: int, duration: int, pitch: int, model: str,
    separation: str = "2120474755580129281",
    harmony: str = "", index_rate: float = 1.0,
    conversion_backend: str = "rvc", conversion_workflow: str = "",
    reference_file: str = "",
) -> str:
    values = [url, start, duration, pitch, model, f"rmvpe-{index_rate}-0.33-phrase60-300-v3"]
    if conversion_backend == "seedvc":
        values = [url, start, duration, pitch,
                  "seedvc-f0-on-auto-off-steps30-cfg0.7-speed1-v1",
                  conversion_workflow, reference_file]
    values.append("phrase60-300-v3")
    if separation:
        values.append(f"melband-fp32-low12-v1:{separation}")
    if harmony:
        values.append(f"karaoke5-calibrated-v1:{harmony}")
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()[:32]


class Jobs:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.files = settings.data_dir / "audio"
        self.files.mkdir(exist_ok=True)
        self.db = sqlite3.connect(settings.data_dir / "jobs.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, key TEXT NOT NULL, body TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, job_id TEXT NOT NULL)"
        )
        self.db.commit()
        self.cloud = Cloud(settings)
        self.bili = Bilibili(settings.cookies_file)
        self.wakeup = asyncio.Event()
        self.worker: asyncio.Task | None = None

    def all(self) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT body FROM jobs ORDER BY rowid")
        ]

    def get(self, job_id: str) -> dict | None:
        row = self.db.execute("SELECT body FROM jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, job: dict):
        job["updated"] = time.time()
        self.db.execute(
            "INSERT OR REPLACE INTO jobs(id,key,body) VALUES (?,?,?)",
            (job["id"], job["key"], json.dumps(job, ensure_ascii=False)),
        )
        self.db.commit()

    def create(
        self, url: str, start: int, duration: int, pitch: int, request_id: str
    ) -> tuple[dict, bool]:
        self.cleanup()
        row = self.db.execute(
            "SELECT job_id FROM requests WHERE id=?", (request_id,)
        ).fetchone()
        if row:
            return self.get(row[0]), True
        key = cache_key(
            url, start, duration, pitch, self.settings.model_file,
            self.settings.separation_workflow_id,
            self.settings.harmony_workflow_id, self.settings.index_rate,
            self.settings.conversion_backend, self.settings.seed_workflow_id,
            self.settings.seed_reference_file,
        )
        jobs = self.all()
        for job in reversed(jobs):
            if job["key"] != key:
                continue
            if job["status"] == "failed" and any(
                state.get("state") == "submitting" for state in job["cloud"].values()
            ):
                raise ValueError(
                    "此前相同片段的云端提交结果未知，请先在 RunningHub 核对任务，再由管理员处理，避免重复计费"
                )
            if job["status"] in ACTIVE or (
                job["status"] == "success" and self.available(job)
            ):
                self.db.execute(
                    "INSERT INTO requests VALUES (?,?)", (request_id, job["id"])
                )
                self.db.commit()
                return job, True
        if sum(j["status"] in ACTIVE for j in jobs) >= self.settings.max_queue:
            raise ValueError("唱歌队列已满，请稍后再试")
        job = {
            "id": uuid.uuid4().hex,
            "key": key,
            "url": url,
            "start": start,
            "duration": duration,
            "pitch": pitch,
            "status": "queued",
            "stage": "queued",
            "title": "",
            "created": time.time(),
            "cloud": {},
            "separation_workflow": self.settings.separation_workflow_id,
            "harmony_workflow": self.settings.harmony_workflow_id,
            "index_rate": self.settings.index_rate,
            "conversion_backend": self.settings.conversion_backend,
            "conversion_workflow": self.settings.seed_workflow_id,
            "reference_file": self.settings.seed_reference_file,
            "usage_points": 0,
        }
        self.save(job)
        self.db.execute("INSERT INTO requests VALUES (?,?)", (request_id, job["id"]))
        self.db.commit()
        self.wakeup.set()
        return job, False

    def directory(self, job: dict) -> Path:
        return self.files / job["id"]

    def available(self, job: dict) -> bool:
        return all(
            (self.directory(job) / f"{kind}.mp3").is_file()
            for kind in ("mixed", "vocals")
        )

    def public(self, job: dict) -> dict:
        keys = (
            "id",
            "status",
            "title",
            "start",
            "duration",
            "actual_start",
            "actual_duration",
            "pitch",
            "usage_points",
            "error",
            "created",
        )
        result = {k: job[k] for k in keys if k in job}
        if job["status"] == "success":
            result["completed_at"] = job["updated"]
        result["stage"] = STAGES.get(job["stage"], job["stage"])
        result["available"] = self.available(job)
        return result

    def cleanup(self):
        cutoff = time.time() - self.settings.retention_days * 86400
        jobs = self.all()
        cached = sorted(
            (job for job in jobs if job["status"] == "success" and self.available(job)),
            key=lambda job: job["updated"],
            reverse=True,
        )
        evicted = {job["id"] for job in cached[self.settings.max_cache_entries :]}
        for job in jobs:
            if job["status"] not in ACTIVE and (
                job["updated"] < cutoff or job["id"] in evicted
            ):
                folder = self.directory(job)
                if folder.parent == self.files and folder.is_dir():
                    shutil.rmtree(folder)
                self.db.execute("DELETE FROM requests WHERE job_id=?", (job["id"],))
                self.db.execute("DELETE FROM jobs WHERE id=?", (job["id"],))
        self.db.commit()

    async def start(self):
        self.cleanup()
        for job in self.all():
            if job["status"] not in ACTIVE:
                continue
            # A process crash between POST and task-id persistence must not charge twice.
            if any(
                state.get("state") == "submitting" for state in job["cloud"].values()
            ):
                job.update(
                    status="failed",
                    error="云端提交时网关中断，结果未知；请先在 RunningHub 核对任务，避免重复计费",
                )
                self.save(job)
        self.worker = asyncio.create_task(self.run())

    async def close(self):
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        await self.cloud.close()
        await self.bili.close()
        self.db.close()

    async def cloud_step(self, job: dict, kind: str, source: Path) -> dict:
        job["stage"] = kind
        self.save(job)
        state = job["cloud"].get(kind, {})
        if state.get("result"):
            return state["result"]
        if not state.get("task_id"):
            filename = await self.cloud.upload(source)
            state = {"state": "submitting"}
            job["cloud"][kind] = state
            self.save(job)
            try:
                if kind == "separation":
                    task_id = await self.cloud.submit(
                        kind, filename, job["pitch"],
                        separation_workflow=job.get("separation_workflow", ""),
                    )
                elif kind == "harmony":
                    task_id = await self.cloud.submit(
                        kind, filename, job["pitch"],
                        harmony_workflow=job["harmony_workflow"],
                    )
                else:
                    task_id = await self.cloud.submit(
                        kind, filename, job["pitch"], index_rate=job.get("index_rate", 1.0),
                        conversion_backend=job.get("conversion_backend", "rvc"),
                        conversion_workflow=job.get("conversion_workflow", ""),
                        reference_file=job.get("reference_file", ""),
                    )
            except CloudError:
                state["state"] = "rejected"
                self.save(job)
                raise
            state.update(state="running", task_id=task_id)
            self.save(job)
        result = await self.cloud.wait(state["task_id"])
        state.update(state="done", result=result)
        charged = (result.get("usage") or {}).get("chargedPoint")
        job["usage_points"] += float(charged or 0)
        self.save(job)
        return result

    async def render(self, job: dict):
        directory = self.directory(job)
        directory.mkdir(exist_ok=True)
        clip = directory / "clip.mp3"
        if not clip.is_file():
            job["stage"] = "download"
            self.save(job)
            source, title = await self.bili.download(job["url"], directory)
            job["title"] = title
            self.save(job)
            actual_start, actual_duration = await trim(source, clip, job["start"])
            job.update(actual_start=actual_start, actual_duration=actual_duration)
            self.save(job)
        separation_input = clip
        if job.get("separation_workflow"):
            separation_input = directory / "separation-input.wav"
            if not job["cloud"].get("separation") and not separation_input.is_file():
                await prepare_separation(clip, separation_input)
        result = await self.cloud_step(job, "separation", separation_input)
        outputs = result.get("results", [])
        vocal_result = next(
            (
                r
                for r in outputs
                if "人声" in r["url"] or "%E4%BA%BA%E5%A3%B0" in r["url"]
            ),
            None,
        )
        instrumental_result = next(
            (
                r
                for r in outputs
                if "背景" in r["url"] or "%E8%83%8C%E6%99%AF" in r["url"]
            ),
            None,
        )
        # These are the app's verified output nodes, not an arbitrary output order.
        if job.get("separation_workflow"):
            vocal_result = next((r for r in outputs if str(r.get("nodeId")) == "4"), None)
            instrumental_result = next((r for r in outputs if str(r.get("nodeId")) == "18"), None)
        elif not vocal_result or not instrumental_result:
            from urllib.parse import unquote

            vocal_result = next(
                (r for r in outputs if "人声" in unquote(r["url"])), None
            )
            instrumental_result = next(
                (r for r in outputs if "背景" in unquote(r["url"])), None
            )
        if not vocal_result or not instrumental_result:
            raise ValueError("人声分离输出不匹配，请检查分离应用配置")
        suffix = ".flac" if job.get("separation_workflow") else ".mp3"
        vocals = directory / f"source-vocals{suffix}"
        instrumental = directory / f"instrumental{suffix}"
        for output, target in (
            (vocal_result, vocals),
            (instrumental_result, instrumental),
        ):
            if not target.is_file():
                if job.get("separation_workflow"):
                    raw = target.with_name(target.stem + "-low.mp3")
                    if not raw.is_file():
                        await self.cloud.download(output["url"], raw)
                    await restore_separation(raw, target)
                else:
                    await self.cloud.download(output["url"], target)
        harmony = None
        if job.get("harmony_workflow"):
            harmony_input = directory / "harmony-input.wav"
            state = job["cloud"].get("harmony", {})
            if not state.get("task_id") and not state.get("result") and not harmony_input.is_file():
                await prepare_separation(vocals, harmony_input)
            split = await self.cloud_step(job, "harmony", harmony_input)
            stems = {str(r.get("nodeId")): r for r in split.get("results", [])}
            if "5" not in stems or "6" not in stems:
                raise ValueError("主唱与和声输出不匹配")
            lead_raw = directory / "lead-raw.flac"
            harmony_raw = directory / "harmony-raw.flac"
            # Verified Karaoke workflow: output 6 is lead, output 5 is backing.
            for node, target in (("6", lead_raw), ("5", harmony_raw)):
                if not target.is_file():
                    await self.cloud.download(stems[node]["url"], target)
            lead = directory / "lead.flac"
            harmony = directory / "harmony.flac"
            if not lead.is_file() or not harmony.is_file():
                gains = await restore_harmony(vocals, lead_raw, harmony_raw, lead, harmony)
                job["harmony_gains"] = gains
                self.save(job)
            vocals = lead
        converted = await self.cloud_step(job, "conversion", vocals)
        output = next(
            (
                r
                for r in converted.get("results", [])
                if r.get("outputType") in {"flac", "wav", "mp3"}
            ),
            None,
        )
        if not output:
            raise ValueError("音色转换未返回音频")
        converted_file = directory / "converted.flac"
        if not converted_file.is_file():
            await self.cloud.download(output["url"], converted_file)
        job["stage"] = "mix"
        self.save(job)
        if harmony:
            performance = directory / "performance.flac"
            await combine_vocals(converted_file, harmony, performance)
            converted_file = performance
        await mix(converted_file, instrumental, directory / "mixed.mp3")
        await encode_vocals(converted_file, directory / "vocals.mp3")
        (directory / "cookies.txt").unlink(missing_ok=True)
        for path in directory.glob("source.*"):
            path.unlink(missing_ok=True)
        job.update(status="success", stage="done")
        self.save(job)
        self.cleanup()

    async def run(self):
        while True:
            pending = next((j for j in self.all() if j["status"] in ACTIVE), None)
            if not pending:
                self.wakeup.clear()
                try:
                    await asyncio.wait_for(self.wakeup.wait(), 3600)
                except TimeoutError:
                    self.cleanup()
                continue
            pending["status"] = "running"
            self.save(pending)
            try:
                await self.render(pending)
            except asyncio.CancelledError:
                raise
            except CloudPending:
                logger.info("job %s will resume its existing cloud task", pending["id"])
                await asyncio.sleep(30)
            except Exception as error:
                if any(
                    state.get("state") == "submitting"
                    for state in pending["cloud"].values()
                ):
                    message = "云端提交结果未知，请先在 RunningHub 核对任务，再由管理员处理，避免重复计费"
                elif isinstance(error, (ValueError, RuntimeError)):
                    message = str(error)
                else:
                    message = f"处理失败（{type(error).__name__}），请检查网关日志与 B 站 Cookie"
                logger.warning("job %s failed: %s", pending["id"], type(error).__name__)
                pending.update(status="failed", error=message)
                self.save(pending)
                (self.directory(pending) / "cookies.txt").unlink(missing_ok=True)
