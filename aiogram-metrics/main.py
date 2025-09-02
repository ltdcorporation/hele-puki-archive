import asyncio
import logging
import os
import signal
import time
from typing import Dict, Any, Optional

try:
    import uvloop  # type: ignore
    uvloop.install()
except Exception:
    pass

from aiohttp import web
from prometheus_client import Gauge, REGISTRY, generate_latest, CONTENT_TYPE_LATEST

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

from redis.asyncio import Redis


def getenv(name: str, default: Optional[str] = None, required: bool = False) -> str:
    v = os.getenv(name, default)
    if required and not v:
        raise RuntimeError(f"Missing required env: {name}")
    return v or ""


TELEGRAM_TOKEN = getenv("TELEGRAM_TOKEN", required=True)
REDIS_URL = getenv("REDIS_URL", "redis://redis:6379/0")
HTTP_HOST = getenv("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(getenv("HTTP_PORT", "8000"))

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":"%(message)s"}',
)
log = logging.getLogger("bot")

# Prometheus gauges (totals modeled as gauges to avoid double counting on scrape)
G_MSGS = Gauge("telegram_msgs_total", "Total messages observed")
G_CMDS = Gauge("telegram_cmds_total", "Total commands observed")
G_USERS = Gauge("telegram_total_users", "Total unique chat_ids")
G_ACTIVE5 = Gauge("telegram_active_users_5m", "Active users in last 5 minutes")
G_RPM1 = Gauge("telegram_rpm_1m", "Events/min last 1 minute")


class Metrics:
    def __init__(self, redis: Redis):
        self.r = redis
        self.k_users = "metrics:total_users"
        self.k_last = "metrics:last_seen"
        self.k_msgs = "metrics:msgs_total"
        self.k_cmds = "metrics:cmds_total"
        self.k_events = "metrics:events"

    async def note_message(self, chat_id: int, is_command: bool, now: int) -> None:
        p = self.r.pipeline(transaction=False)
        p.sadd(self.k_users, chat_id)
        p.hset(self.k_last, chat_id, now)
        p.incr(self.k_msgs)
        p.zadd(self.k_events, {str(now): now})
        if is_command:
            p.incr(self.k_cmds)
        await p.execute()

    async def prune_events(self, before_ts: int) -> None:
        await self.r.zremrangebyscore(self.k_events, "-inf", before_ts)

    async def snapshot(self, now: int) -> Dict[str, int]:
        await self.prune_events(now - 600)
        total_users = await self.r.scard(self.k_users)
        msgs_total = int(await self.r.get(self.k_msgs) or 0)
        cmds_total = int(await self.r.get(self.k_cmds) or 0)
        rpm_1m = await self.r.zcount(self.k_events, now - 60, now)
        last_seen = await self.r.hgetall(self.k_last)
        active_users_5m = 0
        for ts in last_seen.values():
            try:
                if now - int(float(ts)) <= 300:
                    active_users_5m += 1
            except Exception:
                continue
        return {
            "total_users": int(total_users),
            "msgs_total": msgs_total,
            "cmds_total": cmds_total,
            "rpm_1m": int(rpm_1m),
            "active_users_5m": int(active_users_5m),
        }


def stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="♻️ Refresh", callback_data="stats:refresh"),
                InlineKeyboardButton(text="✖️ Close", callback_data="stats:close"),
            ]
        ]
    )


def format_stats(s: Dict[str, Any]) -> str:
    return (
        "📊 Live Stats\n"
        f"👥 Total users: {s['total_users']}\n"
        f"🔥 Active (5m): {s['active_users_5m']}\n"
        f"💬 Messages: {s['msgs_total']}\n"
        f"⌨️ Commands: {s['cmds_total']}\n"
        f"⚡ RPM(1m): {s['rpm_1m']}"
    )


def make_web_app(metrics: Metrics) -> web.Application:
    async def metrics_handler(_: web.Request) -> web.Response:
        now = int(time.time())
        snap = await metrics.snapshot(now)
        G_MSGS.set(snap["msgs_total"])
        G_CMDS.set(snap["cmds_total"])
        G_USERS.set(snap["total_users"])
        G_ACTIVE5.set(snap["active_users_5m"])
        G_RPM1.set(snap["rpm_1m"])
        data = generate_latest(REGISTRY)
        return web.Response(body=data, content_type="text/plain; version=0.0.4")

    app = web.Application()
    app.add_routes([web.get("/metrics", metrics_handler)])
    return app


def create_router(metrics: Metrics) -> Router:
    router = Router()

    @router.message(Command("start"))
    async def cmd_start(m: Message) -> None:
        now = int(time.time())
        await metrics.note_message(chat_id=m.chat.id, is_command=True, now=now)
        await m.answer("Hello! I track live usage stats.\nUse /stats to view them.")

    @router.message(Command("stats"))
    async def cmd_stats(m: Message) -> None:
        now = int(time.time())
        await metrics.note_message(chat_id=m.chat.id, is_command=True, now=now)
        snap = await metrics.snapshot(now)
        await m.answer(format_stats(snap), reply_markup=stats_keyboard())

    @router.message()
    async def any_message(m: Message) -> None:
        now = int(time.time())
        is_cmd = bool(m.text and m.text.startswith("/"))
        await metrics.note_message(chat_id=m.chat.id, is_command=is_cmd, now=now)

    @router.callback_query(F.data == "stats:refresh")
    async def cb_refresh(cb: CallbackQuery) -> None:
        now = int(time.time())
        snap = await metrics.snapshot(now)
        try:
            await cb.message.edit_text(format_stats(snap), reply_markup=stats_keyboard())
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
        await cb.answer("Updated")

    @router.callback_query(F.data == "stats:close")
    async def cb_close(cb: CallbackQuery) -> None:
        try:
            text = (cb.message.text or "").rstrip()
            if not text.endswith("(closed)"):
                text = f"{text} (closed)".strip()
            await cb.message.edit_text(text, reply_markup=None)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                try:
                    await cb.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
        await cb.answer("Closed")

    return router


async def run() -> None:
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    metrics = Metrics(redis)
    bot = Bot(TELEGRAM_TOKEN)
    dp = Dispatcher()
    dp.include_router(create_router(metrics))

    app = make_web_app(metrics)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HTTP_HOST, HTTP_PORT)
    await site.start()
    log.info(f"HTTP /metrics on http://{HTTP_HOST}:{HTTP_PORT}/metrics")

    stop_event = asyncio.Event()

    def _stop(*_: Any) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    try:
        await asyncio.gather(
            dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()),
            stop_event.wait(),
        )
    finally:
        await runner.cleanup()
        await bot.session.close()
        await redis.close()

if __name__ == "__main__":
    asyncio.run(run())
