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
    # ── Experience / seniority ────────────────────────────────────────────
    (r"years.{0,20}experience|experience.{0,20}years",  "3-4 years"),
    (r"how many years",                                  "3-4 years"),
    (r"level of experience|seniority",                   "Mid-level"),

    # ── Specialization / domain checkboxes ───────────────────────────────
    (r"specializ|area.{0,15}work|domain",               ["Backend", "Full-Stack"]),
    (r"which.{0,20}area",                               ["Backend"]),

    # ── Programming languages checkboxes ─────────────────────────────────
    (r"language|tech.?stack|primary.{0,10}language",    ["Python", "Java"]),

    # ── Pronouns ──────────────────────────────────────────────────────────
    (r"pronoun",                                         "he/him/his"),

    # ── Portfolio / personal site ─────────────────────────────────────────
    (r"portfolio|personal.?site|website",               "https://github.com/lovethakur8436"),

    # ── Cover letter / motivation ─────────────────────────────────────────
    (r"cover.?letter|why.{0,20}(us|company|role)|motivation|interested in",
     "I am excited about this opportunity and believe my skills in backend engineering "
     "and API development align well with the role."),

    # ── Salary / compensation ─────────────────────────────────────────────
    (r"salary|compensation|expected.{0,10}pay",          "Open to discussion"),

    # ── Notice period / availability ──────────────────────────────────────
    (r"notice.?period|start.?date|when.{0,15}start|available", "30 days"),

    # ── Relocation ────────────────────────────────────────────────────────
    (r"relocat",                                         "Yes"),

    # ── Visa / sponsorship — candidate does NOT need sponsorship ─────────
    (r"sponsor|visa",                                    "No"),

    # ── Work authorization ────────────────────────────────────────────────
    (r"authoriz.{0,30}work|work.{0,30}authoriz|eligible.{0,20}work",
     ["Yes", "Yes, I am authorized", "Authorized to work"]),

    # ── Remote preference ────────────────────────────────────────────────
    # NOTE: We list many variants; _pick_best_remote_option() handles the
    # actual option matching at fill-time to avoid hardcoding one string.
    (r"remote|work.{0,10}(from|preference|location)|plan.{0,10}work.{0,10}remote",
     "__REMOTE_PICK__"),

    # ── Referral / how did you hear ───────────────────────────────────────
    (r"how.{0,20}hear|referr|source",                    "LinkedIn"),

    # ── Preferred contact / communication ────────────────────────────────
    (r"preferred.{0,15}contact|best.{0,10}way",          "Email"),

    # ── Current / previous employer ───────────────────────────────────────
    (r"current.{0,15}(company|employer)|previous.{0,15}(company|employer)|who is your current",
     "N/A"),

    # ── Current / previous job title ─────────────────────────────────────
    (r"job.?title|current.{0,15}title|previous.{0,15}title",
     "Software Engineer"),

    # ── Previously employed at THIS company ───────────────────────────────
    (r"(previously|ever|before).{0,30}(employ|work).{0,30}(stripe|company|us|affiliate)"
     r"|(employ|work).{0,30}(stripe|company|us|affiliate).{0,30}(previously|before|ever)",
     ["No", "No, I have not"]),

    # ── Education: school attended ───────────────────────────────────────
    (r"school|universit|college|institution|attended",
     "Maharshi Dayanand University"),

    # ── Education: degree obtained ────────────────────────────────────────
    (r"degree|qualification|highest.{0,15}education",
     ["Bachelor", "Bachelor's", "B.Tech", "Bachelor of Technology",
      "Bachelors", "B.E.", "B.Sc", "Undergraduate"]),

    # ── Location / city ──────────────────────────────────────────────────
    (r"location.{0,15}city|city.{0,15}reside|current.{0,15}city|location \(city\)",
     "Hyderabad"),

    # ── Country of residence (Stripe custom questions) ────────────────────
    (r"country.{0,30}(reside|live|located|based)"
     r"|(reside|live|located|based).{0,30}country"
     r"|country where you currently",
     ["India", "India (IN)", "IN"]),

    # ── Countries anticipating working in (checkbox group) ────────────────
    (r"countr.{0,30}(anticipat|plan|intend).{0,30}work"
     r"|(anticipat|plan|intend).{0,30}countr.{0,30}work"
     r"|countr.{0,10}(you|to).{0,10}(work|apply)",
     ["India", "India (IN)", "IN"]),

    # ── WhatsApp / messaging opt-in ───────────────────────────────────────
    (r"whatsapp|opt.{0,10}(in|out).{0,20}(message|receiv|sms)"
     r"|(message|receiv|sms).{0,20}opt.{0,10}(in|out)",
     ["No", "No, I do not opt-in", "I do not consent"]),

    # ── BrightHire / interview recording consent ──────────────────────────
    (r"brighthire|record.{0,30}(interview|transcrib)"
     r"|(interview|transcrib).{0,30}record"
     r"|consent.{0,20}(record|transcrib|interview)",
     ["Yes", "I consent", "Yes, I consent"]),

    # ── Generic yes/no ────────────────────────────────────────────────────
    # (catch-all for employment agreements, non-compete, accommodation)
    (r"non.?compete|employment.?agreement|reasonable.?accommodation",
     ["No", "No, I do not"]),

    # ── US city/state (skip if not in US) ────────────────────────────────
    (r"(city|state).{0,15}(reside|located).{0,15}us"
     r"|if located in the us",
     "N/A"),

    # ── LinkedIn fallback ─────────────────────────────────────────────────
    (r"linkedin",                                        "linkedin.com/in/luv-kumar-06975b175"),

    # ── GitHub fallback ───────────────────────────────────────────────────
    (r"github",                                          "https://github.com/lovethakur8436"),
]


