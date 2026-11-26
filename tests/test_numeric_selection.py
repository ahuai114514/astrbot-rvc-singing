"""Exercise selection handlers without importing the AstrBot framework."""

import ast
import asyncio
import hashlib
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

SOURCE = Path(__file__).parents[1] / "astrbot_plugin_rvc_singing" / "main.py"


def plugin(now, selections):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    methods = []
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
            "owner",
            "argument",
            "select",
            "select_number",
            "confirm_song_tool",
            "repeat",
            "vocals",
            "accompanied",
            "forward_song",
            "accept",
            "progress",
            "track",
        }:
            node.decorator_list = []
            if node.name == "argument":
                node.decorator_list = [ast.Name(id="staticmethod", ctx=ast.Load())]
            methods.append(node)
    cls.body = methods
    cls.decorator_list = []
    cls.bases = []
    constants = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id in {"SELECTION_TTL", "VERSION_TTL"}
            for target in node.targets
        )
    ]
    module = ast.fix_missing_locations(ast.Module(body=[*constants, cls], type_ignores=[]))
    scope = {
        "re": re,
        "asyncio": asyncio,
        "hashlib": hashlib,
        "httpx": httpx,
        "uuid": uuid,
        "AstrMessageEvent": object,
        "MessageType": SimpleNamespace(
            GROUP_MESSAGE=SimpleNamespace(value="GroupMessage"),
            FRIEND_MESSAGE=SimpleNamespace(value="FriendMessage"),
        ),
        "time": SimpleNamespace(time=lambda: now),
    }
    exec(compile(module, str(SOURCE), "exec"), scope)
    obj = scope[cls.name]()
    obj.state = {"selections": selections, "last": {}}
    obj.accept = AsyncMock(return_value="accepted")
    obj.send_audio = AsyncMock()
    obj.persist = lambda: None
    obj.describe = str
    return obj


class Event:
    def __init__(self, value, origin="group-a", sender="user-a"):
        self.message_str = value
        self.unified_msg_origin = origin
        self.stopped = False
        self.get_sender_id = lambda: sender
        self.plain_result = lambda text: text

    def stop_event(self):
        self.stopped = True


def selection(created=100):
    return {
        "created": created,
        "results": [{"url": "first", "title": "第一首"}, {"url": "second", "title": "第二首"}],
        "options": {"start": 60, "kind": "mixed"},
    }


def collect(handler, event):
    async def run():
        return [result async for result in handler(event)]

    return asyncio.run(run())


def test_same_sender_can_queue_multiple_jobs_and_check_progress():
    async def run():
        obj = plugin(120, {})
        obj.accept = type(obj).accept.__get__(obj)
        obj.state["pending"] = {}
        obj.tasks = {}
        obj.lock = asyncio.Lock()
        obj.submit = AsyncMock(
            side_effect=[
                {"id": "one", "stage": "生成中"},
                {"id": "two", "stage": "排队中"},
            ]
        )
        wait_for_result = asyncio.Event()

        async def get_job(method, path):
            job_id = path.rsplit("/", 1)[-1]
            if asyncio.current_task() is current:
                return {"stage": {"one": "生成中", "two": "排队中"}[job_id]}
            await wait_for_result.wait()
            return {"id": job_id, "status": "success", "title": job_id}

        current = asyncio.current_task()
        obj.request = get_job
        event = Event("")
        event.message_obj = SimpleNamespace(message_id="1")
        options = {"kind": "mixed"}
        assert "生成中" in await obj.accept(event, "first", options, "第一首")
        assert "已提交" in await obj.accept(event, "first", options, "第一首")
        event.message_obj.message_id = "2"
        assert "排队中" in await obj.accept(event, "second", options, "第二首")
        assert [line async for line in obj.progress(event)] == [
            "你的点歌进度\n1. 第一首 · 生成中\n2. 第二首 · 排队中"
        ]
        assert [line async for line in obj.progress(Event("", sender="user-b"))] == [
            "当前没有正在生成的歌曲。"
        ]
        tasks = list(obj.tasks.values())
        wait_for_result.set()
        await asyncio.gather(*tasks)
        assert obj.send_audio.await_count == 2
        assert obj.state["pending"] == {}

    asyncio.run(run())


@pytest.mark.parametrize(("number", "source"), [("1", "first"), ("2", "second")])
def test_plain_number_selects_same_sender_in_same_conversation(number, source):
    obj = plugin(219, {"group-a|user-a": selection()})
    event = Event(number)
    assert collect(obj.select_number, event) == ["accepted"]
    song = selection()["results"][int(number) - 1]
    obj.accept.assert_awaited_once_with(event, source, selection()["options"], song["title"])
    assert event.stopped


@pytest.mark.parametrize(
    ("origin", "sender", "now"),
    [
        ("group-a", "user-b", 120),
        ("group-b", "user-a", 120),
        ("group-a", "user-a", 220),
        ("group-a", "user-a", 221),
    ],
)
def test_other_users_conversations_and_expired_numbers_pass_through(
    origin, sender, now
):
    obj = plugin(now, {"group-a|user-a": selection()})
    event = Event("1", origin, sender)
    assert collect(obj.select_number, event) == []
    assert not event.stopped
    obj.accept.assert_not_awaited()


