"""
app/apply_agent/form_resolver.py

Dynamic apply-form resolver.

Problem: Different companies host their Greenhouse forms in different ways:
  - boards.greenhouse.io/stripe/jobs/123        -> redirects to stripe.com/jobs/listing/... (apply link at bottom)
  - job-boards.greenhouse.io/figma/jobs/123     -> form is inline at the bottom of the listing page
  - boards.greenhouse.io/gitlab/jobs/123        -> direct apply form, no redirect

Solution: After navigating to any URL, call `resolve_apply_form(page, logs)`.
It checks whether the current page already HAS the apply form (input#first_name),
and if not, it hunts for an 'Apply' button/link and follows it.

Returns True if the apply form is now present and ready to fill.
Returns False if no form could be found (job genuinely closed/removed).
"""

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
import re

# Selectors that indicate the real apply form is present on the page
_FORM_PRESENT_SELECTOR = "input#first_name"

# CSS / text selectors for "Apply" buttons/links on listing pages
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

# If the final URL after clicking contains any of these, we landed on a
# form page (or close enough to try scraping it).
_APPLY_URL_SIGNALS = ["/apply", "/application", "gh_src=", "grnh.se"]


def _form_is_present(page: Page) -> bool:
    """Return True if the Greenhouse apply form is already visible on this page."""
    try:
        page.wait_for_selector(_FORM_PRESENT_SELECTOR, timeout=5_000)
        return True
    except PlaywrightTimeoutError:
        return False


def _scroll_and_check(page: Page) -> bool:
    """
    Some portals (Figma) embed the form at the bottom of the listing page.
    Scroll to the bottom and recheck for the form.
    """
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)
        page.wait_for_selector(_FORM_PRESENT_SELECTOR, timeout=5_000)
        return True
    except PlaywrightTimeoutError:
        return False


def _find_and_click_apply(page: Page, logs: list) -> bool:
    """
    Hunt for an Apply button/link on the current page and click it.
    Returns True if a button was found and clicked.
    """
    for selector in _APPLY_BUTTON_SELECTORS:
        try:
            btn = page.locator(selector).first
            if btn.count() == 0:
                continue
            href = btn.get_attribute("href") or ""
            text = btn.inner_text().strip()
            logs.append(f"Found apply trigger: '{text}' href='{href}' (selector: {selector})")
            btn.click(timeout=5_000)
            page.wait_for_load_state("networkidle", timeout=15_000)
            logs.append(f"After click — URL: {page.url}")
            return True
        except Exception as e:
            logs.append(f"Apply button selector '{selector}' failed: {e}")
            continue
    return False


def _try_append_apply(page: Page, logs: list) -> bool:
    """
    Last-resort heuristic: if current URL looks like a listing page,
    try appending '/apply' and navigating directly.
    e.g. stripe.com/jobs/listing/.../7292520  ->  .../7292520/apply
    """
    current = page.url.rstrip("/")
    if "/apply" in current:
        return False  # Already on apply page, no need
    candidate_url = current + "/apply"
    try:
        logs.append(f"Trying direct /apply URL: {candidate_url}")
        page.goto(candidate_url, timeout=15_000)
        page.wait_for_load_state("networkidle", timeout=10_000)
        logs.append(f"After /apply navigate — URL: {page.url}")
        return True
    except Exception as e:
        logs.append(f"/apply navigation failed: {e}")
        return False


def resolve_apply_form(page: Page, logs: list) -> bool:
    """
    Ensure the Greenhouse apply form (input#first_name) is present and ready.

    Strategy (tried in order, stops as soon as form is found):
      1. Form already on page -> done.
      2. Scroll to bottom (Figma-style embedded forms) -> recheck.
      3. Click an 'Apply for this role' / 'Apply now' button -> recheck.
      4. Append '/apply' to current URL and navigate -> recheck.
      5. Give up — return False.

    Returns:
        True  — form is ready, agent can start filling fields.
        False — no form found, job is likely closed or unsupported portal.
    """
    current_url = page.url
    logs.append(f"resolve_apply_form: starting on {current_url}")

    # Step 1: Already on the form?
    if _form_is_present(page):
        logs.append("resolve_apply_form: form already present.")
        return True

    # Step 2: Scroll to bottom (Figma embeds form at page bottom)
    logs.append("resolve_apply_form: form not visible — scrolling to bottom...")
    if _scroll_and_check(page):
        logs.append("resolve_apply_form: form found after scroll.")
        return True

    # Step 3: Click Apply button
    logs.append("resolve_apply_form: hunting for Apply button...")
    clicked = _find_and_click_apply(page, logs)
    if clicked:
        if _form_is_present(page):
            logs.append("resolve_apply_form: form found after clicking Apply button.")
            return True
        # Maybe form is embedded at bottom of new page too
        if _scroll_and_check(page):
            logs.append("resolve_apply_form: form found after scroll post-click.")
            return True

    # Step 4: Append /apply heuristic (Stripe pattern)
    logs.append("resolve_apply_form: trying /apply URL heuristic...")
    _try_append_apply(page, logs)
    if _form_is_present(page):
        logs.append("resolve_apply_form: form found via /apply URL.")
        return True
    if _scroll_and_check(page):
        logs.append("resolve_apply_form: form found after scroll on /apply URL.")
        return True

    logs.append("resolve_apply_form: EXHAUSTED all strategies — no form found.")
    return False
