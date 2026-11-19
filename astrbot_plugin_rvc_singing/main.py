from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from typing import Any

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Plain, Record
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.config.astrbot_config import AstrBotConfig

HELP = (
    "角色唱歌\n"
    "/点歌 歌名：搜索后1分钟内直接回复序号选择版本\n"
    "/点歌 B站链接：直接生成，默认带伴奏\n"
    "默认从头唱，满60秒后寻找静音句尾，最长300秒\n"
    "末尾可加：纯人声、从60秒、升调2 或 降调2\n"
    "/唱歌进度：查看当前任务\n"
    "/纯人声：获取上一首纯人声，不重新生成\n"
    "/带伴奏：获取上一首带伴奏版本\n"
    "示例：/点歌 BV1LiVKzFEVf 从60秒"
)
OPTIONS = re.compile(r"(?:从\s*(\d+)\s*秒|(升调|降调)\s*(\d+)|纯人声|带伴奏)\s*$")
SELECTION_TTL = 60


def parse_request(text: str, defaults: dict) -> tuple[str, dict]:
    options = dict(defaults)
    text = text.strip()
    while match := OPTIONS.search(text):
        token = match.group().strip()
        if match[1] is not None:
            options["start"] = int(match[1])
        elif match[2]:
            options["pitch"] = int(match[3]) * (1 if match[2] == "升调" else -1)
        elif token in {"纯人声", "带伴奏"}:
            options["kind"] = "vocals" if token == "纯人声" else "mixed"
        text = text[: match.start()].strip()
    if not 0 <= options["start"] <= 3600 or not -12 <= options["pitch"] <= 12:
        raise ValueError("开始时间须为 0～3600 秒，升降调不超过 12 半音")
    if re.search(r"时长\s*\d+\s*秒", text):
        raise ValueError("演唱按60～300秒自动寻找静音句尾，无需指定时长")
    options["duration"] = 60
    return text, options


