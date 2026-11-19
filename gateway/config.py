from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    token: str
    api_key: str
    workflow_id: str
    model_file: str
    cookies_file: str = ""
    api_base: str = "https://www.runninghub.cn"
    separation_app: str = "2047408096384917505"
    separation_workflow_id: str = "2120474755580129281"
    harmony_workflow_id: str = ""
    index_rate: float = 1.0
    max_queue: int = 10
    retention_days: int = 7
    max_cache_entries: int = 50
    conversion_backend: str = "rvc"
    seed_workflow_id: str = ""
    seed_reference_file: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        settings = cls(
            data_dir=Path(
                os.environ.get("RVC_DATA_DIR", "/www/ahuai/rvc-singing-data")
            ),
            token=os.environ["RVC_GATEWAY_TOKEN"],
            api_key=os.environ["RUNNINGHUB_API_KEY"],
            workflow_id=os.environ["RVC_WORKFLOW_ID"],
            model_file=os.environ["RVC_MODEL_FILE"],
            cookies_file=os.environ.get("BILIBILI_COOKIES_FILE", ""),
            conversion_backend=os.environ.get("SINGING_CONVERSION_BACKEND", "rvc"),
            seed_workflow_id=os.environ.get("SEEDVC_WORKFLOW_ID", ""),
            seed_reference_file=os.environ.get("SEEDVC_REFERENCE_FILE", ""),
        )
        if len(settings.token) < 24:
            raise ValueError("网关令牌至少需要 24 位")
        if settings.conversion_backend not in {"rvc", "seedvc"}:
            raise ValueError("音色转换后端配置无效")
        if settings.conversion_backend == "seedvc" and not (
            settings.seed_workflow_id and settings.seed_reference_file
        ):
            raise ValueError("Seed-VC 工作流或参考音频未配置")
        return settings
