from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import structlog

from backend import config

log = structlog.get_logger()


@dataclass(frozen=True)
class CredentialStatus:
    username_env: str | None
    password_env: str | None
    has_username: bool
    has_password: bool

    @property
    def complete(self) -> bool:
        if not self.username_env and not self.password_env:
            return False
        return self.has_username and self.has_password


class PlatformSessionManager:
    """Shared session helper for adapters that need an authenticated browser.

    Job boards differ sharply on MFA, captcha, SSO, and regional privacy flows.
    This manager keeps the reusable pieces in one place: credential discovery,
    browser-state persistence, and clear manual-login errors when a platform
    cannot be safely logged into by a generic adapter.
    """

    def __init__(
        self,
        platform_key: str,
        *,
        auth_required: bool,
        username_env: str | None = None,
        password_env: str | None = None,
        session_dir: Path | None = None,
    ) -> None:
        self.platform_key = platform_key
        self.auth_required = auth_required
        self.username_env = username_env
        self.password_env = password_env
        self.session_dir = session_dir or config.DATA_DIR / "platform-sessions"

    @property
    def storage_state_path(self) -> Path:
        return self.session_dir / f"{self.platform_key}.storage-state.json"

    def credential_status(self) -> CredentialStatus:
        username = bool(self.username_env and os.getenv(self.username_env))
        password = bool(self.password_env and os.getenv(self.password_env))
        return CredentialStatus(self.username_env, self.password_env, username, password)

    async def restore_cookies(self, page) -> bool:
        """Restore cookies from a previously captured Playwright storage state."""
        path = self.storage_state_path
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            cookies = payload.get("cookies") or []
            if cookies:
                await page.context.add_cookies(cookies)
            log.info("platform_session_restored", platform=self.platform_key, cookie_count=len(cookies), path=str(path))
            return bool(cookies)
        except Exception as exc:
            log.warning("platform_session_restore_failed", platform=self.platform_key, path=str(path), error=str(exc))
            return False

    async def capture(self, page) -> Path:
        """Persist current browser context state for this platform."""
        self.session_dir.mkdir(parents=True, exist_ok=True)
        path = self.storage_state_path
        await page.context.storage_state(path=str(path))
        log.info("platform_session_captured", platform=self.platform_key, path=str(path))
        return path

    async def ensure_ready(self, page) -> None:
        """Ensure an authenticated platform can proceed, or raise a useful error."""
        if not self.auth_required:
            return
        await self.restore_cookies(page)
        if not await self._looks_like_login_wall(page):
            return

        status = self.credential_status()
        env_hint = ""
        if status.username_env or status.password_env:
            missing = [
                env_name
                for env_name, present in ((status.username_env, status.has_username), (status.password_env, status.has_password))
                if env_name and not present
            ]
            if missing:
                env_hint = f" Missing credential env vars: {', '.join(missing)}."
            else:
                env_hint = " Credentials are configured, but this adapter requires a manual authenticated session for MFA/captcha-safe login."

        raise NotImplementedError(
            f"{self.platform_key} requires an authenticated browser session. "
            f"Sign in once with the persistent JobPilot browser profile; the adapter will reuse that session."
            f"{env_hint}"
        )

    async def _looks_like_login_wall(self, page) -> bool:
        try:
            return await page.evaluate(
                r"""
                () => {
                  const text = (document.body?.innerText || '').replace(/\s+/g, ' ').toLowerCase();
                  const visibleInputs = Array.from(document.querySelectorAll('input')).filter((el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden'
                      && style.display !== 'none'
                      && rect.width > 0
                      && rect.height > 0;
                  });
                  const names = visibleInputs.map((el) => `${el.name || ''} ${el.id || ''} ${el.type || ''}`).join(' ').toLowerCase();
                  return /(sign in|log in|login|create account|forgot password|two-factor|multi-factor|verification code)/.test(text)
                    && /(password|username|email|identifier|otp|code)/.test(names);
                }
                """
            )
        except Exception:
            return False