# Sentinel value: when SAFE_DEFAULTS returns this, the caller must
# enumerate the actual dropdown options and pick the best remote choice.
_REMOTE_SENTINEL = "__REMOTE_PICK__"

# Ordered preference list for remote-preference dropdowns
_REMOTE_PREFERENCE_KEYWORDS = [
    "yes", "remote", "open to remote", "open to hybrid", "hybrid",
    "flexible", "either", "both",
]


def pick_best_remote_option(options: list[str]) -> str | None:
    """
    Given the actual dropdown options for a remote-preference question,
    return the best matching option text.
    Prefers options containing 'yes', 'remote', 'hybrid', 'open', 'flexible'.
    Falls back to the first available option.
    """
    if not options:
        return None
    opts_lower = [(o.lower(), o) for o in options]
    for kw in _REMOTE_PREFERENCE_KEYWORDS:
        for ol, o in opts_lower:
            if kw in ol:
                return o
    # Last resort: return first non-empty option
    return options[0]


def get_safe_default(label: str, options: list[str] | None = None) -> str | None:
    """
    Return a safe default answer for a required field whose label matches
    one of the SAFE_DEFAULTS patterns.

    If the default is a list (checkbox group / multi-option), intersect with
    available `options` and return the first match; fall back to the first
    list item.

    Returns None if no pattern matches.
    Special sentinel '__REMOTE_PICK__' is returned as-is; callers must
    call pick_best_remote_option() to resolve it.
    """
    label_lower = (label or "").lower()
    for pattern, default in SAFE_DEFAULTS:
        if re.search(pattern, label_lower):
            if default == _REMOTE_SENTINEL:
                if options:
                    resolved = pick_best_remote_option(options)
                    if resolved:
                        return resolved
                return None  # no options available yet; skip for now

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

_ERROR_FIELD_SELECTORS = (
    ".field_with_errors input, .field_with_errors textarea, "
    "[aria-invalid='true'], "
    "input.error, textarea.error"
)

