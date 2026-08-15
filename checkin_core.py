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
- 新 AgentRouter session 是签到成功的必要条件
- 余额只用于展示和本地增量估算，不决定签到是否成功
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import sys
import time
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlencode, urlparse

from dotenv import load_dotenv

load_dotenv()

# All default relative paths are anchored to this file, not to the process CWD,
# so `python checkin_core.py` from any directory touches the same profiles.
BASE_DIR = Path(__file__).resolve().parent


def _resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


PROVIDER_DOMAIN = os.getenv("AGENTROUTER_DOMAIN", "https://agentrouter.org").rstrip("/")
PROVIDER_HOST = urlparse(PROVIDER_DOMAIN).hostname or ""
LOGIN_PATH = "/login"
CONSOLE_PATH = "/console"
USER_SELF_PATH = "/api/user/self"

GITHUB_PROFILE_URL = "https://github.com/settings/profile"

# AgentRouter reports quota in internal units; 500000 units == $1.
QUOTA_UNITS_PER_DOLLAR = 500_000

PROFILE_MARKER = ".agentrouter-github-profile.json"
PROFILE_ROOT = _resolve_path(os.getenv("CHECKIN_BROWSER_PROFILE_DIR", ".browser_profiles")) / "agentrouter"
STATE_FILE = _resolve_path(os.getenv("AGENTROUTER_LOCAL_STATE_FILE", "agentrouter_local_state.json"))

