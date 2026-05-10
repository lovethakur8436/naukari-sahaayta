from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from app.apply_agent.base import BaseApplyAgent
from app.apply_agent.form_resolver import resolve_apply_form, get_form_frame
from app.apply_agent.submit_guard import (
    get_safe_default,
    pick_best_remote_option,
    _scan_required_empty,
    submit_with_retry,
)
from app.models.application import Application
import json
import os
import glob
import re
from groq import Groq

_MAX_OPTIONS_FOR_LLM = 40


def _safe_css_id(field_id: str) -> str:
    return f"[id='{field_id}']"


# ─────────────────────────────────────────────────────────────────────────────
# Resume path resolver
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_resume_path(application: Application, logs: list) -> str | None:
    tailored = getattr(application, 'tailored_resume_pdf_path', None)
    if tailored and os.path.exists(tailored):
        logs.append(f"Resume: using tailored PDF '{tailored}'")
        return tailored
    if tailored:
        logs.append(f"Resume: tailored path '{tailored}' does not exist — trying fallbacks")

    base = "data/base_resume.pdf"
    if os.path.exists(base):
        logs.append(f"Resume: tailored PDF missing, falling back to base resume '{base}'")
        return base

    candidates = sorted(glob.glob("data/resume_*.pdf"), key=os.path.getmtime, reverse=True)
    if candidates:
        logs.append(f"Resume: no base resume found, using most-recent compiled PDF '{candidates[0]}'")
        return candidates[0]

    logs.append(
        "Resume: WARNING — no PDF found (tailored missing, no base_resume.pdf, no data/resume_*.pdf). "
        "Skipping upload — application will likely be rejected."
    )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# react-select helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_react_select_options(frame, field_id: str, page: Page = None) -> list[str]:
    control_div = frame.locator(
        f"xpath=//input[@id='{field_id}']/ancestor::div[contains(@class,'select__control')]"
    )
    try:
        control_div.click(timeout=4000)
        frame.wait_for_timeout(400)
    except Exception:
        return []

    options = frame.locator(f"[id^='react-select-{field_id}-option']")
    if options.count() == 0:
        options = frame.locator("div[role='option']:visible")

    texts = []
    for i in range(options.count()):
        try:
            texts.append(options.nth(i).inner_text().strip())
        except Exception:
            pass

    try:
        kb = page.keyboard if page is not None else None
        if kb:
            kb.press("Escape")
        else:
            frame.locator("body").click(position={"x": 5, "y": 5}, timeout=1000)
    except Exception:
        pass
    frame.wait_for_timeout(200)
    return texts


def _get_react_select_options_typeahead(
    frame, field_id: str, search_term: str, page: Page = None
) -> list[str]:
    input_el = frame.locator(f"input{_safe_css_id(field_id)}")
    control_div = frame.locator(
        f"xpath=//input[@id='{field_id}']/ancestor::div[contains(@class,'select__control')]"
    )
    try:
        control_div.click(timeout=4000)
        frame.wait_for_timeout(300)
        input_el.type(search_term[:10], delay=60)
        frame.wait_for_timeout(800)
    except Exception:
        try:
            kb = page.keyboard if page is not None else None
            if kb:
                kb.press("Escape")
        except Exception:
            pass
        return []

    options = frame.locator(f"[id^='react-select-{field_id}-option']")
    if options.count() == 0:
        options = frame.locator("div[role='option']:visible")

    texts = []
    for i in range(options.count()):
        try:
            texts.append(options.nth(i).inner_text().strip())
        except Exception:
            pass

    try:
        kb = page.keyboard if page is not None else None
        if kb:
            kb.press("Escape")
        else:
            frame.locator("body").click(position={"x": 5, "y": 5}, timeout=1000)
    except Exception:
        pass
    frame.wait_for_timeout(200)
    return texts


