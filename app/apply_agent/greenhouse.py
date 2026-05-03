from playwright.sync_api import Page
from app.apply_agent.base import BaseApplyAgent
from app.models.application import Application
import json
import os
import re
from groq import Groq


def _fill_react_select(page, field_id: str, answer_text: str, logs: list) -> bool:
    """
    Interact with a React Select combobox widget exactly like a human:
      1. Click the control to open the dropdown
      2. Type the search text to narrow down options (critical for 200+ item lists)
      3. Wait for filtered options, then pick the best exact match
      4. Click it

    Greenhouse uses react-select for ALL dropdowns - there are zero native
    <select> elements on the page. select_option() and JS value-setters
    do not work here; only real click interaction does.
    """
    control_div = page.locator(
        f"xpath=//input[@id='{field_id}']/ancestor::div[contains(@class,'select__control')]"
    )
    input_el = page.locator(f"input#{field_id}")

    # Step 1 - click the control to open the menu
    try:
        control_div.click(timeout=4000)
        page.wait_for_timeout(300)
    except Exception as e:
        logs.append(f"react-select open failed for {field_id}: {e}")
        return False

    # Step 2 - type the search text so the list is filtered
    # This is CRITICAL for country fields with 200+ options where partial
    # substring match would otherwise hit wrong entries (e.g. "India" -> "British Indian...")
    try:
        input_el.type(answer_text, delay=50)
        page.wait_for_timeout(500)
    except Exception as e:
        logs.append(f"react-select type failed for {field_id}: {e}")

    # Step 3 - wait for options to appear/filter
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
        logs.append(f"react-select: no options visible for {field_id} after typing '{answer_text}'")
        page.keyboard.press("Escape")
        return False

    logs.append(f"react-select: {count} options visible for {field_id}, looking for '{answer_text}'")

    # Collect visible option texts
    option_texts = []
    for i in range(count):
        try:
            option_texts.append(options.nth(i).inner_text().strip())
        except Exception:
            option_texts.append("")

    # Match priority:
    #   1. Exact match
    #   2. Case-insensitive exact
    #   3. Option text STARTS WITH the search text (avoids "British Indian..." for "India")
    #   4. Search text is the full word at start of option (word-boundary)
    #   5. Partial contains (last resort)
    matched_idx = None

    for i, t in enumerate(option_texts):
        if t == answer_text:
            matched_idx = i
            break

    if matched_idx is None:
        for i, t in enumerate(option_texts):
            if t.lower() == answer_text.lower():
                matched_idx = i
                logs.append(f"react-select: case-matched '{answer_text}' -> '{t}'")
                break

    if matched_idx is None:
        # starts-with match (e.g. "India +91" starts with "India")
        for i, t in enumerate(option_texts):
            if t.lower().startswith(answer_text.lower()):
                matched_idx = i
                logs.append(f"react-select: startswith-matched '{answer_text}' -> '{t}'")
                break

    if matched_idx is None:
        # partial contains - last resort
        for i, t in enumerate(option_texts):
            if answer_text.lower() in t.lower():
                matched_idx = i
                logs.append(f"react-select: partial-matched '{answer_text}' -> '{t}'")
                break

    if matched_idx is None:
        logs.append(f"react-select: no match for '{answer_text}' in {option_texts}. Closing.")
        page.keyboard.press("Escape")
        return False

    # Step 4 - click the matched option
    try:
        options.nth(matched_idx).click(timeout=3000)
        page.wait_for_timeout(400)
        logs.append(f"react-select: clicked option '{option_texts[matched_idx]}' for {field_id}")
        return True
    except Exception as e:
        logs.append(f"react-select: click failed for {field_id}: {e}")
        return False


def _clean_phone(phone: str) -> str:
    """
    Greenhouse's phone field has a country-code prefix selector built in.
    The visible phone input should only contain the local number WITHOUT
    the country code (e.g. "7689961477" not "+91-7689961477").
    Strip any leading +XX or +XXX country code and separators.
    """
    if not phone:
        return phone
    # Remove leading + and digits up to first space/dash that looks like a country code
    # Handles: +91-7689961477, +91 7689961477, 7689961477
    cleaned = re.sub(r'^\+?\d{1,3}[-\s]', '', phone.strip())
    return cleaned