# JS is stored as a plain string to avoid Python-triple-quote / JS-backtick
# conflicts that caused 'SyntaxError: Unexpected end of input' at runtime.
_SCAN_REQUIRED_JS = (
    "() => {"
    "  const skip = new Set(['first_name','last_name','email','phone','country']);"
    "  const empty = [];"
    # text / textarea
    "  document.querySelectorAll("
    "    'input[type=\"text\"]:not([hidden]), textarea:not([hidden])'"
    "  ).forEach(function(el) {"
    "    if (skip.has(el.id)) return;"
    "    if (el.offsetWidth === 0 && el.offsetHeight === 0) return;"
    "    var req = el.required"
    "      || el.getAttribute('aria-required') === 'true'"
    "      || !!(document.querySelector('label[for=\"' + el.id + '\"]')"
    "            && document.querySelector('label[for=\"' + el.id + '\"]').innerText.includes('*'));"
    "    if (!req) return;"
    "    if ((el.value || '').trim() !== '') return;"
    "    var lbl = (document.querySelector('label[for=\"' + el.id + '\"]') || {}).innerText || '';"
    "    lbl = lbl.replace('*','').trim() || el.placeholder || el.name || el.id;"
    "    empty.push({ id: el.id, name: el.name, label: lbl, type: 'text' });"
    "  });"
    # react-select combobox
    "  document.querySelectorAll('input[role=\"combobox\"]').forEach(function(el) {"
    "    if (skip.has(el.id)) return;"
    "    if (el.offsetWidth === 0 && el.offsetHeight === 0) return;"
    "    var req = el.required"
    "      || el.getAttribute('aria-required') === 'true'"
    "      || !!(document.querySelector('label[for=\"' + el.id + '\"]')"
    "            && document.querySelector('label[for=\"' + el.id + '\"]').innerText.includes('*'));"
    "    if (!req) return;"
    "    var control = el.closest('.select__control');"
    "    if (!control) return;"
    "    var placeholder = control.querySelector('.select__placeholder');"
    "    if (!placeholder) return;"
    "    var lbl = (document.querySelector('label[for=\"' + el.id + '\"]') || {}).innerText || '';"
    "    lbl = lbl.replace('*','').trim() || el.name || el.id;"
    "    empty.push({ id: el.id, name: el.name, label: lbl, type: 'react-select' });"
    "  });"
    # file input
    "  document.querySelectorAll('input[type=\"file\"]').forEach(function(el) {"
    "    if (el.offsetWidth === 0 && el.offsetHeight === 0) return;"
    "    var req = el.required || el.getAttribute('aria-required') === 'true';"
    "    if (!req) return;"
    "    if (el.files && el.files.length > 0) return;"
    "    empty.push({ id: el.id, name: el.name, label: 'Resume/CV', type: 'file' });"
    "  });"
    # checkbox groups
    "  var groups = {};"
    "  document.querySelectorAll('input[type=\"checkbox\"]').forEach(function(el) {"
    "    if (el.offsetWidth === 0 && el.offsetHeight === 0) return;"
    "    var req = el.required || el.getAttribute('aria-required') === 'true';"
    "    if (!req) return;"
    "    var grp = el.name || el.id;"
    "    if (!groups[grp]) groups[grp] = { any_checked: false, id: el.id, name: grp, label: grp };"
    "    if (el.checked) groups[grp].any_checked = true;"
    "    var lbl = (document.querySelector('label[for=\"' + el.id + '\"]') || {}).innerText || '';"
    "    lbl = lbl.replace('*','').trim();"
    "    if (lbl && !groups[grp].label_text) groups[grp].label_text = lbl;"
    "  });"
    "  Object.values(groups).forEach(function(g) {"
    "    if (!g.any_checked)"
    "      empty.push({ id: g.id, name: g.name, label: g.label_text || g.name, type: 'checkbox' });"
    "  });"
    "  return empty;"
    "}"
)


