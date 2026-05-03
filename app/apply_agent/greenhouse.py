from playwright.sync_api import Page
from app.apply_agent.base import BaseApplyAgent
from app.models.application import Application
import json
import os
import re
from groq import Groq


def _get_react_select_options(page, field_id: str) -> list[str]:
    """
    Open a React Select dropdown, collect ALL visible option texts, then close it.
    Returns a list of exact option strings as the user would see them.
    This must be called BEFORE Groq so we send real options to the LLM.
    """
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


def _fill_react_select(page, field_id: str, answer_text: str, logs: list) -> bool:
    """
    Interact with a React Select combobox widget exactly like a human:
      1. Click control to open the dropdown
      2. Type a SHORT search term (first word only) to filter the list
      3. Find the option whose text EXACTLY matches answer_text
      4. Click it

    NOTE: answer_text must be the EXACT option string from the dropdown
    (collected by _get_react_select_options before calling Groq).
    """
    control_div = page.locator(
        f"xpath=//input[@id='{field_id}']/ancestor::div[contains(@class,'select__control')]"
    )
    input_el = page.locator(f"input#{field_id}")

    # Step 1 - open
    try:
        control_div.click(timeout=4000)
        page.wait_for_timeout(300)
    except Exception as e:
        logs.append(f"react-select open failed for {field_id}: {e}")
        return False

    # Step 2 - type only the first word to filter
    # Using the FULL answer_text as search can fail if it contains apostrophes
    # or special characters. First word is safe and narrows the list enough.
    first_word = answer_text.split()[0] if answer_text else answer_text
    try:
        input_el.type(first_word, delay=50)
        page.wait_for_timeout(500)
    except Exception as e:
        logs.append(f"react-select type failed for {field_id}: {e}")

    # Step 3 - collect filtered options
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

    # Match priority: exact -> case-insensitive -> startswith -> contains
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
        page.keyboard.press("Escape")
        return False

    # Step 4 - click
    try:
        options.nth(matched_idx).click(timeout=3000)
        page.wait_for_timeout(400)
        logs.append(f"react-select: clicked '{option_texts[matched_idx]}' for {field_id}")
        return True
    except Exception as e:
        logs.append(f"react-select: click failed for {field_id}: {e}")
        return False


def _clean_phone(phone: str) -> str:
    """
    Strip the country code prefix from a phone number.
    Greenhouse adds the country code via the country selector,
    so the phone input should only contain the local number.
    e.g. '+91-7689961477' -> '7689961477'
    """
    if not phone:
        return phone
    return re.sub(r'^\+?\d{1,3}[-\s]', '', phone.strip())


class GreenhouseApplyAgent(BaseApplyAgent):
    def run_apply_flow(self, page: Page, application: Application, candidate_profile: dict, result: dict):
        url = application.job.url
        result["logs"].append(f"Navigating to {url}")
        page.goto(url)
        page.wait_for_load_state("networkidle")

        try:
            page.screenshot(path=f"data/debug_initial_{application.id}.png")
        except Exception:
            pass

        def fill_and_blur(selector: str, value: str):
            page.fill(selector, value)
            page.locator(selector).evaluate(
                "el => el.dispatchEvent(new Event('blur', { bubbles: true }))"
            )

        # ------------------------------------------------------------------ #
        # Basic fields                                                        #
        # ------------------------------------------------------------------ #
        fill_and_blur("input#first_name", candidate_profile.get("first_name", ""))
        fill_and_blur("input#last_name",  candidate_profile.get("last_name",  ""))
        fill_and_blur("input#email",      candidate_profile.get("email",      ""))

        # Country selector then clean phone
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

        # ------------------------------------------------------------------ #
        # Resume upload                                                       #
        # ------------------------------------------------------------------ #
        if application.tailored_resume_pdf_path and os.path.exists(application.tailored_resume_pdf_path):
            try:
                # Try direct hidden input first
                resume_input = page.locator("input[type='file']")
                if resume_input.count() > 0:
                    resume_input.first.set_input_files(application.tailored_resume_pdf_path)
                    result["logs"].append(f"Resume uploaded via file input: {application.tailored_resume_pdf_path}")
                else:
                    # Fallback: click Attach button
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

        # ------------------------------------------------------------------ #
        # Scrape custom questions                                             #
        # ------------------------------------------------------------------ #
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
                        fi.options = [];  // will be populated by Python before Groq call
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

            # ------------------------------------------------------------------ #
            # KEY FIX: Open each react-select, collect REAL options, close it    #
            # Then send those exact options to Groq so it can only pick valid ones#
            # ------------------------------------------------------------------ #
            for field in questions_data:
                if field.get("type") == "react-select":
                    real_opts = _get_react_select_options(page, field["id"])
                    field["options"] = real_opts
                    result["logs"].append(f"Scraped options for {field['id']}: {real_opts}")

            result["logs"].append(
                f"Found {len(questions_data)} custom questions. Asking Groq for answers..."
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

CRITICAL RULE FOR react-select FIELDS:
- The `options` list contains the EXACT visible text of every choice available in that dropdown.
- You MUST return one of those exact strings as the answer. Do NOT invent text.
- Pick the most appropriate option given the candidate's profile.
- For yes/no dropdowns: if the answer is No, pick the option that starts with 'No'.
- For disability: pick the option that means 'No disability'.
- For veteran: pick the option that means 'not a veteran'.
- For country of residence: pick the option matching the candidate's country.

Form Fields (each has type, label, and for react-select fields an `options` array):
{json.dumps(questions_data, indent=2)}

Return a flat JSON object: keys = field `id`, values = the answer.
- react-select: value must be one of the strings in the `options` array exactly.
- checkbox: value = the `id` of the option to check.
- radio: value = the `id` of the option to select.
- text/textarea: value = the string to type.
"""

            client = Groq(api_key=os.getenv("GROQ_API_KEY"))
            chat_completion = client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.3-70b-versatile",
                response_format={"type": "json_object"},
            )
            answers = json.loads(chat_completion.choices[0].message.content)

            # ------------------------------------------------------------------ #
            # Fill each field                                                     #
            # ------------------------------------------------------------------ #
            for field_id, answer_val in answers.items():
                if not field_id:
                    continue
                try:
                    field_info = next(
                        (f for f in questions_data if f["id"] == field_id or f["name"] == field_id),
                        None
                    )
                    if not field_info:
                        # search inside options (checkbox/radio)
                        for q in questions_data:
                            if any(o.get("id") == field_id for o in q.get("options", [])):
                                field_info = q
                                break
                    if not field_info:
                        continue

                    ftype = field_info.get("type", "text")

                    if ftype == "react-select":
                        ok = _fill_react_select(
                            page, field_info["id"], str(answer_val), result["logs"]
                        )
                        if not ok:
                            result["logs"].append(
                                f"WARNING: react-select failed for {field_id} = '{answer_val}'"
                            )

                    elif ftype == "checkbox":
                        page.locator(f"[id='{answer_val}']").click(force=True, timeout=5000)

                    elif ftype == "radio":
                        el = page.locator(f"[id='{answer_val}']")
                        el.click(force=True, timeout=5000)
                        el.evaluate("el => el.dispatchEvent(new Event('change', { bubbles: true }))")

                    else:
                        sel = (
                            f"[id='{field_info['id']}']" if field_info["id"] and "'" not in field_info["id"]
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

        # Screenshot before submit
        try:
            page.screenshot(path=f"data/debug_before_submit_{application.id}.png", full_page=True)
            result["logs"].append(f"Debug screenshot saved.")
        except Exception:
            pass

        # Block on visible validation errors
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

        # Submit
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