def test_number_without_selection_passes_through():
    obj = plugin(120, {})
    event = Event("1")
    assert collect(obj.select_number, event) == []
    assert not event.stopped
    obj.accept.assert_not_awaited()


@pytest.mark.parametrize("number", ["3", "999"])
def test_out_of_range_number_does_not_submit(number):
    obj = plugin(120, {"group-a|user-a": selection()})
    event = Event(number)
    assert "例如 1" in collect(obj.select_number, event)[0]
    assert event.stopped
    obj.accept.assert_not_awaited()


def test_zero_clears_current_selection():
    selections = {"group-a|user-a": selection(), "group-a|user-b": selection()}
    obj = plugin(219, selections)
    event = Event("0")
    assert collect(obj.select_number, event) == ["已退出选歌。"]
    assert event.stopped
    assert selections == {"group-a|user-b": selection()}
    assert collect(obj.select_number, Event("1")) == []
    obj.accept.assert_not_awaited()


@pytest.mark.parametrize("now", [220, 221])
def test_tool_and_command_share_two_minute_expiry(now):
    obj = plugin(now, {"group-a|user-a": selection()})
    event = Event("选歌 1")
    assert collect(obj.select, event) == []
    assert event.stopped
    result = asyncio.run(obj.confirm_song_tool(Event("1"), 1))
    assert "有效候选" in result
    obj.accept.assert_not_awaited()


def test_legacy_command_still_selects():
    obj = plugin(120, {"group-a|user-a": selection()})
    event = Event("/选歌 2")
    assert collect(obj.select, event) == ["accepted"]
    obj.accept.assert_awaited_once_with(event, "second", selection()["options"], "第二首")


@pytest.mark.parametrize(
    ("argument", "group_id", "friend_id", "expected"),
    [
        ("123", 123, 456, "GroupMessage"),
        ("123", 456, 123, "FriendMessage"),
        ("群 123", 123, 123, "GroupMessage"),
        ("qq 123", 123, 123, "FriendMessage"),
        ("123", 123, 123, "重复"),
        ("123", 456, 456, "找不到目标"),
    ],
)
def test_forward_routes_last_song(argument, group_id, friend_id, expected):
    obj = plugin(120, {})
    obj.state["last"]["group-a|user-a"] = {
        "job_id": "song-id",
        "title": "song",
        "kind": "vocals",
        "completed_at": 100,
    }
    event = Event(f"/点歌转发 {argument}")
    event.get_platform_name = lambda: "aiocqhttp"
    event.get_platform_id = lambda: "qq-custom"
    targets = {
        "get_group_list": [{"group_id": group_id}],
        "get_friend_list": [{"user_id": friend_id}],
    }
    event.bot = SimpleNamespace(call_action=AsyncMock(side_effect=targets.__getitem__))

    result = collect(obj.forward_song, event)[0]
    assert event.stopped
    if expected.endswith("Message"):
        assert "已转发" in result
        obj.send_audio.assert_awaited_once_with(
            f"qq-custom:{expected}:123", "song-id", "vocals", "song"
        )
    else:
        assert expected in result
        obj.send_audio.assert_not_awaited()


@pytest.mark.parametrize(("sender", "now"), [("user-b", 120), ("user-a", 700)])
def test_forward_rejects_other_users_or_expired_songs(sender, now):
    obj = plugin(now, {})
    obj.state["last"]["group-a|user-a"] = {"completed_at": 100}
    event = Event("/点歌转发 123", sender=sender)
    event.get_platform_name = lambda: "aiocqhttp"
    assert "原会话" in collect(obj.forward_song, event)[0]
    obj.send_audio.assert_not_awaited()


@pytest.mark.parametrize(("command", "kind"), [("vocals", "vocals"), ("accompanied", "mixed")])
@pytest.mark.parametrize("now", [699, 700, 701])
def test_version_commands_stop_chat_and_only_send_within_ten_minutes(command, kind, now):
    obj = plugin(now, {})
    obj.state["last"]["group-a|user-a"] = {
        "job_id": "song-id",
        "title": "song",
        "completed_at": 100,
    }
    event = Event(command)
    assert collect(getattr(obj, command), event) == []
    assert event.stopped
    if now < 700:
        obj.send_audio.assert_awaited_once_with("group-a", "song-id", kind, "song")
    else:
        obj.send_audio.assert_not_awaited()


def test_regex_only_matches_bare_ascii_numbers():
    cls = next(
        n
        for n in ast.parse(SOURCE.read_text(encoding="utf-8")).body
        if isinstance(n, ast.ClassDef)
    )
    method = next(n for n in cls.body if getattr(n, "name", "") == "select_number")
    expression = ast.literal_eval(method.decorator_list[0].args[0])
    for text in ("1", "2", "12"):
        assert re.search(expression, text)
    for text in ("选歌 1", "abc1", "1 2", "1.0", "-1", "①"):
        assert not re.search(expression, text)
