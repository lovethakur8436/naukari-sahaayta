"""
app/apply_agent/form_resolver.py

Dynamic apply-form resolver.

Portal behaviours handled:
  - boards.greenhouse.io/gitlab/...     -> direct Greenhouse form (input#first_name)
  - job-boards.greenhouse.io/figma/...  -> Greenhouse form embedded at bottom of listing page
  - boards.greenhouse.io/stripe/...     -> redirects to stripe.com listing, then
                                           stripe.com/.../apply loads a Workday-embedded
                                           iframe — no input#first_name in top-level DOM.

Resolution strategy (tried in order):
  1. Strict Greenhouse form present? (input#first_name)              -> done.
  2. Scroll to bottom -> recheck strict.                             -> Figma pattern.
  3. Click Apply button -> wait for SPA render
     -> recheck strict OR broad OR iframe (with deep iframe wait).   -> Stripe pattern.
  4. /apply URL heuristic (only if NOT already on /apply URL)
     -> wait for SPA render -> recheck strict OR broad OR iframe.    -> Stripe fallback.
  5. Give up -> SKIPPED.

Broad form detection (steps 3 & 4):
  - Checks for any visible <input type=text/email> or <textarea> inside <form>/[role=main].
  - Also detects iframe-embedded forms (Stripe/Workday) by presence of <iframe> on /apply URLs.
  - For confirmed iframe patterns, performs a deeper poll (up to 12s) to wait for Workday
    widget hydration before returning True, so the caller can switch into the iframe context.
"""

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

# Strict selector — standard Greenhouse boards (GitLab, Figma, etc.)
_STRICT_FORM_SELECTOR = "input#first_name"

# Broad selectors — React SPA portals where field IDs are dynamic
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

# Extra wait after navigation to let React SPAs hydrate
_SPA_RENDER_WAIT_MS = 3_000
# Extra deep-wait for Workday / iframe widgets to fully render
_IFRAME_DEEP_WAIT_MS = 12_000


# ------------------------------------------------------------------ #
# Internal helpers                                                     #
# ------------------------------------------------------------------ #

def _check_strict(page: Page, timeout_ms: int = 6_000) -> bool:
    """Standard Greenhouse form — input#first_name present in top-level DOM."""
    try:
        page.wait_for_selector(_STRICT_FORM_SELECTOR, timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False


def _check_strict_in_any_frame(page: Page, logs: list, timeout_ms: int = 6_000) -> bool:
    """
    Search for input#first_name inside every iframe on the page.
    Used for Stripe/Workday embed pattern where the form lives inside an iframe.
    Returns True and appends the confirmed frame URL to logs.
    """
    try:
        frames = page.frames
        for frame in frames[1:]:  # skip main frame
            try:
                frame.wait_for_selector(_STRICT_FORM_SELECTOR, timeout=timeout_ms)
                logs.append(
                    f"_check_strict_in_any_frame: found input#first_name inside iframe '{frame.url}'"
                )
                return True
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue
    except Exception as e:
        logs.append(f"_check_strict_in_any_frame: error — {e}")
    return False


def _wait_for_iframe_input(page: Page, logs: list) -> bool:
    """
    Poll every iframe on the page every 1.5s for up to _IFRAME_DEEP_WAIT_MS
    looking for input#first_name.  This gives Workday / embedded widgets
    enough time to fully hydrate before we give up.
    """
    import time
    deadline = time.monotonic() + (_IFRAME_DEEP_WAIT_MS / 1000)
    poll_interval = 1.5
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        if _check_strict_in_any_frame(page, logs, timeout_ms=1_000):
            logs.append(f"_wait_for_iframe_input: form found after {attempt} poll(s)")
            return True
        page.wait_for_timeout(int(poll_interval * 1000))
    logs.append(f"_wait_for_iframe_input: iframe form NOT found after {_IFRAME_DEEP_WAIT_MS}ms")
    return False


def _check_broad(page: Page, logs: list) -> tuple[bool, bool]:
    """
    Broader form detection for React SPA / iframe portals.
    Only runs when on an /apply or /application URL to avoid false-positives.

    Returns:
        (form_present: bool, is_iframe_embed: bool)
        is_iframe_embed=True signals the caller that filling must happen
        inside a child frame, not the top-level page.
    """
    url_lower = page.url.lower()
    if "/apply" not in url_lower and "/application" not in url_lower:
        logs.append("_check_broad: skipped (not on an /apply URL)")
        return False, False

    # Layer 1: visible inputs in top-level DOM
    for sel in _BROAD_FORM_SELECTORS:
        try:
            if page.locator(sel).first.count() > 0:
                logs.append(f"_check_broad: form detected via top-level selector '{sel}'")
                return True, False
        except Exception:
            continue

    # Layer 2: iframe presence (Stripe/Workday embed pattern)
    try:
        iframe_count = page.locator("iframe").count()
        if iframe_count > 0:
            logs.append(
                f"_check_broad: found {iframe_count} iframe(s) on /apply page — "
                "starting deep iframe wait for Workday/embedded form"
            )
            # Deep-poll: wait until the iframe actually renders input#first_name
            hydrated = _wait_for_iframe_input(page, logs)
            if hydrated:
                logs.append("_check_broad: iframe form hydrated and ready")
                return True, True  # is_iframe_embed = True
            else:
                logs.append("_check_broad: iframe(s) found but form never hydrated — treating as no form")
                return False, False
    except Exception:
        pass

    logs.append("_check_broad: no form inputs found")
    return False, False


def _scroll_and_check_strict(page: Page) -> bool:
    """Scroll to bottom then recheck strict selector (Figma pattern)."""
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)
        page.wait_for_selector(_STRICT_FORM_SELECTOR, timeout=5_000)
        return True
    except PlaywrightTimeoutError:
        return False


