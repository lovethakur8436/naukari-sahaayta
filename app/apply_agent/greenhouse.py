from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from app.apply_agent.base import BaseApplyAgent
from app.apply_agent.form_resolver import resolve_apply_form
from app.models.application import Application
import json
import os
import re
from groq import Groq

_MAX_OPTIONS_FOR_LLM = 40


def _safe_css_id(field_id: str) -> str:
    """
    Return a safe Playwright attribute selector for a field ID.
    CSS shorthand  input#foo[]  is invalid when the ID contains brackets.
    Using [id='foo[]'] is always safe regardless of special characters.
    """
    return f"[id='{field_id}']"


def _get_react_select_options(page, field_id: str) -> list[str]:
    control_div = page.locator(
        f"xpath=//input[@id='{field_id}']/ancestor::div[contains(@class,'select__control')]"
    )
    try:
        control_div.click(timeout=4000)
        page.wait_for_timeout(400)
    except Exception:
        return []

    options = page.locator(f"[id^='react-select-{field_id}-option']")
    if options.count() == 0:
        options = page.locator("div[role='option']:visible")

    texts = []
    for i in range(options.count()):
        try:
            texts.append(options.nth(i).inner_text().strip())
        except Exception:
            pass

    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    return texts


def _get_react_select_options_typeahead(page, field_id: str, search_term: str) -> list[str]:
    # Use safe attribute selector for input to avoid [] crash
    input_el = page.locator(f"input{_safe_css_id(field_id)}")
    control_div = page.locator(
        f"xpath=//input[@id='{field_id}']/ancestor::div[contains(@class,'select__control')]"
    )
    try:
        control_div.click(timeout=4000)
        page.wait_for_timeout(300)
        input_el.type(search_term[:10], delay=60)
        page.wait_for_timeout(800)
    except Exception:
        page.keyboard.press("Escape")
        return []

    options = page.locator(f"[id^='react-select-{field_id}-option']")
    if options.count() == 0:
        options = page.locator("div[role='option']:visible")

    texts = []
    for i in range(options.count()):
        try:
            texts.append(options.nth(i).inner_text().strip())
        except Exception:
            pass

    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    return texts


def _fill_react_select(page, field_id: str, answer_text: str, logs: list) -> bool:
    # Fix 2: skip filling entirely when LLM returned empty/null answer.
    # Previously an empty string would match via startswith('') and silently
    # pick the FIRST option (e.g. 'she/her/hers', '0-2 years', 'Yes').
    if not answer_text or not answer_text.strip():
        logs.append(f"react-select: skipping '{field_id}' — LLM returned empty answer, leaving blank")
        return False

    control_div = page.locator(
        f"xpath=//input[@id='{field_id}']/ancestor::div[contains(@class,'select__control')]"
    )
    # Fix 1: use safe attribute selector — avoids CSS crash on IDs like foo[]
    input_el = page.locator(f"input{_safe_css_id(field_id)}")

    try:
        control_div.click(timeout=4000)
        page.wait_for_timeout(300)
    except Exception as e:
        logs.append(f"react-select open failed for {field_id}: {e}")
        return False

    first_word = answer_text.split()[0]
    try:
        input_el.type(first_word, delay=50)
        page.wait_for_timeout(500)
    except Exception as e:
        logs.append(f"react-select type failed for {field_id}: {e}")

    listbox = page.locator(f"[id='react-select-{field_id}-listbox'], div[role='listbox']").first
    try:
        listbox.wait_for(state="visible", timeout=4000)
    except Exception:
        pass

    options = page.locator(f"[id^='react-select-{field_id}-option']")
    count = options.count()
    if count == 0:
        options = page.locator("div[role='option']:visible")
        count = options.count()

    if count == 0:
        logs.append(f"react-select: no options after typing '{first_word}' for {field_id}")
        page.keyboard.press("Escape")
        return False

    option_texts = []
    for i in range(count):
        try:
            option_texts.append(options.nth(i).inner_text().strip())
        except Exception:
            option_texts.append("")

    logs.append(f"react-select: {count} options for {field_id}, want '{answer_text}': {option_texts}")

    matched_idx = None
    for i, t in enumerate(option_texts):
        if t == answer_text:
            matched_idx = i; break
    if matched_idx is None:
        for i, t in enumerate(option_texts):
            if t.lower() == answer_text.lower():
                matched_idx = i
                logs.append(f"react-select: case-matched -> '{t}'"); break
    if matched_idx is None:
        for i, t in enumerate(option_texts):
            if t.lower().startswith(answer_text.lower()):
                matched_idx = i
                logs.append(f"react-select: startswith-matched -> '{t}'"); break
    if matched_idx is None:
        for i, t in enumerate(option_texts):
            if answer_text.lower() in t.lower():
                matched_idx = i
                logs.append(f"react-select: partial-matched -> '{t}'"); break

    if matched_idx is None:
        logs.append(f"react-select: NO MATCH for '{answer_text}' in {option_texts}")
        page.keyboard.press("Escape")
        return False

    try:
        options.nth(matched_idx).click(timeout=3000)
        page.wait_for_timeout(400)
        logs.append(f"react-select: clicked '{option_texts[matched_idx]}' for {field_id}")
        return True
    except Exception as e:
        logs.append(f"react-select: click failed for {field_id}: {e}")
        return False


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

