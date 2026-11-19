import ast
import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.cloud import Cloud
from gateway.config import Settings
from gateway.jobs import Jobs
from gateway.media import video_url


def settings(tmp_path):
    return Settings(tmp_path, "x" * 32, "fake-api-key", "workflow", "model.zip")


def test_request_deduplication_survives_restart(tmp_path):
    async def scenario():
        store = Jobs(settings(tmp_path))
        first, reused = store.create("url", 35, 90, 0, "message-a")
        assert not reused
        again, reused = store.create("url", 35, 90, 0, "message-b")
        assert reused and first["id"] == again["id"]
        await store.close()
        store = Jobs(settings(tmp_path))
        again, reused = store.create("url", 35, 90, 0, "message-a")
        assert reused and first["id"] == again["id"]
        await store.close()

    asyncio.run(scenario())


def test_expired_audio_is_not_reused_for_new_request(tmp_path):
    async def scenario():
        store = Jobs(settings(tmp_path))
        first, _ = store.create("url", 35, 90, 0, "a")
        first["status"] = "success"
        store.save(first)
        second, reused = store.create("url", 35, 90, 0, "b")
        assert not reused and second["id"] != first["id"]
        await store.close()

    asyncio.run(scenario())


def test_existing_cloud_task_is_queried_without_resubmission(tmp_path):
    async def scenario():
        store = Jobs(settings(tmp_path))
        job, _ = store.create("url", 35, 90, 0, "a")
        job["cloud"]["conversion"] = {"state": "running", "task_id": "accepted"}

        async def wait(task_id):
            assert task_id == "accepted"
            return {"status": "SUCCESS", "usage": {"chargedPoint": "2.55"}}

        store.cloud.wait = wait
        await store.cloud_step(job, "conversion", tmp_path / "nonexistent.mp3")
        assert job["usage_points"] == 2.55
        await store.cloud_step(job, "conversion", tmp_path / "nonexistent.mp3")
        assert job["usage_points"] == 2.55
        await store.close()

    asyncio.run(scenario())


def test_uncertain_submission_is_not_retried_on_restart(tmp_path):
    async def scenario():
        store = Jobs(settings(tmp_path))
        job, _ = store.create("url", 35, 90, 0, "a")
        job["cloud"]["conversion"] = {"state": "submitting"}
        store.save(job)
        await store.start()
        assert store.get(job["id"])["status"] == "failed"
        with pytest.raises(ValueError, match="结果未知"):
            store.create("url", 35, 90, 0, "b")
        await store.close()

    asyncio.run(scenario())


def test_api_auth_input_and_audio_paths(tmp_path, monkeypatch):
    async def no_worker(self):
        pass

    monkeypatch.setattr(Jobs, "start", no_worker)
    with TestClient(create_app(settings(tmp_path))) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/v1/jobs/" + "a" * 32).status_code == 401
        auth = {"Authorization": "Bearer " + "x" * 32}
        assert (
            client.post(
                "/v1/jobs",
                headers=auth,
                json={
                    "source": "BV1LiVKzFEVf",
                    "duration": 301,
                    "request_id": "a",
                },
            ).status_code
            == 422
        )
        created = client.post(
            "/v1/jobs",
            headers=auth,
            json={
                "source": "BV1LiVKzFEVf",
                "request_id": "a",
            },
        ).json()
        assert created["duration"] == 60
        assert created["start"] == 0
        assert (
            client.get(
                f"/v1/jobs/{created['id']}/audio/mixed", headers=auth
            ).status_code
            == 409
        )
        assert (
            client.get(
                f"/v1/jobs/{created['id']}/audio/secrets", headers=auth
            ).status_code
            == 422
        )
        assert client.get("/v1/jobs/not-a-job", headers=auth).status_code == 404