def _fill_react_select(
    frame, field_id: str, answer_text: str, logs: list, page: Page = None
) -> bool:
    if not answer_text or not answer_text.strip():
        logs.append(f"react-select: skipping '{field_id}' — empty answer, leaving blank")
        return False

    try:
        open_listbox = frame.locator("div[role='listbox']:visible")
        if open_listbox.count() > 0:
            try:
                if page:
                    page.keyboard.press("Escape")
                else:
                    frame.locator("body").click(position={"x": 5, "y": 5}, timeout=800)
            except Exception:
                pass
            frame.wait_for_timeout(200)
    except Exception:
        pass

    control_div = frame.locator(
        f"xpath=//input[@id='{field_id}']/ancestor::div[contains(@class,'select__control')]"
    )
    input_el = frame.locator(f"input{_safe_css_id(field_id)}")

    try:
        control_div.click(timeout=4000)
        frame.wait_for_timeout(300)
    except Exception as e:
        logs.append(f"react-select open failed for {field_id}: {e}")
        return False

    first_word = answer_text.split()[0]
    try:
        input_el.type(first_word, delay=50)
        frame.wait_for_timeout(500)
    except Exception as e:
        logs.append(f"react-select type failed for {field_id}: {e}")

    listbox = frame.locator(
        f"[id='react-select-{field_id}-listbox'], div[role='listbox']"
    ).first
    try:
        listbox.wait_for(state="visible", timeout=4000)
    except Exception:
        pass

    options = frame.locator(f"[id^='react-select-{field_id}-option']")
    count = options.count()
    if count == 0:
        options = frame.locator("div[role='option']:visible")
        count = options.count()

    if count == 0:
        logs.append(f"react-select: no options after typing '{first_word}' for {field_id}")
        try:
            kb = page.keyboard if page is not None else None
            if kb:
                kb.press("Escape")
        except Exception:
            pass
        return False

    option_texts = []
    for i in range(count):
        try:
            option_texts.append(options.nth(i).inner_text().strip())
        except Exception:
            option_texts.append("")

    logs.append(
        f"react-select: {count} options for {field_id}, want '{answer_text}': {option_texts}"
    )

    matched_idx = None
    for i, t in enumerate(option_texts):
        if t == answer_text:
            matched_idx = i
            break
    if matched_idx is None:
        for i, t in enumerate(option_texts):
            if t.lower() == answer_text.lower():
                matched_idx = i
                logs.append(f"react-select: case-matched -> '{t}'")
                break
    if matched_idx is None:
        for i, t in enumerate(option_texts):
            if t.lower().startswith(answer_text.lower()):
                matched_idx = i
                logs.append(f"react-select: startswith-matched -> '{t}'")
                break
    if matched_idx is None:
        for i, t in enumerate(option_texts):
            if answer_text.lower() in t.lower():
                matched_idx = i
                logs.append(f"react-select: partial-matched -> '{t}'")
                break

    if matched_idx is None:
        logs.append(f"react-select: NO MATCH for '{answer_text}' in {option_texts}")
        try:
            kb = page.keyboard if page is not None else None
            if kb:
                kb.press("Escape")
        except Exception:
            pass
        return False

    try:
        options.nth(matched_idx).click(timeout=3000)
        frame.wait_for_timeout(400)
        logs.append(f"react-select: clicked '{option_texts[matched_idx]}' for {field_id}")
        try:
            kb = page.keyboard if page is not None else None
            if kb:
                kb.press("Escape")
            else:
                frame.locator("body").click(position={"x": 5, "y": 5}, timeout=800)
        except Exception:
            pass
        frame.wait_for_timeout(150)
        return True
    except Exception as e:
        logs.append(f"react-select: click failed for {field_id}: {e}")
        return False


def _fill_react_select_remote(
    frame, field_id: str, logs: list, page: Page = None
) -> bool:
    live_opts = _get_react_select_options(frame, field_id, page=page)
    if not live_opts:
        logs.append(f"react-select remote: could not enumerate options for '{field_id}'")
        return False
    best = pick_best_remote_option(live_opts)
    if not best:
        logs.append(f"react-select remote: no suitable option found in {live_opts}")
        return False
    logs.append(
        f"react-select remote: picked '{best}' from {live_opts} for '{field_id}'"
    )
    return _fill_react_select(frame, field_id, best, logs, page=page)


def _clean_phone(phone: str) -> str:
    if not phone:
        return phone
    return re.sub(r'^\+?\d{1,3}[-\s]', '', phone.strip())


_LARGE_LIST_PROFILE_KEYS = {
    "country":     "country",
    "nation":      "country",
    "located":     "country",
    "citizenship": "country",
    "nationality": "country",
    "state":       "state",
    "province":    "state",
    "region":      "state",
}


