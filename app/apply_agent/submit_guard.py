"""
submit_guard.py
───────────────
Three-layer safety net around form submission:

  Layer 1 — SAFE_DEFAULTS
    When the LLM returns null/empty for a field, check whether the field
    is required. If so, look up a sensible default answer keyed on the
    field label pattern and apply it instead of silently skipping.

  Layer 2 — Pre-Submit Scan (_scan_required_empty)
    Before clicking Submit, walk every required field and check if it is
    still visually empty. Re-fill anything missed.

  Layer 3 — Post-Submit Retry (submit_with_retry)
    After clicking Submit, detect validation error messages. Parse which
    fields are highlighted and attempt a targeted re-fill. Retry up to
    MAX_SUBMIT_RETRIES times before giving up.
"""

from __future__ import annotations
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page, Frame

# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — SAFE_DEFAULTS
# Keys are regex patterns matched against the lowercase field label.
# Values are the answer to use when LLM returns null for a REQUIRED field.
# List values = checkbox groups (pick the first matching option).
# ─────────────────────────────────────────────────────────────────────────────

SAFE_DEFAULTS: list[tuple[str, str | list]] = [
    # Experience / seniority
    (r"years.{0,20}experience|experience.{0,20}years",  "3-4 years"),
    (r"how many years",                                  "3-4 years"),
    (r"level of experience|seniority",                   "Mid-level"),

    # Specialization / domain checkboxes
    (r"specializ|area.{0,15}work|domain",               ["Backend", "Full-Stack"]),
    (r"which.{0,20}area",                               ["Backend"]),

    # Programming languages checkboxes
    (r"language|tech.?stack|primary.{0,10}language",    ["Python", "Java"]),

    # Pronouns — safest to decline
    (r"pronoun",                                         "he/him/his"),

    # Portfolio / personal site
    (r"portfolio|personal.?site|website",               "https://github.com/lovethakur8436"),

    # Cover letter / motivation — short safe answer
    (r"cover.?letter|why.{0,20}(us|company|role)|motivation",
     "I am excited about this opportunity and believe my skills align well with the role."),

    # Salary / compensation
    (r"salary|compensation|expected.{0,10}pay",          "Open to discussion"),

    # Notice period / availability
    (r"notice.?period|start.?date|when.{0,15}start|available", "30 days"),

    # Relocation
    (r"relocat",                                         "Yes"),

    # Visa / sponsorship — candidate does NOT need sponsorship
    (r"sponsor|visa|work.{0,15}authoriz",                "No"),

    # Remote preference
    (r"remote|work.{0,10}(from|preference)",             "Open to remote or hybrid"),

    # Referral / how did you hear
    (r"how.{0,20}hear|referr|source",                    "LinkedIn"),

    # Preferred contact / communication
    (r"preferred.{0,15}contact|best.{0,10}way",          "Email"),

    # Current company / employer
    (r"current.{0,15}(company|employer)",                "N/A"),

    # LinkedIn fallback
    (r"linkedin",                                        "linkedin.com/in/luv-kumar-06975b175"),

    # GitHub fallback
    (r"github",                                          "https://github.com/lovethakur8436"),
]


def get_safe_default(label: str, options: list[str] | None = None) -> str | None:
    """
    Return a safe default answer for a required field whose label matches
    one of the SAFE_DEFAULTS patterns.

    If the default is a list (checkbox group), intersect with available
    `options` and return the first match; fall back to the first list item.

    Returns None if no pattern matches.
    """
    label_lower = (label or "").lower()
    for pattern, default in SAFE_DEFAULTS:
        if re.search(pattern, label_lower):
            if isinstance(default, list):
                if options:
                    opts_lower = {o.lower(): o for o in options}
                    for d in default:
                        if d.lower() in opts_lower:
                            return opts_lower[d.lower()]
                    # partial match
                    for d in default:
                        for ol, o in opts_lower.items():
                            if d.lower() in ol:
                                return o
                return default[0]  # raw value; caller handles checkbox logic
            return default
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — Pre-Submit Empty-Required Field Scan
# ─────────────────────────────────────────────────────────────────────────────