def test_cloud_paid_submission_protocol(tmp_path):
    async def scenario():
        cloud = Cloud(settings(tmp_path))
        await cloud.client.aclose()
        seen = []

        def handle(request):
            payload = json.loads(request.content)
            seen.append((request.url.path, payload))
            if request.url.path.endswith("create"):
                assert payload["apiKey"] == "fake-api-key"
                if payload["workflowId"] == "2120474755580129281":
                    assert payload["nodeInfoList"] == [
                        {"nodeId": "16", "fieldName": "audio", "fieldValue": "input.wav"}
                    ]
                    return httpx.Response(200, json={"code": 0, "data": {"taskId": "separation"}})
                if payload["workflowId"] == "2122219257252630529":
                    nodes = {n["fieldName"]: n["fieldValue"] for n in payload["nodeInfoList"]}
                    assert nodes == {"audio": "input.wav", "model": "UVR/5_HP-Karaoke-UVR.pth", "use_cache": "false", "agg": "10", "format": "flac"}
                    return httpx.Response(200, json={"code": 0, "data": {"taskId": "harmony"}})
                assert payload["nodeInfoList"][-1]["fieldValue"] == "-2"
                assert next(n["fieldValue"] for n in payload["nodeInfoList"] if n["fieldName"] == "index_rate") == "1.0"
                return httpx.Response(
                    200, json={"code": 0, "data": {"taskId": "conversion"}}
                )
            assert request.headers["Authorization"] == "Bearer fake-api-key"
            return httpx.Response(200, json={"taskId": "separation"})

        cloud.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        assert await cloud.submit("conversion", "input.wav", -2) == "conversion"
        assert await cloud.submit("separation", "input.wav", 0) == "separation"
        assert await cloud.submit("separation", "input.wav", 0, separation_workflow="") == "separation"
        assert await cloud.submit("harmony", "input.wav", 0, harmony_workflow="2122219257252630529") == "harmony"
        assert len(seen) == 4
        await cloud.close()

    asyncio.run(scenario())


def test_separation_cache_does_not_reuse_legacy_audio(tmp_path):
    from gateway.jobs import cache_key

    async def scenario():
        store = Jobs(settings(tmp_path))
        old, _ = store.create("url", 60, 60, 0, "old")
        old.pop("separation_workflow")
        old["key"] = cache_key("url", 60, 60, 0, "model.zip", "")
        old["status"] = "success"
        folder = store.directory(old)
        folder.mkdir()
        for kind in ("mixed", "vocals"):
            (folder / f"{kind}.mp3").write_bytes(b"audio")
        store.save(old)
        new, reused = store.create("url", 60, 60, 0, "new")
        assert not reused and new["id"] != old["id"]
        assert new["separation_workflow"] == "2120474755580129281"
        original, reused = store.create("url", 60, 60, 0, "old")
        assert reused and original["id"] == old["id"]
        await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("legacy", [False, True])
def test_render_resumes_separation_and_maps_stems_by_node(tmp_path, monkeypatch, legacy):
    from gateway import jobs as module

    async def scenario():
        store = Jobs(settings(tmp_path))
        job, _ = store.create("url", 60, 60, 0, "a")
        job.pop("harmony_workflow", None)
        if legacy:
            job.pop("separation_workflow")
        folder = store.directory(job)
        folder.mkdir()
        (folder / "clip.mp3").write_bytes(b"clip")
        job["cloud"]["separation"] = {"state": "running", "task_id": "paid-task"}
        job["cloud"]["conversion"] = {"state": "running", "task_id": "paid-rvc"}
        downloads = []
        restored = []

        async def wait(task_id):
            if task_id == "paid-task":
                return {"results": [
                    {"nodeId": "18", "url": "https://test/背景.mp3" if legacy else "https://test/back.mp3"},
                    {"nodeId": "4", "url": "https://test/人声.mp3" if legacy else "https://test/voice.mp3"},
                ]}
            return {"results": [{"outputType": "flac", "url": "https://test/rvc.flac"}]}

        async def download(url, target):
            downloads.append((url, target.name))
            target.write_bytes(b"audio")

        async def restore(source, target):
            restored.append((source.name, target.name))
            target.write_bytes(b"restored")

        async def mix(vocals, instrumental, target):
            assert instrumental.stem == "instrumental"
            target.write_bytes(b"mixed")

        async def encode(source, target):
            target.write_bytes(b"vocals")

        async def forbidden(*args, **kwargs):
            raise AssertionError("An accepted cloud task must never be resubmitted")

        store.cloud.wait = wait
        store.cloud.download = download
        store.cloud.submit = forbidden
        store.cloud.upload = forbidden
        monkeypatch.setattr(module, "restore_separation", restore)
        monkeypatch.setattr(module, "mix", mix)
        monkeypatch.setattr(module, "encode_vocals", encode)
        await store.render(job)
        assert job["status"] == "success"
        assert "source-vocals" in downloads[0][1]
        assert "人声" in downloads[0][0] if legacy else "voice" in downloads[0][0]
        assert "instrumental" in downloads[1][1]
        assert len(restored) == (0 if legacy else 2)
        await store.close()

    asyncio.run(scenario())