class GreenhouseApplyAgent(BaseApplyAgent):
    def run_apply_flow(self, page: Page, application: Application, candidate_profile: dict, result: dict):
        url = application.job.url
        result["logs"].append(f"Navigating to {url}")
        page.goto(url)
        page.wait_for_load_state("networkidle")

        try:
            page.screenshot(path=f"data/debug_initial_{application.id}.png")
        except:
            pass

        def fill_and_blur(selector: str, value: str):
            page.fill(selector, value)
            page.locator(selector).evaluate(
                "el => el.dispatchEvent(new Event('blur', { bubbles: true }))"
            )

        fill_and_blur("input#first_name", candidate_profile.get("first_name", ""))
        fill_and_blur("input#last_name",  candidate_profile.get("last_name",  ""))
        fill_and_blur("input#email",      candidate_profile.get("email",      ""))

        # Phone: strip country code - Greenhouse adds it via the country selector
        raw_phone = candidate_profile.get("phone", "")
        clean_phone = _clean_phone(raw_phone)
        result["logs"].append(f"Phone cleaned: '{raw_phone}' -> '{clean_phone}'")
        fill_and_blur("input#phone", clean_phone)
        page.wait_for_timeout(500)

        # ------------------------------------------------------------------ #
        #  Country code selector (React Select with phone-flag prefix)       #
        #  Must be set BEFORE filling the phone number so the correct        #
        #  country code prefix is applied.                                   #
        # ------------------------------------------------------------------ #
        country_from_profile = candidate_profile.get("country", "India")
        result["logs"].append(f"Setting phone country selector to '{country_from_profile}'")
        ok = _fill_react_select(page, "country", country_from_profile, result["logs"])
        if not ok:
            result["logs"].append("WARNING: country selector fill failed, phone may have wrong country code")
        # Re-fill phone after country is set to ensure no override
        fill_and_blur("input#phone", clean_phone)
        page.wait_for_timeout(500)

        # Upload resume
        if application.tailored_resume_pdf_path:
            resume_input = page.locator(
                "input[type='file'][name='job_application[answers_attributes][0][resume]']"
            )
            if resume_input.count() > 0:
                resume_input.set_input_files(application.tailored_resume_pdf_path)
            else:
                attach_button = page.locator("button:has-text('Attach')").first
                if attach_button.count() > 0:
                    with page.expect_file_chooser() as fc_info:
                        attach_button.click()
                    fc_info.value.set_files(application.tailored_resume_pdf_path)

        # ------------------------------------------------------------------ #
        #  Scrape & answer custom questions                                   #
        # ------------------------------------------------------------------ #
        try:
            questions_data = page.evaluate("""() => {
                const fields = [];
                const elements = document.querySelectorAll(
                    'input[role="combobox"],'
                    + 'input[type="text"]:not([role="combobox"]):not([hidden]):not([style*="display: none"]),'
                    + 'textarea:not([hidden]):not([style*="display: none"]),'
                    + 'input[type="checkbox"]:not([hidden]):not([style*="display: none"]),'
                    + 'input[type="radio"]:not([hidden]):not([style*="display: none"])'
                );
                elements.forEach(el => {
                    if (['first_name','last_name','email','phone','country'].includes(el.id)) return;
                    if (el.className && el.className.includes('recaptcha')) return;
                    if (el.id && el.id.includes('recaptcha')) return;
                    if (el.offsetWidth === 0 || el.offsetHeight === 0) return;

                    const container = el.closest('div.field-wrapper, div.field, div.custom_question');
                    const labelEl   = document.querySelector(`label[for="${el.id}"]`)
                                   || container?.querySelector('label');

                    let label = labelEl ? labelEl.innerText.replace('*','').trim() : (el.name || el.id);
                    const isRequired = el.required
                        || el.getAttribute('aria-required') === 'true'
                        || (labelEl && labelEl.innerText.includes('*'));

                    let fi = { id: el.id, name: el.name, label, required: isRequired };

                    if (el.getAttribute('role') === 'combobox') {
                        fi.type = 'react-select';
                        fi.options = [];
                    } else if (el.type === 'checkbox') {
                        fi.type = 'checkbox';
                        const existing = fields.find(f => f.name === fi.name);
                        if (existing) {
                            existing.options = existing.options || [];
                            existing.options.push({ value: el.value, id: el.id, label });
                            return;
                        }
                        fi.options = [{ value: el.value, id: el.id, label }];
                    } else if (el.type === 'radio') {
                        fi.type = 'radio';
                        const existing = fields.find(f => f.name === fi.name);
                        if (existing) {
                            existing.options = existing.options || [];
                            existing.options.push({ value: el.value, id: el.id, label });
                            return;
                        }
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

            result["logs"].append(
                f"Found {len(questions_data)} custom questions. Asking Groq for answers..."
            )

            prompt = f"""
You are helping a candidate fill out a job application form.
Based on the candidate's profile, answer the following custom form fields.

Candidate Profile:
{json.dumps(candidate_profile)}

CRITICAL INSTRUCTIONS:
1. LinkedIn: use the EXACT `linkedin` URL from the candidate profile.
2. GitHub/Portfolio: use `github` or `portfolio` URL from the profile.
3. Work authorization / sponsorship / visa questions:
   - If the candidate is from India applying to a US company and needs sponsorship: "Yes"
   - If the job is remote-India and no sponsorship needed: "No"
   - Use candidate profile `sponsorship` field if present.
4. Gender/race/veteran/disability: use `gender`, `hispanic`, `veteran`, `disability` from profile.
5. "How did you hear about us" -> "LinkedIn"
6. checkbox: return the `id` of the option to check.
7. radio: return the `id` of the option to select.
8. Employment agreements / non-compete -> "No"
9. "Have you worked at / consulted for this company before" -> "No"
10. Reasonable accommodation / accessibility adjustments -> "No"

IMPORTANT FOR react-select DROPDOWNS (type=react-select):
Return the EXACT visible text of the option as it appears in the dropdown.
Examples of exact option texts you must use:
- Employment agreement: "No"
- Visa sponsorship: "No" or "Yes"
- Country of residence: "India" (just country name, no phone code)
- Gender: "Male" / "Female" / "Decline to self identify"
- Hispanic/Latino: "No" / "Yes"
- Previously worked here: "No" / "Yes"
- Veteran status: "I am not a protected veteran"
- Disability status: "No, I do not have a disability and have not had one in the past"

Form Fields:
{json.dumps(questions_data, indent=2)}

Return a JSON object: keys = field `id`, values = answer string.
"""

            client = Groq(api_key=os.getenv("GROQ_API_KEY"))
            chat_completion = client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.3-70b-versatile",
                response_format={"type": "json_object"},
            )
            answers = json.loads(chat_completion.choices[0].message.content)

            for field_id, answer_val in answers.items():
                if not field_id:
                    continue
                try:
                    field_info = next(
                        (f for f in questions_data if f['id'] == field_id or f['name'] == field_id),
                        None
                    )
                    if not field_info:
                        for q in questions_data:
                            for opt in q.get('options', []):
                                if opt.get('id') == field_id:
                                    field_info = q
                                    break
                            if field_info:
                                break
                    if not field_info:
                        continue

                    field_type = field_info.get('type', 'text')

                    if field_type == 'react-select':
                        ok = _fill_react_select(
                            page, field_info['id'], str(answer_val), result["logs"]
                        )
                        if not ok:
                            result["logs"].append(
                                f"WARNING: react-select fill failed for {field_id} with '{answer_val}'"
                            )

                    elif field_type == 'checkbox':
                        page.locator(f"[id='{answer_val}']").click(force=True, timeout=5000)

                    elif field_type == 'radio':
                        radio_el = page.locator(f"[id='{answer_val}']")
                        radio_el.click(force=True, timeout=5000)
                        radio_el.evaluate(
                            "el => el.dispatchEvent(new Event('change', { bubbles: true }))"
                        )

                    else:
                        selector = (
                            f"[id='{field_info['id']}']" if field_info['id'] and "'" not in field_info['id']
                            else f"[name='{field_info['name']}']"
                        )
                        page.locator(selector).fill(str(answer_val), timeout=5000)
                        page.locator(selector).evaluate(
                            "el => el.dispatchEvent(new Event('blur', { bubbles: true }))"
                        )

                    result["logs"].append(f"Filled {field_id} with: {answer_val}")

                except Exception as e:
                    result["logs"].append(f"Failed to fill {field_id}: {str(e)}")

        except Exception as e:
            result["logs"].append(f"Error processing custom questions: {str(e)}")

        page.wait_for_timeout(1500)

        # Screenshot before submit
        try:
            page.screenshot(
                path=f"data/debug_before_submit_{application.id}.png", full_page=True
            )
            result["logs"].append(
                f"Saved debug screenshot: data/debug_before_submit_{application.id}.png"
            )
        except:
            pass

        # Block submit on visible validation errors
        try:
            raw_errors = page.locator(
                ".field_with_errors, [class*='error']:visible, [class*='invalid']:visible"
            ).all_inner_texts()
            blocking = [e.strip() for e in raw_errors if "This field is required" in e]
            if blocking:
                result["logs"].append(f"BLOCKED SUBMIT: Validation errors: {blocking}")
                result["status"] = "VALIDATION_FAILED"
                return
        except:
            pass

        page.wait_for_timeout(500)

        # Submit
        try:
            submit_btn = page.locator("input#submit_app, button#submit_app, button[type='submit']")
            if submit_btn.count() > 0:
                submit_btn.first.click()
                result["logs"].append("Clicked actual submit button! Application sent.")
                page.wait_for_timeout(4000)
                if "application" in page.url or "jobs" in page.url:
                    try:
                        error_texts = page.locator(
                            ".error-message, .field_with_errors"
                        ).all_inner_texts()
                        if error_texts:
                            result["logs"].append(
                                f"VALIDATION ERRORS FOUND AFTER SUBMIT: {error_texts}"
                            )
                            result["status"] = "VALIDATION_FAILED"
                    except:
                        pass
            else:
                result["logs"].append("Could not find submit button. Submission failed.")
        except Exception as e:
            result["logs"].append(f"Error clicking submit button: {str(e)}")
