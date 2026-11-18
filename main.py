# -*- coding: utf-8 -*-
"""astrbot_plugin_pixiv_daily —— Pixiv 每日排行榜推送。

要点：
1. 定时任务用 asyncio 实现，到点推送到「订阅」的会话；
2. 图片域名 i.pximg.net 有防盗链，必须带 Referer 下载到本地再发；
3. 每条消息文字很短，避免被 AstrBot 的「文本转图像」截走；
4. 订阅、退订、立即推送都做成指令，便于验证。

指令：
  /pixiv              只向当前会话推送一次
  /pixiv 订阅          把当前会话加入每日推送
  /pixiv 退订          移出每日推送
  /pixiv 状态          查看配置与下次推送时间
  /pixiv 清理          手动清理图片缓存
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_NAME = "astrbot_plugin_pixiv_daily"

RANKING_URL = "https://www.pixiv.net/ranking.php"
REFERER = "https://www.pixiv.net/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

MODE_MAP = {"daily": "每日", "weekly": "每周", "monthly": "每月", "rookie": "新人", "original": "原创"}
CONTENT_MAP = {"all": "全部", "illust": "插画", "manga": "漫画"}

_THUMB_RE = re.compile(r"/c/\d+x\d+/")

# 连接类异常（代理挂了、DNS 失败、连接被拒），可回退直连重试
_CLIENT_ERRORS = tuple(
    e
    for e in (
        getattr(aiohttp, "ClientConnectorError", None),
        getattr(aiohttp, "ClientProxyConnectionError", None),
        getattr(aiohttp, "ClientOSError", None),
        getattr(aiohttp, "ServerDisconnectedError", None),
    )
    if isinstance(e, type) and issubclass(e, BaseException)
) or (OSError,)


@register(
    PLUGIN_NAME,
    "rice-eater24",
    "Pixiv 每日排行榜推送，支持订阅、定时、手动触发",
    "1.0.0",
    "https://github.com/rice-eater24/astrbot_plugin_pixiv_daily",
)
class PixivDailyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.image_dir = self._resolve_image_dir()
        self.state_path = self.data_dir / "state.json"
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._proxy: str | None = None
        self._state: dict = self._load_state()
        self._running = False

    # ------------------------------------------------------------------ 缓存目录

    def _resolve_image_dir(self) -> Path:
        custom = str(self.config.get("image_dir", "") or "").strip()
        if custom:
            p = Path(os.path.expanduser(custom))
            if not p.is_absolute():
                p = Path(get_astrbot_data_path()) / custom
        else:
            p = self.data_dir / "images"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _cache_stats(self) -> tuple[int, float]:
        count, total = 0, 0
        try:
            for f in self.image_dir.glob("*"):
                if f.is_file():
                    count += 1
                    total += f.stat().st_size
        except OSError:
            pass
        return count, total / 1024 / 1024

    def _cleanup(self) -> tuple[int, float]:
        keep_days = int(self.config.get("keep_days", 7) or 0)
        max_mb = float(self.config.get("max_cache_mb", 500) or 0)
        if keep_days <= 0 and max_mb <= 0:
            return 0, 0.0
        now = time.time()
        files: list[tuple[float, int, Path]] = []
        try:
            for f in self.image_dir.glob("*"):
                if f.is_file():
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    files.append((st.st_mtime, st.st_size, f))
        except OSError:
            return 0, 0.0

        removed, freed = 0, 0
        if keep_days > 0:
            deadline = now - keep_days * 86400
            for item in list(files):
                mtime, size, f = item
                if mtime < deadline:
                    try:
                        f.unlink()
                        removed += 1
                        freed += size
                        files.remove(item)
                    except OSError:
                        pass
        if max_mb > 0:
            limit = max_mb * 1024 * 1024
            total = sum(s for _, s, _ in files)
            if total > limit:
                for _, size, f in sorted(files, key=lambda x: x[0]):
                    if total <= limit:
                        break
                    try:
                        f.unlink()
                        removed += 1
                        freed += size
                        total -= size
                    except OSError:
                        pass
        if removed:
            logger.info("[pixiv] 缓存清理：删除 %d 个，释放 %.1f MB", removed, freed / 1024 / 1024)
        return removed, freed / 1024 / 1024

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        await self._setup()
        if self.config.get("cleanup_enable", True):
            self._start_cleanup_loop()
        self._ensure_scheduler()

    @filter.on_astrbot_loaded()
    async def _on_loaded(self):
        """兜底：确保 AstrBot 加载完成后定时器已启动。"""
        try:
            if self._session is None:
                await self._setup()
            self._ensure_scheduler()
        except Exception as e:  # noqa: BLE001
            logger.warning("[pixiv] 启动定时器失败：%s", e)

    async def _setup(self) -> None:
        if self._session is not None and not self._session.closed:
            return
        timeout = aiohttp.ClientTimeout(total=int(self.config.get("timeout", 30)))
        self._proxy = (str(self.config.get("proxy", "") or "")).strip() or None
        headers = {"User-Agent": UA, "Referer": REFERER, "Accept-Language": "ja,en;q=0.8"}
        cookie = str(self.config.get("cookie", "") or "").strip()
        if cookie:
            headers["Cookie"] = cookie
        self._session = aiohttp.ClientSession(
            timeout=timeout, headers=headers, trust_env=True
        )
        self._cleanup()

    async def terminate(self) -> None:
        for task in (self._task, self._cleanup_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._task = self._cleanup_task = None
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _start_cleanup_loop(self) -> None:
        if self._cleanup_task and not self._cleanup_task.done():
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    def _ensure_scheduler(self) -> None:
        """按当前配置启停定时推送任务。"""
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        if not self.config.get("enable", True):
            logger.info("[pixiv] 推送开关关闭，定时任务不启动")
            return
        if not self._targets():
            logger.info("[pixiv] 没有订阅会话，定时任务不启动（用 /pixiv 订阅 添加）")
            return
        self._task = asyncio.create_task(self._scheduler_loop())
        logger.info(
            "[pixiv] 定时推送已启动：每天 %s，订阅 %d 个会话",
            self.config.get("push_time", "08:30"),
            len(self._targets()),
        )

    # ------------------------------------------------------------------ 定时

    def _targets(self) -> list[str]:
        raw = self.config.get("targets") or []
        return [str(x).strip() for x in raw if str(x).strip()]

    def _save_config(self) -> None:
        try:
            self.config.save_config()
        except Exception as e:  # noqa: BLE001
            logger.warning("[pixiv] 保存配置失败：%s", e)

    @staticmethod
    def _next_at(text: str, default: tuple[int, int], now: datetime | None = None) -> datetime:
        now = now or datetime.now()
        hh, mm = default
        m = re.match(r"^(\d{1,2}):(\d{2})$", str(text or "").strip())
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            if 0 <= h < 24 and 0 <= mi < 60:
                hh, mm = h, mi
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    def _next_run(self, now: datetime | None = None) -> datetime:
        return self._next_at(str(self.config.get("push_time", "08:30")), (8, 30), now)

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                target = self._next_run()
                wait = (target - datetime.now()).total_seconds()
                logger.info("[pixiv] 下次推送：%s（%.1f 小时后）", target, wait / 3600)
                await asyncio.sleep(max(wait, 1))
                await self._push_all()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("[pixiv] 定时任务异常，5 分钟后重试：%s", e)
                await asyncio.sleep(300)

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                target = self._next_at(str(self.config.get("cleanup_time", "04:00")), (4, 0))
                wait = (target - datetime.now()).total_seconds()
                logger.info("[pixiv] 下次缓存清理：%s", target)
                await asyncio.sleep(max(wait, 1))
                self._cleanup()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("[pixiv] 清理任务异常，10 分钟后重试：%s", e)
                await asyncio.sleep(600)

    async def _push_all(self) -> tuple[int, int]:
        """推送给所有订阅会话，返回 (成功数, 失败数)。"""
        if self._running:
            logger.info("[pixiv] 上一次推送还没结束，跳过本轮")
            return 0, 0
        if self._session is None:
            await self._setup()
        self._running = True
        ok = fail = 0
        try:
            items = await self._fetch_ranking()
            if not items:
                logger.warning("[pixiv] 榜单为空，跳过推送")
                return 0, 0
            for umo in self._targets():
                try:
                    await self._push_to(umo, items)
                    ok += 1
                except Exception as e:  # noqa: BLE001
                    fail += 1
                    logger.warning("[pixiv] 推送到 %s 失败：%s", umo, e)
            self._state["last_push"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_state()
        finally:
            self._running = False
            self._cleanup()
        return ok, fail

    # ------------------------------------------------------------------ 抓取

    def _proxies(self) -> list[str | None]:
        """先试配置的代理；连接失败且允许回退时，再试直连。"""
        if self._proxy and self.config.get("proxy_fallback", True):
            return [self._proxy, None]
        return [self._proxy]

    async def _get_json(self, url: str, params: dict | None = None):
        if self._session is None:
            await self._setup()
        last: Exception | None = None
        for proxy in self._proxies():
            try:
                async with self._session.get(url, params=params, proxy=proxy) as resp:
                    body = await resp.text()
                    if resp.status == 403:
                        raise RuntimeError("Pixiv 拒绝访问（403），可能需要配置 Cookie 或更换节点")
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}：{body[:80]}")
                    return json.loads(body)
            except _CLIENT_ERRORS as e:
                last = e
                logger.warning("[pixiv] 请求失败（proxy=%s）：%s", proxy or "直连", e)
                continue
        if last is not None:
            raise RuntimeError(
                f"连不上 Pixiv：{last.__class__.__name__}。"
                "检查代理端口是否在运行，或把 proxy 留空改走直连"
            )
        raise RuntimeError("请求 Pixiv 失败")

    async def _fetch_ranking(self, limit: int | None = None) -> list[dict]:
        mode = str(self.config.get("mode", "daily") or "daily")
        content = str(self.config.get("content", "all") or "all")
        need = int(limit or self.config.get("count", 10) or 10)
        filter_r18 = bool(self.config.get("filter_r18", True))

        data = await self._get_json(
            RANKING_URL, {"mode": mode, "content": content, "p": 1, "format": "json"}
        )
        contents = data.get("contents") or []

        picked: list[dict] = []
        for it in contents:
            if filter_r18:
                ct = it.get("illust_content_type") or {}
                if it.get("x_restrict") or ct.get("sexual") or ct.get("grotesque") or ct.get("lo"):
                    continue
            picked.append(it)
            if len(picked) >= need:
                break

        items = []
        for idx, it in enumerate(picked, 1):
            items.append(
                {
                    "rank": idx,
                    "id": it.get("illust_id"),
                    "title": it.get("title") or "无题",
                    "user": it.get("user_name") or "未知画师",
                    "url": it.get("illust_page_url")
                    or f"https://www.pixiv.net/artworks/{it.get('illust_id')}",
                    "image": self._original_url(it.get("url") or ""),
                }
            )
        return items

    @staticmethod
    def _original_url(url: str) -> str:
        if not url:
            return ""
        return _THUMB_RE.sub("/", url, count=1)

    async def _download(self, url: str, art_id) -> Path | None:
        if not url:
            return None
        max_mb = float(self.config.get("max_image_mb", 8) or 8)
        dest = self.image_dir / f"{art_id}.jpg"
        if dest.exists() and dest.stat().st_size > 1024:
            return dest
        if self._session is None:
            await self._setup()
        for proxy in self._proxies():
            try:
                async with self._session.get(url, proxy=proxy) as resp:
                    if resp.status != 200:
                        logger.info("[pixiv] 图片 %s 返回 %s", art_id, resp.status)
                        return None
                    if "image" not in resp.headers.get("Content-Type", ""):
                        logger.info("[pixiv] 图片 %s 类型异常", art_id)
                        return None
                    data = await resp.read()
                    if len(data) > max_mb * 1024 * 1024:
                        logger.info("[pixiv] 图片 %s 超过 %.1fMB", art_id, max_mb)
                        return None
                    dest.write_bytes(data)
                    return dest
            except _CLIENT_ERRORS as e:
                logger.warning(
                    "[pixiv] 下载图片 %s 失败（proxy=%s）：%s", art_id, proxy or "直连", e
                )
                continue
            except Exception as e:  # noqa: BLE001
                logger.warning("[pixiv] 下载图片 %s 异常：%s", art_id, e)
                return None
        return None

    # ------------------------------------------------------------------ 消息组装

    @staticmethod
    def _image_comp(path: Path):
        """兼容不同 AstrBot 版本的图片组件构造方式。"""
        for maker in (
            lambda: Comp.Image.fromFileSystem(str(path)),
            lambda: Comp.Image(file=str(path)),
            lambda: Comp.Image(path=str(path)),
        ):
            try:
                return maker()
            except Exception:
                continue
        return None

    async def _build_chunks(self, items: list[dict], with_images: bool) -> list[list]:
        """每条消息里「图 + 一行短文字」，避免长文本触发文本转图像。"""
        per_msg = max(1, int(self.config.get("images_per_message", 3) or 3))
        show_url = bool(self.config.get("show_url", False))
        chunks: list[list] = []
        buf: list = []
        count = 0

        for it in items:
            cap = f"{it['rank']}. {it['title']} — {it['user']}"
            if show_url:
                cap += f"\n{it['url']}"
            comp = None
            if with_images:
                path = await self._download(it["image"], it["id"])
                if path is not None:
                    comp = self._image_comp(path)
            if comp is not None:
                buf.append(comp)
                buf.append(Comp.Plain(cap))
            else:
                buf.append(Comp.Plain(f"{cap}\n{it['url']}"))
            count += 1
            if count >= per_msg:
                chunks.append(buf)
                buf = []
                count = 0
        if buf:
            chunks.append(buf)
        return chunks

    async def _push_to(self, umo: str, items: list[dict]) -> None:
        mode = MODE_MAP.get(str(self.config.get("mode", "daily")), "每日")
        header = f"Pixiv {mode} {datetime.now():%m-%d} · {len(items)} 张"
        await self.context.send_message(umo, self._to_chain([Comp.Plain(header)]))
        await asyncio.sleep(float(self.config.get("send_interval", 2) or 2))
        chunks = await self._build_chunks(items, bool(self.config.get("download_images", True)))
        for chunk in chunks:
            await self.context.send_message(umo, self._to_chain(chunk))
            await asyncio.sleep(float(self.config.get("send_interval", 2) or 2))

    @staticmethod
    def _to_chain(comps: list) -> MessageChain:
        try:
            return MessageChain(chain=comps)
        except Exception:
            mc = MessageChain()
            mc.chain = comps
            return mc

    # ------------------------------------------------------------------ 状态

    @staticmethod
    def _friendly(e: Exception) -> str:
        """把常见网络异常翻译成人话。"""
        if isinstance(e, _CLIENT_ERRORS):
            return "连不上 Pixiv：确认代理端口在运行，或把 proxy 留空改走直连"
        msg = str(e) or e.__class__.__name__
        if "403" in msg:
            return "Pixiv 拒绝访问（403）：换节点，或配置 Cookie"
        if "Timeout" in msg or "timeout" in msg:
            return "请求超时：网络太慢或节点不通"
        return msg

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            self.state_path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("[pixiv] 保存状态失败：%s", e)

    # ------------------------------------------------------------------ 指令

    @filter.command("pixiv")
    async def cmd_pixiv(self, event: AstrMessageEvent):
        """Pixiv 排行：/pixiv 推送 | 订阅 | 退订 | 状态 | 清理"""
        args = (event.message_str or "").split()
        sub = args[1].lower() if len(args) > 1 else ""

        if sub in ("订阅", "subscribe"):
            umo = event.unified_msg_origin
            targets = self._targets()
            if umo in targets:
                yield event.plain_result(f"这个会话已经订阅过了，共 {len(targets)} 个。")
                return
            targets.append(umo)
            self.config["targets"] = targets
            self.config["enable"] = True
            self._save_config()
            self._ensure_scheduler()
            yield event.plain_result(
                f"已订阅。每天 {self.config.get('push_time', '08:30')} 推送到本会话，"
                f"当前共 {len(targets)} 个。"
            )
            return

        if sub in ("退订", "unsubscribe"):
            umo = event.unified_msg_origin
            targets = self._targets()
            if umo not in targets:
                yield event.plain_result("这个会话本来就没订阅。")
                return
            targets.remove(umo)
            self.config["targets"] = targets
            self._save_config()
            self._ensure_scheduler()
            yield event.plain_result(f"已退订，还剩 {len(targets)} 个会话。")
            return

        if sub in ("状态", "status"):
            count, mb = self._cache_stats()
            keep_days = int(self.config.get("keep_days", 7) or 0)
            max_mb = float(self.config.get("max_cache_mb", 500) or 0)
            task_ok = bool(self._task and not self._task.done())
            yield event.plain_result(
                "Pixiv 每日推送\n"
                f"开关：{'开' if self.config.get('enable', True) else '关'}\n"
                f"定时任务：{'运行中' if task_ok else '未运行'}\n"
                f"时间：每天 {self.config.get('push_time', '08:30')}，"
                f"下次 {self._next_run():%m-%d %H:%M}\n"
                f"模式：{MODE_MAP.get(str(self.config.get('mode', 'daily')), '每日')} / "
                f"{CONTENT_MAP.get(str(self.config.get('content', 'all')), '全部')}\n"
                f"数量：{self.config.get('count', 10)} 张\n"
                f"订阅会话：{len(self._targets())} 个\n"
                f"上次推送：{self._state.get('last_push', '无')}\n"
                f"缓存：{self.image_dir}\n"
                f"占用：{count} 个 / {mb:.1f} MB（保留 {keep_days or '不限'} 天，"
                f"上限 {f'{max_mb:.0f}MB' if max_mb > 0 else '不限'}）"
            )
            return

        if sub in ("清理", "clean"):
            before = self._cache_stats()
            removed, freed = self._cleanup()
            after = self._cache_stats()
            yield event.plain_result(
                f"清理完成：删除 {removed} 个，释放 {freed:.1f} MB\n"
                f"{before[0]} 个 / {before[1]:.1f} MB → {after[0]} 个 / {after[1]:.1f} MB"
            )
            return

        # 其余一律：只向当前会话推送一次，不影响其他订阅
        try:
            items = await self._fetch_ranking()
            if not items:
                yield event.plain_result("没拉到数据，可能是网络或排行榜接口变动。")
                return
            await self._push_to(event.unified_msg_origin, items)
        except Exception as e:  # noqa: BLE001
            logger.warning("[pixiv] 手动推送失败：%s", e)
            yield event.plain_result(f"推送失败：{self._friendly(e)}")
