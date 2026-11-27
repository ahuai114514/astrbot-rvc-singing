from __future__ import annotations

import asyncio
import array
import html
import http.cookiejar
import json
import math
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

BV_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}
MUSIC_CATEGORIES = {3, 28, 29, 30, 31, 59, 130, 193, 243, 244, 265, 266, 267}
SEARCH_EXCLUSIONS = re.compile(
    r"教程|教学|解说|舞蹈|跳舞|合集|串烧|盘点|伴奏|无[人原]声|"
    r"纯音乐|卡拉OK|karaoke|instrumental|\b(?:dance|tutorial)\b", re.IGNORECASE,
)


def song_search_rank(item: dict, keyword: str) -> tuple | None:
    title = html.unescape(re.sub(r"<[^>]+>", "", item.get("title", "")))
    normalized = re.sub(r"[\W_]+", "", title).casefold()
    terms = [re.sub(r"[\W_]+", "", t).casefold() for t in keyword.split()]
    if not any(terms) or not all(t in normalized for t in terms if t):
        return None
    # A requested song name can itself contain an exclusion word.
    context_title = title
    for term in keyword.split():
        context_title = re.sub(re.escape(term), "", context_title, flags=re.IGNORECASE)
    tags = item.get("tags", [])
    if SEARCH_EXCLUSIONS.search(context_title) or any(SEARCH_EXCLUSIONS.search(t) for t in tags):
        return None
    duration = item.get("duration", "")
    try:
        if isinstance(duration, (int, float)):
            seconds = float(duration)
        else:
            seconds = 0
            for part in str(duration).split(":"):
                seconds = seconds * 60 + int(part)
    except (ValueError, TypeError):
        return None
    if not 30 <= seconds <= 1200:
        return None
    try:
        tid = int(item.get("typeid") or 0)
    except (ValueError, TypeError):
        tid = 0
    music = tid in MUSIC_CATEGORIES or "音乐" in item.get("typename", "")
    return (0 if music else 1,)


def video_url(text: str) -> str | None:
    match = BV_PATTERN.search(text)
    if match:
        return f"https://www.bilibili.com/video/{match.group()}"
    if text.startswith(("http://", "https://")):
        parsed = urlparse(text)
        if (
            parsed.hostname == "b23.tv"
            and parsed.username is None
            and parsed.port in (None, 443, 80)
        ):
            return text
        raise ValueError("目前支持 B 站视频链接、BV 号和 b23.tv 短链接")
    return None


async def command(*args: str, timeout: int = 180, stderr_output: bool = False) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.communicate()
        raise
    if proc.returncode:
        # Do not expose CDN URLs or cookies from downloader errors.
        raise RuntimeError(f"{Path(args[0]).name} 执行失败，退出码 {proc.returncode}")
    return (stderr if stderr_output else stdout).decode("utf-8", errors="replace")


