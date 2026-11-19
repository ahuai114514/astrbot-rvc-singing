from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from .config import Settings


class CloudError(RuntimeError):
    pass


class CloudPending(CloudError):
    """An accepted task can be queried again without another paid submission."""


class Cloud:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=90, follow_redirects=True)

    async def close(self):
        await self.client.aclose()

    async def upload(self, path: Path) -> str:
        with path.open("rb") as stream:
            response = await self.client.post(
                f"{self.settings.api_base}/task/openapi/upload",
                data={"apiKey": self.settings.api_key, "fileType": "input"},
                files={"file": (path.name, stream, "application/octet-stream")},
                timeout=240,
            )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise CloudError(f"素材上传失败（{data.get('code')}）")
        return data["data"]["fileName"]

    async def submit(
        self, kind: str, filename: str, pitch: int,
        *, separation_workflow: str | None = None,
        harmony_workflow: str | None = None, index_rate: float = 1.0,
        conversion_backend: str | None = None,
        conversion_workflow: str | None = None,
        reference_file: str | None = None,
    ) -> str:
        workflow = self.settings.separation_workflow_id if separation_workflow is None else separation_workflow
        if kind == "harmony":
            response = await self.client.post(
                f"{self.settings.api_base}/task/openapi/create",
                json={
                    "apiKey": self.settings.api_key,
                    "workflowId": harmony_workflow or self.settings.harmony_workflow_id,
                    "instanceType": "default",
                    "nodeInfoList": [
                        {"nodeId": "1", "fieldName": "audio", "fieldValue": filename},
                        {"nodeId": "2", "fieldName": "model", "fieldValue": "UVR/5_HP-Karaoke-UVR.pth"},
                        {"nodeId": "2", "fieldName": "use_cache", "fieldValue": "false"},
                        {"nodeId": "2", "fieldName": "agg", "fieldValue": "10"},
                        {"nodeId": "2", "fieldName": "format", "fieldValue": "flac"},
                    ],
                },
            )
        elif kind == "separation" and workflow:
            response = await self.client.post(
                f"{self.settings.api_base}/task/openapi/create",
                json={
                    "apiKey": self.settings.api_key,
                    "workflowId": workflow,
                    "instanceType": "default",
                    "nodeInfoList": [
                        {"nodeId": "16", "fieldName": "audio", "fieldValue": filename}
                    ],
                },
            )
        elif kind == "separation":
            url = f"{self.settings.api_base}/openapi/v2/run/ai-app/{self.settings.separation_app}"
            payload: dict[str, Any] = {
                "nodeInfoList": [
                    {"nodeId": "4", "fieldName": "audio", "fieldValue": filename}
                ],
                "instanceType": "default",
                "usePersonalQueue": False,
            }
            response = await self.client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self.settings.api_key}"},
            )
        elif kind == "conversion" and (conversion_backend or self.settings.conversion_backend) == "seedvc":
            seed_workflow = self.settings.seed_workflow_id if conversion_workflow is None else conversion_workflow
            seed_reference = self.settings.seed_reference_file if reference_file is None else reference_file
            if not seed_workflow or not seed_reference:
                raise CloudError("Seed-VC 工作流或参考音频未配置")
            response = await self.client.post(
                f"{self.settings.api_base}/task/openapi/create",
                json={
                    "apiKey": self.settings.api_key,
                    "workflowId": seed_workflow,
                    "instanceType": "default",
                    "nodeInfoList": [
                        {"nodeId": "2", "fieldName": "audio", "fieldValue": filename},
                        {"nodeId": "3", "fieldName": "audio", "fieldValue": seed_reference},
                        {"nodeId": "1", "fieldName": "steps", "fieldValue": "30"},
                        {"nodeId": "1", "fieldName": "speed", "fieldValue": "1.0"},
                        {"nodeId": "1", "fieldName": "inference_cfg_rate", "fieldValue": "0.7"},
                        {"nodeId": "1", "fieldName": "f0_condition", "fieldValue": "true"},
                        {"nodeId": "1", "fieldName": "auto_f0_adjust", "fieldValue": "false"},
                        {"nodeId": "1", "fieldName": "pitch_shift", "fieldValue": str(pitch)},
                    ],
                },
            )
        else:
            payload = {
                "apiKey": self.settings.api_key,
                "workflowId": self.settings.workflow_id,
                "instanceType": "default",
                "nodeInfoList": [
                    {
                        "nodeId": "1",
                        "fieldName": "zip_file",
                        "fieldValue": self.settings.model_file,
                    },
                    {"nodeId": "2", "fieldName": "audio", "fieldValue": filename},
                    {"nodeId": "3", "fieldName": "index_rate", "fieldValue": str(index_rate)},
                    {"nodeId": "3", "fieldName": "f0_up_key", "fieldValue": str(pitch)},
                ],
            }
            response = await self.client.post(
                f"{self.settings.api_base}/task/openapi/create", json=payload
            )
        response.raise_for_status()
        data = response.json()
        result = data.get("data") or data
        task_id = result.get("taskId")
        if not task_id:
            raise CloudError(
                f"云端未受理任务（{data.get('code', data.get('errorCode', 'unknown'))}），请检查额度和配置"
            )
        return str(task_id)

    async def query(self, task_id: str) -> dict:
        response = await self.client.post(
            f"{self.settings.api_base}/openapi/v2/query",
            json={"taskId": task_id},
            headers={"Authorization": f"Bearer {self.settings.api_key}"},
        )
        response.raise_for_status()
        result = response.json()
        if "status" not in result:
            raise CloudError("云端任务查询失败，请检查 API 密钥")
        return result

    async def download(self, url: str, path: Path):
        temporary = path.with_suffix(path.suffix + ".part")
        try:
            async with self.client.stream("GET", url, timeout=240) as response:
                response.raise_for_status()
                size = 0
                with temporary.open("wb") as stream:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 100 * 1024 * 1024:
                            raise CloudError("云端音频超出大小限制")
                        stream.write(chunk)
            if size < 128:
                raise CloudError("云端返回了空音频")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    async def wait(self, task_id: str, timeout: float = 1800) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        failures = 0
        while asyncio.get_running_loop().time() < deadline:
            try:
                result = await self.query(task_id)
                failures = 0
            except (httpx.TransportError, httpx.HTTPStatusError):
                failures += 1
                if failures >= 10:
                    raise CloudPending(
                        f"云端任务 {task_id} 查询暂时不可用，稍后自动继续查询"
                    ) from None
                await asyncio.sleep(5)
                continue
            if result["status"] == "SUCCESS":
                return result
            if result["status"] in {"FAILED", "CANCELED", "CANCELLED"}:
                raise CloudError(
                    f"云端任务失败（{result.get('errorCode') or result['status']}），任务号 {task_id}"
                )
            await asyncio.sleep(3)
        raise CloudPending(f"云端任务 {task_id} 尚未完成，稍后自动继续查询")