def test_harmony_gain_calibration_restores_relative_volume():
    import array
    import math
    from gateway.media import harmony_gains

    lead = array.array("f", (0.2 * math.sin(i * 0.2) for i in range(16000)))
    backing = array.array("f", (0.1 * math.sin(i * 0.37) for i in range(16000)))
    reference = array.array("f", (lead[i] * 0.7 + backing[i] * 0.12 for i in range(16000)))
    gains = harmony_gains(reference, lead, backing)
    assert gains["lead"] == pytest.approx(0.7, abs=1e-6)
    assert gains["harmony"] == pytest.approx(0.12, abs=1e-6)
    assert gains["relative_error"] < 1e-6
    silence = array.array("f", [0] * len(lead))
    assert harmony_gains(lead, lead, silence)["harmony"] == 0
    with pytest.raises(ValueError, match="长度"):
        harmony_gains(reference, lead[:100], backing)
    unrelated = array.array("f", (0.2 * math.sin(i * 0.51) for i in range(16000)))
    with pytest.raises(ValueError, match="校准"):
        harmony_gains(unrelated, lead, backing)


def test_harmony_render_only_converts_lead_and_reuses_paid_tasks(tmp_path, monkeypatch):
    from gateway import jobs as module

    async def scenario():
        store = Jobs(settings(tmp_path))
        job, _ = store.create("url", 60, 60, 0, "a")
        job["harmony_workflow"] = "2122219257252630529"
        job["index_rate"] = 0.66
        folder = store.directory(job)
        folder.mkdir()
        (folder / "clip.mp3").write_bytes(b"clip")
        for kind in ("separation", "harmony"):
            job["cloud"][kind] = {"state": "running", "task_id": "paid-" + kind}
        submitted = []
        downloads = []

        async def wait(task):
            results = {
                "paid-separation": [{"nodeId": "18", "url": "back"}, {"nodeId": "4", "url": "vocals"}],
                "paid-harmony": [{"nodeId": "5", "url": "harmony"}, {"nodeId": "6", "url": "lead"}],
                "new-conversion": [{"outputType": "flac", "url": "converted"}],
            }
            return {"results": results[task], "usage": {"chargedPoint": "1"}}

        async def download(url, target):
            downloads.append((url, target.name))
            target.write_bytes(url.encode())

        async def restore(source, target):
            target.write_bytes(b"restored")

        async def restore_harmony(reference, lead_raw, backing_raw, lead, backing):
            assert lead_raw.read_bytes() == b"lead"
            assert backing_raw.read_bytes() == b"harmony"
            lead.write_bytes(b"calibrated-lead")
            backing.write_bytes(b"calibrated-harmony")
            return {"lead": 0.5, "harmony": 0.2}

        async def upload(source):
            assert source.name == "lead.flac"
            assert source.read_bytes() == b"calibrated-lead"
            return "uploaded-lead"

        async def submit(kind, filename, pitch, **kwargs):
            submitted.append(kind)
            assert kind == "conversion" and filename == "uploaded-lead"
            assert kwargs == {"index_rate": 0.66, "conversion_backend": "rvc",
                              "conversion_workflow": "", "reference_file": ""}
            return "new-conversion"

        async def combine(lead, backing, target):
            assert lead.read_bytes() == b"converted"
            assert backing.read_bytes() == b"calibrated-harmony"
            target.write_bytes(b"performance")

        async def mix(vocals, instrumental, target):
            assert vocals.name == "performance.flac"
            assert instrumental.name == "instrumental.flac"
            target.write_bytes(b"mixed")

        async def encode(source, target):
            assert source.name == "performance.flac"
            target.write_bytes(b"pure-vocals")

        store.cloud.wait, store.cloud.download = wait, download
        store.cloud.upload, store.cloud.submit = upload, submit
        monkeypatch.setattr(module, "restore_separation", restore)
        monkeypatch.setattr(module, "restore_harmony", restore_harmony)
        monkeypatch.setattr(module, "combine_vocals", combine)
        monkeypatch.setattr(module, "mix", mix)
        monkeypatch.setattr(module, "encode_vocals", encode)
        await store.render(job)
        await store.render(job)
        assert submitted == ["conversion"]
        assert job["usage_points"] == 3
        assert job["status"] == "success"
        await store.close()

    asyncio.run(scenario())


def test_plugin_options_without_loading_astrbot():
    path = Path(__file__).parents[1] / "astrbot_plugin_rvc_singing" / "main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wanted = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name == "parse_request")
        or (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "OPTIONS"
                for target in node.targets
            )
        )
    ]
    scope = {"re": re}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(path), "exec"), scope)
    defaults = {"start": 60, "duration": 60, "pitch": 0, "kind": "mixed"}
    source, options = scope["parse_request"]("青衣 从40秒 降调2 纯人声", defaults)
    assert source == "青衣" and options == {
        "start": 40,
        "duration": 60,
        "pitch": -2,
        "kind": "vocals",
    }
    assert scope["parse_request"]("青衣", defaults)[1]["kind"] == "mixed"
    with pytest.raises(ValueError):
        scope["parse_request"]("青衣 时长999秒", defaults)


