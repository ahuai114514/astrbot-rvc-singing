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
from astrbot.core.platform.message_type import MessageType

HELP = (
    "角色唱歌\n"
    "/点歌 歌名：搜索后120秒内回复序号选歌，回复0退出\n"
    "/点歌 B站链接：直接生成，默认带伴奏\n"
    "默认从头唱，满60秒后寻找静音句尾，最长300秒\n"
    "末尾可加：纯人声、从60秒、升调2 或 降调2\n"
    "可以连续点歌，歌曲会依次生成\n"
    "/唱歌进度：查看你在当前会话的全部任务\n"
    "/纯人声、/带伴奏：生成成功后10分钟内获取对应版本\n"
    "/点歌转发 群号或QQ号：10分钟内转发你的上一首\n"
    "示例：/点歌 BV1LiVKzFEVf 从60秒"
)
OPTIONS = re.compile(r"(?:从\s*(\d+)\s*秒|(升调|降调)\s*(\d+)|纯人声|带伴奏)\s*$")
SELECTION_TTL = 120
VERSION_TTL = 600


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


@register("astrbot_plugin_rvc_singing", "ahuai", "角色音色翻唱，支持B站点歌", "1.2.5")
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
        # Older versions keyed pending entries by owner.
        self.state["pending"] = {
            pending["request_id"]: {**pending, "owner": pending.get("owner", key)}
            for key, pending in self.state["pending"].items()
        }
        self.client = httpx.AsyncClient(timeout=45, trust_env=False)
        self.tasks: dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()

    async def initialize(self):
        for request_id, pending in self.state["pending"].items():
            self.tasks[request_id] = asyncio.create_task(self.track(pending))
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
            part = target.with_suffix(f".{uuid.uuid4().hex}.part")
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
            sent = await self.context.send_message(
                origin, MessageChain([Record.fromFileSystem(str(target))])
            )
        except Exception:
            title = re.sub(r'[\\/:*?"<>|]', "_", title)[:70] or "角色翻唱"
            suffix = "纯人声" if kind == "vocals" else "带伴奏"
            sent = await self.context.send_message(
                origin,
                MessageChain([File(file=str(target), name=f"{title}-{suffix}.mp3")]),
            )
        if sent is False:
            raise ValueError("找不到目标会话对应的机器人连接")

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
        return "120秒内回复序号选择演唱版本，回复0退出：\n" + "\n".join(
            f"{i}. {r['title']}\n   {r['author']} · {r['duration']}\n{r['url']}"
            for i, r in enumerate(results, 1)
        )

    async def accept(
        self, event: AstrMessageEvent, source: str, options: dict, title: str = ""
    ) -> str:
        async with self.lock:
            owner = self.owner(event)
            now = time.time()
            event_id = str(
                getattr(event.message_obj, "message_id", "") or uuid.uuid4().hex
            )
            request_id = hashlib.sha256(
                f"{owner}|{event_id}|{source}|{options}".encode()
            ).hexdigest()
            if request_id in self.state["pending"]:
                return "这首歌已提交，可用 /唱歌进度 查看。"
            pending = {
                "owner": owner,
                "origin": event.unified_msg_origin,
                "source": source,
                "title": title,
                "options": options,
                "request_id": request_id,
                "created": now,
            }
            self.state["pending"][request_id] = pending
            self.persist()
            try:
                job = await self.submit(pending)
            except (httpx.TimeoutException, httpx.TransportError):
                self.tasks[request_id] = asyncio.create_task(self.track(pending))
                return "请求已保存，正在确认网关任务状态；不会重复提交计费。"
            except Exception:
                self.state["pending"].pop(request_id, None)
                self.persist()
                raise
            pending["job_id"] = job["id"]
            self.persist()
            self.tasks[request_id] = asyncio.create_task(self.track(pending))
            return (
                f"已接单 · {job.get('title') or title or source}\n"
                f"当前状态：{job['stage']}。可继续 /点歌，或用 /唱歌进度 查看。\n"
                "完成后10分钟内可用 /纯人声 或 /带伴奏 获取对应版本。"
            )

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

    @staticmethod
    def describe(error: Exception) -> str:
        return (
            str(error)[:300]
            if isinstance(error, ValueError)
            else f"连接或处理失败（{type(error).__name__}）"
        )

    async def track(self, pending: dict):
        request_id = pending["request_id"]
        try:
            # Reuse the saved request ID when the first response was lost.
            while not pending.get("job_id"):
                try:
                    job = await self.submit(pending)
                except (httpx.TimeoutException, httpx.TransportError):
                    await asyncio.sleep(15)
                    continue
                pending["job_id"] = job["id"]
                self.persist()
            while True:
                try:
                    job = await self.request("GET", f"/v1/jobs/{pending['job_id']}")
                except (httpx.TimeoutException, httpx.TransportError):
                    await asyncio.sleep(15)
                    continue
                if job["status"] == "success":
                    self.state["last"][pending["owner"]] = {
                        "job_id": job["id"],
                        "title": job["title"],
                        "kind": pending["options"]["kind"],
                        "completed_at": job.get("completed_at", time.time()),
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
            title = pending.get("title") or pending["source"]
            await self.send_text(
                pending["origin"], f"《{title}》唱歌任务失败：{self.describe(error)}"
            )
        finally:
            if not asyncio.current_task().cancelling():
                self.state["pending"].pop(request_id, None)
                self.persist()
            self.tasks.pop(request_id, None)

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
        async for result in self.select(event):
            yield result

    @filter.command("选歌")
    async def select(self, event: AstrMessageEvent):
        event.stop_event()
        try:
            owner = self.owner(event)
            selection = self.state["selections"].get(owner)
            if not selection or time.time() - selection["created"] >= SELECTION_TTL:
                return
            value = self.argument(event, "选歌")
            if value == "0":
                self.state["selections"].pop(owner)
                self.persist()
                yield event.plain_result("已退出选歌。")
                return
            if not value.isdigit() or not 1 <= int(value) <= len(selection["results"]):
                yield event.plain_result("请直接回复候选列表中的序号，例如 1")
                return
            song = selection["results"][int(value) - 1]
            yield event.plain_result(
                await self.accept(event, song["url"], selection["options"], song["title"])
            )
        except Exception as error:
            yield event.plain_result(self.describe(error))

    @filter.command("唱歌进度")
    async def progress(self, event: AstrMessageEvent):
        owner = self.owner(event)
        tasks = [
            task for task in self.state["pending"].values() if task["owner"] == owner
        ]
        if not tasks:
            yield event.plain_result("当前没有正在生成的歌曲。")
            return
        lines = ["你的点歌进度"]
        for number, task in enumerate(tasks, 1):
            title = task.get("title") or task["source"]
            stage = "正在确认网关受理结果"
            if job_id := task.get("job_id"):
                try:
                    job = await self.request("GET", f"/v1/jobs/{job_id}")
                    title = job.get("title") or title
                    stage = job["stage"]
                except Exception as error:
                    stage = self.describe(error)
            lines.append(f"{number}. {title} · {stage}")
        yield event.plain_result("\n".join(lines))

    async def repeat(self, event: AstrMessageEvent, kind: str):
        event.stop_event()
        last = self.state["last"].get(self.owner(event))
        if not last:
            yield event.plain_result("还没有生成过歌曲，先用 /点歌。")
            return
        completed_at = last.get("completed_at")
        if completed_at is None or time.time() - completed_at >= VERSION_TTL:
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

    @filter.command("点歌转发")
    async def forward_song(self, event: AstrMessageEvent):
        event.stop_event()
        if event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("点歌转发目前仅支持 OneBot QQ。")
            return

        target = re.fullmatch(
            r"(?:(群|QQ)\s*)?([1-9][0-9]{0,19})",
            self.argument(event, "点歌转发"),
            re.IGNORECASE,
        )
        if not target:
            yield event.plain_result("用法：/点歌转发 群号或QQ号，例如 /点歌转发 123456789")
            return

        last = self.state["last"].get(self.owner(event))
        if not last or time.time() - (last.get("completed_at") or 0) >= VERSION_TTL:
            yield event.plain_result("请在原会话点歌，并在生成成功后10分钟内转发。")
            return

        try:
            target_type, target_id = target.groups()
            routes = []
            if target_type in {None, "群"}:
                groups = await event.bot.call_action("get_group_list")
                if any(str(group["group_id"]) == target_id for group in groups):
                    routes.append(MessageType.GROUP_MESSAGE)
            if target_type != "群":
                friends = await event.bot.call_action("get_friend_list")
                if any(str(friend["user_id"]) == target_id for friend in friends):
                    routes.append(MessageType.FRIEND_MESSAGE)
            if not routes:
                yield event.plain_result("找不到目标。机器人需要已加入该群或已添加该 QQ 为好友。")
                return
            if len(routes) > 1:
                yield event.plain_result(
                    f"群号和QQ号重复，请用 /点歌转发 群 {target_id} 或 /点歌转发 QQ {target_id}。"
                )
                return
            origin = f"{event.get_platform_id()}:{routes[0].value}:{target_id}"
            await self.send_audio(
                origin, last["job_id"], last.get("kind", "mixed"), last["title"]
            )
        except Exception as error:
            yield event.plain_result(f"转发失败：{self.describe(error)}")
        else:
            label = "群" if routes[0] == MessageType.GROUP_MESSAGE else "QQ"
            yield event.plain_result(f"已转发到{label} {target_id}。")

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
            song = selection["results"][choice - 1]
            return await self.accept(
                event, song["url"], selection["options"], song["title"]
            )
        except Exception as error:
            return self.describe(error)

    async def terminate(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.aclose()
