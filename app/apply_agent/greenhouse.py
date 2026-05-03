from playwright.sync_api import Page
from app.apply_agent.base import BaseApplyAgent
from app.models.application import Application
import json
import os
from groq import Groq


def _fill_react_select(page, field_id: str, answer_text: str, logs: list) -> bool:
    """
    Interact with a React Select combobox widget exactly like a human:
      1. Click the toggle button (or control div) to open the dropdown
      2. Wait for the listbox/menu to appear
      3. Find the option whose visible text best matches answer_text
      4. Click it

    Greenhouse uses react-select for ALL dropdowns — there are zero native
    <select> elements on the page. select_option() and JS value-setters
    do not work here; only real click interaction does.
    """
    toggle_btn  = page.locator(f"[id='{field_id}']").locator("xpath=ancestor::div[contains(@class,'select__container')]").locator("button[aria-label='Toggle flyout']")
    control_div = page.locator(f"[id='{field_id}']").locator("xpath=ancestor::div[contains(@class,'select__control')]")

    # Step 1 — open the menu
    try:
        if toggle_btn.count() > 0:
            toggle_btn.click(timeout=4000)
        else:
            control_div.click(timeout=4000)
        page.wait_for_timeout(400)
    except Exception as e:
        logs.append(f"react-select open failed for {field_id}: {e}")
        return False

    # Step 2 — wait for the listbox to appear
    listbox = page.locator(f"[id='react-select-{field_id}-listbox'], div[role='listbox']").first
    try:
        listbox.wait_for(state="visible", timeout=4000)
    except Exception:
        # fallback: any visible option
        pass

    # Step 3 — find best matching option
    #   React Select renders options as divs with id react-select-{id}-option-{n}
    options = page.locator(f"[id^='react-select-{field_id}-option']")
    count = options.count()
    if count == 0:
        # fallback: grab all visible option divs in an open menu
        options = page.locator("div[role='option']")
        count = options.count()

    if count == 0:
        logs.append(f"react-select: no options visible for {field_id} after opening")
        page.keyboard.press("Escape")
        return False

    logs.append(f"react-select: {count} options visible for {field_id}, looking for '{answer_text}'")

    # Collect all option texts for matching
    option_texts = []
    for i in range(count):
        try:
            option_texts.append(options.nth(i).inner_text().strip())
        except Exception:
            option_texts.append("")

    # Match priority: exact → case-insensitive → partial
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
        for i, t in enumerate(option_texts):
            if answer_text.lower() in t.lower():
                matched_idx = i
                logs.append(f"react-select: partial-matched '{answer_text}' -> '{t}'")
                break

    if matched_idx is None:
        logs.append(f"react-select: no match for '{answer_text}' in {option_texts}. Closing.")
        page.keyboard.press("Escape")
        return False

    # Step 4 — click the matched option
    try:
        options.nth(matched_idx).click(timeout=3000)
        page.wait_for_timeout(300)
        logs.append(f"react-select: clicked option '{option_texts[matched_idx]}' for {field_id}")
        return True
    except Exception as e:
        logs.append(f"react-select: click failed for {field_id}: {e}")
        return False


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
            page.locator(selector).evaluate("el => el.dispatchEvent(new Event('blur', { bubbles: true }))")

        fill_and_blur("input#first_name", candidate_profile.get("first_name", ""))
        fill_and_blur("input#last_name",  candidate_profile.get("last_name",  ""))
        fill_and_blur("input#email",      candidate_profile.get("email",      ""))
        fill_and_blur("input#phone",      candidate_profile.get("phone",      ""))
        page.wait_for_timeout(500)

        # Upload resume
        if application.tailored_resume_pdf_path:
            resume_input = page.locator("input[type='file'][name='job_application[answers_attributes][0][resume]']")
            if resume_input.count() > 0:
                resume_input.set_input_files(application.tailored_resume_pdf_path)
            else:
                attach_button = page.locator("button:has-text('Attach')").first
                if attach_button.count() > 0:
                    with page.expect_file_chooser() as fc_info:
                        attach_button.click()
                    fc_info.value.set_files(application.tailored_resume_pdf_path)

        # ------------------------------------------------------------------ #
        #  Scrape custom questions                                            #
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
                    if (['first_name','last_name','email','phone'].includes(el.id)) return;
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
                        // React Select — collect visible options from the container's
                        // hidden select-equivalent or note it as react-select type
                        fi.type = 'react-select';
                        // Try to find options from a sibling hidden input or data
                        // (Options are only in DOM when menu is open, so we just mark the type)
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

            result["logs"].append(f"Found {len(questions_data)} custom questions. Asking Groq for answers...")

            prompt = f"""
You are helping a candidate fill out a job application form.
Based on the candidate's profile, answer the following custom form fields.

Candidate Profile:
{json.dumps(candidate_profile)}

CRITICAL INSTRUCTIONS:
1. If asked for a LinkedIn profile, use the EXACT `linkedin` URL from the candidate profile.
2. If asked for a GitHub or Portfolio, use the `github` or `portfolio` URL from the profile.
3. If asked about work authorization, sponsorship, or visas: if they need sponsorship, answer "Yes". If not, answer "No".
4. If asked about gender, race/ethnicity, veteran, or disability: use `gender`, `hispanic`, `veteran`, `disability` from the profile.
5. If asked "How did you hear about us", say "LinkedIn".
6. For `checkbox` types, output the `id` of the checkbox to check.
7. For `radio` types, output the `id` of the option to select.
8. If asked about employment agreements or non-compete, answer "No".
9. If asked "Have you ever been employed by GitLab", answer "No".
10. Answer "No" or "None" to reasonable accommodation questions.

IMPORTANT FOR react-select FIELDS:
- These are dropdown menus. Return the EXACT visible text of the option to select.
- Example: for "Are you subject to employment agreements?" return "No"
- Example: for "Will you require sponsorship?" return "No" (or "Yes" if applicable)
- Example: for "What is your current country of residence?" return "India"
- For gender: return the exact text like "Male", "Female", "Decline to self identify"
- For veteran status: return the exact text like "I am not a protected veteran"
- For disability: return the exact text like "No, I don't have a disability"

Form Fields:
{json.dumps(questions_data, indent=2)}

Return a JSON object: keys = field `id` (or `name` if id empty), values = the answer string.
For react-select: value = exact visible option text to click.
For checkbox/radio: value = the `id` of the option element to click.
For text: value = the string to type.
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
                        ok = _fill_react_select(page, field_info['id'], str(answer_val), result["logs"])
                        if not ok:
                            result["logs"].append(f"WARNING: react-select fill failed for {field_id} with '{answer_val}'")

                    elif field_type == 'checkbox':
                        page.locator(f"[id='{answer_val}']").click(force=True, timeout=5000)

                    elif field_type == 'radio':
                        radio_el = page.locator(f"[id='{answer_val}']")
                        radio_el.click(force=True, timeout=5000)
                        radio_el.evaluate("el => el.dispatchEvent(new Event('change', { bubbles: true }))")

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

        # Screenshot just before submit
        try:
            page.screenshot(path=f"data/debug_before_submit_{application.id}.png", full_page=True)
            result["logs"].append(f"Saved debug screenshot: data/debug_before_submit_{application.id}.png")
        except:
            pass

        # Block submit if required fields still show validation errors
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
                        error_texts = page.locator(".error-message, .field_with_errors").all_inner_texts()
                        if error_texts:
                            result["logs"].append(f"VALIDATION ERRORS FOUND AFTER SUBMIT: {error_texts}")
                            result["status"] = "VALIDATION_FAILED"
                    except:
                        pass
            else:
                result["logs"].append("Could not find submit button. Submission failed.")
        except Exception as e:
            result["logs"].append(f"Error clicking submit button: {str(e)}")