def test_video_input_canonicalizes_to_bilibili():
    assert (
        video_url("https://evil.example/BV1LiVKzFEVf")
        == "https://www.bilibili.com/video/BV1LiVKzFEVf"
    )
    with pytest.raises(ValueError):
        video_url("http://127.0.0.1/secrets")
    with pytest.raises(ValueError):
        video_url("https://b23.tv.evil.example/abc")


def test_download_uses_range_and_backup_cdn(tmp_path, monkeypatch):
    from gateway import media

    async def unavailable(*args, **kwargs):
        raise RuntimeError("HTML blocked")

    monkeypatch.setattr(media, "command", unavailable)

    async def scenario():
        bili = media.Bilibili("")
        await bili.client.aclose()

        def handle(request):
            if request.url.path.endswith("/view"):
                return httpx.Response(
                    200, json={"code": 0, "data": {"cid": 1, "title": "song"}}
                )
            if request.url.path.endswith("/playurl"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "dash": {
                                "audio": [
                                    {
                                        "baseUrl": "https://primary.bilivideo.com/audio",
                                        "backupUrl": [
                                            "https://backup.bilivideo.com/audio"
                                        ],
                                    }
                                ]
                            }
                        },
                    },
                )
            assert request.headers["Range"] == "bytes=0-"
            if request.url.host.startswith("primary"):
                return httpx.Response(567)
            return httpx.Response(206, content=b"audio" * 100)

        bili.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        path, title = await bili.download(
            "https://www.bilibili.com/video/BV1LiVKzFEVf", tmp_path
        )
        assert title == "song" and path.read_bytes() == b"audio" * 100
        assert not list(tmp_path.glob("*.part"))
        await bili.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("total", "events", "expected"),
    [
        (400, "silence_start: 140.25\nsilence_end: 141.2", (60, 80.25)),
        (400, "silence_start: 110\nsilence_end: 111", (60, 300)),
        (400, "silence_start: 200", (60, 140)),
        (400, "", (60, 300)),
        (90, "", (29, 61)),
        (45, "", (0, 45)),
    ],
)
def test_clip_matches_original_phrase_policy(
    tmp_path, monkeypatch, total, events, expected
):
    from gateway import media

    async def fake_command(*args, **kwargs):
        if args[0] == "ffprobe":
            return json.dumps({"format": {"duration": total}})
        if "silencedetect=noise=-35dB:d=0.5" in args:
            assert kwargs["stderr_output"]
            return events
        assert float(args[args.index("-ss") + 1]) == expected[0]
        assert float(args[args.index("-t") + 1]) == expected[1]
        Path(args[-1]).write_bytes(b"encoded")
        return ""

    monkeypatch.setattr(media, "command", fake_command)
    target = tmp_path / "clip.mp3"
    assert asyncio.run(media.trim(tmp_path / "source.mp3", target, 60)) == expected
    assert target.read_bytes() == b"encoded"
    assert not list(tmp_path.glob("*.part.mp3"))


def test_cache_keeps_fifty_newest_results_and_seven_days(tmp_path):
    import time

    async def scenario():
        store = Jobs(settings(tmp_path))
        now = time.time()
        for i in range(52):
            job = {
                "id": f"{i:032x}",
                "key": str(i),
                "status": "success",
                "updated": now - i,
            }
            folder = store.directory(job)
            folder.mkdir()
            for kind in ("mixed", "vocals"):
                (folder / f"{kind}.mp3").write_bytes(b"audio")
            store.db.execute(
                "INSERT INTO jobs VALUES (?,?,?)",
                (job["id"], job["key"], json.dumps(job)),
            )
        store.db.commit()
        store.cleanup()
        assert len(store.all()) == 50
        assert store.get(f"{51:032x}") is None
        oldest = store.get(f"{49:032x}")
        oldest["updated"] = now - 8 * 86400
        store.db.execute(
            "UPDATE jobs SET body=? WHERE id=?", (json.dumps(oldest), oldest["id"])
        )
        store.db.commit()
        store.cleanup()
        assert len(store.all()) == 49
        assert not store.directory(oldest).exists()
        await store.close()

    asyncio.run(scenario())


