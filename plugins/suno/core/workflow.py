from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import random
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from playwright.async_api import BrowserContext, Page, async_playwright

from .auth_bundle import write_auth_bundle


@dataclass(slots=True)
class SunoPaths:
    storage_state: Path
    auth_bundle: Path
    manual_capture: Path
    bearer_token: Path
    api_headers: Path


class SunoWorkflow:
    def __init__(self, paths: SunoPaths) -> None:
        self.paths = paths

    def _plugin_dotenv_path(self) -> Path:
        artifact_dir = self.paths.storage_state.parent
        if artifact_dir.name == "artifacts" and artifact_dir.parent.name == "profile":
            return artifact_dir.parent.parent / ".env"
        return Path(".env")

    @staticmethod
    def _load_dotenv_values(env_path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        if not env_path.exists():
            return values
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                values[key] = value
        return values

    @staticmethod
    async def _safe_wait_visible(locator: Any, timeout_ms: int = 8000) -> bool:
        try:
            await locator.first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except Exception:
            return False

    @staticmethod
    async def _safe_click(locator: Any, timeout_ms: int = 3000) -> bool:
        try:
            if await locator.count() > 0:
                try:
                    await locator.first.click(timeout=timeout_ms)
                    return True
                except Exception:
                    await locator.first.click(timeout=timeout_ms, force=True)
                    return True
        except Exception:
            return False
        return False

    @staticmethod
    async def _safe_click_no_force(locator: Any, timeout_ms: int = 3000) -> bool:
        try:
            if await locator.count() > 0:
                await locator.first.click(timeout=timeout_ms)
                return True
        except Exception:
            return False
        return False

    async def _get_signin_dialog_root(self, page: Page) -> Any | None:
        # Anchor to the exact modal content shown by Suno auth flow.
        candidates = [
            page.locator("div:has(> h1:text-is('Welcome back')):has(p:text-is('Choose a sign in method'))"),
            page.locator("div:has(h1:text-is('Welcome back')):has-text('Choose a sign in method')"),
            page.locator("h1:text-is('Welcome back')").locator("xpath=ancestor::div[1]"),
        ]
        for candidate in candidates:
            try:
                if await candidate.count() > 0 and await candidate.first.is_visible():
                    return candidate.first
            except Exception:
                continue
        return None

    @staticmethod
    async def _safe_fill(locator: Any, value: str, timeout_ms: int = 12000) -> bool:
        try:
            await locator.first.wait_for(state="visible", timeout=timeout_ms)
            await locator.first.fill(value, timeout=timeout_ms)
            return True
        except Exception:
            return False

    @staticmethod
    async def _collect_login_dialog_state(page: Page) -> dict[str, Any]:
        try:
            return await page.evaluate(
                """
                () => {
                  const textHas = (el, txt) => (el?.textContent || '').toLowerCase().includes(txt.toLowerCase());
                  const allDivs = Array.from(document.querySelectorAll('div'));
                  const dialogRoot = allDivs.find((d) => {
                    const h1 = d.querySelector('h1');
                    const p = d.querySelector('p');
                    return h1 && p && textHas(h1, 'Welcome back') && textHas(p, 'Choose a sign in method');
                  }) || null;

                  const signInBtn = Array.from(document.querySelectorAll('button')).find((b) =>
                    (b.textContent || '').trim().toLowerCase() === 'sign in'
                  ) || null;

                  const googleBtn = dialogRoot
                    ? dialogRoot.querySelector("button[aria-label='Continue with Google']")
                    : null;

                  const rectToObj = (el) => {
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
                  };

                  const isVisible = (el) => {
                    if (!el) return false;
                    const s = getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
                  };

                  return {
                    ts: Date.now(),
                    readyState: document.readyState,
                    activeTag: document.activeElement ? document.activeElement.tagName : null,
                    activeAria: document.activeElement ? document.activeElement.getAttribute('aria-label') : null,
                    hasDialogRoot: !!dialogRoot,
                    dialogVisible: isVisible(dialogRoot),
                    googleVisible: isVisible(googleBtn),
                    googleEnabled: !!googleBtn && !googleBtn.disabled,
                    signInVisible: isVisible(signInBtn),
                    signInRect: rectToObj(signInBtn),
                    googleRect: rectToObj(googleBtn),
                  };
                }
                """
            )
        except Exception as exc:
            return {"ts": int(time.time() * 1000), "state_error": str(exc)}

    async def _log_login_dialog_probe(self, page: Page, phase: str, samples: int = 10, interval_ms: int = 200) -> None:
        last: dict[str, Any] | None = None
        for idx in range(max(samples, 1)):
            state = await self._collect_login_dialog_state(page)
            if last is None or any(
                state.get(k) != last.get(k)
                for k in ("hasDialogRoot", "dialogVisible", "googleVisible", "googleEnabled", "signInVisible", "activeAria")
            ):
                print(
                    "[suno] login-probe "
                    f"phase={phase} sample={idx + 1}/{samples} "
                    f"dialog={state.get('dialogVisible')} google_visible={state.get('googleVisible')} "
                    f"google_enabled={state.get('googleEnabled')} signin_visible={state.get('signInVisible')} "
                    f"active={state.get('activeTag')} aria={state.get('activeAria')}"
                )
            last = state
            await page.wait_for_timeout(max(interval_ms, 20))

    async def _try_click_continue_with_google(self, page: Page, timeout_s: float = 8.0) -> bool:
        google_deadline = asyncio.get_running_loop().time() + max(timeout_s, 0.5)
        while asyncio.get_running_loop().time() < google_deadline:
            dialog_root = await self._get_signin_dialog_root(page)
            if dialog_root is not None:
                dialog_button = dialog_root.locator("button[aria-label='Continue with Google']").first
                if await self._safe_wait_visible(dialog_button, timeout_ms=300):
                    # Keep dialog stable so handlers finish mounting before click.
                    await page.wait_for_timeout(1000)
                    try:
                        if not await dialog_button.is_enabled():
                            await asyncio.sleep(0.08)
                            continue
                    except Exception:
                        await asyncio.sleep(0.08)
                        continue
                    try:
                        await dialog_button.scroll_into_view_if_needed(timeout=1000)
                    except Exception:
                        pass
                    if await self._safe_click_no_force(dialog_button, timeout_ms=1400):
                        print("[suno] clicked Continue with Google")
                        return True
            await asyncio.sleep(0.08)
        return False

    @staticmethod
    async def _wait_ready_state_complete(page: Page, timeout_ms: int = 12000) -> bool:
        try:
            await page.wait_for_function("() => document.readyState === 'complete'", timeout=timeout_ms)
            # Give reactive UI frameworks a short settle window after readyState switches to complete.
            await page.wait_for_timeout(250)
            return True
        except Exception:
            return False

    async def _try_open_google_login(
        self,
        page: Page,
        sign_in_timeout_s: float = 5.0,
        google_timeout_s: float = 12.0,
    ) -> tuple[bool, bool]:
        pre_state = await self._collect_login_dialog_state(page)
        if pre_state.get("dialogVisible") and pre_state.get("googleVisible"):
            print("[suno] auth dialog already visible; skip Sign In and click Continue with Google")
            google_clicked = await self._try_click_continue_with_google(page, timeout_s=google_timeout_s)
            return False, google_clicked

        sign_in_candidates = [
            page.get_by_role("button", name=re.compile(r"^\s*sign in\s*$", re.I)),
            page.locator("button:has(span:text-is('Sign In'))"),
            page.locator("button:has(span:has-text('Sign In'))"),
            page.locator("button:has-text('Sign In')"),
        ]

        # Fast polling avoids long sequential waits (6s x selector) on cold page loads.
        sign_in_clicked = False
        sign_in_deadline = asyncio.get_running_loop().time() + max(sign_in_timeout_s, 0.5)
        while asyncio.get_running_loop().time() < sign_in_deadline:
            pre = await self._collect_login_dialog_state(page)
            print(
                "[suno] signin-probe pre-click "
                f"dialog={pre.get('dialogVisible')} google_visible={pre.get('googleVisible')} "
                f"google_enabled={pre.get('googleEnabled')} signin_visible={pre.get('signInVisible')}"
            )
            for candidate in sign_in_candidates:
                # Avoid force click here to reduce risk of backdrop/off-target interactions.
                if await self._safe_click_no_force(candidate, timeout_ms=900):
                    print("[suno] clicked Sign In")
                    await self._log_login_dialog_probe(page, phase="post-signin", samples=12, interval_ms=180)
                    sign_in_clicked = True
                    break
            if sign_in_clicked:
                break
            await asyncio.sleep(0.08)
        if not sign_in_clicked:
            return False, False

        # The auth dialog often animates/mounts after Sign In click; settle long enough to avoid an immediate second Sign In click.
        await page.wait_for_timeout(900)
        google_clicked = await self._try_click_continue_with_google(page, timeout_s=google_timeout_s)
        return sign_in_clicked, google_clicked

    async def _wait_for_google_redirect(self, context: BrowserContext, page: Page, timeout_s: float = 70.0) -> bool:
        deadline = asyncio.get_running_loop().time() + max(timeout_s, 1.0)
        while asyncio.get_running_loop().time() < deadline:
            for p in context.pages:
                if "accounts.google." in (p.url or "").lower():
                    print(f"[suno] google redirect detected: {p.url}")
                    return True
            if "accounts.google." in (page.url or "").lower():
                print(f"[suno] google redirect detected on current page: {page.url}")
                return True
            await asyncio.sleep(0.08)
        return False

    async def _find_google_page(self, context: BrowserContext, fallback_page: Page) -> Page | None:
        for _ in range(300):
            for p in context.pages:
                if "accounts.google." in (p.url or "").lower():
                    return p
            await asyncio.sleep(0.08)
        if "accounts.google." in (fallback_page.url or "").lower():
            return fallback_page
        return None

    async def _fill_google_credentials(self, page: Page, email: str | None, password: str | None) -> None:
        if "accounts.google." not in (page.url or "").lower():
            return

        did_email_step = False
        if email:
            email_ok = (
                await self._safe_fill(page.locator("#identifierId"), email, timeout_ms=35_000)
                or await self._safe_fill(page.locator('input[name="identifier"]'), email, timeout_ms=35_000)
                or await self._safe_fill(page.locator('input[type="email"]'), email, timeout_ms=35_000)
            )
            if email_ok:
                clicked_next = (
                    await self._safe_click(page.locator("#identifierNext button"), timeout_ms=8000)
                    or await self._safe_click(page.locator("#identifierNext"), timeout_ms=8000)
                    or await self._safe_click(page.get_by_role("button", name=re.compile(r"weiter|next", re.I)), timeout_ms=8000)
                )
                if not clicked_next:
                    try:
                        await page.keyboard.press("Enter")
                    except Exception:
                        pass
                did_email_step = True

        if password and (did_email_step or await self._safe_wait_visible(page.locator('input[type="password"]'), timeout_ms=7000)):
            password_ok = (
                await self._safe_fill(page.locator('input[name="Passwd"]'), password, timeout_ms=60_000)
                or await self._safe_fill(page.locator('input[type="password"]'), password, timeout_ms=60_000)
            )
            if password_ok:
                clicked_next = (
                    await self._safe_click(page.locator("#passwordNext button"), timeout_ms=8000)
                    or await self._safe_click(page.locator("#passwordNext"), timeout_ms=8000)
                    or await self._safe_click(page.get_by_role("button", name=re.compile(r"weiter|next", re.I)), timeout_ms=8000)
                )
                if not clicked_next:
                    try:
                        await page.keyboard.press("Enter")
                    except Exception:
                        pass

    async def _check_logged_in(self, context: BrowserContext) -> tuple[bool, int, str]:
        auth_cookie_names = {"__session", "__session_jnxw-mut"}
        try:
            resp = await context.request.get("https://studio-api-prod.suno.com/api/session/")
            status = int(resp.status)
            text = await resp.text()
            sid_resp = await context.request.get("https://studio-api-prod.suno.com/api/user/get_user_session_id/")
            sid_status = int(sid_resp.status)
            sid_text = await sid_resp.text()
            cookies = await context.cookies([
                "https://suno.com",
                "https://studio-api-prod.suno.com",
                "https://auth.suno.com",
            ])
            auth_cookie = None
            for c in cookies:
                name = str(c.get("name", "")).lower()
                if name in auth_cookie_names:
                    val = str(c.get("value", "")).strip()
                    if val:
                        auth_cookie = val
                        break

            # Avoid false positives during Clerk handshake pages: require both endpoints + non-empty auth cookie.
            ok = status == 200 and sid_status == 200 and bool(auth_cookie)
            info = f"session_status={status} sid_status={sid_status} sid={sid_text[:120]}"
            return ok, status, info if ok else f"{info} body={text[:200]}"
        except Exception as exc:
            return False, 0, str(exc)

    async def _wait_until_logged_in(self, context: BrowserContext, wait_seconds: float) -> tuple[bool, int, str]:
        deadline = asyncio.get_running_loop().time() + max(wait_seconds, 1.0)
        last_status = 0
        last_info = ""
        while asyncio.get_running_loop().time() < deadline:
            # OAuth denial can happen during Google 2FA/consent steps (for example "No, it's not me").
            # If observed, fail fast with a specific marker for clearer CLI messaging.
            for p in context.pages:
                url = (p.url or "").lower()
                if "access_denied" in url or "error=access_denied" in url:
                    return False, 403, "auth_denied:access_denied"
                if "signin/rejected" in url:
                    return False, 403, "auth_denied:google_signin_rejected"
                if "accounts.google." in url:
                    try:
                        body_text = (await p.inner_text("body")).lower()
                    except Exception:
                        body_text = ""
                    if (
                        "no, it's not me" in body_text
                        or "nein, das bin nicht ich" in body_text
                        or "you denied" in body_text
                        or "access denied" in body_text
                        or "anmeldung gestoppt" in body_text
                        or "anmeldung erneut versuchen" in body_text
                        or "sie haben uns mitgeteilt" in body_text
                        or "wiederholen" in body_text
                    ):
                        return False, 403, "auth_denied:user_rejected_google_2fa"

            ok, status, info = await self._check_logged_in(context)
            last_status, last_info = status, info
            if ok:
                return True, status, info
            await asyncio.sleep(0.8)
        return False, last_status, last_info

    async def _harvest_p1_token_via_cdp(self, cdp_url: str, payload: dict[str, Any]) -> str:
        if not cdp_url:
            raise RuntimeError("CDP URL missing for P1 token fallback.")

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(cdp_url)
            contexts = list(browser.contexts)
            context = contexts[0] if contexts else await browser.new_context()

            page: Page | None = None
            for pg in context.pages:
                if "suno.com/create" in (pg.url or ""):
                    page = pg
                    break
            if page is None:
                page = await context.new_page()
                await page.goto("https://suno.com/create", wait_until="domcontentloaded", timeout=60_000)

            await page.bring_to_front()

            token_box: dict[str, str | None] = {"token": None}

            async def _capture_and_abort(route: Any) -> None:
                req = route.request
                try:
                    body_text = req.post_data or ""
                    body = json.loads(body_text) if body_text else {}
                    token = body.get("token") if isinstance(body, dict) else None
                    if isinstance(token, str) and token.startswith("P1_"):
                        token_box["token"] = token
                except Exception:
                    pass
                await route.abort()

            await context.route("**/api/generate/v2-web/", _capture_and_abort)
            try:
                for txt in ("Reject All", "Accept All Cookies", "Accept All"):
                    btn = page.get_by_role("button", name=txt)
                    if await btn.count() > 0:
                        if await self._safe_click(btn, timeout_ms=1200):
                            break

                adv = page.get_by_role("button", name=re.compile(r"advanced", re.I))
                if await adv.count() > 0:
                    await self._safe_click(adv, timeout_ms=5000)

                lyrics = str(payload.get("lyrics", "")) or "Kurztest"
                styles = str(payload.get("styles", "")) or "hip-hop"
                exclude = str(payload.get("exclude_styles", ""))
                title = str(payload.get("song_title", "")) or "API Fallback"

                lyr = page.locator("textarea[placeholder*='Write some lyrics']")
                if await lyr.count() > 0:
                    await lyr.first.fill(lyrics)
                else:
                    cedit = page.locator("div[contenteditable='true']").first
                    if await cedit.count() > 0:
                        await cedit.click()
                        await page.keyboard.press("Control+A")
                        await page.keyboard.type(lyrics)

                st = page.locator("textarea[placeholder*='Styles'], input[placeholder*='Styles']")
                if await st.count() > 0:
                    try:
                        if await st.first.is_visible():
                            await st.first.fill(styles)
                    except Exception:
                        pass

                more = page.locator("text=More Options")
                if await more.count() > 0:
                    await self._safe_click(more.first, timeout_ms=3000)

                ex = page.locator("input[placeholder*='Exclude styles']")
                if await ex.count() > 0 and exclude:
                    try:
                        if await ex.first.is_visible():
                            await ex.first.fill(exclude)
                    except Exception:
                        pass

                title_in = page.locator("input[placeholder*='Song Title']")
                if await title_in.count() > 0:
                    try:
                        if await title_in.first.is_visible():
                            await title_in.first.fill(title)
                    except Exception:
                        pass

                create_btn = page.get_by_role("button", name=re.compile(r"^\s*create\s*$", re.I))
                clicked = await self._safe_click(create_btn, timeout_ms=8000)
                if not clicked:
                    alt = page.locator("button:has-text('Create')")
                    clicked = await self._safe_click(alt, timeout_ms=8000)
                if not clicked:
                    # Headless UIs can fail role-based clicks; try direct forced click on visible Create button.
                    try:
                        forced = page.locator("button:visible:has-text('Create')").first
                        if await forced.count() > 0:
                            await forced.scroll_into_view_if_needed(timeout=3000)
                            await forced.click(timeout=3000, force=True)
                            clicked = True
                    except Exception:
                        pass
                if not clicked:
                    # Keyboard fallback: in Create view Ctrl+Enter usually triggers generation.
                    try:
                        await page.keyboard.press("Control+Enter")
                        clicked = True
                    except Exception:
                        pass

                deadline = asyncio.get_running_loop().time() + 20.0
                while asyncio.get_running_loop().time() < deadline:
                    token = token_box.get("token")
                    if isinstance(token, str) and token.startswith("P1_"):
                        print("[suno] harvested fresh P1 token via CDP")
                        return token
                    await asyncio.sleep(0.1)

                if not clicked:
                    raise RuntimeError("Could not trigger Create while harvesting P1 token.")
            finally:
                try:
                    await context.unroute("**/api/generate/v2-web/", _capture_and_abort)
                except Exception:
                    pass

        raise RuntimeError("Failed to harvest fresh P1 token via CDP.")

    @staticmethod
    async def _shutdown_remote_browser(page: Page, browser: Any) -> bool:
        # With CDP attach, browser.close() may only detach. Browser.close over CDP exits Chrome.
        closed = False
        try:
            session = await page.context.new_cdp_session(page)
            await session.send("Browser.close")
            closed = True
        except Exception:
            closed = False
        try:
            await browser.close()
        except Exception:
            pass
        return closed

    async def login_via_cdp(
        self,
        cdp_url: str,
        wait_seconds: float = 600,
        target_url: str = "https://suno.com/create",
        close_browser_after_login: bool = False,
    ) -> dict[str, Any]:
        dotenv_values = self._load_dotenv_values(self._plugin_dotenv_path())
        email = (os.environ.get("SUNO_GOOGLE_EMAIL", "").strip() or dotenv_values.get("SUNO_GOOGLE_EMAIL", "").strip() or None)
        password = (os.environ.get("SUNO_GOOGLE_PASSWORD", "").strip() or dotenv_values.get("SUNO_GOOGLE_PASSWORD", "").strip() or None)

        async with async_playwright() as p:
            print(f"[suno] attaching via CDP: {cdp_url}")
            browser = await p.chromium.connect_over_cdp(cdp_url)
            contexts = list(browser.contexts)
            context = contexts[0] if contexts else await browser.new_context()

            token_box: dict[str, str | None] = {"bearer": None}
            replay_header_box: dict[str, str] = {}

            def on_request(req: Any) -> None:
                try:
                    if "studio-api-prod.suno.com/api/" not in str(req.url):
                        return
                    headers = req.headers or {}
                    auth = headers.get("authorization") or headers.get("Authorization")
                    if isinstance(auth, str) and auth.lower().startswith("bearer "):
                        token_box["bearer"] = auth.split(" ", 1)[1].strip()
                        replay_header_box["authorization"] = auth.strip()
                        for key in ("browser-token", "device-id", "user-agent", "referer"):
                            val = headers.get(key) or headers.get(key.title())
                            if isinstance(val, str) and val.strip():
                                replay_header_box[key] = val.strip()
                except Exception:
                    return

            context.on("request", on_request)

            # Prefer the already-open first tab (CDP start script opens target URL there).
            # Only create a new tab if the context currently has no pages.
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.bring_to_front()
            except Exception:
                pass
            current_url = (page.url or "").lower()
            target_norm = target_url.lower()
            nav_task: Any | None = None

            # Clerk handshake pages can transiently open/close auth UI while cookies are settling.
            # Normalize to stable target URL before starting click flow.
            if "__clerk_handshake=" in current_url:
                print("[suno] detected clerk handshake URL; normalizing to stable target page")
                try:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=45_000)
                    current_url = (page.url or "").lower()
                    print(f"[suno] normalized url={page.url}")
                except Exception as exc:
                    print(f"[suno] warning: handshake normalization failed: {exc}")

            if not current_url.startswith(target_norm):
                print(f"[suno] goto={target_url}")
                # Do not block on ready state: start navigation and immediately poll for Sign In visibility.
                nav_task = asyncio.create_task(page.goto(target_url, wait_until="commit", timeout=12_000))
            else:
                print(f"[suno] reuse current page={page.url}")

            print("[suno] waiting for page readyState=complete before Sign In")
            ready_ok = await self._wait_ready_state_complete(page, timeout_ms=12_000)
            if not ready_ok and nav_task is not None:
                try:
                    await asyncio.wait_for(nav_task, timeout=8.0)
                except Exception:
                    pass
                ready_ok = await self._wait_ready_state_complete(page, timeout_ms=6000)
            if not ready_ok:
                print("[suno] warning: readyState complete timeout; continue with Sign In probing")

            # Turbo path: do not wait for full page state/session checks first.
            # If Sign In appears, click it immediately to open Google flow.
            sign_in_clicked, google_clicked = await self._try_open_google_login(
                page,
                sign_in_timeout_s=1.5,
                google_timeout_s=12.0,
            )
            fast_login_started = google_clicked

            # If navigation is still in progress and we did not hit Sign In yet, give it one short settle window.
            if not fast_login_started and nav_task is not None:
                try:
                    await asyncio.wait_for(nav_task, timeout=0.8)
                except Exception:
                    pass
                if sign_in_clicked:
                    await page.wait_for_timeout(900)
                    fast_login_started = await self._try_click_continue_with_google(page, timeout_s=12.0)
                    if not fast_login_started:
                        print("[suno] continue dialog still loading; waiting longer without second Sign In click")
                        fast_login_started = await self._try_click_continue_with_google(page, timeout_s=12.0)
                else:
                    sign_in_clicked, google_clicked = await self._try_open_google_login(
                        page,
                        sign_in_timeout_s=1.2,
                        google_timeout_s=10.0,
                    )
                    fast_login_started = google_clicked

            if fast_login_started:
                ok, status, info = False, 0, ""
            else:
                ok, status, info = await self._check_logged_in(context)

            if not ok:
                if not fast_login_started:
                    if sign_in_clicked:
                        await page.wait_for_timeout(900)
                        fast_login_started = await self._try_click_continue_with_google(page, timeout_s=14.0)
                        if not fast_login_started:
                            print("[suno] continue dialog disappeared; waiting for it to reappear before failing")
                            fast_login_started = await self._try_click_continue_with_google(page, timeout_s=10.0)
                    else:
                        _, fast_login_started = await self._try_open_google_login(page)

                if not fast_login_started:
                    # Before failing, re-check session once: Sign In dialogs can close while the session is already valid.
                    ok2, status2, info2 = await self._check_logged_in(context)
                    if ok2 or status2 == 200:
                        ok, status, info = True, status2, info2
                    else:
                        return {"ok": False, "error": "login_buttons_not_clicked", "status": status2, "info": info2}

                if not ok:
                    redirect_ok = await self._wait_for_google_redirect(context, page, timeout_s=70)
                    if not redirect_ok:
                        return {"ok": False, "error": "google_redirect_not_detected", "status": status, "info": info}

                    google_page = await self._find_google_page(context, page)
                    if google_page is None:
                        return {"ok": False, "error": "google_page_not_found", "status": status, "info": info}

                    await self._fill_google_credentials(google_page, email, password)
                    print(f"[suno] waiting for login session ({int(wait_seconds)}s)")
                    ok, status, info = await self._wait_until_logged_in(context, wait_seconds)
                    if not ok and isinstance(info, str) and info.startswith("auth_denied:"):
                        return {"ok": False, "error": "authorization_denied", "status": status, "info": info}

            # Trigger authenticated requests once.
            try:
                await page.goto("https://suno.com/create", wait_until="domcontentloaded", timeout=90_000)
                await page.wait_for_timeout(4000)
            except Exception:
                pass

            self.paths.storage_state.parent.mkdir(parents=True, exist_ok=True)
            self.paths.auth_bundle.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(self.paths.storage_state))
            write_auth_bundle(self.paths.storage_state, self.paths.auth_bundle)

            if token_box.get("bearer"):
                self.paths.bearer_token.write_text(str(token_box["bearer"]), encoding="utf-8")
                print(f"[suno] bearer token saved: {self.paths.bearer_token}")

            if replay_header_box:
                self.paths.api_headers.write_text(
                    json.dumps(replay_header_box, indent=2, ensure_ascii=True),
                    encoding="utf-8",
                )
                print(f"[suno] api headers saved: {self.paths.api_headers}")

            if close_browser_after_login:
                try:
                    did_shutdown = await self._shutdown_remote_browser(page, browser)
                    if did_shutdown:
                        print("[suno] browser closed after login")
                    else:
                        print("[suno] browser disconnected after login (remote close not confirmed)")
                except Exception as exc:
                    print(f"[suno] warning: failed to close browser automatically: {exc}")

            return {"ok": ok, "status": status, "info": info}

    @staticmethod
    def _cookie_session_from_storage(storage_state: Path) -> requests.Session:
        raw = json.loads(storage_state.read_text(encoding="utf-8"))
        s = requests.Session()
        for c in raw.get("cookies", []) or []:
            name = c.get("name")
            value = c.get("value")
            domain = c.get("domain")
            path = c.get("path") or "/"
            if name and value:
                s.cookies.set(str(name), str(value), domain=domain, path=path)
        return s

    def _load_capture_template(self) -> tuple[dict[str, str], dict[str, Any]]:
        if not self.paths.manual_capture.exists():
            raise FileNotFoundError(f"Manual capture missing: {self.paths.manual_capture}")
        raw = json.loads(self.paths.manual_capture.read_text(encoding="utf-8"))
        request_obj = raw.get("request", {})
        headers = {
            str(k).lower(): str(v)
            for k, v in (request_obj.get("headers", {}) or {}).items()
            if isinstance(k, str) and isinstance(v, str)
        }
        body_text = request_obj.get("post_data")
        if not isinstance(body_text, str) or not body_text.strip():
            raise ValueError("manual capture has no request.post_data")
        body = json.loads(body_text)
        if not isinstance(body, dict):
            raise ValueError("manual capture post_data is not a JSON object")
        return headers, body

    def _load_runtime_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.paths.api_headers.exists():
            try:
                raw = json.loads(self.paths.api_headers.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if isinstance(k, str) and isinstance(v, str):
                            headers[k.lower()] = v
            except Exception:
                pass
        if self.paths.bearer_token.exists():
            token = self.paths.bearer_token.read_text(encoding="utf-8").strip()
            if token:
                headers["authorization"] = f"Bearer {token}"
        return headers

    def _pick_working_token(self, session: requests.Session, base_headers: dict[str, str]) -> str:
        candidates: list[str] = []
        if self.paths.bearer_token.exists():
            tok = self.paths.bearer_token.read_text(encoding="utf-8").strip()
            if tok:
                candidates.append(tok)
        if self.paths.auth_bundle.exists():
            raw = json.loads(self.paths.auth_bundle.read_text(encoding="utf-8"))
            for item in raw.get("token_candidates", []) or []:
                if isinstance(item, dict):
                    val = item.get("value")
                    if isinstance(val, str) and val and val not in candidates:
                        candidates.append(val)

        for token in candidates:
            h = dict(base_headers)
            h["authorization"] = f"Bearer {token}"
            resp = session.get("https://studio-api-prod.suno.com/api/user/get_user_session_id/", headers=h, timeout=30)
            if resp.status_code == 200:
                return token
        raise RuntimeError("No working bearer token found.")

    @staticmethod
    def _random_payload(seed_idx: int) -> dict[str, Any]:
        title_left = ["Neon", "Shadow", "Turbo", "Polar", "Urban", "Echo", "Velvet", "Chrome"]
        title_right = ["Pulse", "District", "Mirage", "Signal", "Ritual", "Orbit", "Heat", "Groove"]
        style_pool = ["hip-hop", "trap", "boom bap", "cloud rap", "drill", "dark pop", "electro", "synthwave", "afrobeats"]
        exclude_pool = ["metal", "country", "jazz", "classical", "ambient", "edm"]
        lyric_lines = [
            "Streetlight flackert, aber ich bleib auf Kurs",
            "Bassline treibt mich durch die Nacht",
            "Kein Zurueck, nur Fokus, nur Schub nach vorn",
            "Jeder Takt ein Schritt, jeder Hook ein Schwur",
            "Wir bauen aus Staub eine Skyline aus Sound",
        ]
        selected_styles = random.sample(style_pool, k=2)
        return {
            "song_title": f"{random.choice(title_left)} {random.choice(title_right)}",
            "lyrics": "\n".join(random.sample(lyric_lines, k=3)),
            "styles": ", ".join(selected_styles),
            "exclude_styles": random.choice(exclude_pool),
            "gender": random.choice(["m", "f"]),
            "weirdness": round(random.uniform(0.08, 0.92), 2),
            "style_influence": round(random.uniform(0.08, 0.92), 2),
            "seed": seed_idx,
        }

    @staticmethod
    def _extract_clip_ids(generate_data: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for clip in generate_data.get("clips", []) or []:
            if isinstance(clip, dict):
                cid = clip.get("id")
                if isinstance(cid, str) and cid:
                    out.append(cid)
        return out

    @staticmethod
    def _fresh_browser_token_header() -> str:
        payload = {"timestamp": int(time.time() * 1000)}
        encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
        return json.dumps({"token": encoded}, separators=(",", ":"))

    @staticmethod
    def _download_file(url: str, out_path: Path) -> None:
        with requests.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with out_path.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)

    def _resolve_project_id(self, session: requests.Session, headers: dict[str, str], selector: str) -> str | None:
        raw = str(selector or "").strip()
        if not raw:
            return None

        # Accept direct UUID-like ids without forcing a lookup.
        if re.fullmatch(r"[0-9a-fA-F-]{24,64}", raw):
            return raw

        target = raw.casefold()

        def _fetch_json(url: str) -> Any:
            try:
                resp = session.get(url, headers=headers, timeout=30)
                if resp.status_code >= 400:
                    return None
                return resp.json()
            except Exception:
                return None

        def _iter_projects(node: Any, depth: int = 0):
            if depth > 4 or node is None:
                return
            if isinstance(node, dict):
                if "id" in node and any(k in node for k in ("name", "title", "slug", "is_default", "isDefault")):
                    yield node
                for value in node.values():
                    yield from _iter_projects(value, depth + 1)
            elif isinstance(node, list):
                for item in node[:200]:
                    yield from _iter_projects(item, depth + 1)

        default_data = _fetch_json("https://studio-api-prod.suno.com/api/project/default")
        projects_data = _fetch_json(
            "https://studio-api-prod.suno.com/api/project/me?page=1&sort=max_created_at_last_updated_clip&show_trashed=false&exclude_shared=false"
        )

        candidates: list[dict[str, Any]] = []
        for obj in (default_data, projects_data):
            for proj in _iter_projects(obj):
                if isinstance(proj, dict):
                    candidates.append(proj)

        for proj in candidates:
            proj_id = str(proj.get("id", "")).strip()
            if not proj_id:
                continue
            name = str(proj.get("name", "")).strip()
            title = str(proj.get("title", "")).strip()
            slug = str(proj.get("slug", "")).strip()
            is_default = bool(proj.get("is_default") or proj.get("isDefault"))

            if target in {proj_id.casefold(), name.casefold(), title.casefold(), slug.casefold()}:
                return proj_id
            if target in {"default", "my workspace", "myworkspace"} and is_default:
                return proj_id

        return None

    def _create_project_by_name(self, session: requests.Session, headers: dict[str, str], name: str) -> str | None:
        project_name = str(name or "").strip()
        if not project_name:
            return None

        # Known aliases should not trigger creation.
        alias = project_name.casefold()
        if alias in {"default", "my workspace", "myworkspace"}:
            return None

        # If caller passed a direct id, creation is never appropriate.
        if re.fullmatch(r"[0-9a-fA-F-]{24,64}", project_name):
            return None

        create_urls = [
            "https://studio-api-prod.suno.com/api/project",
            "https://studio-api-prod.suno.com/api/project/",
            "https://studio-api-prod.suno.com/api/project/create",
        ]
        payloads = [
            {"name": project_name, "description": ""},
            {"name": project_name},
            {"title": project_name},
            {"project_name": project_name},
            {"workspace_name": project_name},
        ]

        def _extract_id(node: Any, depth: int = 0) -> str | None:
            if depth > 4 or node is None:
                return None
            if isinstance(node, dict):
                pid = node.get("id")
                if isinstance(pid, str) and pid.strip():
                    return pid.strip()
                for value in node.values():
                    found = _extract_id(value, depth + 1)
                    if found:
                        return found
            elif isinstance(node, list):
                for item in node[:100]:
                    found = _extract_id(item, depth + 1)
                    if found:
                        return found
            return None

        for url in create_urls:
            for payload in payloads:
                try:
                    resp = session.post(url, headers=headers, json=payload, timeout=30)
                except Exception:
                    continue

                # 409/conflict can mean the workspace already exists.
                if resp.status_code == 409:
                    resolved = self._resolve_project_id(session, headers, project_name)
                    if resolved:
                        return resolved
                    continue

                if resp.status_code >= 400:
                    continue

                try:
                    data = resp.json()
                except Exception:
                    data = None

                created_id = _extract_id(data)
                if created_id:
                    return created_id

                # Fallback: server accepted request but returned unexpected shape.
                resolved = self._resolve_project_id(session, headers, project_name)
                if resolved:
                    return resolved

        return None

    def _attach_clips_to_project(
        self,
        session: requests.Session,
        headers: dict[str, str],
        project_id: str,
        clip_ids: list[str],
    ) -> bool:
        pid = str(project_id or "").strip()
        valid_clip_ids = [cid for cid in clip_ids if isinstance(cid, str) and cid.strip()]
        if not pid or not valid_clip_ids:
            return False

        try:
            resp = session.post(
                f"https://studio-api-prod.suno.com/api/project/{pid}/clips",
                headers=headers,
                json={"update_type": "add", "metadata": {"clip_ids": valid_clip_ids}},
                timeout=60,
            )
            return resp.status_code < 400
        except Exception:
            return False

    def authenticated_session_headers(self) -> tuple[requests.Session, dict[str, str]]:
        session = self._cookie_session_from_storage(self.paths.storage_state)
        captured_headers, _ = self._load_capture_template()
        runtime_headers = self._load_runtime_headers()
        headers = dict(captured_headers)
        headers.update(runtime_headers)
        headers.setdefault("origin", "https://suno.com")
        headers.setdefault("content-type", "application/json")

        keep = {
            "accept",
            "accept-language",
            "authorization",
            "browser-token",
            "content-type",
            "device-id",
            "origin",
            "referer",
            "sec-ch-ua",
            "sec-ch-ua-mobile",
            "sec-ch-ua-platform",
            "user-agent",
        }
        headers = {k.lower(): v for k, v in headers.items() if k.lower() in keep and isinstance(v, str) and v}
        token = self._pick_working_token(session, headers)
        headers["authorization"] = f"Bearer {token}"
        return session, headers

    def set_clip_metadata(self, clip_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        cid = str(clip_id or "").strip()
        if not cid:
            raise ValueError("clip_id is required")
        if not isinstance(updates, dict) or not updates:
            raise ValueError("updates must be a non-empty object")
        session, headers = self.authenticated_session_headers()
        resp = session.post(
            f"https://studio-api-prod.suno.com/api/gen/{cid}/set_metadata/",
            headers=headers,
            json=updates,
            timeout=60,
        )
        return {
            "ok": resp.status_code < 400,
            "status": resp.status_code,
            "clip_id": cid,
            "response_text": (resp.text or "")[:1000],
        }

    def move_to_trash(self, clip_ids: list[str]) -> dict[str, Any]:
        ids = [x.strip() for x in clip_ids if isinstance(x, str) and x.strip()]
        if not ids:
            raise ValueError("clip_ids is required")
        session, headers = self.authenticated_session_headers()
        payloads = [
            {"clip_ids": ids},
            {"ids": ids},
            {"tokens": ids},
        ]
        last_status = 0
        last_text = ""
        for payload in payloads:
            resp = session.post("https://studio-api-prod.suno.com/api/gen/trash", headers=headers, json=payload, timeout=60)
            last_status = resp.status_code
            last_text = (resp.text or "")[:1000]
            if resp.status_code < 400:
                return {"ok": True, "status": resp.status_code, "clip_ids": ids}
        return {"ok": False, "status": last_status, "clip_ids": ids, "response_text": last_text}

    def move_to_workspace(self, clip_ids: list[str], workspace_selector: str) -> dict[str, Any]:
        ids = [x.strip() for x in clip_ids if isinstance(x, str) and x.strip()]
        if not ids:
            raise ValueError("clip_ids is required")
        selector = str(workspace_selector or "").strip()
        if not selector:
            raise ValueError("workspace_selector is required")

        session, headers = self.authenticated_session_headers()
        project_id = self._resolve_project_id(session, headers, selector)
        if not project_id:
            project_id = self._create_project_by_name(session, headers, selector)
        if not project_id:
            return {"ok": False, "status": 404, "error": "workspace_not_found", "selector": selector}
        attached = self._attach_clips_to_project(session, headers, project_id, ids)
        return {
            "ok": bool(attached),
            "project_id": project_id,
            "clip_ids": ids,
        }

    def create_playlist(self, name: str) -> dict[str, Any]:
        title = str(name or "").strip()
        if not title:
            raise ValueError("playlist name is required")
        session, headers = self.authenticated_session_headers()
        payloads = [
            {"name": title},
            {"title": title},
            {"playlist_name": title},
        ]
        last_status = 0
        last_text = ""
        for payload in payloads:
            resp = session.post("https://studio-api-prod.suno.com/api/playlist/create/", headers=headers, json=payload, timeout=60)
            last_status = resp.status_code
            last_text = (resp.text or "")[:1000]
            if resp.status_code < 400:
                pid = ""
                with contextlib.suppress(Exception):
                    data = resp.json()
                    if isinstance(data, dict):
                        for k in ("id", "token", "playlist_id"):
                            val = data.get(k)
                            if isinstance(val, str) and val.strip():
                                pid = val.strip()
                                break
                return {"ok": True, "status": resp.status_code, "playlist_name": title, "playlist_id": pid}
        return {"ok": False, "status": last_status, "playlist_name": title, "response_text": last_text}

    @staticmethod
    def _extract_clips_from_songs_payload(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, dict):
            for key in ("songs", "clips", "results", "data"):
                val = data.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return []

    @staticmethod
    def _iter_dict_nodes(node: Any, depth: int = 0) -> Any:
        if depth > 8 or node is None:
            return
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from SunoWorkflow._iter_dict_nodes(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:400]:
                yield from SunoWorkflow._iter_dict_nodes(item, depth + 1)

    def list_workspaces(
        self,
        page: int = 1,
        include_trashed: bool = False,
        exclude_shared: bool = False,
    ) -> dict[str, Any]:
        session, headers = self.authenticated_session_headers()

        responses: list[tuple[int, Any]] = []
        status = 0

        try:
            resp_default = session.get("https://studio-api-prod.suno.com/api/project/default", headers=headers, timeout=30)
            status = max(status, resp_default.status_code)
            data_default: Any = None
            with contextlib.suppress(Exception):
                data_default = resp_default.json()
            responses.append((resp_default.status_code, data_default))
        except Exception:
            responses.append((0, None))

        try:
            resp_me = session.get(
                "https://studio-api-prod.suno.com/api/project/me",
                headers=headers,
                params={
                    "page": max(1, int(page)),
                    "sort": "max_created_at_last_updated_clip",
                    "show_trashed": "true" if include_trashed else "false",
                    "exclude_shared": "true" if exclude_shared else "false",
                },
                timeout=30,
            )
            status = max(status, resp_me.status_code)
            data_me: Any = None
            with contextlib.suppress(Exception):
                data_me = resp_me.json()
            responses.append((resp_me.status_code, data_me))
        except Exception:
            responses.append((0, None))

        workspaces: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _, payload in responses:
            for node in self._iter_dict_nodes(payload):
                pid = node.get("id")
                if not isinstance(pid, str) or not pid.strip():
                    continue
                if not any(k in node for k in ("name", "title", "slug", "is_default", "isDefault")):
                    continue
                pid = pid.strip()
                if pid in seen:
                    continue
                seen.add(pid)
                workspaces.append(
                    {
                        "id": pid,
                        "name": str(node.get("name") or "").strip(),
                        "title": str(node.get("title") or "").strip(),
                        "slug": str(node.get("slug") or "").strip(),
                        "is_default": bool(node.get("is_default") or node.get("isDefault")),
                    }
                )

        ok = any(code and code < 400 for code, _ in responses)
        return {
            "ok": ok,
            "status": status,
            "page": max(1, int(page)),
            "workspaces": workspaces,
            "count": len(workspaces),
        }

    def list_songs(
        self,
        page: int = 1,
        include_trashed: bool = False,
        exclude_shared: bool = False,
        workspace_selector: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        session, headers = self.authenticated_session_headers()

        page_num = max(1, int(page))
        max_items = max(1, min(int(limit), 500))
        selector = str(workspace_selector or "").strip()
        resolved_project_id = ""
        if selector:
            resolved = self._resolve_project_id(session, headers, selector)
            if isinstance(resolved, str) and resolved.strip():
                resolved_project_id = resolved.strip()

        payload: dict[str, Any] = {
            "page": page_num,
            "limit": max_items,
            "show_trashed": bool(include_trashed),
            "exclude_shared": bool(exclude_shared),
            "filters": {},
        }
        if resolved_project_id:
            payload["filters"] = {
                "project": {"presence": "True", "projectIds": [resolved_project_id]},
            }

        resp = session.post(
            "https://studio-api-prod.suno.com/api/feed/v3",
            headers=headers,
            json=payload,
            timeout=90,
        )

        data: Any = None
        with contextlib.suppress(Exception):
            data = resp.json()

        songs: list[dict[str, Any]] = []
        if isinstance(data, dict):
            clips = data.get("clips")
            if isinstance(clips, list):
                for clip in clips[:max_items]:
                    if not isinstance(clip, dict):
                        continue
                    cid = str(clip.get("id") or "").strip()
                    if not cid:
                        continue
                    songs.append(
                        {
                            "id": cid,
                            "title": str(clip.get("title") or "").strip(),
                            "created_at": clip.get("created_at"),
                            "audio_url": clip.get("audio_url") or clip.get("stream_url") or clip.get("url"),
                            "video_url": clip.get("video_url") or clip.get("video_download_url"),
                            "project_id": clip.get("project_id") or clip.get("projectId"),
                        }
                    )

        return {
            "ok": resp.status_code < 400,
            "status": resp.status_code,
            "page": page_num,
            "workspace_selector": selector,
            "resolved_project_id": resolved_project_id,
            "songs": songs,
            "count": len(songs),
            "limit": max_items,
        }

    def get_songs_by_ids(self, clip_ids: list[str]) -> dict[str, Any]:
        ids = [x.strip() for x in clip_ids if isinstance(x, str) and x.strip()]
        if not ids:
            raise ValueError("clip_ids is required")
        session, headers = self.authenticated_session_headers()
        resp = session.get(
            "https://studio-api-prod.suno.com/api/clips/get_songs_by_ids",
            headers=headers,
            params={"ids": ",".join(ids)},
            timeout=60,
        )
        data: Any = None
        with contextlib.suppress(Exception):
            data = resp.json()
        clips = self._extract_clips_from_songs_payload(data)
        return {"ok": resp.status_code < 400, "status": resp.status_code, "clips": clips, "raw": data}

    def get_feed_clips_by_ids(self, clip_ids: list[str]) -> dict[str, Any]:
        ids = [x.strip() for x in clip_ids if isinstance(x, str) and x.strip()]
        if not ids:
            raise ValueError("clip_ids is required")
        session, headers = self.authenticated_session_headers()
        payload = {
            "filters": {"ids": {"presence": "True", "clipIds": ids}},
            "limit": max(len(ids), 1),
        }
        resp = session.post(
            "https://studio-api-prod.suno.com/api/feed/v3",
            headers=headers,
            json=payload,
            timeout=90,
        )
        data: Any = None
        with contextlib.suppress(Exception):
            data = resp.json()
        clips: list[dict[str, Any]] = []
        if isinstance(data, dict):
            val = data.get("clips")
            if isinstance(val, list):
                clips = [x for x in val if isinstance(x, dict)]
        return {"ok": resp.status_code < 400, "status": resp.status_code, "clips": clips, "raw": data}

    def adjust_speed(self, clip_id: str, speed: float, keep_pitch: bool | None = None) -> dict[str, Any]:
        cid = str(clip_id or "").strip()
        if not cid:
            raise ValueError("clip_id is required")
        s = float(speed)
        session, headers = self.authenticated_session_headers()
        payloads = [
            {"clip_id": cid, "speed": s, "keep_pitch": bool(keep_pitch) if keep_pitch is not None else True},
            {"id": cid, "speed": s, "keep_pitch": bool(keep_pitch) if keep_pitch is not None else True},
            {"token": cid, "speed": s, "keep_pitch": bool(keep_pitch) if keep_pitch is not None else True},
        ]
        last_status = 0
        last_text = ""
        for payload in payloads:
            resp = session.post("https://studio-api-prod.suno.com/api/clips/adjust-speed/", headers=headers, json=payload, timeout=60)
            last_status = resp.status_code
            last_text = (resp.text or "")[:1000]
            if resp.status_code < 400:
                return {"ok": True, "status": resp.status_code, "clip_id": cid, "speed": s}
        return {"ok": False, "status": last_status, "clip_id": cid, "response_text": last_text}

    def crop_clip(self, clip_id: str, start_s: float, end_s: float) -> dict[str, Any]:
        cid = str(clip_id or "").strip()
        if not cid:
            raise ValueError("clip_id is required")
        session, headers = self.authenticated_session_headers()
        payloads = [
            {"start_s": float(start_s), "end_s": float(end_s)},
            {"start": float(start_s), "end": float(end_s)},
            {"from_s": float(start_s), "to_s": float(end_s)},
        ]
        last_status = 0
        last_text = ""
        for payload in payloads:
            resp = session.post(f"https://studio-api-prod.suno.com/api/edit/crop/{cid}/", headers=headers, json=payload, timeout=60)
            last_status = resp.status_code
            last_text = (resp.text or "")[:1000]
            if resp.status_code < 400:
                return {"ok": True, "status": resp.status_code, "clip_id": cid}
        return {"ok": False, "status": last_status, "clip_id": cid, "response_text": last_text}

    def create_voice(
        self,
        clip_id: str,
        name: str,
        styles: str = "",
        description: str = "",
        is_public: bool = False,
        start_s: float | None = None,
        end_s: float | None = None,
    ) -> dict[str, Any]:
        cid = str(clip_id or "").strip()
        voice_name = str(name or "").strip()
        if not cid:
            raise ValueError("clip_id is required")
        if not voice_name:
            raise ValueError("name is required")
        session, headers = self.authenticated_session_headers()
        payload = {
            "clip_id": cid,
            "name": voice_name,
            "styles": str(styles or ""),
            "description": str(description or ""),
            "public": bool(is_public),
            "is_public": bool(is_public),
        }
        if start_s is not None:
            payload["start_s"] = float(start_s)
        if end_s is not None:
            payload["end_s"] = float(end_s)

        resp = session.post("https://studio-api-prod.suno.com/api/persona/create/", headers=headers, json=payload, timeout=60)
        data: Any = None
        with contextlib.suppress(Exception):
            data = resp.json()
        return {
            "ok": resp.status_code < 400,
            "status": resp.status_code,
            "clip_id": cid,
            "name": voice_name,
            "response": data if data is not None else (resp.text or "")[:1000],
        }

    def get_stems(self, clip_id: str, mode: str = "all_detected") -> dict[str, Any]:
        cid = str(clip_id or "").strip()
        if not cid:
            raise ValueError("clip_id is required")
        m = str(mode or "all_detected").strip().lower()
        session, headers = self.authenticated_session_headers()
        params = {"mode": m}
        resp = session.get(f"https://studio-api-prod.suno.com/api/clip/{cid}/stems/pages", headers=headers, params=params, timeout=90)
        data: Any = None
        with contextlib.suppress(Exception):
            data = resp.json()
        return {
            "ok": resp.status_code < 400,
            "status": resp.status_code,
            "clip_id": cid,
            "mode": m,
            "response": data if data is not None else (resp.text or "")[:1000],
        }

    def _resolve_asset_url(self, clip: dict[str, Any], kind: str) -> str:
        k = kind.lower().strip()
        if k == "mp3":
            for key in ("audio_url", "stream_url", "url"):
                val = clip.get(key)
                if isinstance(val, str) and val.startswith("http"):
                    return val
            return ""
        if k == "wav":
            for key in ("wav_url", "lossless_audio_url", "audio_wav_url"):
                val = clip.get(key)
                if isinstance(val, str) and val.startswith("http"):
                    return val
            return ""
        if k == "video":
            for key in ("video_url", "video_download_url"):
                val = clip.get(key)
                if isinstance(val, str) and val.startswith("http"):
                    return val
            return ""
        return ""

    def download_clip_asset(self, clip_id: str, kind: str, out_dir: Path) -> dict[str, Any]:
        cid = str(clip_id or "").strip()
        if not cid:
            raise ValueError("clip_id is required")
        k = str(kind or "mp3").strip().lower()
        if k not in {"mp3", "wav", "video"}:
            raise ValueError("kind must be mp3|wav|video")

        info = self.get_songs_by_ids([cid])
        clips = info.get("clips") if isinstance(info, dict) else []
        if not isinstance(clips, list) or not clips:
            feed_info = self.get_feed_clips_by_ids([cid])
            clips = feed_info.get("clips") if isinstance(feed_info, dict) else []
        clip = clips[0] if isinstance(clips, list) and clips else {}
        if not isinstance(clip, dict):
            clip = {}

        url = self._resolve_asset_url(clip, k)
        if not url:
            return {
                "ok": False,
                "status": 404,
                "clip_id": cid,
                "kind": k,
                "error": "asset_url_not_found",
            }

        out_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".mp3" if k == "mp3" else (".wav" if k == "wav" else ".mp4")
        out_path = out_dir / f"{cid}_{k}{suffix}"
        self._download_file(url, out_path)
        return {
            "ok": True,
            "clip_id": cid,
            "kind": k,
            "file": str(out_path),
            "url": url,
        }

    def generate_without_browser(
        self,
        out_dir: Path,
        songs_count: int = 1,
        poll_attempts: int = 20,
        poll_interval: float = 6.0,
        browser_cdp_url: str = "",
        auto_refresh_p1: bool = True,
        song_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        out_dir.mkdir(parents=True, exist_ok=True)

        captured_headers, captured_body = self._load_capture_template()
        runtime_headers = self._load_runtime_headers()

        headers = dict(captured_headers)
        headers.update(runtime_headers)
        headers.setdefault("origin", "https://suno.com")
        headers.setdefault("content-type", "application/json")

        keep = {
            "accept", "accept-language", "authorization", "browser-token", "content-type", "device-id",
            "origin", "referer", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "user-agent",
        }
        headers = {k.lower(): v for k, v in headers.items() if k.lower() in keep and isinstance(v, str) and v}

        session = self._cookie_session_from_storage(self.paths.storage_state)
        token = self._pick_working_token(session, headers)
        headers["authorization"] = f"Bearer {token}"

        summary: dict[str, Any] = {
            "storage_state": str(self.paths.storage_state),
            "auth_bundle": str(self.paths.auth_bundle),
            "manual_capture": str(self.paths.manual_capture),
            "songs": [],
        }

        workspace_selector = ""
        workspace_selector_source = ""
        if isinstance(song_params, dict):
            for key in ("workspace", "workspace_id", "project_id"):
                val = song_params.get(key)
                if isinstance(val, str) and val.strip():
                    workspace_selector = val.strip()
                    workspace_selector_source = key
                    break

        resolved_project_id: str | None = None
        if workspace_selector:
            resolved_project_id = self._resolve_project_id(session, headers, workspace_selector)
            if resolved_project_id:
                print(f"[suno] target workspace resolved: selector='{workspace_selector}' -> project_id={resolved_project_id}")
            else:
                if workspace_selector_source == "workspace":
                    created_project_id = self._create_project_by_name(session, headers, workspace_selector)
                    if created_project_id:
                        resolved_project_id = created_project_id
                        print(
                            f"[suno] workspace created and selected: selector='{workspace_selector}' -> project_id={resolved_project_id}"
                        )
                    else:
                        print(
                            "[suno] warning: workspace selector could not be resolved or created; "
                            "generating without explicit project assignment"
                        )
                else:
                    print(
                        "[suno] warning: workspace selector could not be resolved; generating without explicit project assignment"
                    )

        for idx in range(1, max(songs_count, 1) + 1):
            payload = self._random_payload(idx)
            if isinstance(song_params, dict):
                for key in (
                    "song_title",
                    "lyrics",
                    "styles",
                    "exclude_styles",
                    "gender",
                    "weirdness",
                    "style_influence",
                    "lyrics_mode",
                    "audio_type",
                    "bpm",
                    "key_root",
                    "key_scale",
                    "inspiration_clip_id",
                    "inspiration_start_s",
                    "inspiration_end_s",
                    "remaster_strength",
                    "remaster_model",
                ):
                    if key in song_params and song_params.get(key) is not None:
                        payload[key] = song_params.get(key)
            print(f"[suno] generating song {idx}/{songs_count}: {payload['song_title']}")

            req_body = dict(captured_body)
            captured_req_token = req_body.get("token")
            has_captcha_token = isinstance(captured_req_token, str) and captured_req_token.startswith("P1_")
            # Browser generates a short-lived P1 token in the request body for captcha-gated flows.
            # Keep it when available; otherwise fallback to token=None.
            if not has_captcha_token:
                req_body["token"] = None
            req_body["title"] = payload["song_title"]
            req_body["tags"] = payload["styles"]
            req_body["negative_tags"] = payload["exclude_styles"]
            req_body["prompt"] = payload["lyrics"]
            req_body["transaction_uuid"] = str(uuid.uuid4())
            if resolved_project_id:
                req_body["project_id"] = resolved_project_id

            inspiration_clip_id = str(payload.get("inspiration_clip_id", "") or "").strip()
            if inspiration_clip_id:
                req_body["artist_clip_id"] = inspiration_clip_id
                if payload.get("inspiration_start_s") is not None:
                    req_body["artist_start_s"] = float(payload["inspiration_start_s"])
                if payload.get("inspiration_end_s") is not None:
                    req_body["artist_end_s"] = float(payload["inspiration_end_s"])

            lyrics_mode = str(payload.get("lyrics_mode", "")).strip().lower()
            if lyrics_mode == "instrumental":
                req_body["make_instrumental"] = True
            elif lyrics_mode:
                req_body["make_instrumental"] = False
            meta = req_body.get("metadata")
            if isinstance(meta, dict):
                meta["create_session_token"] = str(uuid.uuid4())
                meta["vocal_gender"] = payload["gender"]
                audio_type = str(payload.get("audio_type", "") or "").strip().lower()
                bpm = str(payload.get("bpm", "") or "").strip().lower()
                key_root = str(payload.get("key_root", "") or "").strip().lower()
                key_scale = str(payload.get("key_scale", "") or "").strip().lower()

                if audio_type:
                    # Keep these fields under metadata to avoid breaking existing text generation contracts.
                    meta["sound_type"] = audio_type
                    meta["create_mode"] = "sounds"
                if bpm:
                    if bpm == "auto":
                        meta["sound_bpm_auto"] = True
                    else:
                        with contextlib.suppress(Exception):
                            meta["sound_bpm"] = int(bpm)
                            meta["sound_bpm_auto"] = False
                if key_root:
                    meta["sound_key_root"] = key_root
                if key_scale:
                    meta["sound_key_scale"] = key_scale

                remaster_strength = str(payload.get("remaster_strength", "") or "").strip().lower()
                remaster_model = str(payload.get("remaster_model", "") or "").strip()
                if remaster_strength:
                    meta["remaster_strength"] = remaster_strength
                if remaster_model:
                    meta["remaster_model"] = remaster_model

                if lyrics_mode in {"custom", "auto"} and not audio_type:
                    meta["create_mode"] = lyrics_mode
                    meta["is_mumble"] = False
                elif lyrics_mode == "mumble" and not audio_type:
                    meta["create_mode"] = "custom"
                    meta["is_mumble"] = True
                elif lyrics_mode == "instrumental":
                    meta["is_mumble"] = False
                sliders = meta.get("control_sliders")
                if isinstance(sliders, dict):
                    sliders["weirdness_constraint"] = payload["weirdness"]
                    sliders["style_weight"] = payload["style_influence"]

            req_headers = {**headers, "browser-token": self._fresh_browser_token_header()}

            # Browser flow performs this check before generation.
            check = session.post(
                "https://studio-api-prod.suno.com/api/c/check",
                headers=req_headers,
                json={"ctype": "generation"},
                timeout=60,
            )
            if check.status_code >= 400:
                raise RuntimeError(f"Captcha check failed: status={check.status_code}, body={check.text[:500]}")
            try:
                check_data = check.json()
            except Exception:
                check_data = {}
            if isinstance(check_data, dict) and check_data.get("required") is True:
                if not has_captcha_token and auto_refresh_p1 and browser_cdp_url:
                    fresh = asyncio.run(self._harvest_p1_token_via_cdp(browser_cdp_url, payload))
                    req_body["token"] = fresh
                    has_captcha_token = True
                elif not has_captcha_token:
                    raise RuntimeError(
                        "Generate blocked: captcha token required (api/c/check returned required=true) and no P1 token "
                        "was present in the capture payload. Provide --browser-cdp-url for auto refresh or run one browser generate first."
                    )
                print("[suno] captcha required=true; proceeding with P1 request token")

            gen = None
            token_retried = False
            project_retried = False
            for gen_attempt in range(1, 5):
                req_headers = {**headers, "browser-token": self._fresh_browser_token_header()}
                gen = session.post(
                    "https://studio-api-prod.suno.com/api/generate/v2-web/",
                    headers=req_headers,
                    json=req_body,
                    timeout=120,
                )
                if gen.status_code < 400:
                    break
                if (
                    gen.status_code == 422
                    and "Token validation failed" in (gen.text or "")
                    and auto_refresh_p1
                    and browser_cdp_url
                    and not token_retried
                ):
                    fresh = asyncio.run(self._harvest_p1_token_via_cdp(browser_cdp_url, payload))
                    req_body["token"] = fresh
                    has_captcha_token = True
                    token_retried = True
                    continue

                # If Suno rejects explicit project assignment, retry once without project_id.
                if (
                    "project_id" in req_body
                    and not project_retried
                    and gen.status_code in {400, 422}
                ):
                    body_l = (gen.text or "").lower()
                    if "project" in body_l or "workspace" in body_l or "invalid" in body_l:
                        req_body.pop("project_id", None)
                        project_retried = True
                        print("[suno] warning: project assignment rejected by API, retrying without project_id")
                    continue
                break

            assert gen is not None
            if gen.status_code >= 400:
                raise RuntimeError(f"Generate failed: status={gen.status_code}, body={gen.text[:500]}")

            gen_data = gen.json()
            clip_ids = self._extract_clip_ids(gen_data)
            if not clip_ids:
                raise RuntimeError("No clip IDs in generate response")

            if resolved_project_id:
                attached = self._attach_clips_to_project(session, headers, resolved_project_id, clip_ids)
                if attached:
                    print(f"[suno] attached {len(clip_ids)} clips to project_id={resolved_project_id}")
                else:
                    print("[suno] warning: could not attach generated clips to selected workspace project")

            song_dir = out_dir / f"song_{idx}"
            song_dir.mkdir(parents=True, exist_ok=True)
            (song_dir / "input.json").write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
            (song_dir / "generate_meta.json").write_text(
                json.dumps({"clip_ids": clip_ids}, indent=2, ensure_ascii=True),
                encoding="utf-8",
            )

            downloaded: list[str] = []
            expected = set(clip_ids)
            got: set[str] = set()
            for attempt in range(1, max(poll_attempts, 1) + 1):
                poll_payload = {
                    "filters": {"ids": {"presence": "True", "clipIds": clip_ids}},
                    "limit": max(len(clip_ids), 1),
                }
                status_resp = session.post(
                    "https://studio-api-prod.suno.com/api/feed/v3",
                    headers=headers,
                    json=poll_payload,
                    timeout=120,
                )
                if status_resp.status_code >= 400:
                    raise RuntimeError(f"Feed polling failed: status={status_resp.status_code}")

                clips = status_resp.json().get("clips", []) or []
                for clip in clips:
                    if not isinstance(clip, dict):
                        continue
                    cid = clip.get("id")
                    if not isinstance(cid, str) or cid not in expected or cid in got:
                        continue
                    audio_url = clip.get("audio_url")
                    if not (isinstance(audio_url, str) and audio_url.startswith("http") and ".mp3" in audio_url):
                        continue
                    title_slug = re.sub(r"[^a-z0-9_]+", "_", payload["song_title"].lower().replace(" ", "_"))
                    out_path = song_dir / f"test_{title_slug}_{cid}.mp3"
                    self._download_file(audio_url, out_path)
                    got.add(cid)
                    downloaded.append(str(out_path))
                    print(f"[suno] audio saved: {out_path}")

                if got == expected:
                    break
                print(f"[suno] waiting audio ({attempt}/{poll_attempts}) downloaded={len(got)}/{len(expected)}")
                time.sleep(max(poll_interval, 0.5))

            summary["songs"].append(
                {
                    "index": idx,
                    "title": payload["song_title"],
                    "clip_ids": clip_ids,
                    "downloaded_files": downloaded,
                }
            )

        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=True), encoding="utf-8")
        print(f"[suno] summary written: {out_dir / 'summary.json'}")
        return summary
