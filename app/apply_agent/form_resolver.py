"""
app/apply_agent/form_resolver.py

Dynamic apply-form resolver.

Portal behaviours handled:
  - boards.greenhouse.io/gitlab/...     -> direct Greenhouse form (input#first_name)
  - job-boards.greenhouse.io/figma/...  -> Greenhouse form embedded at bottom of listing page
  - boards.greenhouse.io/stripe/...     -> redirects to stripe.com listing, then
                                           stripe.com/.../apply which is a React SPA
                                           (no input#first_name — uses generic input/textarea)

Resolution strategy (tried in order):
  1. Strict Greenhouse form present? (input#first_name)         -> done.
  2. Scroll to bottom -> recheck strict.                        -> Figma pattern.
  3. Click Apply button -> wait 3s for SPA render
     -> recheck strict OR broad.                                -> Stripe pattern.
  4. /apply URL heuristic (only if NOT already on /apply URL)
     -> wait for SPA render -> recheck strict OR broad.         -> Stripe fallback.
  5. Give up -> SKIPPED.

Broad form detection (steps 3 & 4):
  Checks for any visible <input type=text/email> or <textarea> inside a <form>
  or [role=main] container. Only runs when on an /apply or /application URL
  to avoid false-positives on listing/search pages.
"""

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

# Strict selector — standard Greenhouse boards (GitLab, Figma, etc.)
_STRICT_FORM_SELECTOR = "input#first_name"

# Broad selectors — React SPA portals (Stripe) where field IDs are dynamic
_BROAD_FORM_SELECTORS = [
    "form input[type='text']:visible",
    "form input[type='email']:visible",
    "form textarea:visible",
    "[role='main'] input[type='text']:visible",
    "[role='main'] input[type='email']:visible",
    "label + input:visible",
    "label + div input:visible",
]

# Apply CTA selectors — listing pages
_APPLY_BUTTON_SELECTORS = [
    "a[href*='/apply']:visible",
    "button:has-text('Apply for this role'):visible",
    "a:has-text('Apply for this role'):visible",
    "button:has-text('Apply now'):visible",
    "a:has-text('Apply now'):visible",
    "button:has-text('Apply'):visible",
    "a:has-text('Apply'):visible",
    "[data-testid*='apply']:visible",
    "[class*='apply-btn']:visible",
    "[class*='ApplyButton']:visible",
]

# Extra wait after navigation to let React SPAs hydrate before checking for form
_SPA_RENDER_WAIT_MS = 3_000


# ------------------------------------------------------------------ #
# Internal helpers                                                     #
# ------------------------------------------------------------------ #

def _check_strict(page: Page, timeout_ms: int = 6_000) -> bool:
    """Standard Greenhouse form — input#first_name present."""
    try:
        page.wait_for_selector(_STRICT_FORM_SELECTOR, timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False


def _check_broad(page: Page, logs: list) -> bool:
    """
    Broader form detection for React SPA portals (Stripe).
    Only runs when already on an /apply or /application URL to avoid
    false-positives on listing/search pages.
    """
    url_lower = page.url.lower()
    if "/apply" not in url_lower and "/application" not in url_lower:
        logs.append("_check_broad: skipped (not on an /apply URL)")
        return False

    for sel in _BROAD_FORM_SELECTORS:
        try:
            if page.locator(sel).first.count() > 0:
                logs.append(f"_check_broad: form detected via '{sel}'")
                return True
        except Exception:
            continue

    logs.append("_check_broad: no form inputs found")
    return False


def _scroll_and_check_strict(page: Page) -> bool:
    """Scroll to bottom then recheck strict selector (Figma pattern)."""
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)
        page.wait_for_selector(_STRICT_FORM_SELECTOR, timeout=5_000)
        return True
    except PlaywrightTimeoutError:
        return False


