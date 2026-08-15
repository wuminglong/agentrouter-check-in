#!/usr/bin/env python3
"""AgentRouter check-in entrypoint.

The core implementation lives in checkin_core.py. This entrypoint keeps runtime
behavior stable: fixed project directory, headed browser mode, persistent
profiles, one-time AgentRouter state cleanup, and resilient balance lookup.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

# Keep daily OAuth runs in the same browser mode as the interactive `add` flow.
# This is intentionally fixed rather than exposed as a user setting.
os.environ["CHECKIN_HEADLESS"] = "false"

import checkin_core as core  # noqa: E402


_original_check_in = core.check_in
_original_capture_user_profile = core._capture_user_profile_from_console
_original_fetch_user_profile_direct = core._fetch_user_profile_direct


async def _clear_agentrouter_auth(context, page, name: str) -> None:
    """Clear AgentRouter auth exactly once while preserving GitHub state."""
    hostname = urlparse(core.PROVIDER_DOMAIN).hostname
    if not hostname:
        raise RuntimeError(f"无法解析 AgentRouter 域名: {core.PROVIDER_DOMAIN}")

    failures: list[str] = []
    for domain in {hostname, f".{hostname}"}:
        try:
            await context.clear_cookies(domain=domain)
        except Exception as exc:
            failures.append(str(exc))

    if len(failures) == 2:
        raise RuntimeError(f"无法只清理 AgentRouter Cookie: {failures[0]}")

    try:
        await page.goto(
            f"{core.PROVIDER_DOMAIN}/",
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
    """Read balance after OAuth, tolerating AgentRouter SPA initialization races."""
    profile = await _original_capture_user_profile(page, timeout_ms=15_000)
    if profile:
        return profile

    print("[WARN] 未从页面原生 /api/user/self 响应读取余额，改用主动查询")

    for attempt in range(1, 5):
        profile = await _original_fetch_user_profile_direct(page)
        if profile:
            return profile

        if attempt < 4:
            print(f"[INFO] 余额查询重试 {attempt}/3")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:
                print(f"[WARN] 余额查询页面刷新失败: {exc}")
            await asyncio.sleep(1)

    return None


async def _add_profile(name: str) -> bool:
    """Login or refresh GitHub auth without deleting the persistent profile."""
    name = core._validate_name(name)
    profile = core._profile_dir(name)
    existed = profile.exists()

    context = await core._launch_context(name, headless=False)
    try:
        page = await context.new_page()
        action = "更新" if existed else "创建"
        print(f"[SETUP] {action} GitHub Profile: {profile}")
        print("[SETUP] 浏览器打开后，请完成 GitHub 登录和可能的二次验证。")
        await page.goto(core.GITHUB_PROFILE_URL, wait_until="domcontentloaded", timeout=60_000)

        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if page.url.startswith(core.GITHUB_PROFILE_URL) and await core._github_logged_in(context):
                core._write_marker(name, "valid")
                print(f"[SUCCESS] {name}: GitHub 登录态已保存")
                return True
            await asyncio.sleep(1)

        print(f"[FAILED] {name}: 等待 GitHub 登录超时")
        return False
    finally:
        await context.close()


async def _check_in(name: str) -> dict:
    """Return precise profile-state errors before entering the core flow."""
    name = core._validate_name(name)
    status = core._profile_status(name)

    if status == "expired":
        return {
            "name": name,
            "success": False,
            "error": f"GitHub 登录态已失效，请执行: uv run python checkin.py add {name}",
        }
    if status != "valid":
        return {
            "name": name,
            "success": False,
            "error": f"Profile 未配置，请执行: uv run python checkin.py add {name}",
        }

    return await _original_check_in(name)


def _list_profiles() -> int:
    """List local configuration state without pretending to live-verify GitHub."""
    names = set(core._load_account_names())
    if core.PROFILE_ROOT.exists():
        names.update(p.name for p in core.PROFILE_ROOT.iterdir() if p.is_dir())

    if not names:
        print("没有 AgentRouter GitHub Profile")
        return 0

    for name in sorted(names):
        status = core._profile_status(name)
        if status == "valid":
            label = "configured"
            icon = "✅"
        elif status == "expired":
            label = "github-session-expired"
            icon = "❌"
        else:
            label = status
            icon = "⚠️"
        print(f"{icon} {name}: {label} ({core._profile_dir(name)})")

    return 0


# Module attribute replacement is intentional: functions in checkin_core resolve
# globals through the module namespace at runtime, so these replacements are used
# by run_daily() and main() without runpy/global-dictionary tricks.
core._clear_agentrouter_auth = _clear_agentrouter_auth
core._fetch_user_profile = _fetch_user_profile
core.add_profile = _add_profile
core.check_in = _check_in
core.list_profiles = _list_profiles

raise SystemExit(core.main())
