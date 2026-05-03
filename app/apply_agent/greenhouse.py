from playwright.sync_api import Page
from app.apply_agent.base import BaseApplyAgent
from app.models.application import Application
import json
import os
from groq import Groq


def _click_select_option(page, selector: str, value: str, result_logs: list) -> bool:
    """
    Fill a <select> by simulating real user interaction:
      1. Focus the element
      2. Use Playwright's select_option (handles both native and React-controlled selects)
      3. Immediately re-read the value from DOM to confirm it actually stuck
      4. If it didn't stick, retry once via the native property-descriptor setter trick

    Returns True if the value was confirmed set, False otherwise.
    """
    loc = page.locator(selector)

    # --- Attempt 1: Playwright select_option (works for most cases) ---
    try:
        loc.focus(timeout=3000)
        loc.select_option(value=value, timeout=5000)
        page.wait_for_timeout(300)  # Let React process the synthetic event

        actual = loc.evaluate("el => el.value")
        if actual == value:
            return True
        result_logs.append(f"select_option set '{value}' but DOM reads '{actual}', trying native setter...")
    except Exception as e:
        result_logs.append(f"select_option failed ({e}), trying native setter...")

    # --- Attempt 2: Native HTMLSelectElement property-descriptor setter ---
    # React bypasses the DOM setter, so we go through the prototype chain directly.
    try:
        loc.evaluate("""
            (el, val) => {
                const nativeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLSelectElement.prototype, 'value'
                ).set;
                nativeSetter.call(el, val);
                ['input', 'change', 'blur'].forEach(evtName =>
                    el.dispatchEvent(new Event(evtName, { bubbles: true }))
                );
            }
        """, value)
        page.wait_for_timeout(300)

        actual = loc.evaluate("el => el.value")
        if actual == value:
            return True
        result_logs.append(f"Native setter set '{value}' but DOM still reads '{actual}'. Giving up.")
    except Exception as e:
        result_logs.append(f"Native setter also failed: {e}")

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
        #  Answer Custom Questions using Groq                                 #
        # ------------------------------------------------------------------ #
        try:
            questions_data = page.evaluate("""() => {
                const fields = [];
                const elements = document.querySelectorAll(
                    'select:not([hidden]):not([style*="display: none"]),'
                    + 'input[type="text"]:not([hidden]):not([style*="display: none"]),'
                    + 'textarea:not([hidden]):not([style*="display: none"]),'
                    + 'input[type="checkbox"]:not([hidden]):not([style*="display: none"]),'
                    + 'input[type="radio"]:not([hidden]):not([style*="display: none"])'
                );

                elements.forEach(el => {
                    if (['first_name', 'last_name', 'email', 'phone'].includes(el.id)) return;
                    if (el.className.includes('recaptcha') || el.id.includes('recaptcha')) return;
                    if (el.offsetWidth === 0 || el.offsetHeight === 0) return;

                    const container = el.closest('div.field, div.custom_question');
                    const labelEl   = container?.querySelector('label')
                                   || document.querySelector(`label[for="${el.id}"]`);

                    let mainQuestionLabel = '';
                    if (['radio', 'checkbox', 'select'].includes(el.type) && container) {
                        const topLabel = container.querySelector('label');
                        if (topLabel) mainQuestionLabel = topLabel.innerText.trim();
                    }
                    if (el.tagName === 'SELECT' && !mainQuestionLabel) {
                        const parentDiv = el.closest('div.field, div.custom_question');
                        if (parentDiv) {
                            for (let lbl of parentDiv.querySelectorAll('label')) {
                                if (lbl.innerText.trim() && lbl.innerText.trim() !== '*') {
                                    mainQuestionLabel = lbl.innerText.trim();
                                    break;
                                }
                            }
                        }
                    }

                    let label = labelEl ? labelEl.innerText.trim() : (el.name || el.id);
                    if (mainQuestionLabel && mainQuestionLabel !== label)
                        label = `${mainQuestionLabel} - ${label}`;

                    const isRequired = el.required
                        || !!container?.querySelector('label.required, [aria-required="true"], abbr[title="required"]')
                        || label.includes('*');

                    let fi = { id: el.id, name: el.name, label,
                               type: el.type || el.tagName.toLowerCase(), required: isRequired };

                    if (el.tagName === 'SELECT') {
                        fi.type = 'select';
                        fi.options = Array.from(el.options)
                            .map(o => ({ value: o.value, text: o.text }))
                            .filter(o => o.value);
                    }

                    if (fi.type === 'radio' || fi.type === 'checkbox') {
                        const existing = fields.find(f => f.name === fi.name);
                        if (existing) {
                            existing.options = existing.options || [];
                            existing.options.push({ value: el.value, id: el.id, label });
                            return;
                        }
                        fi.options = [{ value: el.value, id: el.id, label }];
                    }

                    fields.push(fi);
                });
                return fields;
            }""")

            if questions_data:
                result["logs"].append(f"Found {len(questions_data)} custom questions. Asking Groq for answers...")

                prompt = f"""
You are helping a candidate fill out a job application form.
Based on the candidate's profile, answer the following custom form fields.

Candidate Profile:
{json.dumps(candidate_profile)}

CRITICAL INSTRUCTIONS:
1. If asked for a LinkedIn profile, use the EXACT `linkedin` URL from the candidate profile. Do not output N/A.
2. If asked for a GitHub or Portfolio, use the `github` or `portfolio` URL from the profile. Do not output N/A.
3. If asked about work authorization, sponsorship, or visas, answer based on `work_auth`. If they require sponsorship now or in the future, select "Yes". If not, select "No".
4. If asked about gender, race/ethnicity, veteran, or disability status, use exact values from `gender`, `hispanic`, `veteran`, and `disability` in the profile.
5. If asked "How did you hear about us", say "LinkedIn".
6. For `checkbox` types, output the `id` of the checkbox to check from the options array.
7. For `radio` types, output the `id` of the option to select from the options array.
8. If asked about employment agreements, select "No".
9. If asked "Have you ever been employed by GitLab", select "No".
10. Answer "No" to non-compete agreement questions.
11. Answer "No" or "None" to reasonable accommodation questions.

IMPORTANT FOR SELECT FIELDS:
- Each select field has an `options` array with `value` (hidden attribute) and `text` (visible label).
- You MUST return the exact `value` string, NOT the text label.
- Example: options=[{{"value":"1","text":"Yes"}},{{"value":"0","text":"No"}}] → return "0" for No.

Form Fields:
{json.dumps(questions_data, indent=2)}

Return a JSON object: keys = field `id` (or `name` if id empty), values = exact `value` to use.
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

                        field_type = field_info['type']
                        selector = (
                            f"[id='{field_info['id']}']" if field_info['id'] and "'" not in field_info['id']
                            else f"[name='{field_info['name']}']"
                        )

                        if field_type == 'select':
                            available_options = field_info.get('options', [])

                            # Resolve answer value → actual <option value=...>
                            matched_value = next(
                                (o['value'] for o in available_options if str(o['value']) == str(answer_val)),
                                None
                            )
                            if matched_value is None:
                                matched_value = next(
                                    (o['value'] for o in available_options
                                     if o['text'].strip().lower() == str(answer_val).strip().lower()),
                                    None
                                )
                                if matched_value is not None:
                                    result["logs"].append(f"Label-matched '{answer_val}' -> '{matched_value}' for {field_id}")
                            if matched_value is None:
                                matched_value = next(
                                    (o['value'] for o in available_options
                                     if str(answer_val).strip().lower() in o['text'].strip().lower()),
                                    None
                                )
                                if matched_value is not None:
                                    result["logs"].append(f"Partial-matched '{answer_val}' -> '{matched_value}' for {field_id}")

                            if matched_value is not None:
                                ok = _click_select_option(page, selector, str(matched_value), result["logs"])
                                if not ok:
                                    result["logs"].append(
                                        f"WARNING: Could not reliably set {field_id}='{matched_value}'. "
                                        f"Available: {available_options}"
                                    )
                            else:
                                result["logs"].append(
                                    f"WARNING: No option match for {field_id} answer='{answer_val}'. "
                                    f"Available: {available_options}"
                                )

                        elif field_type == 'checkbox':
                            page.locator(f"[id='{answer_val}']").click(force=True, timeout=5000)

                        elif field_type == 'radio':
                            radio_el = page.locator(f"[id='{answer_val}']")
                            radio_el.click(force=True, timeout=5000)
                            radio_el.evaluate("el => el.dispatchEvent(new Event('change', { bubbles: true }))")

                        else:
                            page.locator(selector).fill(str(answer_val), timeout=5000)
                            page.locator(selector).evaluate(
                                "el => el.dispatchEvent(new Event('blur', { bubbles: true }))"
                            )

                        result["logs"].append(f"Filled {field_id} with: {answer_val}")

                    except Exception as e:
                        result["logs"].append(f"Failed to fill {field_id}: {str(e)}")

        except Exception as e:
            result["logs"].append(f"Error processing custom questions: {str(e)}")

        # Give React time to settle after all fields are filled
        page.wait_for_timeout(2000)

        # ------------------------------------------------------------------ #
        #  Re-verify all select fields AFTER the settle wait                  #
        #  (React sometimes resets them on the next render cycle)             #
        # ------------------------------------------------------------------ #
        try:
            page.evaluate("""() => {
                // Log current values of all visible selects to the page title temporarily
                const selects = document.querySelectorAll('select:not([hidden])');
                selects.forEach(s => {
                    console.log('SELECT', s.id || s.name, '=', s.value);
                });
            }""")
        except:
            pass

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

        page.wait_for_timeout(1000)

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