class Bilibili:
    def __init__(self, cookies_file: str):
        jar = http.cookiejar.MozillaCookieJar()
        if cookies_file and Path(cookies_file).is_file():
            jar.load(cookies_file, ignore_discard=True, ignore_expires=True)
        self.client = httpx.AsyncClient(headers=HEADERS, cookies=jar, timeout=30)
        self.cookies_file = cookies_file

    async def close(self):
        await self.client.aclose()

    async def resolve(self, text: str) -> str:
        url = video_url(text)
        if not url:
            raise ValueError("请先搜歌选版本，或者提供 B 站链接")
        if urlparse(url).hostname == "b23.tv":
            for _ in range(5):
                response = await self.client.get(url, follow_redirects=False)
                if response.is_redirect:
                    destination = response.headers.get("location", "")
                    if destination.startswith("//"):
                        destination = "https:" + destination
                    host = urlparse(destination).hostname or ""
                    if host != "b23.tv" and not (
                        host == "bilibili.com" or host.endswith(".bilibili.com")
                    ):
                        raise ValueError("短链接指向非 B 站地址")
                    direct = video_url(destination)
                    if direct and BV_PATTERN.search(direct):
                        return direct
                    url = destination
                else:
                    break
            raise ValueError("B 站短链接解析失败")
        return url

    async def search(self, keyword: str) -> list[dict]:
        try:
            await self.client.get("https://www.bilibili.com/", follow_redirects=True, timeout=5)
            response = await self.client.get(
                "https://api.bilibili.com/x/web-interface/search/type",
                params={"search_type": "video", "keyword": keyword, "page": 1}, timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                raise ValueError("search rejected")
            candidates = payload.get("data", {}).get("result", [])[:12]
            # Enrich only plausible songs, with bounded concurrency. Detail/tag
            # endpoint failures do not discard otherwise valid search metadata.
            semaphore = asyncio.Semaphore(4)

            async def enrich(item):
                if song_search_rank(item, keyword) is None or not re.fullmatch(r"BV[0-9A-Za-z]{10}", item.get("bvid", "")):
                    return None
                item = dict(item)
                async with semaphore:
                    try:
                        detail = await self.client.get(
                            "https://api.bilibili.com/x/web-interface/view",
                            params={"bvid": item["bvid"]}, timeout=3,
                        )
                        detail.raise_for_status()
                        data = detail.json()
                        if data.get("code") == 0:
                            view = data.get("data") or {}
                            item["typeid"] = view.get("tid", item.get("typeid"))
                            item["typename"] = view.get("tname", item.get("typename", ""))
                            item["duration"] = view.get("duration", item.get("duration"))
                        tags = await self.client.get(
                            "https://api.bilibili.com/x/tag/archive/tags",
                            params={"bvid": item["bvid"]}, timeout=3,
                        )
                        tags.raise_for_status()
                        data = tags.json()
                        if data.get("code") == 0:
                            item["tags"] = [t.get("tag_name", "") for t in data.get("data", [])]
                    except (httpx.HTTPError, ValueError, TypeError):
                        pass
                rank = song_search_rank(item, keyword)
                return (rank, item) if rank is not None else None

            enriched = await asyncio.gather(*(enrich(item) for item in candidates))
            ranked = sorted((entry for entry in enriched if entry is not None), key=lambda entry: entry[0])
            result = []
            for _, item in ranked[:5]:
                duration = item.get("duration", "")
                if isinstance(duration, (int, float)):
                    minutes, seconds = divmod(int(duration), 60)
                    duration = f"{minutes}:{seconds:02d}"
                result.append(
                    {
                        "title": html.unescape(re.sub(r"<[^>]+>", "", item["title"])),
                        "author": item.get("author", ""),
                        "duration": duration,
                        "url": f"https://www.bilibili.com/video/{item['bvid']}",
                    }
                )
            return result
        except (httpx.HTTPError, ValueError, KeyError):
            raise ValueError("B 站搜索暂时不可用，请直接发视频链接点歌") from None

    async def download(self, url: str, directory: Path) -> tuple[Path, str]:
        output = directory / "source.%(ext)s"
        args = [
            sys.executable,
            "-B",
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-cache-dir",
            "--no-playlist",
            "--add-header",
            "Range:bytes=0-",
            "--max-filesize",
            "100M",
            "--socket-timeout",
            "25",
            "--retries",
            "1",
            "--print",
            "after_move:filepath",
            "--print",
            "title",
            "-f",
            "bestaudio",
            "-o",
            str(output),
        ]
        # Copy cookies because yt-dlp may update its cookie file on exit.
        if self.cookies_file and Path(self.cookies_file).is_file():
            import shutil

            snapshot = directory / "cookies.txt"
            shutil.copyfile(self.cookies_file, snapshot)
            snapshot.chmod(0o600)
            args += ["--cookies", str(snapshot)]
        try:
            lines = (await command(*args, url, timeout=240)).strip().splitlines()
            candidates = [
                p
                for p in directory.glob("source.*")
                if p.suffix in {".m4a", ".mp3", ".webm", ".mp4"}
            ]
            if candidates:
                return candidates[0], lines[0] if lines else "B站歌曲"
        except (TimeoutError, RuntimeError):
            pass
        # Bilibili's metadata API can work when its HTML endpoint returns 412.
        bvid = BV_PATTERN.search(url).group()
        response = await self.client.get(
            "https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid}
        )
        response.raise_for_status()
        view = response.json()
        if view.get("code") == 0:
            info = view["data"]
            cid, title = info["cid"], info["title"]
        elif view.get("code") == -404:
            # Bilibili can still expose the first page when video details are unavailable.
            response = await self.client.get(
                "https://api.bilibili.com/x/player/pagelist", params={"bvid": bvid}
            )
            response.raise_for_status()
            pages = response.json()
            entries = pages.get("data")
            if pages.get("code") != 0 or not entries or not entries[0].get("cid"):
                raise ValueError("B 站视频不可访问，请检查链接或更新 Cookie")
            cid = entries[0]["cid"]
            title = entries[0].get("part") or bvid
        else:
            raise ValueError("B 站视频不可访问，请检查链接或更新 Cookie")
        response = await self.client.get(
            "https://api.bilibili.com/x/player/playurl",
            params={
                "bvid": bvid,
                "cid": cid,
                "fnval": 16,
                "qn": 64,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise ValueError("B 站音轨获取失败，请更新 Cookie 或换一个视频")
        audios = payload.get("data", {}).get("dash", {}).get("audio", [])
        if not audios:
            raise ValueError("视频没有可下载音轨")
        audio = max(audios, key=lambda x: x.get("bandwidth", 0))
        target = directory / "source.m4a"
        partial = directory / "source.m4a.part"
        urls = [audio.get("baseUrl") or audio.get("base_url")]
        urls.extend(audio.get("backupUrl") or audio.get("backup_url") or [])
        for cdn_url in urls:
            host = urlparse(cdn_url).hostname or ""
            if urlparse(cdn_url).scheme != "https" or not any(
                host == s or host.endswith("." + s)
                for s in (
                    "bilivideo.com",
                    "bilivideo.cn",
                    "bilivideo.net",
                    "akamaized.net",
                )
            ):
                continue
            try:
                async with self.client.stream(
                    "GET", cdn_url, headers={"Range": "bytes=0-"}
                ) as response:
                    response.raise_for_status()
                    size = 0
                    with partial.open("wb") as stream:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 100 * 1024 * 1024:
                                raise ValueError(
                                    "视频音轨超过 100MB，请选择较短的单曲视频"
                                )
                            stream.write(chunk)
                if size < 128:
                    continue
                partial.replace(target)
                return target, title
            except httpx.HTTPError:
                continue
            finally:
                partial.unlink(missing_ok=True)
        raise ValueError("B 站音轨下载失败，请稍后再试或换一个视频")


async def trim(source: Path, target: Path, start: int) -> tuple[int, float]:
    details = json.loads(
        await command(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(source),
        )
    )
    total = float(details["format"]["duration"])
    if total < 3:
        raise ValueError("歌曲音轨太短")
    start = min(start, max(0, int(total) - 61))
    minimum_end = start + 60
    maximum_end = min(start + 300, total)
    end = maximum_end
    if maximum_end > minimum_end:
        try:
            events = await command(
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-i",
                str(source),
                "-t",
                str(maximum_end),
                "-af",
                "silencedetect=noise=-35dB:d=0.5",
                "-f",
                "null",
                "-",
                timeout=60,
                stderr_output=True,
            )
        except (RuntimeError, TimeoutError):
            events = ""
        silence_start = None
        for kind, value in re.findall(r"silence_(start|end):\s*([0-9.]+)", events):
            value = float(value)
            if kind == "start":
                silence_start = value
            elif silence_start is not None:
                if (
                    value - silence_start >= 0.5
                    and minimum_end <= silence_start <= maximum_end
                ):
                    end = silence_start
                    break
                silence_start = None
        else:
            if (
                silence_start is not None
                and minimum_end <= silence_start <= maximum_end
            ):
                end = silence_start
    duration = end - start
    partial = target.with_suffix(".part.mp3")
    try:
        await _encode_clip(source, partial, start, duration)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return start, duration


async def _encode_clip(source: Path, target: Path, start: int, duration: float):
    await command(
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        str(start),
        "-i",
        str(source),
        "-t",
        str(duration),
        "-vn",
        "-ac",
        "2",
        "-ar",
        "44100",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        str(target),
    )
    details = json.loads(
        await command(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(target),
        )
    )
    if float(details["format"]["duration"]) < 3:
        raise ValueError("截取片段太短，请减小开始时间")


async def prepare_separation(source: Path, target: Path):
    # Match the validated cloud trial: stereo PCM16, 44.1 kHz, -12 dB.
    await _separation_audio(source, target, "volume=-12dB", "pcm_s16le")


async def restore_separation(source: Path, target: Path):
    # Restore both stems equally before RVC/mixing, with a ceiling for overshoots.
    await _separation_audio(
        source, target, "volume=12dB,alimiter=limit=0.95:level=0:latency=1", "flac"
    )


async def _separation_audio(source: Path, target: Path, filters: str, codec: str):
    partial = target.with_name(target.stem + ".part" + target.suffix)
    try:
        await command(
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-map_metadata", "-1", "-ac", "2", "-ar", "44100",
            "-af", filters, "-c:a", codec, str(partial),
        )
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


async def _harmony_pcm(path: Path) -> array.array:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
        "-ac", "1", "-ar", "8000", "-f", "f32le", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        data, _ = await asyncio.wait_for(proc.communicate(), 60)
    except (TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.communicate()
        raise
    if proc.returncode:
        raise ValueError("和声音轨读取失败")
    samples = array.array("f")
    samples.frombytes(data)
    if not samples or not all(math.isfinite(x) for x in samples):
        raise ValueError("和声音轨无效")
    return samples


def harmony_gains(reference: array.array, lead: array.array, backing: array.array) -> dict:
    # The cloud separator independently normalizes stems. Fit both gains together
    # to reconstruct the original vocal level without amplifying quiet harmonies.
    lengths = [len(a) for a in (reference, lead, backing)]
    if min(lengths) < 800 or max(lengths) - min(lengths) > 2000:
        raise ValueError("主唱与和声音轨长度不一致")
    aa = bb = ab = ax = bx = xx = 0.0
    for i in range(min(lengths)):
        a, b, x = lead[i], backing[i], reference[i]
        aa += a * a
        bb += b * b
        ab += a * b
        ax += a * x
        bx += b * x
        xx += x * x
    if xx <= 1e-12:
        return {"lead": 0.0, "harmony": 0.0, "relative_error": 0.0}
    determinant = aa * bb - ab * ab
    if determinant > max(aa * bb * 1e-8, 1e-20):
        lead_gain = (ax * bb - bx * ab) / determinant
        backing_gain = (bx * aa - ax * ab) / determinant
    elif aa >= bb and aa > 1e-12:
        lead_gain, backing_gain = ax / aa, 0.0
    elif bb > 1e-12:
        lead_gain, backing_gain = 0.0, bx / bb
    else:
        raise ValueError("主唱与和声为空")
    error = max(0, xx - 2 * lead_gain * ax - 2 * backing_gain * bx
                + lead_gain ** 2 * aa + backing_gain ** 2 * bb
                + 2 * lead_gain * backing_gain * ab)
    relative_error = math.sqrt(error / xx)
    if (not all(math.isfinite(g) and 0 <= g <= 100 for g in (lead_gain, backing_gain))
            or relative_error > 0.2):
        raise ValueError("主唱与和声音量校准失败，请检查分离输出")
    return {"lead": lead_gain, "harmony": backing_gain, "relative_error": relative_error}


async def restore_harmony(
    reference: Path, lead_raw: Path, backing_raw: Path, lead: Path, backing: Path,
) -> dict:
    ref_pcm, lead_pcm, backing_pcm = await asyncio.gather(
        _harmony_pcm(reference), _harmony_pcm(lead_raw), _harmony_pcm(backing_raw),
    )
    gains = harmony_gains(ref_pcm, lead_pcm, backing_pcm)
    for source, target, gain in (
        (lead_raw, lead, gains["lead"]), (backing_raw, backing, gains["harmony"]),
    ):
        await _separation_audio(
            source, target, f"volume={gain},alimiter=limit=0.95:level=0:latency=1", "flac",
        )
    return gains


async def combine_vocals(lead: Path, backing: Path, target: Path):
    partial = target.with_name(target.stem + ".part.flac")
    try:
        await command(
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(lead), "-i", str(backing), "-filter_complex",
            "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95:level=0:latency=1[a]",
            "-map", "[a]", "-c:a", "flac", str(partial),
        )
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


async def mix(vocals: Path, instrumental: Path, target: Path):
    await command(
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(vocals),
        "-i",
        str(instrumental),
        "-filter_complex",
        "[0:a]volume=1[v];[1:a]volume=0.75[b];[v][b]amix=inputs=2:duration=shortest:normalize=0,alimiter=limit=0.95:level=0[a]",
        "-map",
        "[a]",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        str(target),
    )


async def encode_vocals(source: Path, target: Path):
    await command(
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        str(target),
    )