def _scan_required_empty(frame, logs: list) -> list[dict]:
    """
    Walk all visible required fields in `frame`.
    Return a list of dicts describing fields that appear empty so the caller
    can attempt a re-fill before hitting Submit.
    """
    try:
        return frame.evaluate(_SCAN_REQUIRED_JS)
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
        try:
            cur = page.url
            if "/apply" in pre_url.lower() and "/apply" not in cur.lower():
                logs.append(f"Submit confirmed: URL changed to '{cur}'")
                return "AUTO_APPLIED"
        except Exception:
            pass

        for sel in _CONFIRM_SELECTORS:
            try:
                if frame.locator(sel).count() > 0:
                    logs.append(f"Submit confirmed: found '{sel}'")
                    return "AUTO_APPLIED"
            except Exception:
                pass

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
    refill_fn,
    resume_path: str | None = None,
) -> str:
    """
    Click Submit, detect validation errors, re-fill highlighted fields,
    and retry up to MAX_SUBMIT_RETRIES times.
    """
    submit_btn_sel = "input#submit_app, button#submit_app, button[type='submit']"

    for attempt in range(1, MAX_SUBMIT_RETRIES + 2):
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

                if ftype == "file" and resume_path:
                    try:
                        frame.locator("input[type='file']").first.set_input_files(resume_path)
                        logs.append("[submit_guard] Re-uploaded resume for empty file field")
                    except Exception as exc:
                        logs.append(f"[submit_guard] Resume re-upload failed: {exc}")
                    continue

                field_info = next(
                    (q for q in questions_data
                     if q.get("id") == ef["id"] or q.get("name") == ef["name"]),
                    ef
                )

                # For remote question: enumerate live options before calling get_safe_default
                opts = field_info.get("options") if isinstance(field_info.get("options"), list) else None
                opt_labels: list[str] | None = None
                if opts:
                    if isinstance(opts[0], dict):
                        opt_labels = [o.get("label", "") for o in opts]
                    else:
                        opt_labels = list(opts)

                # If remote sentinel and no options cached, try to scrape them live
                label_lower = (ef.get("label") or "").lower()
                is_remote_field = bool(re.search(
                    r"remote|work.{0,10}(from|preference|location)|plan.{0,10}work.{0,10}remote",
                    label_lower
                ))
                if is_remote_field and not opt_labels and ef.get("type") == "react-select":
                    try:
                        from app.apply_agent.greenhouse import _get_react_select_options
                        live_opts = _get_react_select_options(frame, ef["id"], page=page)
                        if live_opts:
                            opt_labels = live_opts
                            logs.append(
                                f"[submit_guard] Scraped remote options live for '{ef['id']}': {live_opts}"
                            )
                    except Exception as scrape_err:
                        logs.append(f"[submit_guard] Live option scrape failed: {scrape_err}")

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
            for err_text in errors:
                err_lower = err_text.lower()
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
                    opt_labels = list(opts)

                # scrape live remote options on retry too
                is_remote = bool(re.search(
                    r"remote|work.{0,10}(from|preference|location)|plan.{0,10}work.{0,10}remote",
                    (matched_field.get("label") or "").lower()
                ))
                if is_remote and not opt_labels and matched_field.get("type") == "react-select":
                    try:
                        from app.apply_agent.greenhouse import _get_react_select_options
                        live_opts = _get_react_select_options(frame, matched_field["id"], page=page)
                        if live_opts:
                            opt_labels = live_opts
                    except Exception:
                        pass

                default = get_safe_default(matched_field.get("label", ""), opt_labels)
                if default:
                    logs.append(
                        f"[submit_guard] Retry re-fill '{matched_field['label']}' -> '{default}'"
                    )
                    try:
                        refill_fn(frame, matched_field, default, logs, page)
                    except Exception as exc:
                        logs.append(f"[submit_guard] Retry refill error: {exc}")
            frame.wait_for_timeout(800)
            continue

        return outcome

    logs.append(f"[submit_guard] All {MAX_SUBMIT_RETRIES} retries exhausted — marking VALIDATION_FAILED")
    return "VALIDATION_FAILED"