def test_search_result_includes_link_without_loading_astrbot(tmp_path):
    path = Path(__file__).parents[1] / "astrbot_plugin_rvc_singing" / "main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    search = next(
        n
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "search"
    )
    scope = {"time": __import__("time"), "AstrMessageEvent": object}
    exec(compile(ast.Module(body=[search], type_ignores=[]), str(path), "exec"), scope)

    class Plugin:
        state = {"selections": {}}

        async def request(self, *args, **kwargs):
            return {
                "results": [
                    {
                        "title": "青衣现场",
                        "author": "UP主",
                        "duration": "4:22",
                        "url": "https://www.bilibili.com/video/BV1LiVKzFEVf",
                    }
                ]
            }

        def owner(self, event):
            return "owner"

        def persist(self):
            pass

    result = asyncio.run(scope["search"](Plugin(), object(), "青衣", {}))
    assert "1. 青衣现场" in result and "UP主" in result and "4:22" in result
    assert "https://www.bilibili.com/video/BV1LiVKzFEVf" in result


def test_plugin_has_no_quota_or_admin_path():
    path = Path(__file__).parents[1] / "astrbot_plugin_rvc_singing" / "main.py"
    source = path.read_text(encoding="utf-8")
    assert (
        "is_admin" not in source
        and "daily_per" not in source
        and "cooldown" not in source
    )
    schema = json.loads(path.with_name("_conf_schema.json").read_text(encoding="utf-8"))
    assert set(schema) == {"gateway_url", "gateway_token", "start", "pitch"}


def test_new_jobs_start_at_zero_and_convert_all_vocals(tmp_path):
    async def scenario():
        store = Jobs(settings(tmp_path))
        job, _ = store.create("url", 0, 60, 0, "new")
        assert job["index_rate"] == 1.0
        assert job["harmony_workflow"] == ""
        assert job["start"] == 0
        await store.close()
    asyncio.run(scenario())


def test_song_search_filters_metadata_and_prioritizes_music():
    from gateway.media import song_search_rank

    base = {"title": "青衣", "duration": "3:20", "typeid": 31}
    assert song_search_rank(base, "青衣") == (0,)
    assert song_search_rank({**base, "typeid": 0}, "青衣") == (1,)
    for title in ("青衣 舞蹈", "青衣解析教程", "青衣伴奏", "青衣歌曲合集", "别的歌曲"):
        assert song_search_rank({**base, "title": title}, "青衣") is None
    for title in ("青衣 原唱", "青衣 翻唱", "青衣 现场版"):
        assert song_search_rank({**base, "title": title}, "青衣") is not None
    for duration in ("0:10", "30:00", "invalid"):
        assert song_search_rank({**base, "duration": duration}, "青衣") is None
    assert song_search_rank({**base, "tags": ["舞蹈教程"]}, "青衣") is None
    assert song_search_rank({**base, "title": "演员 现场"}, "演员") is not None


@pytest.mark.parametrize("metadata_failure", [False, True])
def test_search_enriches_tags_and_keeps_fallback_results(metadata_failure):
    from gateway.media import Bilibili

    async def scenario():
        bili = Bilibili("")
        await bili.client.aclose()
        items = [
            {"bvid": "BV0000000001", "title": "青衣 原唱", "typeid": 0, "duration": "3:10", "author": "a"},
            {"bvid": "BV0000000002", "title": "<em>青衣</em> 翻唱", "typeid": 31, "duration": "4:00", "author": "b"},
            {"bvid": "BV0000000003", "title": "青衣", "typeid": 31, "duration": "3:00", "author": "c"},
            {"bvid": "BV0000000004", "title": "青衣伴奏", "typeid": 31, "duration": "3:00", "author": "d"},
        ]
        def handle(request):
            if request.url.host == "www.bilibili.com":
                return httpx.Response(200)
            if request.url.path.endswith("/search/type"):
                return httpx.Response(200, json={"code": 0, "data": {"result": items}})
            bvid = request.url.params["bvid"]
            assert bvid != "BV0000000004"
            if metadata_failure:
                return httpx.Response(503)
            if request.url.path.endswith("/view"):
                return httpx.Response(200, json={"code": 0, "data": {"tid": 31 if bvid != "BV0000000001" else 0, "duration": 200}})
            return httpx.Response(200, json={"code": 0, "data": [{"tag_name": "舞蹈" if bvid == "BV0000000003" else "翻唱"}]})
        bili.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        results = await bili.search("青衣")
        assert len(results) == (3 if metadata_failure else 2)
        assert results[0]["title"] == "青衣 翻唱"
        assert results[0]["url"].endswith("BV0000000002")
        assert results[0]["duration"] == ("4:00" if metadata_failure else "3:20")
        await bili.close()
    asyncio.run(scenario())
