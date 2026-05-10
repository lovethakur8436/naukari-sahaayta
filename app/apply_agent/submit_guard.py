"""
submit_guard.py
───────────────
Three-layer safety net around form submission:

  Layer 1 — SAFE_DEFAULTS
  Layer 2 — Pre-Submit Scan (_scan_required_empty)
  Layer 3 — Post-Submit Retry (submit_with_retry)
"""

from __future__ import annotations
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page, Frame

# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — SAFE_DEFAULTS
# ─────────────────────────────────────────────────────────────────────────────

SAFE_DEFAULTS: list[tuple[str, str | list]] = [
    # FIX: Map to EXACT option text that Stripe/Greenhouse scraped:
    # '5 - 10 years of experience as a software engineer'
    # Candidate has ~5 years (Nov 2018 - May 2026)
    (r"years.{0,20}experience|experience.{0,20}years|how many years",
     ["5 - 10 years of experience as a software engineer",
      "5-10 years", "5+ years", "5 - 10 years", "3-4 years"]),
    (r"level of experience|seniority",                   "Mid-level"),
    (r"specializ|area.{0,15}work|domain",               ["Backend", "Full-Stack"]),
    (r"which.{0,20}area",                               ["Backend"]),
    (r"language|tech.?stack|primary.{0,10}language",    ["Python", "Java"]),
    (r"pronoun",                                         "he/him/his"),
    (r"portfolio|personal.?site|website",               "https://github.com/lovethakur8436"),
    (r"cover.?letter|why.{0,20}(us|company|role)|motivation|interested in",
     "I am excited about this opportunity and believe my skills in backend engineering "
     "and API development align well with the role."),
    (r"salary|compensation|expected.{0,10}pay",          "Open to discussion"),
    (r"notice.?period|start.?date|when.{0,15}start|available", "30 days"),
    (r"relocat",                                         "Yes"),
    (r"sponsor|visa",                                    "No"),
    (r"authoriz.{0,30}work|work.{0,30}authoriz|eligible.{0,20}work",
     ["Yes", "Yes, I am authorized", "Authorized to work"]),
    (r"remote|work.{0,10}(from|preference|location)|plan.{0,10}work.{0,10}remote",
     "__REMOTE_PICK__"),
    (r"how.{0,20}hear|referr|source",                    "LinkedIn"),
    (r"preferred.{0,15}contact|best.{0,10}way",          "Email"),
    # FIX: Use real employer name, not N/A
    (r"current.{0,15}(company|employer)|previous.{0,15}(company|employer)|who is your current",
     "Wells Fargo"),
    (r"job.?title|current.{0,15}title|previous.{0,15}title",
     "Software Engineer"),
    (r"(previously|ever|before).{0,30}(employ|work).{0,30}(stripe|company|us|affiliate)"
     r"|(employ|work).{0,30}(stripe|company|us|affiliate).{0,30}(previously|before|ever)",
     ["No", "No, I have not"]),
    (r"school|universit|college|institution|attended",
     "Lovely Professional University"),
    (r"degree|qualification|highest.{0,15}education",
     ["Bachelor", "Bachelor's", "B.Tech", "Bachelor of Technology",
      "Bachelors", "B.E.", "B.Sc", "Undergraduate"]),
    # location city — plain text, not react-select
    (r"location.{0,15}city|city.{0,15}reside|current.{0,15}city|location \(city\)",
     "Hyderabad"),
    # country of residence
    (r"country.{0,30}(reside|live|located|based)"
     r"|(reside|live|located|based).{0,30}country"
     r"|country where you currently",
     ["India", "India (IN)", "IN"]),
    # countries anticipating working in — checkbox group, India only
    (r"countr.{0,30}(anticipat|plan|intend).{0,30}work"
     r"|(anticipat|plan|intend).{0,30}countr.{0,30}work"
     r"|countr.{0,10}(you|to).{0,10}(work|apply)",
     ["India", "India (IN)", "IN"]),
    (r"whatsapp|opt.{0,10}(in|out).{0,20}(message|receiv|sms)"
     r"|(message|receiv|sms).{0,20}opt.{0,10}(in|out)",
     ["No", "No, I do not opt-in", "I do not consent"]),
    (r"brighthire|record.{0,30}(interview|transcrib)"
     r"|(interview|transcrib).{0,30}record"
     r"|consent.{0,20}(record|transcrib|interview)",
     ["Yes", "I consent", "Yes, I consent"]),
    (r"non.?compete|employment.?agreement|reasonable.{0,20}accommodation",
     ["No", "No, I do not"]),
    # FIX: 'If located in the US' is a plain text field — candidate answers N/A
    (r"(city|state).{0,15}(reside|located).{0,15}us"
     r"|if located in the us",
     "N/A"),
    (r"linkedin",                                        "linkedin.com/in/luv-kumar-06975b175"),
    (r"github",                                          "https://github.com/lovethakur8436"),
]