def _pre_resolve_large_options(
    field: dict, candidate_profile: dict, logs: list
) -> str | None:
    opts = field.get("options", [])
    if len(opts) <= _MAX_OPTIONS_FOR_LLM:
        return None

    label_lower = field.get("label", "").lower()
    profile_key = next(
        (v for k, v in _LARGE_LIST_PROFILE_KEYS.items() if k in label_lower), None
    )
    if not profile_key:
        return None

    profile_val = candidate_profile.get(profile_key, "")
    if not profile_val:
        return None

    pv_lower = profile_val.lower()
    for o in opts:
        if o.lower() == pv_lower:
            logs.append(f"pre-resolved '{field['id']}' -> '{o}' (exact, skipping LLM)")
            return o
    for o in opts:
        if o.lower().startswith(pv_lower):
            logs.append(f"pre-resolved '{field['id']}' -> '{o}' (startswith, skipping LLM)")
            return o
    for o in opts:
        if pv_lower in o.lower():
            logs.append(f"pre-resolved '{field['id']}' -> '{o}' (contains, skipping LLM)")
            return o
    return None


def _call_llm_with_fallback(prompt: str, logs: list) -> dict:
    groq_key = os.getenv("GROQ_API_KEY")
    if groq_key:
        try:
            client = Groq(api_key=groq_key)
            resp = client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.3-70b-versatile",
                response_format={"type": "json_object"},
            )
            logs.append("LLM: Groq responded OK")
            return json.loads(resp.choices[0].message.content)
        except Exception as groq_err:
            err_str = str(groq_err)
            is_rate_limit = (
                "429" in err_str
                or "rate_limit_exceeded" in err_str
                or "tokens per day" in err_str
            )
            if is_rate_limit:
                logs.append(
                    f"Groq rate-limit hit: {err_str[:200]}. Falling back to Gemini Flash."
                )
            else:
                logs.append(
                    f"Groq error (non-429): {err_str[:200]}. Falling back to Gemini Flash."
                )
    else:
        logs.append("GROQ_API_KEY not set. Falling back to Gemini Flash.")

    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        raise RuntimeError(
            "Both Groq (rate-limited) and Gemini (no GEMINI_API_KEY set) are unavailable."
        )

    try:
        import google.generativeai as genai
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            generation_config={
                "response_mime_type": "application/json",
                "temperature": 0.1,
            },
        )
        response = model.generate_content(prompt)
        logs.append("LLM: Gemini Flash responded OK")
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = re.sub(r'^```[a-z]*\n?', '', raw)
            raw = re.sub(r'\n?```$', '', raw)
        return json.loads(raw)
    except Exception as gemini_err:
        raise RuntimeError(f"Gemini Flash also failed: {gemini_err}") from gemini_err


# ─────────────────────────────────────────────────────────────────────────────
# JS for scraping custom questions
# ─────────────────────────────────────────────────────────────────────────────

