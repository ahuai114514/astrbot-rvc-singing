import asyncio
from dataclasses import replace
import json

import httpx
import pytest

from gateway.cloud import Cloud
from gateway.config import Settings
from gateway.jobs import Jobs


def seed_settings(path):
    return Settings(path, "x" * 32, "fake-api-key", "old-workflow", "old-model.zip",
                    conversion_backend="seedvc", seed_workflow_id="seed-workflow",
                    seed_reference_file="api/jessica-reference.flac")


def test_seedvc_uses_validated_reference_pitch_and_direct_output_workflow(tmp_path):
    async def scenario():
        cloud = Cloud(seed_settings(tmp_path))
        await cloud.client.aclose()
        seen = []

        def handle(request):
            payload = json.loads(request.content)
            seen.append(payload)
            assert payload["workflowId"] == "seed-workflow"
            nodes = {(n["nodeId"], n["fieldName"]): n["fieldValue"] for n in payload["nodeInfoList"]}
            assert nodes[("2", "audio")] == "vocals.flac"
            assert nodes[("3", "audio")] == "api/jessica-reference.flac"
            assert nodes[("1", "f0_condition")] == "true"
            assert nodes[("1", "auto_f0_adjust")] == "false"
            assert nodes[("1", "pitch_shift")] == "-2"
            assert nodes[("1", "steps")] == "30"
            assert nodes[("1", "inference_cfg_rate")] == "0.7"
            assert nodes[("1", "speed")] == "1.0"
            assert not any(n["fieldName"] in {"zip_file", "index_rate"} for n in payload["nodeInfoList"])
            return httpx.Response(200, json={"data": {"taskId": "accepted"}})

        cloud.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        assert await cloud.submit("conversion", "vocals.flac", -2) == "accepted"
        assert len(seen) == 1
        await cloud.close()
    asyncio.run(scenario())


def test_switch_invalidates_old_cache_and_pins_each_jobs_engine(tmp_path):
    async def scenario():
        s = seed_settings(tmp_path)
        old_store = Jobs(replace(s, conversion_backend="rvc"))
        old, _ = old_store.create("url", 0, 60, 0, "old")
        old.pop("conversion_backend")
        old.pop("conversion_workflow")
        old.pop("reference_file")
        old["status"] = "success"
        folder = old_store.directory(old)
        folder.mkdir()
        for kind in ("mixed", "vocals"):
            (folder / f"{kind}.mp3").write_bytes(b"cached audio")
        old_store.save(old)
        await old_store.close()

        store = Jobs(s)
        new, reused = store.create("url", 0, 60, 0, "new")
        assert not reused and new["key"] != old["key"]
        assert new["conversion_backend"] == "seedvc"
        assert new["reference_file"] == s.seed_reference_file
        again, reused = store.create("url", 0, 60, 0, "repeat")
        assert reused and again["id"] == new["id"]
        assert store.create("url", 0, 60, 0, "old")[0]["id"] == old["id"]
        submitted = []

        async def upload(path):
            return "vocals.flac"

        async def submit(kind, filename, pitch, **kwargs):
            submitted.append(kwargs)
            return "paid-" + str(len(submitted))

        async def wait(task):
            return {"status": "SUCCESS", "usage": {"chargedPoint": "1"}}

        store.cloud.upload, store.cloud.submit, store.cloud.wait = upload, submit, wait
        await store.cloud_step(new, "conversion", tmp_path / "unused")
        await store.cloud_step(new, "conversion", tmp_path / "unused")
        await store.cloud_step(old, "conversion", tmp_path / "unused")
        assert len(submitted) == 2
        assert submitted[0]["conversion_backend"] == "seedvc"
        assert submitted[0]["conversion_workflow"] == "seed-workflow"
        assert submitted[1]["conversion_backend"] == "rvc"
        await store.close()
    asyncio.run(scenario())


def test_seedvc_requires_both_workflow_and_reference(monkeypatch, tmp_path):
    monkeypatch.setenv("RVC_GATEWAY_TOKEN", "x" * 32)
    monkeypatch.setenv("RUNNINGHUB_API_KEY", "fake")
    monkeypatch.setenv("RVC_WORKFLOW_ID", "old")
    monkeypatch.setenv("RVC_MODEL_FILE", "old.zip")
    monkeypatch.setenv("RVC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SINGING_CONVERSION_BACKEND", "seedvc")
    monkeypatch.delenv("SEEDVC_WORKFLOW_ID", raising=False)
    monkeypatch.delenv("SEEDVC_REFERENCE_FILE", raising=False)
    with pytest.raises(ValueError, match="未配置"):
        Settings.from_env()
    monkeypatch.setenv("SEEDVC_WORKFLOW_ID", "seed")
    monkeypatch.setenv("SEEDVC_REFERENCE_FILE", "api/reference.flac")
    assert Settings.from_env().conversion_backend == "seedvc"