_REMOTE_SENTINEL = "__REMOTE_PICK__"

_REMOTE_PREFERENCE_KEYWORDS = [
    "yes", "remote", "open to remote", "open to hybrid", "hybrid",
    "flexible", "either", "both",
]


def pick_best_remote_option(options: list[str]) -> str | None:
    if not options:
        return None
    opts_lower = [(o.lower(), o) for o in options]
    for kw in _REMOTE_PREFERENCE_KEYWORDS:
        for ol, o in opts_lower:
            if kw in ol:
                return o
    return options[0]


def get_safe_default(label: str, options: list[str] | None = None) -> str | None:
    label_lower = (label or "").lower()
    for pattern, default in SAFE_DEFAULTS:
        if re.search(pattern, label_lower):
            if default == _REMOTE_SENTINEL:
                if options:
                    resolved = pick_best_remote_option(options)
                    if resolved:
                        return resolved
                return None

            if isinstance(default, list):
                if options:
                    opts_lower = {o.lower(): o for o in options}
                    for d in default:
                        if d.lower() in opts_lower:
                            return opts_lower[d.lower()]
                    for d in default:
                        for ol, o in opts_lower.items():
                            if d.lower() in ol:
                                return o
                return default[0]
            return default
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — Pre-Submit Empty-Required Field Scan
# ─────────────────────────────────────────────────────────────────────────────