def _pre_resolve_large_options(field: dict, candidate_profile: dict, logs: list) -> str | None:
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
            is_rate_limit = "429" in err_str or "rate_limit_exceeded" in err_str or "tokens per day" in err_str
            if is_rate_limit:
                logs.append(f"Groq rate-limit hit: {err_str[:200]}. Falling back to Gemini Flash.")
            else:
                logs.append(f"Groq error (non-429): {err_str[:200]}. Falling back to Gemini Flash.")
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
            generation_config={"response_mime_type": "application/json", "temperature": 0.1},
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


class GreenhouseApplyAgent(BaseApplyAgent):
    def run_apply_flow(self, page: Page, application: Application, candidate_profile: dict, result: dict):
        url = application.job.url
        result["logs"].append(f"Navigating to {url}")
        page.goto(url)
        page.wait_for_load_state("networkidle")

        form_found = resolve_apply_form(page, result["logs"], app_id=application.id)
        if not form_found:
            result["logs"].append(
                "No apply form found after exhausting all strategies. "
                f"Final URL: {page.url}. Marking SKIPPED."
            )
            result["status"] = "SKIPPED"
            return

        result["logs"].append(f"Apply form located at: {page.url}")

        try:
            page.screenshot(path=f"data/debug_initial_{application.id}.png")
        except Exception:
            pass

        def fill_and_blur(selector: str, value: str):
            page.fill(selector, value)
            page.locator(selector).evaluate(
                "el => el.dispatchEvent(new Event('blur', { bubbles: true }))"
            )

        fill_and_blur("input#first_name", candidate_profile.get("first_name", ""))
        fill_and_blur("input#last_name",  candidate_profile.get("last_name",  ""))
        fill_and_blur("input#email",      candidate_profile.get("email",      ""))

        country_from_profile = candidate_profile.get("country", "India")
        result["logs"].append(f"Setting phone country selector to '{country_from_profile}'")
        ok = _fill_react_select(page, "country", country_from_profile, result["logs"])
        if not ok:
            result["logs"].append("WARNING: country selector failed")

        raw_phone = candidate_profile.get("phone", "")
        clean_phone = _clean_phone(raw_phone)
        result["logs"].append(f"Phone cleaned: '{raw_phone}' -> '{clean_phone}'")
        fill_and_blur("input#phone", clean_phone)
        page.wait_for_timeout(500)

        if application.tailored_resume_pdf_path and os.path.exists(application.tailored_resume_pdf_path):
            try:
                resume_input = page.locator("input[type='file']")
                if resume_input.count() > 0:
                    resume_input.first.set_input_files(application.tailored_resume_pdf_path)
                    result["logs"].append(f"Resume uploaded via file input: {application.tailored_resume_pdf_path}")
                else:
                    attach_btn = page.locator("button:has-text('Attach'), label:has-text('Attach')").first
                    with page.expect_file_chooser() as fc_info:
                        attach_btn.click()
                    fc_info.value.set_files(application.tailored_resume_pdf_path)
                    result["logs"].append(f"Resume uploaded via Attach button: {application.tailored_resume_pdf_path}")
                page.wait_for_timeout(1000)
            except Exception as e:
                result["logs"].append(f"Resume upload failed: {e}")
        else:
            result["logs"].append(
                f"WARNING: No resume PDF found at '{application.tailored_resume_pdf_path}'. Skipping upload."
            )

        try:
            questions_data = page.evaluate("""() => {
                const fields = [];
                const skip = new Set(['first_name','last_name','email','phone','country']);
                const els = document.querySelectorAll(
                    'input[role="combobox"],'
                    + 'input[type="text"]:not([role="combobox"]):not([hidden]),'
                    + 'textarea:not([hidden]),'
                    + 'input[type="checkbox"]:not([hidden]),'
                    + 'input[type="radio"]:not([hidden])'
                );
                els.forEach(el => {
                    if (skip.has(el.id)) return;
                    if ((el.className || '').includes('recaptcha')) return;
                    if ((el.id || '').includes('recaptcha')) return;
                    if (el.offsetWidth === 0 && el.offsetHeight === 0) return;

                    const container = el.closest('div.field-wrapper, div.field, div.custom_question');
                    const labelEl   = document.querySelector(`label[for="${el.id}"]`)
                                   || container?.querySelector('label');
                    let label = labelEl ? labelEl.innerText.replace('*','').trim() : (el.name || el.id);
                    const isRequired = el.required
                        || el.getAttribute('aria-required') === 'true'
                        || !!(labelEl && labelEl.innerText.includes('*'));

                    let fi = { id: el.id, name: el.name || '', label, required: isRequired };

                    if (el.getAttribute('role') === 'combobox') {
                        fi.type = 'react-select';
                        fi.options = [];
                    } else if (el.type === 'checkbox') {
                        fi.type = 'checkbox';
                        const ex = fields.find(f => f.name === fi.name && f.type === 'checkbox');
                        if (ex) { ex.options.push({ value: el.value, id: el.id, label }); return; }
                        fi.options = [{ value: el.value, id: el.id, label }];
                    } else if (el.type === 'radio') {
                        fi.type = 'radio';
                        const ex = fields.find(f => f.name === fi.name && f.type === 'radio');
                        if (ex) { ex.options.push({ value: el.value, id: el.id, label }); return; }
                        fi.options = [{ value: el.value, id: el.id, label }];
                    } else {
                        fi.type = 'text';
                    }
                    fields.push(fi);
                });
                return fields;
            }""")

            if not questions_data:
                questions_data = []

            TYPEAHEAD_FIELD_HINTS = {
                "candidate-location": candidate_profile.get("location", "Hyderabad"),
            }

            for field in questions_data:
                if field.get("type") == "react-select":
                    real_opts = _get_react_select_options(page, field["id"])
                    if not real_opts:
                        hint = TYPEAHEAD_FIELD_HINTS.get(
                            field["id"],
                            field.get("label", "").split()[0] if field.get("label") else ""
                        )
                        if hint:
                            real_opts = _get_react_select_options_typeahead(page, field["id"], hint)
                            if real_opts:
                                result["logs"].append(
                                    f"Typeahead scraped options for {field['id']} (hint='{hint}'): {real_opts}"
                                )
                    field["options"] = real_opts
                    result["logs"].append(
                        f"Scraped options for {field['id']}: {real_opts[:5]}{'...' if len(real_opts) > 5 else ''} ({len(real_opts)} total)"
                    )

            pre_resolved: dict[str, str] = {}
            llm_questions: list[dict] = []

            for field in questions_data:
                if field.get("type") == "react-select" and len(field.get("options", [])) > _MAX_OPTIONS_FOR_LLM:
                    resolved = _pre_resolve_large_options(field, candidate_profile, result["logs"])
                    if resolved:
                        pre_resolved[field["id"]] = resolved
                        result["logs"].append(
                            f"Skipping {field['id']} from LLM payload (pre-resolved -> '{resolved}')"
                        )
                        continue
                    else:
                        field = dict(field)
                        field["options"] = field["options"][:_MAX_OPTIONS_FOR_LLM]
                        result["logs"].append(
                            f"Truncated options for {field['id']} to {_MAX_OPTIONS_FOR_LLM} items for LLM"
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
9. OPTIONAL FIELDS: If a field is optional and you have no clear answer, omit the key entirely
   or return null — do NOT return an empty string "". Returning "" will silently pick
   the first available option, which is almost always wrong.

CRITICAL RULE FOR react-select FIELDS:
- The `options` list contains the EXACT visible text of every choice available in that dropdown.
- You MUST return one of those exact strings as the answer. Do NOT invent or paraphrase text.
- If options is empty ([]), the field is a free-text typeahead — fill with the most appropriate value from the candidate profile.
- For yes/no dropdowns: pick the option that starts with 'No' for negative answers.
- For disability: pick the option that means 'No disability'.
- For veteran: pick the option that means 'not a veteran'.
- For skill level scales (e.g. ['Poor', 'Fair', 'Average', 'Good', 'Excellent'] or ['0', '1', '2', '3', '4', '5']): pick an appropriate level based on the candidate's skills.

CRITICAL RULE FOR checkbox FIELDS:
- Return the `id` of the checkbox option to check (from the options array).

CRITICAL RULE FOR radio FIELDS:
- Return the `id` of the radio button to select (from the options array).

Form Fields:
{json.dumps(llm_questions, indent=2)}

Return a flat JSON object: keys = field `id`, values = the answer string (or omit key if no clear answer).
"""

            answers = _call_llm_with_fallback(prompt, result["logs"])
            answers.update(pre_resolved)

            for field_id, answer_val in answers.items():
                # Fix 2: skip null / empty answers from LLM entirely
                if not field_id:
                    continue
                if answer_val is None or str(answer_val).strip() == "":
                    result["logs"].append(f"Skipping '{field_id}' — LLM returned null/empty answer")
                    continue
                try:
                    field_info = next(
                        (f for f in questions_data if f["id"] == field_id or f["name"] == field_id),
                        None
                    )
                    if not field_info:
                        for q in questions_data:
                            if any(o.get("id") == field_id for o in q.get("options", [])):
                                field_info = q
                                break
                    if not field_info:
                        continue

                    ftype = field_info.get("type", "text")

                    if ftype == "react-select":
                        ok = _fill_react_select(page, field_info["id"], str(answer_val), result["logs"])
                        if not ok:
                            result["logs"].append(f"WARNING: react-select failed for {field_id} = '{answer_val}'")
                            if not field_info.get("options"):
                                try:
                                    # Fix 1: safe selector for typeahead fallback too
                                    page.locator(f"input{_safe_css_id(field_info['id'])}").fill(str(answer_val), timeout=3000)
                                    result["logs"].append(f"Typeahead plain-fill fallback: {field_id} = '{answer_val}'")
                                except Exception:
                                    pass

                    elif ftype == "checkbox":
                        target_id = str(answer_val)
                        opts = field_info.get("options", [])
                        valid_ids = {o.get("id") for o in opts}
                        if target_id not in valid_ids and opts:
                            affirmative = next(
                                (o for o in opts if o.get("label", "").lower() in ("yes", "i agree", "true")),
                                opts[0]
                            )
                            target_id = affirmative["id"]
                            result["logs"].append(f"Checkbox: LLM returned invalid id '{answer_val}', using fallback '{target_id}'")
                        page.locator(f"[id='{target_id}']").check(force=True, timeout=5000)

                    elif ftype == "radio":
                        el = page.locator(f"[id='{answer_val}']")
                        el.click(force=True, timeout=5000)
                        el.evaluate("el => el.dispatchEvent(new Event('change', { bubbles: true }))")

                    else:
                        # Fix 1: always use attribute selector, never # shorthand
                        sel = (
                            f"[id='{field_info['id']}']" if field_info["id"]
                            else f"[name='{field_info['name']}']"
                        )
                        page.locator(sel).fill(str(answer_val), timeout=5000)
                        page.locator(sel).evaluate(
                            "el => el.dispatchEvent(new Event('blur', { bubbles: true }))"
                        )

                    result["logs"].append(f"Filled '{field_id}' = '{answer_val}'")

                except Exception as e:
                    result["logs"].append(f"Failed to fill '{field_id}': {e}")

        except Exception as e:
            result["logs"].append(f"Error processing custom questions: {e}")

        page.wait_for_timeout(1500)

        try:
            page.screenshot(path=f"data/debug_before_submit_{application.id}.png", full_page=True)
            result["logs"].append(f"Debug screenshot saved.")
        except Exception:
            pass

        try:
            raw_errors = page.locator(
                ".field_with_errors, [class*='error']:visible, [class*='invalid']:visible"
            ).all_inner_texts()
            blocking = [e.strip() for e in raw_errors if "required" in e.lower()]
            if blocking:
                result["logs"].append(f"BLOCKED SUBMIT - validation errors: {blocking}")
                result["status"] = "VALIDATION_FAILED"
                return
        except Exception:
            pass

        page.wait_for_timeout(500)

        try:
            submit_btn = page.locator("input#submit_app, button#submit_app, button[type='submit']")
            if submit_btn.count() > 0:
                submit_btn.first.click()
                result["logs"].append("Submit clicked.")
                page.wait_for_timeout(4000)
                try:
                    errs = page.locator(".error-message, .field_with_errors").all_inner_texts()
                    if errs:
                        result["logs"].append(f"Post-submit errors: {errs}")
                        result["status"] = "VALIDATION_FAILED"
                except Exception:
                    pass
            else:
                result["logs"].append("Submit button not found.")
        except Exception as e:
            result["logs"].append(f"Submit error: {e}")