# Selectors that indicate a validation error has already fired on a field
_ERROR_FIELD_SELECTORS = (
    ".field_with_errors input, .field_with_errors textarea, "
    "[aria-invalid='true'], "
    "input.error, textarea.error"
)


def _scan_required_empty(frame, logs: list) -> list[dict]:
    """
    Walk all visible required fields in `frame`.
    Return a list of dicts describing fields that appear empty so the caller
    can attempt a re-fill before hitting Submit.

    Each dict has: id, name, label, type ('text'|'react-select'|'checkbox'|'file')
    """
    try:
        return frame.evaluate("""
        () => {
            const skip = new Set(['first_name','last_name','email','phone','country']);
            const empty = [];

            // ── text / textarea ───────────────────────────────────────────
            document.querySelectorAll(
                'input[type="text"]:not([hidden]), textarea:not([hidden])'
            ).forEach(el => {
                if (skip.has(el.id)) return;
                if (el.offsetWidth === 0 && el.offsetHeight === 0) return;
                const req = el.required
                    || el.getAttribute('aria-required') === 'true'
                    || !!(document.querySelector(`label[for="${el.id}"]`)?.innerText?.includes('*'));
                if (!req) return;
                if ((el.value || '').trim() !== '') return;
                const label = document.querySelector(`label[for="${el.id}"]`)?.innerText?.replace('*','').trim()
                    || el.placeholder || el.name || el.id;
                empty.push({ id: el.id, name: el.name, label, type: 'text' });
            });

            // ── react-select (combobox) ───────────────────────────────────
            document.querySelectorAll('input[role="combobox"]').forEach(el => {
                if (skip.has(el.id)) return;
                if (el.offsetWidth === 0 && el.offsetHeight === 0) return;
                const req = el.required
                    || el.getAttribute('aria-required') === 'true'
                    || !!(document.querySelector(`label[for="${el.id}"]`)?.innerText?.includes('*'));
                if (!req) return;
                const control = el.closest('.select__control');
                if (!control) return;
                const placeholder = control.querySelector('.select__placeholder');
                if (!placeholder) return;  // already has a value
                const label = document.querySelector(`label[for="${el.id}"]`)?.innerText?.replace('*','').trim()
                    || el.name || el.id;
                empty.push({ id: el.id, name: el.name, label, type: 'react-select' });
            });

            // ── file input (resume) ───────────────────────────────────────
            document.querySelectorAll('input[type="file"]').forEach(el => {
                if (el.offsetWidth === 0 && el.offsetHeight === 0) return;
                const req = el.required || el.getAttribute('aria-required') === 'true';
                if (!req) return;
                if (el.files && el.files.length > 0) return;
                empty.push({ id: el.id, name: el.name, label: 'Resume/CV', type: 'file' });
            });

            // ── checkbox groups ───────────────────────────────────────────
            const groups = {};
            document.querySelectorAll('input[type="checkbox"]').forEach(el => {
                if (el.offsetWidth === 0 && el.offsetHeight === 0) return;
                const req = el.required || el.getAttribute('aria-required') === 'true';
                if (!req) return;
                const grp = el.name || el.id;
                if (!groups[grp]) groups[grp] = { any_checked: false, id: el.id, name: grp };
                if (el.checked) groups[grp].any_checked = true;
            });
            Object.values(groups).forEach(g => {
                if (!g.any_checked)
                    empty.push({ id: g.id, name: g.name, label: g.name, type: 'checkbox' });
            });

            return empty;
        }
        """)
    except Exception as exc:
        logs.append(f"[submit_guard] _scan_required_empty error: {exc}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — Post-Submit Retry
# ─────────────────────────────────────────────────────────────────────────────

MAX_SUBMIT_RETRIES = 2

_CONFIRM_SELECTORS = [
    "[class*='confirmation']:visible",
    "[class*='success']:visible",
    "[class*='thank']:visible",
    "h1:has-text('Thank you'):visible",
    "h2:has-text('Thank you'):visible",
    "h1:has-text('Application submitted'):visible",
    "p:has-text('Application received'):visible",
    "p:has-text('successfully submitted'):visible",
]

_ERROR_TEXT_SELECTORS = [
    ".error-message:visible",
    ".field_with_errors:visible",
    "[class*='error']:visible",
    "[class*='invalid']:visible",
    "[aria-invalid='true']:visible",
    ".help-inline:visible",
    ".field-message:visible",
]


def _parse_validation_errors(frame, logs: list) -> list[str]:
    """
    Return a deduplicated list of non-empty error message strings
    found after a failed submit attempt.
    """
    seen: set[str] = set()
    errors: list[str] = []
    for sel in _ERROR_TEXT_SELECTORS:
        try:
            texts = frame.locator(sel).all_inner_texts()
            for t in texts:
                t = t.strip()
                if t and t not in seen:
                    seen.add(t)
                    errors.append(t)
        except Exception:
            pass
    return errors


def _wait_outcome(page, frame, pre_url: str, logs: list, wait_s: float = 8) -> str:
    """
    Poll for up to `wait_s` seconds and return one of:
      'AUTO_APPLIED'     — confirmation element found or URL changed away from /apply
      'VALIDATION_FAILED' — validation error messages detected
      'FAILED'           — timeout with no signal
    """
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        # ── URL redirect (Greenhouse often redirects after submit) ──────
        try:
            cur = page.url
            if "/apply" in pre_url.lower() and "/apply" not in cur.lower():
                logs.append(f"Submit confirmed: URL changed to '{cur}'")
                return "AUTO_APPLIED"
        except Exception:
            pass

        # ── Confirmation element ────────────────────────────────────────
        for sel in _CONFIRM_SELECTORS:
            try:
                if frame.locator(sel).count() > 0:
                    logs.append(f"Submit confirmed: found '{sel}'")
                    return "AUTO_APPLIED"
            except Exception:
                pass

        # ── Validation errors ───────────────────────────────────────────
        errors = _parse_validation_errors(frame, logs)
        blocking = [e for e in errors if "required" in e.lower() or "invalid" in e.lower() or len(e) > 3]
        if blocking:
            logs.append(f"Submit validation errors detected: {blocking}")
            return "VALIDATION_FAILED"

        frame.wait_for_timeout(600)

    logs.append("Submit outcome: no confirmation or error detected after 8s — marking FAILED")
    return "FAILED"


def submit_with_retry(
    page,
    frame,
    pre_submit_url: str,
    questions_data: list[dict],
    candidate_profile: dict,
    logs: list,
    *,
    refill_fn,          # callable(frame, field_info, answer, logs, page)
    resume_path: str | None = None,
) -> str:
    """
    Click Submit, detect validation errors, re-fill highlighted fields,
    and retry up to MAX_SUBMIT_RETRIES times.

    `refill_fn` is a callback with signature:
        refill_fn(frame, field_info: dict, answer: str, logs: list, page: Page)
    It should call the appropriate fill helper (_fill_react_select, frame.fill, etc.)
    and is supplied by greenhouse.py to avoid circular imports.

    Returns the final outcome string: 'AUTO_APPLIED' | 'VALIDATION_FAILED' | 'FAILED'
    """
    submit_btn_sel = "input#submit_app, button#submit_app, button[type='submit']"

    for attempt in range(1, MAX_SUBMIT_RETRIES + 2):  # 1, 2, 3 — first is the real submit
        # ── Layer 2: pre-submit scan on every attempt ───────────────────
        if attempt > 1:
            logs.append(f"[submit_guard] Pre-submit scan (attempt {attempt})...")

        empty_fields = _scan_required_empty(frame, logs)
        if empty_fields:
            logs.append(
                f"[submit_guard] Found {len(empty_fields)} empty required field(s) before submit: "
                f"{[f['label'] for f in empty_fields]}"
            )
            for ef in empty_fields:
                ftype = ef.get("type")

                # Resume re-upload
                if ftype == "file" and resume_path:
                    try:
                        frame.locator("input[type='file']").first.set_input_files(resume_path)
                        logs.append(f"[submit_guard] Re-uploaded resume for empty file field")
                    except Exception as exc:
                        logs.append(f"[submit_guard] Resume re-upload failed: {exc}")
                    continue

                # Look up a safe default for this field
                field_info = next(
                    (q for q in questions_data
                     if q.get("id") == ef["id"] or q.get("name") == ef["name"]),
                    ef  # fallback: use the empty-field descriptor itself
                )
                opts = field_info.get("options") if isinstance(field_info.get("options"), list) else None
                # For checkbox groups options is a list of dicts; extract labels
                opt_labels = None
                if opts:
                    if opts and isinstance(opts[0], dict):
                        opt_labels = [o.get("label", "") for o in opts]
                    else:
                        opt_labels = opts

                default = get_safe_default(ef["label"], opt_labels)
                if default:
                    logs.append(
                        f"[submit_guard] Applying safe default for '{ef['label']}' "
                        f"(id={ef['id']}): '{default}'"
                    )
                    try:
                        refill_fn(frame, field_info, default, logs, page)
                    except Exception as exc:
                        logs.append(f"[submit_guard] refill_fn error for '{ef['id']}': {exc}")
                else:
                    logs.append(
                        f"[submit_guard] No safe default for required field '{ef['label']}' "
                        f"(id={ef['id']}) — leaving blank"
                    )

        # ── Click Submit ────────────────────────────────────────────────
        try:
            btn = frame.locator(submit_btn_sel)
            if btn.count() == 0:
                logs.append("Submit button not found.")
                return "FAILED"
            btn.first.click()
            logs.append(f"Submit clicked (attempt {attempt}).")
        except Exception as exc:
            logs.append(f"Submit click error (attempt {attempt}): {exc}")
            return "FAILED"

        outcome = _wait_outcome(page, frame, pre_submit_url, logs)

        if outcome == "AUTO_APPLIED":
            return "AUTO_APPLIED"

        if outcome == "VALIDATION_FAILED" and attempt <= MAX_SUBMIT_RETRIES:
            errors = _parse_validation_errors(frame, logs)
            logs.append(
                f"[submit_guard] Retry {attempt}/{MAX_SUBMIT_RETRIES}: "
                f"validation errors = {errors}"
            )
            # Map error messages back to field IDs for targeted re-fill
            for err_text in errors:
                err_lower = err_text.lower()
                # Find the field whose label is most mentioned in the error
                matched_field = None
                for q in questions_data:
                    lbl = (q.get("label") or "").lower()
                    if lbl and lbl in err_lower:
                        matched_field = q
                        break
                if not matched_field:
                    continue
                opts = matched_field.get("options")
                opt_labels = None
                if opts and isinstance(opts[0], dict):
                    opt_labels = [o.get("label", "") for o in opts]
                elif opts:
                    opt_labels = opts
                default = get_safe_default(matched_field.get("label", ""), opt_labels)
                if default:
                    logs.append(
                        f"[submit_guard] Retry re-fill '{matched_field['label']}' "
                        f"-> '{default}'"
                    )
                    try:
                        refill_fn(frame, matched_field, default, logs, page)
                    except Exception as exc:
                        logs.append(f"[submit_guard] Retry refill error: {exc}")
            frame.wait_for_timeout(800)
            continue  # next attempt

        # FAILED or exhausted retries
        return outcome

    # Exhausted all retries
    logs.append(f"[submit_guard] All {MAX_SUBMIT_RETRIES} retries exhausted — marking VALIDATION_FAILED")
    return "VALIDATION_FAILED"