_SCAN_REQUIRED_JS = (
    "() => {"
    "  var skip = new Set(['first_name','last_name','email','phone','country']);"
    "  var seenIds = new Set();"
    "  var empty = [];"
    # ── text / textarea ──────────────────────────────────────────────────
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
    "    if (seenIds.has(el.id)) return;"
    "    seenIds.add(el.id);"
    "    if (el.id === 'candidate-location') { empty.push({ id: el.id, name: el.name, label: 'Location (City)', type: 'location-typeahead' }); return; }"
    "    var lbl = (document.querySelector('label[for=\"' + el.id + '\"]') || {}).innerText || '';"
    "    lbl = lbl.replace('*','').trim() || el.placeholder || el.name || el.id;"
    "    empty.push({ id: el.id, name: el.name, label: lbl, type: 'text' });"
    "  });"
    # ── react-select combobox ────────────────────────────────────────────
    "  document.querySelectorAll('input[role=\"combobox\"]').forEach(function(el) {"
    "    if (skip.has(el.id)) return;"
    "    if (el.offsetWidth === 0 && el.offsetHeight === 0) return;"
    "    if (seenIds.has(el.id)) return;"
    "    var req = el.required"
    "      || el.getAttribute('aria-required') === 'true'"
    "      || !!(document.querySelector('label[for=\"' + el.id + '\"]')"
    "            && document.querySelector('label[for=\"' + el.id + '\"]').innerText.includes('*'));"
    "    if (!req) return;"
    "    var control = el.closest('.select__control');"
    "    if (!control) return;"
    "    var hasValue = control.querySelector('.select__single-value, .select__multi-value');"
    "    if (hasValue) return;"
    "    seenIds.add(el.id);"
    "    var lbl = (document.querySelector('label[for=\"' + el.id + '\"]') || {}).innerText || '';"
    "    lbl = lbl.replace('*','').trim() || el.name || el.id;"
    "    empty.push({ id: el.id, name: el.name, label: lbl, type: 'react-select' });"
    "  });"
    # ── file input ───────────────────────────────────────────────────────
    "  document.querySelectorAll('input[type=\"file\"]').forEach(function(el) {"
    "    if (el.offsetWidth === 0 && el.offsetHeight === 0) return;"
    "    var req = el.required || el.getAttribute('aria-required') === 'true';"
    "    if (!req) return;"
    "    if (el.files && el.files.length > 0) return;"
    "    empty.push({ id: el.id, name: el.name, label: 'Resume/CV', type: 'file' });"
    "  });"
    # ── checkbox groups ──────────────────────────────────────────────────
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
    try:
        results = frame.evaluate(_SCAN_REQUIRED_JS)
        seen: set[str] = set()
        deduped: list[dict] = []
        for ef in (results or []):
            fid = ef.get("id", "")
            if fid and fid in seen:
                logs.append(f"[submit_guard] dedup: skipping duplicate field id '{fid}'")
                continue
            if fid:
                seen.add(fid)
            deduped.append(ef)
        return deduped
    except Exception as exc:
        logs.append(f"[submit_guard] _scan_required_empty error: {exc}")
        return []


def _close_open_dropdowns(frame, page, logs: list) -> None:
    """Press Escape + blur to close any stray open react-select listbox, then wait."""
    try:
        try:
            if page:
                page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            frame.locator("body").click(position={"x": 5, "y": 5}, timeout=500, force=True)
        except Exception:
            pass
        try:
            frame.wait_for_selector(
                "div[role='listbox']:visible",
                state="hidden",
                timeout=600,
            )
        except Exception:
            pass
        frame.wait_for_timeout(150)
    except Exception:
        pass


def _get_react_select_current_value(frame, field_id: str) -> str:
    js = f"""
    () => {{
        var inp = document.getElementById('{field_id}');
        if (!inp) return '';
        var ctrl = inp.closest('.select__control');
        if (!ctrl) return '';
        var sv = ctrl.querySelector('.select__single-value');
        return sv ? sv.innerText.trim() : '';
    }}
    """
    try:
        return frame.evaluate(js) or ""
    except Exception:
        return ""


def _fill_react_select_isolated(
    frame,
    page,
    field_id: str,
    value: str,
    logs: list,
) -> bool:
    _close_open_dropdowns(frame, page, logs)

    def _attempt_fill() -> bool:
        try:
            inp_locator = frame.locator(f"input#{field_id}")
            if inp_locator.count() == 0:
                logs.append(f"[guard-isolated] input#{field_id} not found")
                return False

            has_ctrl = frame.evaluate(f"""
            () => {{
                var inp = document.getElementById('{field_id}');
                if (!inp) return false;
                return !!inp.closest('.select__control');
            }}
            """)
            if not has_ctrl:
                logs.append(f"[guard-isolated] no .select__control for #{field_id}")
                return False

            frame.evaluate(f"""
            () => {{
                var inp = document.getElementById('{field_id}');
                if (inp) {{
                    var ctrl = inp.closest('.select__control');
                    if (ctrl) ctrl.click();
                }}
            }}
            """)
            frame.wait_for_timeout(350)

            inp_locator.fill("")
            inp_locator.type(value[:12], delay=40)
            frame.wait_for_timeout(450)

            opts_js = f"""
            () => {{
                var inp = document.getElementById('{field_id}');
                if (!inp) return [];
                var listbox_id = inp.getAttribute('aria-controls');
                var listbox = listbox_id ? document.getElementById(listbox_id) : null;
                if (listbox) {{
                    var container = inp.closest('.select__container, .select__control');
                    if (container && !container.contains(listbox) && !document.getElementById(listbox_id)) {{
                        listbox = null;
                    }}
                }}
                if (!listbox) {{
                    var parentContainer = inp.closest('[class*="select"]');
                    if (parentContainer) {{
                        listbox = parentContainer.querySelector("div[role='listbox']");
                    }}
                }}
                if (!listbox) return [];
                var opts = Array.from(listbox.querySelectorAll("div[role='option']")).map(o => o.innerText.trim());
                return opts.filter(o => o.length > 0);
            }}
            """
            options = frame.evaluate(opts_js)

            if not options:
                logs.append(
                    f"[guard-isolated] no options in field-scoped listbox for "
                    f"#{field_id} after typing '{value}' — closing and aborting"
                )
                _close_open_dropdowns(frame, page, logs)
                return False

            logs.append(f"[guard-isolated] #{field_id}: {len(options)} options, want '{value}'")

            value_lower = value.lower()
            matched = None
            for opt in options:
                if opt.lower() == value_lower:
                    matched = opt
                    break
            if not matched:
                for opt in options:
                    if opt.lower().startswith(value_lower):
                        matched = opt
                        break
            if not matched:
                for opt in options:
                    if value_lower in opt.lower():
                        matched = opt
                        break

            if not matched:
                logs.append(f"[guard-isolated] no match for '{value}' in {options[:5]}")
                _close_open_dropdowns(frame, page, logs)
                return False

            click_js = f"""
            (matchedText) => {{
                var inp = document.getElementById('{field_id}');
                if (!inp) return false;
                var listbox_id = inp.getAttribute('aria-controls');
                var listbox = listbox_id ? document.getElementById(listbox_id) : null;
                if (!listbox) {{
                    var parentContainer = inp.closest('[class*="select"]');
                    if (parentContainer) listbox = parentContainer.querySelector("div[role='listbox']");
                }}
                if (!listbox) return false;
                var opts = listbox.querySelectorAll("div[role='option']");
                for (var o of opts) {{
                    if (o.innerText.trim() === matchedText) {{
                        o.click();
                        return true;
                    }}
                }}
                return false;
            }}
            """
            clicked = frame.evaluate(click_js, matched)

            frame.wait_for_timeout(700)
            _close_open_dropdowns(frame, page, logs)

            if clicked:
                logs.append(f"[guard-isolated] clicked '{matched}' for #{field_id}")
                return True
            else:
                logs.append(f"[guard-isolated] click failed for '{matched}' in #{field_id}")
                return False

        except Exception as exc:
            logs.append(f"[guard-isolated] error for #{field_id}: {exc}")
            _close_open_dropdowns(frame, page, logs)
            return False

    ok = _attempt_fill()
    if not ok:
        return False

    frame.wait_for_timeout(350)
    current = _get_react_select_current_value(frame, field_id)
    if current and value.lower() in current.lower():
        logs.append(f"[guard-isolated] verified #{field_id} = '{current}'")
        return True

    logs.append(
        f"[guard-isolated] #{field_id} value cleared after fill (got '{current}') — retrying once"
    )
    _close_open_dropdowns(frame, page, logs)
    frame.wait_for_timeout(400)
    ok2 = _attempt_fill()
    if ok2:
        frame.wait_for_timeout(350)
        current2 = _get_react_select_current_value(frame, field_id)
        logs.append(f"[guard-isolated] retry result for #{field_id}: '{current2}'")
    return ok2


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


def _guard_fill_field(
    frame,
    page,
    ef: dict,
    field_info: dict,
    default: str,
    logs: list,
    refill_fn,
) -> None:
    ftype = ef.get("type")
    field_id = ef.get("id", "")

    _close_open_dropdowns(frame, page, logs)

    if ftype == "react-select" and field_id:
        success = _fill_react_select_isolated(frame, page, field_id, default, logs)
        if not success:
            logs.append(f"[guard] isolated fill failed for #{field_id}, falling back to refill_fn")
            _close_open_dropdowns(frame, page, logs)
            try:
                refill_fn(frame, field_info, default, logs, page)
            except Exception as exc:
                logs.append(f"[guard] refill_fn fallback error for #{field_id}: {exc}")
    elif ftype == "text" and field_id:
        # FIX: plain text fields must be filled directly — not via react-select path
        try:
            el = frame.locator(f"[id='{field_id}']")
            el.fill(str(default), timeout=5000)
            el.evaluate("el => el.dispatchEvent(new Event('blur', { bubbles: true }))")
            logs.append(f"[guard] text fill '{field_id}' = '{default}'")
        except Exception as exc:
            logs.append(f"[guard] text fill error for '{field_id}': {exc}")
    else:
        try:
            refill_fn(frame, field_info, default, logs, page)
        except Exception as exc:
            logs.append(f"[guard] refill_fn error for '{field_id}': {exc}")

    _close_open_dropdowns(frame, page, logs)


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

                if ftype == "location-typeahead" or ef.get("id") == "candidate-location":
                    try:
                        city = candidate_profile.get("location", "Hyderabad, India").split(",")[0].strip()
                        refill_fn(
                            frame,
                            {"id": "candidate-location", "type": "text", "name": ""},
                            city,
                            logs,
                            page,
                        )
                    except Exception as exc:
                        logs.append(f"[submit_guard] location typeahead refill error: {exc}")
                    continue

                field_info = next(
                    (q for q in questions_data
                     if q.get("id") == ef["id"] or q.get("name") == ef["name"]),
                    ef
                )

                opts = field_info.get("options") if isinstance(field_info.get("options"), list) else None
                opt_labels: list[str] | None = None
                if opts:
                    if isinstance(opts[0], dict):
                        opt_labels = [o.get("label", "") for o in opts]
                    else:
                        opt_labels = list(opts)

                # For years-of-experience, scrape live options so we can exact-match
                label_lower = (ef.get("label") or "").lower()
                is_years_exp = bool(re.search(r"years.{0,20}experience|how many years", label_lower))
                if is_years_exp and ef.get("type") == "react-select" and not opt_labels:
                    try:
                        from app.apply_agent.greenhouse import _get_react_select_options
                        live_opts = _get_react_select_options(frame, ef["id"], page=page)
                        if live_opts:
                            opt_labels = live_opts
                            logs.append(
                                f"[submit_guard] Scraped years-exp options for '{ef['id']}': {live_opts}"
                            )
                    except Exception as scrape_err:
                        logs.append(f"[submit_guard] Live option scrape failed: {scrape_err}")

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
                    _guard_fill_field(frame, page, ef, field_info, default, logs, refill_fn)
                else:
                    logs.append(
                        f"[submit_guard] No safe default for required field '{ef['label']}' "
                        f"(id={ef['id']}) — leaving blank"
                    )

        _close_open_dropdowns(frame, page, logs)

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

                if matched_field.get("id") == "candidate-location":
                    try:
                        city = candidate_profile.get("location", "Hyderabad, India").split(",")[0].strip()
                        refill_fn(
                            frame,
                            {"id": "candidate-location", "type": "text", "name": ""},
                            city,
                            logs,
                            page,
                        )
                    except Exception as exc:
                        logs.append(f"[submit_guard] Retry location refill error: {exc}")
                    continue

                opts = matched_field.get("options")
                opt_labels = None
                if opts and isinstance(opts[0], dict):
                    opt_labels = [o.get("label", "") for o in opts]
                elif opts:
                    opt_labels = list(opts)

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
                    _guard_fill_field(frame, page, matched_field, matched_field, default, logs, refill_fn)
            frame.wait_for_timeout(800)
            continue

        return outcome

    logs.append(f"[submit_guard] All {MAX_SUBMIT_RETRIES} retries exhausted — marking VALIDATION_FAILED")
    return "VALIDATION_FAILED"