def _wait_spa_then_check(page: Page, logs: list, app_id=None) -> bool:
    """
    Wait for SPA hydration, take a debug screenshot, then try both
    strict and broad detection. Used after any navigation to an /apply page.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        logs.append("_wait_spa_then_check: networkidle timeout — continuing anyway")

    page.wait_for_timeout(_SPA_RENDER_WAIT_MS)

    # Debug screenshot so you can visually confirm what loaded
    if app_id:
        try:
            page.screenshot(path=f"data/debug_spa_{app_id}.png")
            logs.append(f"Debug SPA screenshot saved: data/debug_spa_{app_id}.png")
        except Exception:
            pass

    if _check_strict(page, timeout_ms=4_000):
        logs.append("_wait_spa_then_check: strict form found after SPA wait")
        return True
    if _check_broad(page, logs):
        logs.append("_wait_spa_then_check: broad form found after SPA wait")
        return True
    return False


def _find_and_click_apply(page: Page, logs: list) -> bool:
    """Find an Apply CTA on the current listing page and click it."""
    for selector in _APPLY_BUTTON_SELECTORS:
        try:
            btn = page.locator(selector).first
            if btn.count() == 0:
                continue
            href = btn.get_attribute("href") or ""
            text = btn.inner_text().strip()
            logs.append(f"Found apply trigger: '{text}' href='{href}' (selector: {selector})")
            btn.click(timeout=5_000)
            logs.append(f"After click — URL: {page.url}")
            return True
        except Exception as e:
            logs.append(f"Apply button selector '{selector}' failed: {e}")
            continue
    return False


def _try_append_apply(page: Page, logs: list) -> bool:
    """Last-resort: append /apply to current URL and navigate directly."""
    current = page.url.rstrip("/")
    if "/apply" in current.lower():
        logs.append("_try_append_apply: already on /apply URL — skipping")
        return False
    candidate_url = current + "/apply"
    try:
        logs.append(f"Trying direct /apply URL: {candidate_url}")
        page.goto(candidate_url, timeout=15_000)
        logs.append(f"After /apply navigate — URL: {page.url}")
        return True
    except Exception as e:
        logs.append(f"/apply navigation failed: {e}")
        return False


# ------------------------------------------------------------------ #
# Public API                                                           #
# ------------------------------------------------------------------ #

def resolve_apply_form(page: Page, logs: list, app_id=None) -> bool:
    """
    Navigate to the actual apply form regardless of portal type.

    Args:
        page:   Playwright Page already navigated to the job URL.
        logs:   List to append diagnostic messages to.
        app_id: Optional application ID used for debug screenshot filenames.

    Returns:
        True  — form is ready, agent can start filling fields.
        False — no form found after all strategies (job closed / unsupported portal).
    """
    logs.append(f"resolve_apply_form: starting on {page.url}")

    # ── Step 1: Already on a standard Greenhouse form ─────────────────────
    if _check_strict(page):
        logs.append("resolve_apply_form: strict form already present.")
        return True

    # ── Step 2: Scroll to bottom (Figma — form embedded at page bottom) ───
    logs.append("resolve_apply_form: form not visible — scrolling to bottom...")
    if _scroll_and_check_strict(page):
        logs.append("resolve_apply_form: strict form found after scroll.")
        return True

    # ── Step 3: Click Apply button then wait for SPA render ───────────────
    logs.append("resolve_apply_form: hunting for Apply button...")
    clicked = _find_and_click_apply(page, logs)
    if clicked:
        logs.append("resolve_apply_form: Apply button clicked — waiting for SPA render...")
        if _wait_spa_then_check(page, logs, app_id=app_id):
            logs.append("resolve_apply_form: form confirmed after Apply click.")
            return True

    # ── Step 4: /apply URL heuristic — only if NOT already on /apply ──────
    if "/apply" not in page.url.lower():
        logs.append("resolve_apply_form: trying /apply URL heuristic...")
        navigated = _try_append_apply(page, logs)
        if navigated:
            if _wait_spa_then_check(page, logs, app_id=app_id):
                logs.append("resolve_apply_form: form confirmed via /apply URL.")
                return True

    # ── Step 5: Give up ───────────────────────────────────────────────────
    logs.append("resolve_apply_form: EXHAUSTED all strategies — no form found.")
    return False