def _wait_spa_then_check(page: Page, logs: list, app_id=None) -> tuple[bool, bool]:
    """
    Wait for SPA/iframe hydration, take a debug screenshot, then try
    strict -> broad detection in order.

    Returns:
        (form_present: bool, is_iframe_embed: bool)
    """
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        logs.append("_wait_spa_then_check: networkidle timeout — continuing anyway")

    page.wait_for_timeout(_SPA_RENDER_WAIT_MS)

    if app_id:
        try:
            page.screenshot(path=f"data/debug_spa_{app_id}.png")
            logs.append(f"Debug SPA screenshot: data/debug_spa_{app_id}.png")
        except Exception:
            pass

    if _check_strict(page, timeout_ms=4_000):
        logs.append("_wait_spa_then_check: strict form found after SPA wait")
        return True, False

    found, is_iframe = _check_broad(page, logs)
    if found:
        logs.append("_wait_spa_then_check: broad/iframe form confirmed after SPA wait")
        return True, is_iframe
    return False, False


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


def get_form_frame(page: Page, is_iframe_embed: bool):
    """
    Return the frame that contains the actual form.
    For iframe-embedded portals (Stripe/Workday) this is the child frame
    that contains input#first_name.  For all others it is the main page.
    """
    if not is_iframe_embed:
        return page
    for frame in page.frames[1:]:
        try:
            if frame.locator(_STRICT_FORM_SELECTOR).count() > 0:
                return frame
        except Exception:
            continue
    return page  # fallback: return main page


# ------------------------------------------------------------------ #
# Public API                                                           #
# ------------------------------------------------------------------ #

def resolve_apply_form(page: Page, logs: list, app_id=None) -> tuple[bool, bool]:
    """
    Navigate to the actual apply form regardless of portal type.

    Args:
        page:   Playwright Page already navigated to the job URL.
        logs:   List to append diagnostic messages to.
        app_id: Optional application ID used for debug screenshot filenames.

    Returns:
        (found: bool, is_iframe_embed: bool)
        found          — form is ready.
        is_iframe_embed — True means the form lives inside a child iframe;
                          caller must use get_form_frame() to get the right frame.
    """
    logs.append(f"resolve_apply_form: starting on {page.url}")

    # ── Step 1: Already on a standard Greenhouse form ─────────────────────
    if _check_strict(page):
        logs.append("resolve_apply_form: strict form already present.")
        return True, False

    # ── Step 2: Scroll to bottom (Figma — form embedded at page bottom) ───
    logs.append("resolve_apply_form: form not visible — scrolling to bottom...")
    if _scroll_and_check_strict(page):
        logs.append("resolve_apply_form: strict form found after scroll.")
        return True, False

    # ── Step 3: Click Apply button then wait for SPA/iframe render ─────────
    logs.append("resolve_apply_form: hunting for Apply button...")
    clicked = _find_and_click_apply(page, logs)
    if clicked:
        logs.append("resolve_apply_form: Apply button clicked — waiting for SPA/iframe render...")
        found, is_iframe = _wait_spa_then_check(page, logs, app_id=app_id)
        if found:
            logs.append("resolve_apply_form: form confirmed after Apply click.")
            return True, is_iframe

    # ── Step 4: /apply URL heuristic — only if NOT already on /apply ──────
    if "/apply" not in page.url.lower():
        logs.append("resolve_apply_form: trying /apply URL heuristic...")
        navigated = _try_append_apply(page, logs)
        if navigated:
            found, is_iframe = _wait_spa_then_check(page, logs, app_id=app_id)
            if found:
                logs.append("resolve_apply_form: form confirmed via /apply URL.")
                return True, is_iframe

    # ── Step 5: Give up ───────────────────────────────────────────────────
    logs.append("resolve_apply_form: EXHAUSTED all strategies — no form found.")
    return False, False