_SCRAPE_QUESTIONS_JS = (
    "() => {"
    "  var fields = [];"
    "  var skip = new Set(['first_name','last_name','email','phone','country']);"
    "  var els = document.querySelectorAll("
    "    'input[role=\"combobox\"],'"
    "    + 'input[type=\"text\"]:not([role=\"combobox\"]):not([hidden]),'"
    "    + 'textarea:not([hidden]),'"
    "    + 'input[type=\"checkbox\"]:not([hidden]),'"
    "    + 'input[type=\"radio\"]:not([hidden])'"
    "  );"
    "  function getGroupLabel(el) {"
    "    var container = el.closest('fieldset, div.field-wrapper, div.field, div.custom_question');"
    "    if (!container) return '';"
    "    var leg = container.querySelector('legend');"
    "    if (leg) return leg.innerText.replace('*','').trim();"
    "    var lbl = container.querySelector('label:not([for])');"
    "    if (lbl) return lbl.innerText.replace('*','').trim();"
    "    var heading = container.querySelector('span.label, p.label, div.label, .question-label');"
    "    if (heading) return heading.innerText.replace('*','').trim();"
    "    return '';"
    "  }"
    "  els.forEach(function(el) {"
    "    if (skip.has(el.id)) return;"
    "    if ((el.className || '').includes('recaptcha')) return;"
    "    if ((el.id || '').includes('recaptcha')) return;"
    "    if (el.offsetWidth === 0 && el.offsetHeight === 0) return;"
    "    var container = el.closest('div.field-wrapper, div.field, div.custom_question');"
    "    var labelEl = document.querySelector('label[for=\"' + el.id + '\"]')"
    "                || (container && container.querySelector('label[for=\"' + el.id + '\"]'));"
    "    var optionLabel = labelEl ? labelEl.innerText.replace('*','').trim() : (el.name || el.id);"
    "    var isRequired = el.required"
    "      || el.getAttribute('aria-required') === 'true'"
    "      || !!(labelEl && labelEl.innerText.includes('*'));"
    "    var fi = { id: el.id, name: el.name || '', label: optionLabel, required: isRequired };"
    "    if (el.getAttribute('role') === 'combobox') {"
    "      fi.type = 'react-select';"
    "      fi.options = [];"
    "    } else if (el.type === 'checkbox') {"
    "      fi.type = 'checkbox';"
    "      var ex = fields.find(function(f) { return f.name === fi.name && f.type === 'checkbox'; });"
    "      if (ex) {"
    "        ex.options.push({ value: el.value, id: el.id, label: optionLabel });"
    "        return;"
    "      }"
    "      var grpLabel = getGroupLabel(el);"
    "      fi.label = grpLabel || optionLabel;"
    "      fi.options = [{ value: el.value, id: el.id, label: optionLabel }];"
    "    } else if (el.type === 'radio') {"
    "      fi.type = 'radio';"
    "      var ex2 = fields.find(function(f) { return f.name === fi.name && f.type === 'radio'; });"
    "      if (ex2) {"
    "        ex2.options.push({ value: el.value, id: el.id, label: optionLabel });"
    "        return;"
    "      }"
    "      var grpLabel2 = getGroupLabel(el);"
    "      fi.label = grpLabel2 || optionLabel;"
    "      fi.options = [{ value: el.value, id: el.id, label: optionLabel }];"
    "    } else {"
    "      fi.type = 'text';"
    "    }"
    "    fields.push(fi);"
    "  });"
    "  return fields;"
    "}"
)


# ─────────────────────────────────────────────────────────────────────────────
# Checkbox fill helper
# ─────────────────────────────────────────────────────────────────────────────

def _fill_checkbox_group(
    frame, field_info: dict, answer_val: str, logs: list
) -> None:
    raw_tokens = [s.strip() for s in str(answer_val).split(",") if s.strip()]
    opts = field_info.get("options", [])
    valid_ids = {o.get("id") for o in opts if o.get("id")}

    for token in raw_tokens:
        target_id: str | None = None

        if token in valid_ids:
            target_id = token
            logs.append(f"Checkbox: exact id match '{token}'")
        else:
            matched = next(
                (o for o in opts if o.get("label", "").lower() == token.lower()),
                None,
            )
            if matched:
                target_id = matched["id"]
                logs.append(
                    f"Checkbox: label-exact matched '{token}' -> id '{target_id}'"
                )
            else:
                matched = next(
                    (
                        o for o in opts
                        if token.lower() in o.get("label", "").lower()
                        or o.get("label", "").lower() in token.lower()
                    ),
                    None,
                )
                if matched:
                    target_id = matched["id"]
                    logs.append(
                        f"Checkbox: label-partial matched '{token}' -> id '{target_id}'"
                    )
                else:
                    logs.append(
                        f"Checkbox: no match for token '{token}' in opts {[o.get('label') for o in opts]}"
                    )
                    continue

        try:
            frame.locator(f"[id='{target_id}']").check(force=True, timeout=5000)
            logs.append(f"Checked checkbox '{target_id}'")
        except Exception as e:
            logs.append(f"Checkbox check failed for '{target_id}': {e}")


# ─────────────────────────────────────────────────────────────────────────────
# candidate-location (Google Places / freeform typeahead)
# ─────────────────────────────────────────────────────────────────────────────