WAIT_TIMEOUT_MS = int(os.getenv("CHECKIN_WAIT_TIMEOUT_MS", "120000"))
HEADLESS = os.getenv("CHECKIN_HEADLESS", "false").strip().lower() in {"1", "true", "yes", "on"}
HUMANIZE = os.getenv("CHECKIN_HUMANIZE", "true").strip().lower() in {"1", "true", "yes", "on"}
PROXY_URL = os.getenv("CHECKIN_PROXY_URL", "").strip() or None
DEBUG = os.getenv("DEBUG_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}

GITHUB_LOGIN_SELECTORS = (
    # Prefer exact login CTA text first. Footer repo links also contain "github"
    # and must never be treated as the OAuth entrypoint.
    'button:has-text("使用 GitHub 继续")',
    'button:has-text("GitHub")',
    "button:has(.semi-icon-github)",
    '.semi-card button:has(.semi-icon-github)',
    'button:has([aria-label*="github" i])',
    '.semi-card button:has([aria-label*="github" i])',
)


def _validate_name(name: str) -> str:
    """Validate a profile name that will be used as a directory name.

    The character class alone is not enough: "." and ".." match it and would
    escape PROFILE_ROOT, which makes `delete <name>` wipe every profile.

    The containment check is purely lexical (normpath, no resolve) so that it
    neither touches the filesystem nor rejects a profile directory the user has
    deliberately symlinked elsewhere.
    """
    name = name.strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("账号名称只能包含字母、数字、点、下划线和短横线")
    if name in {".", ".."}:
        raise ValueError(f"账号名称不能是 {name!r}（保留的目录名）")

    root = os.path.normpath(str(PROFILE_ROOT))
    candidate = os.path.normpath(os.path.join(root, name))
    if candidate == root or os.path.dirname(candidate) != root:
        raise ValueError(f"账号名称非法，会逃逸出 profile 目录: {name!r}")
    return name


def _profile_dir(name: str) -> Path:
    return PROFILE_ROOT / _validate_name(name)


def _marker_path(name: str) -> Path:
    return _profile_dir(name) / PROFILE_MARKER


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON via a temp file + os.replace.

    A truncated marker loses fingerprint_seed, which silently changes the
    browser identity GitHub sees. Never leave a half-written file behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_json_file(path: Path) -> dict:
    """Read a JSON dict, quarantining corrupt files instead of silently losing them."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        backup = path.with_name(f"{path.name}.corrupt")
        try:
            os.replace(path, backup)
            print(f"[WARN] {path.name} 解析失败（{exc}），已备份为 {backup.name}")
        except OSError:
            print(f"[WARN] {path.name} 解析失败: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _read_marker(name: str) -> dict:
    return _read_json_file(_marker_path(name))


def _write_marker(name: str, status: str) -> None:
    data = _read_marker(name)
    data.update(
        {
            "profile": name,
            "status": status,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    _atomic_write_json(_marker_path(name), data)


def _coerce_fingerprint_seed(value: object) -> int | None:
    """Normalize a stored seed to the integer form the patched Chromium expects.

    cloakbrowser documents `--fingerprint=12345` and its own default is
    `random.randint(10000, 99999)`. Earlier versions of this script stored a
    32-char hex string, which the binary does not parse as a seed. Fold any
    legacy hex value down deterministically so a given profile keeps a stable
    identity instead of getting a fresh random one.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 10_000 <= value <= 99_999 else None

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        return number if 10_000 <= number <= 99_999 else None

    try:
        int(text, 16)
    except ValueError:
        return None
    # Deterministic legacy-hex -> numeric-seed migration.
    digest = sha256(text.encode("utf-8")).digest()
    return 10_000 + int.from_bytes(digest[:8], "big") % 90_000


def _ensure_fingerprint_seed(name: str) -> int:
    """为每个 Profile 生成并持久化固定浏览器指纹 seed。

    CloakBrowser 在未指定 fingerprint seed 时，每次启动会生成新的浏览器身份。
    GitHub 会将这类变化视为设备环境变化，因此同一个 Profile 必须始终复用同一个 seed。
    seed 必须是 cloakbrowser/补丁 Chromium 能解析的数字（见 _coerce_fingerprint_seed）。
    """
    data = _read_marker(name)
    stored = data.get("fingerprint_seed")
    seed = _coerce_fingerprint_seed(stored)

    if seed is not None and seed == stored:
        return seed

    if seed is None:
        seed = 10_000 + secrets.randbelow(90_000)
        if stored is not None:
            print(f"[WARN] {name}: fingerprint seed 无法识别，已重新生成（浏览器指纹会变化）")
    else:
        print(f"[INFO] {name}: 已将旧版 fingerprint seed 迁移为数字 {seed}")

    data.update(
        {
            "profile": name,
            "fingerprint_seed": seed,
            "status": str(data.get("status") or "initializing"),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    _atomic_write_json(_marker_path(name), data)
    return seed


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

    discovered: list[str] = []
    for path in PROFILE_ROOT.iterdir():
        if not path.is_dir():
            continue
        try:
            candidate = _validate_name(path.name)
        except ValueError:
            continue
        if _marker_path(candidate).exists():
            discovered.append(candidate)
    return sorted(discovered)


def _load_state() -> dict:
    return _read_json_file(STATE_FILE)


def _save_state(state: dict) -> None:
    _atomic_write_json(STATE_FILE, state)


async def _launch_context(name: str, *, headless: bool):
    from cloakbrowser import launch_persistent_context_async

    profile = _profile_dir(name)
    profile.mkdir(parents=True, exist_ok=True)
    fingerprint_seed = _ensure_fingerprint_seed(name)

    print(f"[INFO] Browser profile: {profile.resolve()}")

    # Do not pass an explicit viewport: cloakbrowser's DEFAULT_VIEWPORT (1920x947)
    # is deliberately screen height minus taskbar and Chrome UI. Forcing 1920x1080
    # makes window.innerHeight == screen.height, which is an automation tell.
    kwargs = {
        "headless": headless,
        "humanize": HUMANIZE,
        "args": [f"--fingerprint={fingerprint_seed}"],
    }
    if HUMANIZE:
        kwargs["human_preset"] = "careful"
    if PROXY_URL:
        kwargs["proxy"] = {"server": PROXY_URL}
        print("[INFO] Browser proxy enabled")

    return await launch_persistent_context_async(str(profile), **kwargs)


def _cookie_matches_host(cookie: dict, host: str) -> bool:
    """True when a cookie belongs to `host` or one of its parent domains."""
    domain = str(cookie.get("domain") or "").lstrip(".").lower()
    if not domain:
        return False
    host = host.lower()
    return host == domain or host.endswith(f".{domain}")


async def _cookies_for(context, url: str, host: str) -> list[dict]:
    """Fetch cookies for `url`, falling back to a domain-filtered full dump.

    The fallback must stay domain-scoped: matching a bare cookie name across the
    whole jar can pick up an unrelated site's `session` / `user_session`.
    """
    try:
        return list(await context.cookies(url))
    except Exception:
        cookies = await context.cookies()
    return [c for c in cookies if _cookie_matches_host(c, host)]


async def _github_logged_in(context) -> bool:
    """Return True only when a real GitHub session cookie is present.

    `logged_in=yes` alone is treated as insufficient because it can linger after
    a session is no longer accepted for OAuth.
    """
    cookies = await _cookies_for(context, "https://github.com", "github.com")
    by_name = {c.get("name"): c.get("value") for c in cookies}
    user_session = by_name.get("user_session")
    return bool(isinstance(user_session, str) and user_session.strip())


def _github_url_kind(url: str) -> str:
    """Classify a GitHub URL for OAuth/session troubleshooting."""
    if not url or "github.com" not in url:
        return "other"

    lower = url.lower()
    path = urlparse(url).path.lower()

    if "github.com/login/oauth/authorize" in lower:
        return "oauth_authorize"
    if "github.com/login/oauth/" in lower:
        return "oauth_flow"
    if any(
        token in lower
        for token in (
            "/sessions/two-factor",
            "/sessions/verified-device",
            "device_verification",
            "/auth/verified-device",
            "captcha",
            "challenge",
            "/sessions/unauthenticated",
        )
    ):
        return "challenge"
    if path.rstrip("/") == "/login":
        return "password_login"
    return "other"


async def _looks_like_password_login(page) -> bool:
    """Detect an actual username/password login form, not OAuth intermediate pages."""
    if page is None or page.is_closed():
        return False

    kind = _github_url_kind(page.url)
    if kind != "password_login":
        return False

    try:
        login_field = page.locator('input[name="login"], #login_field')
        password = page.locator('input[name="password"], #password')
        if await login_field.count() > 0 and await password.count() > 0:
            if await login_field.first.is_visible() and await password.first.is_visible():
                return True
    except Exception:
        pass

    return False


async def _wait_github_auth_settled(page, timeout_ms: int = 15_000) -> str:
    """Wait for GitHub redirects after OAuth entry, then return the settled kind.

    A freshly opened popup sits on about:blank and a mid-flight tab can briefly
    be on AgentRouter, both of which classify as "other". Returning immediately
    on "other" means the gate check never observes the real destination, so keep
    polling until we see a decisive kind, the page leaves GitHub for good, or we
    run out of time.
    """
    if page is None or page.is_closed():
        return "other"

    deadline = time.monotonic() + timeout_ms / 1000
    # A popup starts on about:blank. Give it a bounded window to navigate before
    # concluding anything; a page that has already navigated is classified at once,
    # so the success path costs nothing extra.
    blank_deadline = time.monotonic() + min(5.0, timeout_ms / 1000)
    last_kind = _github_url_kind(page.url)

    while time.monotonic() < deadline:
        if page.is_closed():
            return last_kind

        try:
            url = page.url
        except Exception:
            return last_kind

        if url == "about:blank":
            if time.monotonic() >= blank_deadline:
                return "other"
            await asyncio.sleep(0.2)
            continue

        last_kind = _github_url_kind(url)

        if last_kind in {"oauth_authorize", "oauth_flow", "challenge"}:
            return last_kind

        if last_kind != "password_login":
            return last_kind

        if await _looks_like_password_login(page):
            await asyncio.sleep(1.0)
            if page.is_closed():
                return "password_login"
            if _github_url_kind(page.url) == "password_login" and await _looks_like_password_login(page):
                return "password_login"

        await asyncio.sleep(0.3)

    return last_kind


async def _github_gate_error(page, context, name: str) -> str | None:
    """Return a precise GitHub gate error, or None when OAuth can continue.

    Only persist marker status=expired when the session cookie is actually
    missing. Challenge/risk pages must not permanently lock the profile.
    """
    if page is None or page.is_closed():
        return None

    kind = await _wait_github_auth_settled(page)
    if kind in {"oauth_authorize", "oauth_flow"}:
        await _confirm_oauth(page)
        kind = await _wait_github_auth_settled(page, timeout_ms=8_000)

    if kind in {"oauth_authorize", "oauth_flow"}:
        return None

    if kind not in {"password_login", "challenge"}:
        return None

    logged_in = await _github_logged_in(context)

    if kind == "challenge":
        return (
            f"GitHub 需要人工验证/二次验证，请在 headed 浏览器中完成后再重试"
            f"（可执行: uv run python checkin.py add {name}）"
        )

    if kind == "password_login":
        if not logged_in:
            _write_marker(name, "expired")
            return f"GitHub 登录态已失效，请执行: uv run python checkin.py add {name}"

        return (
            f"GitHub 未接受当前浏览器会话（cookie 仍在，但 OAuth 落到登录页），"
            f"请执行: uv run python checkin.py add {name} 刷新会话或完成验证"
        )

    return None


async def _provider_session(context) -> str | None:
    cookies = await _cookies_for(context, PROVIDER_DOMAIN, PROVIDER_HOST)
    # Sort for determinism: an exact-host cookie and a parent-domain cookie can
    # coexist, and flip-flopping between them would look like a new session.
    for cookie in sorted(cookies, key=lambda c: str(c.get("domain") or "")):
        if cookie.get("name") == "session" and cookie.get("value"):
            return str(cookie["value"])
    return None


async def _clear_agentrouter_auth(context, page, name: str) -> None:
    """只清理 AgentRouter 登录态，保留 GitHub profile。"""
    hostname = PROVIDER_HOST
    if not hostname:
        raise RuntimeError(f"无法解析 AgentRouter 域名: {PROVIDER_DOMAIN}")

    domains = [hostname, f".{hostname}"]
    failures = []
    for domain in domains:
        try:
            await context.clear_cookies(domain=domain)
        except Exception as exc:
            failures.append(str(exc))

    if len(failures) == len(domains):
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

    try:
        current_host = urlparse(page.url).hostname
        if current_host == hostname:
            await page.evaluate(
                """() => {
                    localStorage.removeItem('user');
                    sessionStorage.clear();
                }"""
            )
    except Exception as exc:
        print(f"[WARN] {name}: AgentRouter storage 清理失败: {exc}")


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
    """Compatibility wrapper: find+click only. Prefer _start_github_oauth_from_button."""
    locator = await _find_github_login_locator(page, timeout_ms=timeout_ms)
    if locator is None:
        return False
    try:
        await locator.scroll_into_view_if_needed()
        try:
            await locator.click(timeout=5_000)
        except Exception:
            await locator.click(force=True, timeout=5_000)
        return True
    except Exception:
        return False


async def _find_github_login_locator(page, timeout_ms: int = 30_000):
    """Return the best visible GitHub login CTA locator, or None."""
    deadline = time.monotonic() + timeout_ms / 1000
    github_name = re.compile(r"(?:使用\s*)?GitHub", re.I)

    while time.monotonic() < deadline:
        await _dismiss_popups(page)

        candidates = []

        try:
            role_button = page.get_by_role("button", name=github_name)
            for i in range(await role_button.count()):
                candidates.append(role_button.nth(i))
        except Exception:
            pass

        for selector in GITHUB_LOGIN_SELECTORS:
            locators = page.locator(selector)
            try:
                count = await locators.count()
            except Exception:
                continue
            for i in range(count):
                candidates.append(locators.nth(i))

        for locator in candidates:
            try:
                if not await locator.is_visible():
                    continue

                try:
                    href = await locator.get_attribute("href")
                except Exception:
                    href = None
                if href and "github.com/login/oauth" not in href and re.search(r"github\.com/(?!login)", href, re.I):
                    continue

                try:
                    text = (await locator.inner_text()).strip()
                except Exception:
                    text = ""
                if text and (not github_name.search(text)) and not href:
                    aria = (await locator.get_attribute("aria-label") or "").strip()
                    if not github_name.search(aria):
                        continue

                return locator
            except Exception:
                continue

        await asyncio.sleep(0.5)

    return None


async def _wait_for_github_oauth_page(context, page, pages_before, timeout_ms: int = 15_000):
    """Wait for GitHub OAuth to open via popup or same-tab navigation.

    AgentRouter currently opens OAuth in a popup. That popup may already finish
    and land back on AgentRouter before we inspect it, so completed provider
    pages are also accepted.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    known = set(pages_before)

    while time.monotonic() < deadline:
        for candidate in context.pages:
            if candidate in known or candidate.is_closed():
                continue
            try:
                url = candidate.url
            except Exception:
                continue
            if url == "about:blank":
                settle_deadline = time.monotonic() + 5
                while time.monotonic() < settle_deadline:
                    if candidate.is_closed():
                        break
                    try:
                        url = candidate.url
                    except Exception:
                        break
                    if "github.com" in url or (
                        PROVIDER_DOMAIN in url and "/login" not in url
                    ):
                        return candidate
                    await asyncio.sleep(0.2)
                continue

            if "github.com" in url:
                return candidate
            if PROVIDER_DOMAIN in url and "/login" not in url:
                return candidate

        if page is not None and not page.is_closed() and "github.com" in page.url:
            return page

        await asyncio.sleep(0.2)

    for candidate in context.pages:
        if candidate.is_closed():
            continue
        try:
            url = candidate.url
            if "github.com" in url or (PROVIDER_DOMAIN in url and "/login" not in url and candidate not in known):
                return candidate
        except Exception:
            continue
    return None


async def _start_github_oauth_from_button(context, page, timeout_ms: int = 20_000):
    """Click the login CTA and wait for the resulting OAuth popup/page."""
    locator = await _find_github_login_locator(page, timeout_ms=min(timeout_ms, 15_000))
    if locator is None:
        return False, None

    pages_before = tuple(context.pages)
    oauth_page = None

    try:
        await locator.scroll_into_view_if_needed()
    except Exception:
        pass

    # Best path: wait for popup concurrently with the click.
    try:
        async with context.expect_page(timeout=timeout_ms) as page_info:
            try:
                await locator.click(timeout=5_000)
            except Exception:
                await locator.click(force=True, timeout=5_000)
        oauth_page = await page_info.value
    except Exception:
        # Do not click again. The first click may already have opened OAuth.
        pass

    if oauth_page is None:
        oauth_page = await _wait_for_github_oauth_page(
            context,
            page,
            pages_before,
            timeout_ms=timeout_ms,
        )
    elif oauth_page is not None:
        # Popup exists; give about:blank a moment to navigate.
        if not oauth_page.is_closed() and oauth_page.url == "about:blank":
            settled = await _wait_for_github_oauth_page(
                context,
                page,
                pages_before,
                timeout_ms=5_000,
            )
            if settled is not None:
                oauth_page = settled

    return True, oauth_page


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


async def _wait_oauth_user_context(page, timeout_ms: int = 10_000) -> bool:
    """Wait for the OAuth callback SPA to persist the returned user object."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if page is None or page.is_closed():
            return False
        try:
            user_id = await page.evaluate(
                """() => {
                    try {
                        const user = JSON.parse(localStorage.getItem('user') || '{}');
                        return user?.id ?? null;
                    } catch (_) {
                        return null;
                    }
                }"""
            )
            if user_id is not None:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


def _oauth_callback_completed(oauth_page, provider_page) -> bool:
    pages = []
    if oauth_page is not None and not oauth_page.is_closed():
        pages.append(oauth_page)
    if provider_page is not None and not provider_page.is_closed():
        pages.append(provider_page)
    return any(
        PROVIDER_DOMAIN in page.url and "/login" not in page.url
        for page in pages
    )


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
    observed_statuses: list[int] = []
    response_errors: list[str] = []

    async def on_response(response) -> None:
        nonlocal captured
        if captured is not None:
            return
        if USER_SELF_PATH not in response.url:
            return
        observed_statuses.append(response.status)
        if response.status != 200:
            response_errors.append(f"HTTP {response.status}")
            return
        try:
            payload = await response.json()
        except Exception as exc:
            response_errors.append(f"响应内容不可读取: {exc}")
            return
        profile = _extract_user_profile(payload)
        if profile:
            captured = profile
            captured_event.set()
        else:
            keys = sorted(str(key) for key in payload) if isinstance(payload, dict) else []
            response_errors.append(f"响应结构无法识别，顶层字段: {keys}")

    page.on("response", on_response)
    try:
        try:
            await page.goto(
                f"{PROVIDER_DOMAIN}{CONSOLE_PATH}",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
        except Exception as exc:
            print(f"[WARN] 打开 AgentRouter 控制台失败: {exc}")

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
        elif not observed_statuses:
            print("[WARN] AgentRouter 控制台未发出 /api/user/self 请求")
        elif response_errors:
            print(f"[WARN] 页面原生 /api/user/self 未能读取余额: {response_errors[-1]}")
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
    profile = await _capture_user_profile_from_console(page, timeout_ms=15_000)
    if profile:
        return profile
    print("[WARN] 未捕获页面原生 /api/user/self 响应，改用浏览器主动查询")
    for attempt in range(1, 5):
        profile = await _fetch_user_profile_direct(page)
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


def _balance(profile: dict | None) -> tuple[float, float] | None:
    if not isinstance(profile, dict):
        return None
    try:
        quota = round(float(profile["quota"]) / QUOTA_UNITS_PER_DOLLAR, 2)
        used = round(float(profile["used_quota"]) / QUOTA_UNITS_PER_DOLLAR, 2)
    except (KeyError, TypeError, ValueError):
        return None
    return quota, used


async def add_profile(name: str) -> bool:
    name = _validate_name(name)
    profile = _profile_dir(name)
    existed = profile.exists()

    context = await _launch_context(name, headless=False)
    try:
        page = await context.new_page()
        action = "更新" if existed else "创建"
        print(f"[SETUP] {action} GitHub Profile: {profile}")
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

    status = _profile_status(name)
    # "expired" is advisory only. Always re-verify live cookies/OAuth instead of
    # hard-failing forever after one transient GitHub challenge.
    if status not in {"valid", "expired"}:
        return {
            "name": name,
            "success": False,
            "error": f"Profile 未配置，请执行: uv run python checkin.py add {name}",
        }
    if status == "expired":
        print(f"[INFO] {name}: marker 为 expired，改为实时重新验证 GitHub 会话")

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
        clicked, oauth_page = await _start_github_oauth_from_button(context, page)

        if oauth_page is not None:
            print(f"[{name}] 已通过登录按钮进入 GitHub OAuth")
            await _confirm_oauth(oauth_page)

        still_on_login = PROVIDER_DOMAIN in page.url and "/login" in page.url
        if oauth_page is None and (not clicked or still_on_login):
            auth_url = await _build_oauth_url(page)
            if not auth_url:
                auth_url = f"{PROVIDER_DOMAIN}/api/oauth/github"

            if clicked:
                print(f"[{name}] 登录按钮已点击，但未观察到 OAuth 页面，使用 OAuth fallback")
            else:
                print(f"[{name}] 未找到可用登录按钮，使用 OAuth fallback")
            await page.goto(auth_url, wait_until="domcontentloaded", timeout=60_000)

            if "github.com/" in page.url:
                oauth_page = page
                await _confirm_oauth(oauth_page)

        for candidate in (oauth_page, page):
            gate_error = await _github_gate_error(candidate, context, name)
            if gate_error:
                return {
                    "name": name,
                    "success": False,
                    "error": gate_error,
                }

        new_session = await _wait_new_session(previous_session, context, timeout_ms=WAIT_TIMEOUT_MS)
        callback_done = _oauth_callback_completed(oauth_page, page)

        if new_session:
            print(f"[{name}] [INFO] 已检测到新的 AgentRouter session")
        else:
            print(f"[{name}] [WARN] 未观察到新的 AgentRouter session")

        if callback_done:
            print(f"[{name}] [INFO] OAuth 回调已完成")
        else:
            print(f"[{name}] [WARN] 未明确观察到 OAuth 回调页面")

        # Bail out before the balance lookup: it costs up to ~40s of listening,
        # retries and reloads, and the result is discarded on a failed run anyway.
        if not new_session:
            return {
                "name": name,
                "success": False,
                "error": "未检测到新的 AgentRouter session，本次不能视为完成签到",
            }

        callback_page = oauth_page if oauth_page is not None and not oauth_page.is_closed() else page
        if await _wait_oauth_user_context(callback_page):
            print(f"[{name}] [INFO] OAuth 用户上下文已就绪")
        else:
            print(f"[{name}] [WARN] OAuth 用户上下文未在预期时间内就绪")

        user_profile = await _fetch_user_profile(page)
        balance = _balance(user_profile)

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
        # Directory contents are untrusted input too; skip anything that would
        # not survive _validate_name rather than letting it reach _profile_dir.
        for path in PROFILE_ROOT.iterdir():
            if not path.is_dir():
                continue
            try:
                names.add(_validate_name(path.name))
            except ValueError:
                print(f"⚠️  跳过非法 Profile 目录名: {path.name!r}")

    if not names:
        print("没有 AgentRouter GitHub Profile")
        return 0

    for name in sorted(names):
        status = _profile_status(name)
        if status == "valid":
            label = "configured"
            icon = "✅"
        elif status == "expired":
            label = "github-session-expired-advisory"
            icon = "❌"
        else:
            label = status
            icon = "⚠️"
        print(f"{icon} {name}: {label} ({_profile_dir(name)})")

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

    try:
        if not args:
            return asyncio.run(run_daily())
        if args[0] == "add" and len(args) == 2:
            return 0 if asyncio.run(add_profile(args[1])) else 1
        if args[0] == "list" and len(args) == 1:
            return list_profiles()
        if args[0] == "delete" and len(args) == 2:
            return delete_profile(args[1])
    except ValueError as exc:
        # Bad profile name or malformed AGENTROUTER_ACCOUNTS: a traceback here
        # is noise, the message already says exactly what to fix.
        print(f"[FAILED] 配置错误: {exc}")
        return 2
    except KeyboardInterrupt:
        print("\n[ABORTED] 已中断")
        return 130

    print_usage()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