@register("astrbot_plugin_rvc_singing", "ahuai", "角色音色翻唱，支持B站点歌", "1.2.0")
class RVCSinging(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data = StarTools.get_data_dir("astrbot_plugin_rvc_singing")
        self.state_path = self.data / "state.json"
        self.audio_dir = self.data / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.state = {"pending": {}, "last": {}, "selections": {}}
        if self.state_path.is_file():
            self.state.update(json.loads(self.state_path.read_text(encoding="utf-8")))
            self.state.pop("usage", None)
        self.client = httpx.AsyncClient(timeout=45, trust_env=False)
        self.tasks: dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()

    async def initialize(self):
        for owner, pending in list(self.state["pending"].items()):
            if pending.get("job_id"):
                self.tasks[owner] = asyncio.create_task(self.watch(owner, pending))
            else:
                # A saved idempotency key permits safe resubmission after reload.
                self.tasks[owner] = asyncio.create_task(self.resume(owner, pending))
        self.cleanup_audio()

    def persist(self):
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.state_path)

    def cleanup_audio(self):
        cutoff = time.time() - 86400
        for path in self.audio_dir.glob("*.mp3"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)

    def defaults(self) -> dict:
        return {
            "start": int(self.config.get("start", 0)),
            "duration": 60,
            "pitch": int(self.config.get("pitch", 0)),
            "kind": "mixed",
        }

    def owner(self, event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}|{event.get_sender_id()}"

    async def request(self, method: str, path: str, **kwargs) -> Any:
        token = str(self.config.get("gateway_token", ""))
        if not token:
            raise ValueError("请先配置唱歌网关令牌")
        base = str(self.config.get("gateway_url", "http://127.0.0.1:9002")).rstrip("/")
        response = await self.client.request(
            method, base + path, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        if response.status_code >= 400:
            try:
                message = response.json().get("detail", "网关请求失败")
            except ValueError:
                message = "网关请求失败"
            raise ValueError(str(message)[:300])
        return response.json()

    async def send_text(self, origin: str, text: str):
        await self.context.send_message(origin, MessageChain([Plain(text)]))

    async def send_audio(self, origin: str, job_id: str, kind: str, title: str):
        target = self.audio_dir / f"{job_id}-{kind}.mp3"
        if not target.exists():
            base = str(self.config.get("gateway_url", "http://127.0.0.1:9002")).rstrip(
                "/"
            )
            part = target.with_suffix(".part")
            try:
                async with self.client.stream(
                    "GET",
                    f"{base}/v1/jobs/{job_id}/audio/{kind}",
                    headers={
                        "Authorization": f"Bearer {self.config.get('gateway_token', '')}"
                    },
                    timeout=120,
                ) as response:
                    if response.status_code != 200:
                        raise ValueError("歌曲缓存已过期，请重新点歌")
                    size = 0
                    with part.open("wb") as output:
                        async for block in response.aiter_bytes():
                            size += len(block)
                            if size > 30 * 1024 * 1024:
                                raise ValueError("歌曲文件过大，请缩短片段")
                            output.write(block)
                part.replace(target)
            finally:
                part.unlink(missing_ok=True)
        try:
            await self.context.send_message(
                origin, MessageChain([Record.fromFileSystem(str(target))])
            )
        except Exception:
            title = re.sub(r'[\\/:*?"<>|]', "_", title)[:70] or "角色翻唱"
            suffix = "纯人声" if kind == "vocals" else "带伴奏"
            await self.context.send_message(
                origin,
                MessageChain([File(file=str(target), name=f"{title}-{suffix}.mp3")]),
            )

    async def search(self, event: AstrMessageEvent, keyword: str, options: dict) -> str:
        data = await self.request("GET", "/v1/search", params={"q": keyword})
        results = data["results"]
        if not results:
            return "没有找到符合条件的歌曲，请换个关键词或直接发 B 站链接。"
        self.state["selections"][self.owner(event)] = {
            "results": results,
            "options": options,
            "created": time.time(),
        }
        self.persist()
        return "选一个演唱版本，1分钟内直接回复序号（如 1、2）：\n" + "\n".join(
            f"{i}. {r['title']}\n   {r['author']} · {r['duration']}\n   {r['url']}"
            for i, r in enumerate(results, 1)
        )

    async def accept(self, event: AstrMessageEvent, source: str, options: dict) -> str:
        async with self.lock:
            owner = self.owner(event)
            if owner in self.state["pending"]:
                return "你已有一首歌正在生成，可用 /唱歌进度 查看。"
            now = time.time()
            event_id = str(
                getattr(event.message_obj, "message_id", "") or uuid.uuid4().hex
            )
            request_id = hashlib.sha256(
                f"{owner}|{event_id}|{source}|{options}".encode()
            ).hexdigest()
            pending = {
                "origin": event.unified_msg_origin,
                "source": source,
                "options": options,
                "request_id": request_id,
                "created": now,
            }
            self.state["pending"][owner] = pending
            self.persist()
            try:
                job = await self.submit(pending)
            except (httpx.TimeoutException, httpx.TransportError):
                self.tasks[owner] = asyncio.create_task(self.resume(owner, pending))
                return "请求已保存，正在确认网关任务状态；不会重复提交计费。"
            except Exception:
                self.state["pending"].pop(owner, None)
                self.persist()
                raise
            pending["job_id"] = job["id"]
            self.persist()
            self.tasks[owner] = asyncio.create_task(self.watch(owner, pending))
            return "正在生成，最长300秒，可用 /唱歌进度 查看。\n生成后可用 /纯人声 或 /带伴奏 切换版本，无需重新生成。"

    async def submit(self, pending: dict):
        options = pending["options"]
        return await self.request(
            "POST",
            "/v1/jobs",
            json={
                "source": pending["source"],
                "start": options["start"],
                "duration": 60,
                "pitch": options["pitch"],
                "request_id": pending["request_id"],
            },
        )

    async def resume(self, owner: str, pending: dict):
        try:
            while True:
                try:
                    job = await self.submit(pending)
                    pending["job_id"] = job["id"]
                    self.persist()
                    await self.watch(owner, pending)
                    return
                except (httpx.TimeoutException, httpx.TransportError):
                    await asyncio.sleep(15)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.state["pending"].pop(owner, None)
            self.persist()
            await self.send_text(pending["origin"], f"点歌失败：{self.describe(error)}")
        finally:
            self.tasks.pop(owner, None)

    @staticmethod
    def describe(error: Exception) -> str:
        return (
            str(error)[:300]
            if isinstance(error, ValueError)
            else f"连接或处理失败（{type(error).__name__}）"
        )

    async def watch(self, owner: str, pending: dict):
        try:
            while True:
                try:
                    job = await self.request("GET", f"/v1/jobs/{pending['job_id']}")
                except (httpx.TimeoutException, httpx.TransportError):
                    await asyncio.sleep(15)
                    continue
                if job["status"] == "success":
                    self.state["last"][owner] = {
                        "job_id": job["id"],
                        "title": job["title"],
                    }
                    self.persist()
                    await self.send_audio(
                        pending["origin"],
                        job["id"],
                        pending["options"]["kind"],
                        job["title"],
                    )
                    break
                if job["status"] == "failed":
                    raise ValueError(job.get("error", "生成失败"))
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("[rvc-singing] task failed: %s", type(error).__name__)
            await self.send_text(
                pending["origin"], f"唱歌任务失败：{self.describe(error)}"
            )
        finally:
            if not asyncio.current_task().cancelling():
                self.state["pending"].pop(owner, None)
                self.persist()
            self.tasks.pop(owner, None)

    @staticmethod
    def argument(event: AstrMessageEvent, name: str) -> str:
        return re.sub(
            rf"^/?{re.escape(name)}\s*", "", event.message_str.strip(), count=1
        )

    @filter.command("点歌")
    async def sing(self, event: AstrMessageEvent):
        try:
            source, options = parse_request(
                self.argument(event, "点歌"), self.defaults()
            )
            if not source:
                yield event.plain_result(HELP)
                return
            if re.search(r"BV[0-9A-Za-z]{10}|https?://", source):
                result = await self.accept(event, source, options)
            else:
                result = await self.search(event, source, options)
            yield event.plain_result(result)
        except Exception as error:
            yield event.plain_result(self.describe(error))

    @filter.command("搜歌")
    async def search_command(self, event: AstrMessageEvent):
        try:
            keyword = self.argument(event, "搜歌")
            yield event.plain_result(
                await self.search(event, keyword, self.defaults())
                if keyword
                else "用法：/搜歌 歌名"
            )
        except Exception as error:
            yield event.plain_result(self.describe(error))

    @filter.regex(r"^[0-9]+$", priority=100)
    async def select_number(self, event: AstrMessageEvent):
        """Handle numeric replies only while this sender has a live selection.

        Args:
            event: Incoming message in the sender's original conversation.

        Yields:
            Selection feedback when a pending list is still valid.
        """
        selection = self.state["selections"].get(self.owner(event))
        if not selection or time.time() - selection["created"] >= SELECTION_TTL:
            return
        event.stop_event()
        async for result in self.select(event):
            yield result

    @filter.command("选歌")
    async def select(self, event: AstrMessageEvent):
        try:
            selection = self.state["selections"].get(self.owner(event))
            if not selection or time.time() - selection["created"] >= SELECTION_TTL:
                yield event.plain_result("请先 /点歌 歌名 搜索，候选版本保留 1 分钟。")
                return
            value = self.argument(event, "选歌")
            if not value.isdigit() or not 1 <= int(value) <= len(selection["results"]):
                yield event.plain_result("请直接回复候选列表中的序号，例如 1")
                return
            source = selection["results"][int(value) - 1]["url"]
            yield event.plain_result(
                await self.accept(event, source, selection["options"])
            )
        except Exception as error:
            yield event.plain_result(self.describe(error))

    @filter.command("唱歌进度")
    async def progress(self, event: AstrMessageEvent):
        pending = self.state["pending"].get(self.owner(event))
        if not pending:
            yield event.plain_result("当前没有正在生成的歌曲。")
            return
        try:
            if not pending.get("job_id"):
                text = "正在确认网关受理结果。"
            else:
                job = await self.request("GET", f"/v1/jobs/{pending['job_id']}")
                text = f"当前进度：{job['stage']}\n任务：{job['id']}"
            yield event.plain_result(text)
        except Exception as error:
            yield event.plain_result(self.describe(error))

    async def repeat(self, event: AstrMessageEvent, kind: str):
        last = self.state["last"].get(self.owner(event))
        if not last:
            yield event.plain_result("还没有生成过歌曲，先用 /点歌。")
            return
        try:
            await self.send_audio(
                event.unified_msg_origin, last["job_id"], kind, last["title"]
            )
        except Exception as error:
            yield event.plain_result(self.describe(error))

    @filter.command("纯人声")
    async def vocals(self, event: AstrMessageEvent):
        async for result in self.repeat(event, "vocals"):
            yield result

    @filter.command("带伴奏")
    async def accompanied(self, event: AstrMessageEvent):
        async for result in self.repeat(event, "mixed"):
            yield result

    @filter.command("点歌帮助")
    async def help(self, event: AstrMessageEvent):
        yield event.plain_result(HELP)

    @filter.llm_tool(name="sing_song")
    async def sing_song_tool(
        self,
        event: AstrMessageEvent,
        song: str,
        start: int | None = None,
        vocals_only: bool = False,
    ) -> str:
        """用角色音色唱歌。用户要求唱歌时调用，支持B站链接或歌名；默认带伴奏。歌名先返回版本列表，再让用户选，不能擅自重复调用。

        Args:
            song(string): 歌名、B站链接或BV号。
            start(int, optional): 截取起点，未指定时使用插件配置，默认从头；满60秒后自动找句尾，最长300秒。
            vocals_only(bool): 用户明确要求纯人声时为true，默认false。
        """
        try:
            options = self.defaults()
            if start is not None:
                options["start"] = start
            options["kind"] = "vocals" if vocals_only else "mixed"
            song, options = parse_request(song, options)
            if re.search(r"BV[0-9A-Za-z]{10}|https?://", song):
                return await self.accept(event, song, options)
            return await self.search(event, song, options)
        except Exception as error:
            return self.describe(error)

    @filter.llm_tool(name="confirm_song")
    async def confirm_song_tool(self, event: AstrMessageEvent, choice: int) -> str:
        """用户确认之前搜歌列表中的版本后，选择该版本并开始唱歌。

        Args:
            choice(int): 用户选中的候选序号，从1开始。
        """
        try:
            selection = self.state["selections"].get(self.owner(event))
            if not selection or time.time() - selection["created"] >= SELECTION_TTL:
                return "没有有效候选列表，请先搜索。"
            if not 1 <= choice <= len(selection["results"]):
                return "序号不在候选列表中。"
            return await self.accept(
                event, selection["results"][choice - 1]["url"], selection["options"]
            )
        except Exception as error:
            return self.describe(error)

    async def terminate(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.aclose()