def _fill_location_typeahead(
    frame, field_id: str, city: str, logs: list, page: Page = None
) -> None:
    try:
        el = frame.locator(f"input{_safe_css_id(field_id)}")
        if el.count() == 0:
            logs.append(f"Location typeahead: field '{field_id}' not found")
            return

        el.click(timeout=3000)
        el.fill("", timeout=2000)
        frame.wait_for_timeout(300)
        el.type(city, delay=80)
        frame.wait_for_timeout(600)

        suggestion_sel = (
            "div[role='option']:visible, "
            "li[role='option']:visible, "
            f"[id^='react-select-{field_id}-option']:visible, "
            "[class*='autocomplete-suggestion']:visible, "
            "[class*='suggestion']:visible, "
            ".pac-item:visible, "
            "ul.pac-container li:visible"
        )

        suggestion_clicked = False
        for _ in range(16):
            frame.wait_for_timeout(250)
            try:
                suggestion = frame.locator(suggestion_sel).first
                if suggestion.count() > 0 and suggestion.is_visible():
                    txt = suggestion.inner_text().strip()
                    suggestion.click(timeout=2000)
                    frame.wait_for_timeout(400)
                    logs.append(
                        f"Location typeahead '{field_id}': clicked suggestion '{txt}'"
                    )
                    suggestion_clicked = True
                    break
            except Exception:
                pass

        if not suggestion_clicked:
            try:
                if page:
                    page.keyboard.press("Enter")
                    frame.wait_for_timeout(200)
                    page.keyboard.press("Tab")
                else:
                    el.press("Enter")
                    frame.wait_for_timeout(200)
                    el.press("Tab")
                frame.wait_for_timeout(300)
                logs.append(
                    f"Location typeahead '{field_id}': no suggestion after 4s — "
                    f"committed '{city}' via Enter+Tab"
                )
            except Exception as tab_err:
                logs.append(f"Location typeahead Tab commit failed: {tab_err}")
                try:
                    el.evaluate("el => el.dispatchEvent(new Event('blur', { bubbles: true }))")
                except Exception:
                    pass
    except Exception as e:
        logs.append(f"Location typeahead fill error for '{field_id}': {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Universal field refill callback (used by submit_with_retry)
# ─────────────────────────────────────────────────────────────────────────────

def _refill_field(frame, field_info: dict, answer: str, logs: list, page: Page):
    ftype = field_info.get("type", "text")
    fid = field_info.get("id", "")
    fname = field_info.get("name", "")

    if ftype == "react-select":
        label_lower = (field_info.get("label") or "").lower()
        is_remote = bool(re.search(
            r"remote|work.{0,10}(from|preference|location)|plan.{0,10}work.{0,10}remote",
            label_lower
        ))
        if is_remote:
            _fill_react_select_remote(frame, fid, logs, page=page)
        else:
            _fill_react_select(frame, fid, answer, logs, page=page)

    elif ftype == "checkbox":
        _fill_checkbox_group(frame, field_info, answer, logs)

    else:
        if fid == "candidate-location":
            _fill_location_typeahead(frame, fid, answer, logs, page=page)
            return

        sel = f"[id='{fid}']" if fid else f"[name='{fname}']"
        try:
            frame.locator(sel).fill(answer, timeout=5000)
            frame.locator(sel).evaluate(
                "el => el.dispatchEvent(new Event('blur', { bubbles: true }))"
            )
            logs.append(f"[refill] Filled text field '{fid}' = '{answer}'")
        except Exception as e:
            logs.append(f"[refill] Text fill failed '{fid}': {e}")


class GreenhouseApplyAgent(BaseApplyAgent):
    def run_apply_flow(
        self,
        page: Page,
        application: Application,
        candidate_profile: dict,
        result: dict,
    ):
        url = application.job.url
        result["logs"].append(f"Navigating to {url}")
        page.goto(url)
        page.wait_for_load_state("networkidle")

        form_found, is_iframe_embed = resolve_apply_form(
            page, result["logs"], app_id=application.id
        )
        if not form_found:
            result["logs"].append(
                "No apply form found after exhausting all strategies. "
                f"Final URL: {page.url}. Marking SKIPPED."
            )
            result["status"] = "SKIPPED"
            return

        result["logs"].append(f"Apply form located at: {page.url}")
        if is_iframe_embed:
            result["logs"].append(
                "Form is inside an iframe — switching to iframe frame context"
            )

        frame = get_form_frame(page, is_iframe_embed)

        try:
            page.screenshot(path=f"data/debug_initial_{application.id}.png")
        except Exception:
            pass

        def fill_and_blur(selector: str, value: str):
            frame.fill(selector, value)
            frame.locator(selector).evaluate(
                "el => el.dispatchEvent(new Event('blur', { bubbles: true }))"
            )

        # ── Core fields ────────────────────────────────────────────────────
        try:
            fill_and_blur("input#first_name", candidate_profile.get("first_name", ""))
            fill_and_blur("input#last_name",  candidate_profile.get("last_name",  ""))
            fill_and_blur("input#email",      candidate_profile.get("email",      ""))
        except Exception as e:
            result["logs"].append(f"FAILED filling basic fields (first/last/email): {e}")
            result["status"] = "FAILED"
            return

        country_from_profile = candidate_profile.get("country", "India")
        result["logs"].append(
            f"Setting phone country selector to '{country_from_profile}'"
        )
        ok = _fill_react_select(
            frame, "country", country_from_profile, result["logs"], page=page
        )
        if not ok:
            result["logs"].append("WARNING: country selector failed")

        raw_phone = candidate_profile.get("phone", "")
        clean_phone = _clean_phone(raw_phone)
        result["logs"].append(f"Phone cleaned: '{raw_phone}' -> '{clean_phone}'")
        try:
            fill_and_blur("input#phone", clean_phone)
        except Exception as e:
            result["logs"].append(f"WARNING: phone fill failed: {e}")
        frame.wait_for_timeout(500)

        # ── Resume upload ──────────────────────────────────────────────────
        resume_path = _resolve_resume_path(application, result["logs"])
        if resume_path:
            try:
                resume_input = frame.locator("input[type='file']")
                if resume_input.count() > 0:
                    resume_input.first.set_input_files(resume_path)
                    result["logs"].append(
                        f"Resume uploaded via file input: {resume_path}"
                    )
                else:
                    attach_btn = frame.locator(
                        "button:has-text('Attach'), label:has-text('Attach')"
                    ).first
                    with page.expect_file_chooser() as fc_info:
                        attach_btn.click()
                    fc_info.value.set_files(resume_path)
                    result["logs"].append(
                        f"Resume uploaded via Attach button: {resume_path}"
                    )
                frame.wait_for_timeout(1000)
            except Exception as e:
                result["logs"].append(f"Resume upload failed: {e}")

        # ── Custom questions ───────────────────────────────────────────────
        questions_data: list[dict] = []
        try:
            questions_data = frame.evaluate(_SCRAPE_QUESTIONS_JS)

            if not questions_data:
                questions_data = []

            LOCATION_FIELDS: dict[str, str] = {
                "candidate-location": candidate_profile.get("location", "Hyderabad, India"),
            }
            for field_id, fill_value in LOCATION_FIELDS.items():
                el = frame.locator(f"input{_safe_css_id(field_id)}")
                if el.count() > 0:
                    city_only = fill_value.split(",")[0].strip()
                    _fill_location_typeahead(frame, field_id, city_only, result["logs"], page=page)
                    questions_data = [q for q in questions_data if q.get("id") != field_id]

            TYPEAHEAD_FIELD_HINTS: dict[str, str] = {}

            for field in questions_data:
                if field.get("type") == "react-select":
                    real_opts = _get_react_select_options(
                        frame, field["id"], page=page
                    )
                    if not real_opts:
                        hint = TYPEAHEAD_FIELD_HINTS.get(
                            field["id"],
                            field.get("label", "").split()[0]
                            if field.get("label")
                            else "",
                        )
                        if hint:
                            real_opts = _get_react_select_options_typeahead(
                                frame, field["id"], hint, page=page
                            )
                            if real_opts:
                                result["logs"].append(
                                    f"Typeahead scraped options for {field['id']} "
                                    f"(hint='{hint}'): {real_opts}"
                                )
                    field["options"] = real_opts
                    result["logs"].append(
                        f"Scraped options for {field['id']}: {real_opts[:5]}"
                        f"{'...' if len(real_opts) > 5 else ''} ({len(real_opts)} total)"
                    )

            pre_resolved: dict[str, str] = {}
            llm_questions: list[dict] = []

            for field in questions_data:
                if (
                    field.get("type") == "react-select"
                    and len(field.get("options", [])) > _MAX_OPTIONS_FOR_LLM
                ):
                    resolved = _pre_resolve_large_options(
                        field, candidate_profile, result["logs"]
                    )
                    if resolved:
                        pre_resolved[field["id"]] = resolved
                        result["logs"].append(
                            f"Skipping {field['id']} from LLM payload "
                            f"(pre-resolved -> '{resolved}')"
                        )
                        continue
                    else:
                        field = dict(field)
                        field["options"] = field["options"][:_MAX_OPTIONS_FOR_LLM]
                        result["logs"].append(
                            f"Truncated options for {field['id']} to "
                            f"{_MAX_OPTIONS_FOR_LLM} items for LLM"
                        )
                llm_questions.append(field)

            result["logs"].append(
                f"Found {len(questions_data)} custom questions. "
                f"{len(pre_resolved)} pre-resolved, {len(llm_questions)} sent to LLM."
            )

            prompt = f"""
You are filling out a job application form for a candidate.

Candidate Profile:
{json.dumps(candidate_profile, indent=2)}

INSTRUCTIONS:
1. LinkedIn -> use the exact `linkedin` value from the profile.
2. GitHub/Portfolio -> use `github` or `portfolio` from the profile.
3. Sponsorship/visa questions -> use `sponsorship` from profile (true/false). If true -> select a 'Yes...' option. If false -> select 'No'.
4. Employment agreements / non-compete -> "No".
5. Previously worked at this company -> "No".
6. Reasonable accommodation -> "No" or leave blank.
7. Gender/veteran/disability/hispanic -> use corresponding profile fields.
8. "How did you hear" -> "LinkedIn".
9. WhatsApp opt-in / messaging consent -> prefer 'No' or 'I do not consent' options.
10. Interview recording consent (BrightHire etc.) -> prefer 'Yes' or 'I consent' — required to proceed.
11. Country where you currently reside -> select 'India' from the available options.
12. Work authorization -> "Yes" (candidate is authorized to work in India).
13. Previously employed at this company -> "No".
14. Current/previous job title -> "Software Engineer".
15. School attended -> "Lovely Professional University".
16. Degree obtained -> match nearest available option to "Bachelor of Technology" or "B.Tech".
17. Current or previous employer -> "Wells Fargo" (NOT 'N/A').
18. If located in the US, city and state -> "N/A" (candidate is in India, not the US).
19. Years of experience -> pick the option from `options` that best fits 5 years (e.g. '5 - 10 years of experience as a software engineer').
20. OPTIONAL FIELDS: If a field is optional and you have no clear answer, omit the key entirely
    or return null — do NOT return an empty string "". Returning "" will silently pick
    the first available option, which is almost always wrong.
21. CHECKBOX GROUPS (type=checkbox): The `options` array lists all available checkboxes with
    their `label` and `id`. Return the LABEL string(s) of the option(s) to check, NOT the DOM id.
    For MULTI-SELECT checkbox groups (e.g. "countries you anticipate working in"),
    return a COMMA-SEPARATED string of matching LABELS.
    - "countries you anticipate working in" or "anticipate working" -> return "India"
    - "areas of specialization" -> return "Backend,Full-Stack"
    - Single-select checkbox groups -> return only one label.
    NEVER return a DOM id as the answer for a checkbox group.

CRITICAL RULE FOR react-select FIELDS:
- The `options` list contains the EXACT visible text of every choice available in that dropdown.
- You MUST return one of those exact strings as the answer. Do NOT invent or paraphrase text.
- If options is empty ([]), the field is a free-text typeahead — fill with the most appropriate value from the candidate profile.
- For yes/no dropdowns: pick the option that starts with 'No' for negative answers.
- For disability: pick the option that means 'No disability'.
- For veteran: pick the option that means 'not a veteran'.
- For skill level scales (e.g. ['Poor', 'Fair', 'Average', 'Good', 'Excellent']): pick an appropriate level.
- For remote preference: pick the option closest to 'Yes / Open to remote or hybrid'.

CRITICAL RULE FOR radio FIELDS:
- Return the `id` of the radio button to select (from the options array).

Form Fields:
{json.dumps(llm_questions, indent=2)}

Return a flat JSON object: keys = field `id` (use the top-level `id` for the group), values = the answer string (or omit key if no clear answer).
"""

            answers = _call_llm_with_fallback(prompt, result["logs"])
            answers.update(pre_resolved)

            for field_id, answer_val in answers.items():
                if not field_id:
                    continue

                if answer_val is None or str(answer_val).strip() == "":
                    field_info = next(
                        (
                            f
                            for f in questions_data
                            if f["id"] == field_id or f["name"] == field_id
                        ),
                        None,
                    )
                    if field_info and field_info.get("required"):
                        opts = field_info.get("options", [])
                        opt_labels = (
                            [o.get("label", "") for o in opts]
                            if opts and isinstance(opts[0], dict)
                            else opts
                        )
                        safe = get_safe_default(
                            field_info.get("label", ""), opt_labels
                        )
                        if safe:
                            result["logs"].append(
                                f"LLM null for required '{field_id}' "
                                f"('{field_info['label']}') "
                                f"— applying safe default: '{safe}'"
                            )
                            answer_val = safe
                        else:
                            result["logs"].append(
                                f"Skipping required '{field_id}' — LLM null, no safe default"
                            )
                            continue
                    else:
                        result["logs"].append(
                            f"Skipping optional '{field_id}' — LLM returned null"
                        )
                        continue

                try:
                    field_info = next(
                        (
                            f
                            for f in questions_data
                            if f["id"] == field_id or f["name"] == field_id
                        ),
                        None,
                    )
                    if not field_info:
                        for q in questions_data:
                            if any(
                                o.get("id") == field_id for o in q.get("options", [])
                            ):
                                field_info = q
                                break
                    if not field_info:
                        continue

                    ftype = field_info.get("type", "text")

                    if ftype == "react-select":
                        label_lower = (field_info.get("label") or "").lower()
                        is_remote = bool(re.search(
                            r"remote|work.{0,10}(from|preference|location)"
                            r"|plan.{0,10}work.{0,10}remote",
                            label_lower,
                        ))
                        if is_remote:
                            ok = _fill_react_select_remote(
                                frame, field_info["id"], result["logs"], page=page
                            )
                        else:
                            ok = _fill_react_select(
                                frame,
                                field_info["id"],
                                str(answer_val),
                                result["logs"],
                                page=page,
                            )
                        if not ok:
                            result["logs"].append(
                                f"WARNING: react-select failed for {field_id} = '{answer_val}'"
                            )
                            if not field_info.get("options"):
                                try:
                                    frame.locator(
                                        f"input{_safe_css_id(field_info['id'])}"
                                    ).fill(str(answer_val), timeout=3000)
                                    result["logs"].append(
                                        f"Typeahead plain-fill fallback: "
                                        f"{field_id} = '{answer_val}'"
                                    )
                                except Exception:
                                    pass

                    elif ftype == "checkbox":
                        _fill_checkbox_group(
                            frame, field_info, str(answer_val), result["logs"]
                        )

                    elif ftype == "radio":
                        el = frame.locator(f"[id='{answer_val}']")
                        el.click(force=True, timeout=5000)
                        el.evaluate(
                            "el => el.dispatchEvent(new Event('change', { bubbles: true }))"
                        )

                    else:
                        sel = (
                            f"[id='{field_info['id']}']" if field_info["id"]
                            else f"[name='{field_info['name']}']"
                        )
                        frame.locator(sel).fill(str(answer_val), timeout=5000)
                        frame.locator(sel).evaluate(
                            "el => el.dispatchEvent(new Event('blur', { bubbles: true }))"
                        )

                    result["logs"].append(f"Filled '{field_id}' = '{answer_val}'")

                except Exception as e:
                    result["logs"].append(f"Failed to fill '{field_id}': {e}")

        except Exception as e:
            result["logs"].append(f"Error processing custom questions: {e}")

        frame.wait_for_timeout(1500)

        try:
            page.screenshot(
                path=f"data/debug_before_submit_{application.id}.png", full_page=True
            )
            result["logs"].append("Debug screenshot saved.")
        except Exception:
            pass

        pre_submit_url = page.url
        outcome = submit_with_retry(
            page=page,
            frame=frame,
            pre_submit_url=pre_submit_url,
            questions_data=questions_data,
            candidate_profile=candidate_profile,
            logs=result["logs"],
            refill_fn=_refill_field,
            resume_path=resume_path,
        )
        result["status"] = outcome

        if outcome == "AUTO_APPLIED":
            try:
                page.screenshot(path=f"data/success_{application.id}.png")
                result["logs"].append(
                    f"Success screenshot: data/success_{application.id}.png"
                )
            except Exception:
                pass
