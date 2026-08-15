#!/usr/bin/env python3
"""AgentRouter check-in launcher.

Keep browser profiles stable, use the same headed mode as interactive login, and
apply small runtime fixes around AgentRouter local state handling.
"""

from __future__ import annotations

import asyncio
import os
import runpy
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

# Keep daily OAuth runs in the same browser mode as the interactive `add` flow.
os.environ["CHECKIN_HEADLESS"] = "false"

# Load the core without executing main so a couple of runtime behaviors can be
# patched without duplicating the OAuth/check-in implementation.
core = runpy.run_path(str(BASE_DIR / "checkin_core.py"), run_name="agentrouter_core")

_original_capture_user_profile = core["_capture_user_profile_from_console"]
_original_fetch_user_profile_direct = core["_fetch_user_profile_direct"]


async def _clear_agentrouter_auth(context, page, name: str) -> None:
    """Clear AgentRouter auth once before login while preserving GitHub state.

    The old implementation used add_init_script(), which ran on every later
    AgentRouter navigation and could delete localStorage.user again after OAuth.
    That made /api/user/self intermittently fail because New-Api-User was lost.
    """
    provider_domain = core["PROVIDER_DOMAIN"]
    hostname = urlparse(provider_domain).hostname
    if not hostname:
        raise RuntimeError(f"无法解析 AgentRouter 域名: {provider_domain}")

    failures = []
    for domain in {hostname, f".{hostname}"}:
        try:
            await context.clear_cookies(domain=domain)
        except Exception as exc:
            failures.append(str(exc))

    if len(failures) == 2:
        raise RuntimeError(f"无法只清理 AgentRouter Cookie: {failures[0]}")

    try:
        await page.goto(
            f"{provider_domain}/",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        await page.evaluate(
            """() => {
                localStorage.removeItem('user');
                sessionStorage.clear();
            }"""
        )
    except Exception as exc:
        print(f"[WARN] {name}: AgentRouter storage 清理失败: {exc}")


async def _fetch_user_profile(page) -> dict | None:
    """Read balance with native-response capture first, then short retries."""
    profile = await _original_capture_user_profile(page, timeout_ms=15_000)
    if profile:
        return profile

    print("[WARN] 未捕获页面原生 /api/user/self 响应，改用浏览器主动查询")

    for attempt in range(1, 5):
        profile = await _original_fetch_user_profile_direct(page)
        if profile:
            return profile

        if attempt < 4:
            print(f"[INFO] 余额查询重试 {attempt}/3")
            await asyncio.sleep(2)
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30_000)
            except Exception:
                pass

    return None


core["_clear_agentrouter_auth"] = _clear_agentrouter_auth
core["_fetch_user_profile"] = _fetch_user_profile

raise SystemExit(core["main"]())
