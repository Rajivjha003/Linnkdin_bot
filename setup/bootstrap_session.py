"""
Interactive LinkedIn session bootstrapper.

Opens a real non-headless Chromium browser (stealth via Patchright) to allow
interactive login by the user. 2FA, SMS verification, or CAPTCHA challenges
are handled directly by the user.

Once logged in, the session profile, cookies, and storage state are persisted
to .linkedin-mcp/ for automated, headless execution.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from patchright.async_api import Error as PlaywrightError

from linkedin_mcp_server.drivers.browser import BrowserManager
from linkedin_mcp_server.profile_claim import ensure_profile_claim
from linkedin_mcp_server.profile_lease import get_profile_lease
from linkedin_mcp_server.session_state import (
    portable_cookie_path,
    write_source_state,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_DIR = REPO_ROOT / ".linkedin-mcp" / "profile"
DEFAULT_EMAIL = "rajiv.jha.0003@gmail.com"


async def check_for_auth(context) -> bool:
    """Check whether the li_at session cookie is present and valid."""
    try:
        cookies = await context.cookies()
        for cookie in cookies:
            if cookie.get("name") == "li_at" and "linkedin.com" in cookie.get("domain", ""):
                if cookie.get("value") and len(cookie["value"]) > 10:
                    return True
    except Exception:
        pass
    return False


async def bootstrap_session(
    email: str = DEFAULT_EMAIL,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    timeout_seconds: int = 1800,
) -> bool:
    """Run interactive LinkedIn authentication flow."""
    profile_dir = Path(profile_dir).resolve()
    auth_root = profile_dir.parent
    auth_root.mkdir(parents=True, exist_ok=True)

    print("=" * 65)
    print("  LinkedIn Session Bootstrap (Patchright Stealth Chromium)")
    print("=" * 65)
    print(f"Target profile dir: {profile_dir}")
    print(f"Target account:     {email}")
    print(f"Timeout:            {timeout_seconds // 60} minutes")
    print("-" * 65)

    # Claim the profile directory
    ensure_profile_claim(profile_dir, claim_anyway=True)

    lease = get_profile_lease(profile_dir)
    if not await lease.acquire(timeout=10.0):
        print("❌ Error: Another process is currently using this profile.")
        return False

    lease.mark_browser_open()
    try:
        mgr = BrowserManager(
            user_data_dir=profile_dir,
            headless=False,
            slow_mo=50,
        )

        async with mgr as browser:
            page = browser.page
            print("\n🌐 Opening LinkedIn login page...")
            try:
                await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
            except Exception as e:
                print(f"⚠️ Navigation note: {e}")

            # Pre-fill email if field is visible and empty
            try:
                username_field = await page.wait_for_selector("#username", timeout=5000)
                if username_field:
                    current_val = await username_field.input_value()
                    if not current_val:
                        await username_field.fill(email)
                        print(f"✨ Pre-filled email: {email}")
                    # Focus password field for convenience
                    password_field = await page.query_selector("#password")
                    if password_field:
                        await password_field.focus()
            except Exception:
                # Login page might already have an account chooser or alternate layout
                pass

            print("\n👉 Please sign in to LinkedIn in the browser window.")
            print("   - Enter your password.")
            print("   - Complete any 2FA or verification prompt if asked.")
            print("\n⏳ Waiting for successful login...")

            start_time = asyncio.get_event_loop().time()
            logged_in = False

            while True:
                elapsed = asyncio.get_event_loop().time() - start_time
                if timeout_seconds and elapsed > timeout_seconds:
                    print(f"\n❌ Login timed out after {timeout_seconds // 60} minutes.")
                    return False

                if page.is_closed() and not browser.context.pages:
                    print("\n❌ Browser was closed before login was completed.")
                    return False

                if await check_for_auth(browser.context):
                    logged_in = True
                    break

                await asyncio.sleep(1.5)

            if logged_in:
                print("\n🎉 Authentication detected! Finalizing session persistence...")
                await asyncio.sleep(3.0)  # Allow session cookies to stabilize

                # 1. Export cookies for MCP server / local bridge
                cookie_dest = portable_cookie_path(profile_dir)
                exported_cookies = await browser.export_cookies(cookie_dest)
                if exported_cookies:
                    print(f"   [✓] Saved cookies:       {cookie_dest}")

                # 2. Export storage state for Playwright / Patchright Easy Apply engine
                storage_state_mcp = auth_root / "storage_state.json"
                storage_state_root = REPO_ROOT / "storage_state.json"
                await browser.export_storage_state(storage_state_mcp)
                await browser.export_storage_state(storage_state_root)
                print(f"   [✓] Saved storage state: {storage_state_mcp}")

                # 3. Write source state metadata
                source_state = write_source_state(profile_dir)
                print(f"   [✓] Session generation:  {source_state.login_generation}")
                print(f"   [✓] Browser profile:     {profile_dir}")

                print("\n" + "=" * 65)
                print("  ✅ LinkedIn Session Successfully Bootstrapped!")
                print("  The bot can now run headlessly without manual login.")
                print("=" * 65 + "\n")
                return True

    except PlaywrightError as pe:
        print(f"\n❌ Browser error: {pe}")
        return False
    except Exception as exc:
        print(f"\n❌ Unexpected error: {exc}")
        return False
    finally:
        lease.mark_browser_closed()
        lease.release()

    return False


def main():
    parser = argparse.ArgumentParser(description="LinkedIn Session Bootstrapper")
    parser.add_argument(
        "--email",
        type=str,
        default=DEFAULT_EMAIL,
        help=f"LinkedIn email address (default: {DEFAULT_EMAIL})",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=DEFAULT_PROFILE_DIR,
        help=f"Path to profile directory (default: {DEFAULT_PROFILE_DIR})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Timeout in seconds for manual login (default: 1800)",
    )
    args = parser.parse_args()

    success = asyncio.run(
        bootstrap_session(
            email=args.email,
            profile_dir=args.profile_dir,
            timeout_seconds=args.timeout,
        )
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
