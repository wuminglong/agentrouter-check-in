#!/usr/bin/env python3
"""AgentRouter 本地 GitHub OAuth 自动签到。

首次运行:
    uv run python checkin.py add <name>

每日运行:
    uv run python checkin.py

核心原则:
- GitHub 登录态只保存在本机 .browser_profiles/
- 每次签到前只清理 AgentRouter 登录态
- 重新走 GitHub OAuth，触发 AgentRouter 每日签到
- 新 AgentRouter session 是 OAuth 成功的重要证据
- 余额查询失败不会把已经完成的 OAuth 签到误判为失败
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse

from dotenv import load_dotenv

load_dotenv()

PROVIDER_DOMAIN = os.getenv("AGENTROUTER_DOMAIN", "https://agentrouter.org").rstrip("/")
LOGIN_PATH = "/login"
CONSOLE_PATH = "/console"
USER_SELF_PATH = "/api/user/self"

GITHUB_PROFILE_URL = "https://github.com/settings/profile"
GITHUB_LOGIN_PREFIX = "https://github.com/login"

PROFILE_MARKER = ".agentrouter-github-profile.json"
PROFILE_ROOT = Path(os.getenv("CHECKIN_BROWSER_PROFILE_DIR", ".browser_profiles")) / "agentrouter"
STATE_FILE = Path(os.getenv("AGENTROUTER_LOCAL_STATE_FILE", "agentrouter_local_state.json"))

WAIT_TIMEOUT_MS = int(os.getenv("CHECKIN_WAIT_TIMEOUT_MS", "120000"))
HEADLESS = os.getenv("CHECKIN_HEADLESS", "true").strip().lower() in {"1", "true", "yes", "on"}
HUMANIZE = os.getenv("CHECKIN_HUMANIZE", "true").strip().lower() in {"1", "true", "yes", "on"}
PROXY_URL = os.getenv("CHECKIN_PROXY_URL", "").strip() or None
DEBUG = os.getenv("DEBUG_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}

GITHUB_LOGIN_SELECTORS = (
    ".semi-card button:has(.semi-icon-github)",
    '.semi-card button:has([aria-label*="github" i])',
    "button:has(.semi-icon-github)",
    'button:has([aria-label*="github" i])',
    'a[href*="github" i]',
)


def _validate_name(name: str) -> str:
    name = name.strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("账号名称只能包含字母、数字、点、下划线和短横线")
    return name


def _profile_dir(name: str) -> Path:
    return PROFILE_ROOT / _validate_name(name)


def _marker_path(name: str) -> Path:
    return _profile_dir(name) / PROFILE_MARKER


def _read_marker(name: str) -> dict:
    path = _marker_path(name)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_marker(name: str, status: str) -> None:
    path = _marker_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_marker(name)
    data.update(
        {
            "profile": name,
            "status": status,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _profile_status(name: str) -> str:
    return str(_read_marker(name).get("status") or "missing")


def _load_account_names() -> list[str]:
    raw = os.getenv("AGENTROUTER_ACCOUNTS", "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"AGENTROUTER_ACCOUNTS 不是合法 JSON: {exc}") from exc
        if not isinstance(data, list):
            raise ValueError("AGENTROUTER_ACCOUNTS 必须是 JSON 数组")

        names: list[str] = []
        for item in data:
            if isinstance(item, str):
                names.append(_validate_name(item))
            elif isinstance(item, dict):
                value = item.get("browser_profile") or item.get("name")
                if not isinstance(value, str):
                    raise ValueError("账号对象必须包含 name 或 browser_profile")
                names.append(_validate_name(value))
            else:
                raise ValueError("AGENTROUTER_ACCOUNTS 只支持字符串或对象")
        return list(dict.fromkeys(names))

    if not PROFILE_ROOT.exists():
        return []
    return sorted(
        p.name for p in PROFILE_ROOT.iterdir()
        if p.is_dir() and _marker_path(p.name).exists()
    )


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


async def _launch_context(name: str, *, headless: bool):
    from cloakbrowser import launch_persistent_context_async

    profile = _profile_dir(name)
    profile.mkdir(parents=True, exist_ok=True)

    kwargs = {
        "headless": headless,
        "humanize": HUMANIZE,
        "viewport": {"width": 1920, "height": 1080},
    }
    if HUMANIZE:
        kwargs["human_preset"] = "careful"
    if PROXY_URL:
        kwargs["proxy"] = {"server": PROXY_URL}
        print("[INFO] Browser proxy enabled")

    return await launch_persistent_context_async(str(profile), **kwargs)


async def _github_logged_in(context) -> bool:
    try:
        cookies = await context.cookies("https://github.com")
    except Exception:
        cookies = await context.cookies()
    by_name = {c.get("name"): c.get("value") for c in cookies}
    return bool(by_name.get("user_session") or by_name.get("logged_in") == "yes")


async def _provider_session(context) -> str | None:
    try:
        cookies = await context.cookies(PROVIDER_DOMAIN)
    except Exception:
        cookies = await context.cookies()
    for cookie in cookies:
        if cookie.get("name") == "session" and cookie.get("value"):
            return str(cookie["value"])
    return None


async def _clear_agentrouter_auth(context, page, name: str) -> None:
    """只清理 AgentRouter 登录态，保留 GitHub profile。"""
    hostname = urlparse(PROVIDER_DOMAIN).hostname
    if not hostname:
        raise RuntimeError(f"无法解析 AgentRouter 域名: {PROVIDER_DOMAIN}")

    failures = []
    for domain in {hostname, f".{hostname}"}:
        try:
            await context.clear_cookies(domain=domain)
        except Exception as exc:
            failures.append(str(exc))

    if len(failures) == 2:
        raise RuntimeError(f"无法只清理 AgentRouter Cookie: {failures[0]}")

    try:
        await page.add_init_script(
            f"""() => {{
                if (location.hostname === {json.dumps(hostname)}) {{
                    localStorage.removeItem('user');
                    sessionStorage.clear();
                }}
            }}"""
        )
    except Exception as exc:
        print(f"[WARN] {name}: storage 清理脚本安装失败: {exc}")


async def _dismiss_popups(page) -> int:
    """尽量关闭 AgentRouter 页面上的公告/弹窗，避免挡住 GitHub 登录按钮。"""
    script = r"""() => {
        const visible = (el) => {
            if (!el) return false;
            const s = getComputedStyle(el);
            const r = el.getBoundingClientRect();
            return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
        };
        let count = 0;
        const selectors = [
            '.semi-modal-close',
            '.semi-dialog-close',
            'button[aria-label="Close"]',
            'button[aria-label="close"]'
        ];
        for (const selector of selectors) {
            for (const el of document.querySelectorAll(selector)) {
                if (visible(el)) {
                    el.click();
                    count++;
                }
            }
        }
        const textRe = /^(知道了|我知道了|关闭|确定|确认|OK|Close|Got it)$/i;
        for (const root of document.querySelectorAll('.semi-modal, .semi-dialog, [role="dialog"]')) {
            if (!visible(root)) continue;
            for (const el of root.querySelectorAll('button')) {
                if (visible(el) && textRe.test((el.innerText || '').trim())) {
                    el.click();
                    count++;
                    break;
                }
            }
        }
        return count;
    }"""
    try:
        return int(await page.evaluate(script))
    except Exception:
        return 0


async def _open_login_page(page, name: str) -> None:
    base = f"{PROVIDER_DOMAIN}/"
    login_url = f"{PROVIDER_DOMAIN}{LOGIN_PATH}"

    try:
        print(f"[INFO] Warming up {base} before login")
        await page.goto(base, wait_until="load", timeout=60_000)
        await asyncio.sleep(3)
        closed = await _dismiss_popups(page)
        if closed:
            print(f"[INFO] Dismissed {closed} popup dialog(s) during warmup")
    except Exception as exc:
        print(f"[WARN] {name}: warmup 失败: {exc}")

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            print(f"[INFO] Navigating login page (attempt {attempt}/3): {login_url}")
            await page.goto(login_url, wait_until="load", timeout=60_000)
            await asyncio.sleep(5)
            await _dismiss_popups(page)
            return
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                await asyncio.sleep(3)

    raise RuntimeError(f"AgentRouter 登录页打开失败: {last_error}")


async def _click_github_login(page, timeout_ms: int = 30_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        await _dismiss_popups(page)

        for selector in GITHUB_LOGIN_SELECTORS:
            locators = page.locator(selector)
            try:
                count = await locators.count()
            except Exception:
                continue

            for i in range(count):
                locator = locators.nth(i)
                try:
                    if not await locator.is_visible():
                        continue
                    await locator.scroll_into_view_if_needed()
                    try:
                        await locator.click(timeout=5_000)
                    except Exception:
                        await locator.click(force=True, timeout=5_000)
                    return True
                except Exception:
                    continue

        try:
            button = page.get_by_role("button", name=re.compile(r"GitHub", re.I)).first
            if await button.is_visible():
                await button.click(timeout=5_000)
                return True
        except Exception:
            pass

        await asyncio.sleep(0.5)

    return False


async def _build_oauth_url(page) -> str | None:
    """从 AgentRouter 当前页面取得 GitHub client_id + OAuth state。"""
    try:
        data = await page.evaluate(
            """async () => {
                try {
                    const status = JSON.parse(localStorage.getItem('status') || '{}');
                    const clientId = status.github_client_id;
                    if (!clientId) return null;
                    const response = await fetch('/api/oauth/state', {cache: 'no-store'});
                    const payload = await response.json();
                    if (!payload?.success || !payload?.data) return null;
                    return {clientId, state: payload.data};
                } catch (_) {
                    return null;
                }
            }"""
        )
    except Exception:
        return None

    if not isinstance(data, dict) or not data.get("clientId") or not data.get("state"):
        return None

    return "https://github.com/login/oauth/authorize?" + urlencode(
        {
            "client_id": str(data["clientId"]),
            "state": str(data["state"]),
            "scope": "user:email",
        }
    )


async def _confirm_oauth(page, timeout_ms: int = 10_000) -> bool:
    """首次授权或授权变化时自动点击 Authorize/Reauthorize。"""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if page.is_closed():
            return False

        if page.url != "about:blank" and "github.com/login/oauth/authorize" not in page.url:
            return False

        try:
            button = page.get_by_role(
                "button",
                name=re.compile(r"^(?:Re)?Authorize\b", re.I),
            ).first
            if await button.is_visible():
                await button.click(timeout=5_000)
                return True
        except Exception:
            pass

        await asyncio.sleep(0.2)

    return False


async def _wait_new_session(previous: str | None, context, timeout_ms: int = 45_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        current = await _provider_session(context)
        if current and current != previous:
            return True
        await asyncio.sleep(0.5)
    return False


def _oauth_callback_completed(oauth_page, provider_page) -> bool:
    if oauth_page is None:
        return PROVIDER_DOMAIN in provider_page.url and "/login" not in provider_page.url
    if oauth_page.is_closed():
        return True
    return PROVIDER_DOMAIN in oauth_page.url and "/login" not in oauth_page.url


def _extract_user_profile(payload: object) -> dict | None:
    if not isinstance(payload, dict):
        return None

    data = payload.get("data")
    if payload.get("success") is True and isinstance(data, dict) and data.get("id"):
        return data
    if payload.get("id"):
        return payload
    return None


async def _capture_user_profile_from_console(page, timeout_ms: int = 15_000) -> dict | None:
    """监听 AgentRouter 页面自身的 /api/user/self 响应。

    这是优先路径，因为页面自己的请求会带上 AgentRouter 前端运行时准备的用户上下文。
    """
    captured: dict | None = None
    captured_event = asyncio.Event()

    async def on_response(response) -> None:
        nonlocal captured
        if captured is not None:
            return
        if USER_SELF_PATH not in response.url or response.status != 200:
            return
        try:
            payload = await response.json()
        except Exception:
            return
        profile = _extract_user_profile(payload)
        if profile:
            captured = profile
            captured_event.set()

    page.on("response", on_response)
    try:
        try:
            await page.goto(
                f"{PROVIDER_DOMAIN}{CONSOLE_PATH}",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
        except Exception:
            pass

        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        if captured is None:
            try:
                await asyncio.wait_for(captured_event.wait(), timeout=timeout_ms / 1000)
            except TimeoutError:
                pass

        if captured is not None:
            print("[INFO] 已监听页面原生 /api/user/self 响应并读取余额")
        return captured
    finally:
        page.remove_listener("response", on_response)


async def _fetch_user_profile_direct(page) -> dict | None:
    """原生监听未命中时，在已登录浏览器上下文中主动查询 /api/user/self。"""
    try:
        result = await page.evaluate(
            """async () => {
                try {
                    let userId = null;
                    try {
                        const user = JSON.parse(localStorage.getItem('user') || '{}');
                        userId = user?.id ?? null;
                    } catch (_) {}

                    const headers = {};
                    if (userId !== null && userId !== undefined) {
                        headers['New-Api-User'] = String(userId);
                    }

                    const response = await fetch('/api/user/self', {
                        method: 'GET',
                        credentials: 'include',
                        cache: 'no-store',
                        headers,
                    });
                    return {
                        status: response.status,
                        text: await response.text(),
                        userId,
                    };
                } catch (error) {
                    return {status: 0, text: String(error), userId: null};
                }
            }"""
        )
    except Exception as exc:
        if DEBUG:
            print(f"[DEBUG] 主动余额查询执行失败: {exc}")
        return None

    if not isinstance(result, dict):
        return None

    status = result.get("status")
    if status != 200:
        print(f"[WARN] 主动 /api/user/self 查询失败: HTTP {status}, user_id={result.get('userId')}")
        return None

    try:
        payload = json.loads(str(result.get("text") or ""))
    except json.JSONDecodeError:
        print("[WARN] 主动 /api/user/self 返回了非 JSON 内容")
        return None

    profile = _extract_user_profile(payload)
    if profile:
        print("[INFO] 已通过浏览器主动请求 /api/user/self 读取余额")
    return profile


async def _fetch_user_profile(page) -> dict | None:
    """先监听页面原生请求，再用浏览器主动请求兜底。"""
    profile = await _capture_user_profile_from_console(page)
    if profile:
        return profile
    print("[WARN] 未捕获页面原生 /api/user/self 响应，改用浏览器主动查询")
    return await _fetch_user_profile_direct(page)


def _balance(profile: dict | None) -> tuple[float, float] | None:
    if not isinstance(profile, dict):
        return None
    try:
        quota = round(float(profile["quota"]) / 500000, 2)
        used = round(float(profile["used_quota"]) / 500000, 2)
    except (KeyError, TypeError, ValueError):
        return None
    return quota, used


async def add_profile(name: str) -> bool:
    name = _validate_name(name)
    profile = _profile_dir(name)

    if profile.exists():
        shutil.rmtree(profile)

    context = await _launch_context(name, headless=False)
    try:
        page = await context.new_page()
        print(f"[SETUP] 创建 GitHub Profile: {profile}")
        print("[SETUP] 浏览器打开后，请完成 GitHub 登录和可能的二次验证。")
        await page.goto(GITHUB_PROFILE_URL, wait_until="domcontentloaded", timeout=60_000)

        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if page.url.startswith(GITHUB_PROFILE_URL) and await _github_logged_in(context):
                _write_marker(name, "valid")
                print(f"[SUCCESS] {name}: GitHub 登录态已保存")
                return True
            await asyncio.sleep(1)

        print(f"[FAILED] {name}: 等待 GitHub 登录超时")
        return False
    finally:
        await context.close()


async def check_in(name: str) -> dict:
    name = _validate_name(name)

    if _profile_status(name) != "valid":
        return {
            "name": name,
            "success": False,
            "error": f"Profile 未配置，请执行: uv run python checkin.py add {name}",
        }

    context = await _launch_context(name, headless=HEADLESS)
    page = None

    try:
        if not await _github_logged_in(context):
            _write_marker(name, "expired")
            return {
                "name": name,
                "success": False,
                "error": f"GitHub 登录态已失效，请执行: uv run python checkin.py add {name}",
            }

        page = await context.new_page()
        await _clear_agentrouter_auth(context, page, name)
        await _open_login_page(page, name)

        previous_session = await _provider_session(context)
        pages_before = tuple(context.pages)

        clicked = await _click_github_login(page)
        await asyncio.sleep(1)

        new_pages = [p for p in context.pages if p not in pages_before]
        oauth_page = new_pages[0] if new_pages else None

        if oauth_page is None and "github.com/" in page.url:
            oauth_page = page

        if oauth_page is not None:
            print(f"[{name}] 已进入 GitHub OAuth")
            await _confirm_oauth(oauth_page)

        still_on_login = PROVIDER_DOMAIN in page.url and "/login" in page.url
        if not clicked or (oauth_page is None and still_on_login):
            auth_url = await _build_oauth_url(page)
            if not auth_url:
                auth_url = f"{PROVIDER_DOMAIN}/api/oauth/github"

            print(f"[{name}] 登录按钮未完成跳转，使用 OAuth fallback")
            await page.goto(auth_url, wait_until="domcontentloaded", timeout=60_000)

            if "github.com/" in page.url:
                oauth_page = page
                await _confirm_oauth(oauth_page)

        for candidate in (oauth_page, page):
            if (
                candidate is not None
                and not candidate.is_closed()
                and candidate.url.startswith(GITHUB_LOGIN_PREFIX)
            ):
                _write_marker(name, "expired")
                return {
                    "name": name,
                    "success": False,
                    "error": f"GitHub 要求重新登录，请执行: uv run python checkin.py add {name}",
                }

        new_session = await _wait_new_session(previous_session, context)
        callback_done = _oauth_callback_completed(oauth_page, page)

        if new_session:
            print(f"[{name}] [INFO] 已检测到新的 AgentRouter session")
        else:
            print(f"[{name}] [WARN] 未观察到新的 AgentRouter session")

        if callback_done:
            print(f"[{name}] [INFO] OAuth 回调已完成")
        else:
            print(f"[{name}] [WARN] 未明确观察到 OAuth 回调页面")

        oauth_verified = new_session

        user_profile = await _fetch_user_profile(page)
        balance = _balance(user_profile)

        if not oauth_verified and balance is None:
            return {
                "name": name,
                "success": False,
                "error": "未检测到新的 AgentRouter session，且无法读取登录后的用户信息",
            }

        _write_marker(name, "valid")

        if balance is None:
            return {
                "name": name,
                "success": True,
                "balance_verified": False,
                "reward": None,
            }

        quota, used = balance
        total = round(quota + used, 2)

        state = _load_state()
        previous = state.get(name) if isinstance(state.get(name), dict) else None
        previous_total = None
        if previous is not None:
            try:
                previous_total = float(previous["total"])
            except (KeyError, TypeError, ValueError):
                previous_total = None

        reward = None if previous_total is None else round(max(total - previous_total, 0), 2)

        state[name] = {
            "quota": quota,
            "used": used,
            "total": total,
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_state(state)

        return {
            "name": name,
            "success": True,
            "balance_verified": True,
            "quota": quota,
            "used": used,
            "total": total,
            "reward": reward,
        }

    except Exception as exc:
        if DEBUG:
            print(f"[DEBUG] {name}: {type(exc).__name__}: {exc}")
        return {"name": name, "success": False, "error": str(exc)}
    finally:
        await context.close()


def list_profiles() -> int:
    names = set(_load_account_names())
    if PROFILE_ROOT.exists():
        names.update(p.name for p in PROFILE_ROOT.iterdir() if p.is_dir())

    if not names:
        print("没有 AgentRouter GitHub Profile")
        return 0

    for name in sorted(names):
        status = _profile_status(name)
        icon = "✅" if status == "valid" else "❌" if status == "expired" else "⚠️"
        print(f"{icon} {name}: {status} ({_profile_dir(name)})")

    return 0


def delete_profile(name: str) -> int:
    name = _validate_name(name)
    path = _profile_dir(name)
    if path.exists():
        shutil.rmtree(path)

    state = _load_state()
    if name in state:
        del state[name]
        _save_state(state)

    print(f"已删除 Profile: {name}")
    return 0


async def run_daily() -> int:
    names = _load_account_names()
    if not names:
        print("[FAILED] 没有配置账号。")
        print("先执行: uv run python checkin.py add <name>")
        print('再在 .env 配置: AGENTROUTER_ACCOUNTS=["name1","name2"]')
        return 1

    print(f"[SYSTEM] AgentRouter GitHub OAuth 本地签到，账号数: {len(names)}")
    results = []

    for name in names:
        result = await check_in(name)
        results.append(result)

        if not result["success"]:
            print(f"[{name}] [FAILED] {result['error']}")
            continue

        if not result.get("balance_verified"):
            print(f"[{name}] [SUCCESS] OAuth 登录已完成；余额查询失败，但不误判签到失败")
            continue

        reward = result.get("reward")
        if reward is None:
            reward_text = "首次记录，暂无上次余额用于计算签到增量"
        elif reward > 0:
            reward_text = f"本次签到 +${reward:.2f}"
        else:
            reward_text = "本次总额度无新增（通常表示今天已经签到）"

        print(
            f"[{name}] [SUCCESS] 余额 ${result['quota']:.2f}，"
            f"累计消耗 ${result['used']:.2f}；{reward_text}"
        )

    success_count = sum(1 for r in results if r["success"])
    print(f"\n[STATS] 成功 {success_count}/{len(results)}")
    return 0 if success_count == len(results) else 1


def print_usage() -> None:
    print("Usage:")
    print("  uv run python checkin.py")
    print("  uv run python checkin.py add <name>")
    print("  uv run python checkin.py list")
    print("  uv run python checkin.py delete <name>")


def main() -> int:
    args = sys.argv[1:]

    if not args:
        return asyncio.run(run_daily())
    if args[0] == "add" and len(args) == 2:
        return 0 if asyncio.run(add_profile(args[1])) else 1
    if args[0] == "list" and len(args) == 1:
        return list_profiles()
    if args[0] == "delete" and len(args) == 2:
        return delete_profile(args[1])

    print_usage()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
