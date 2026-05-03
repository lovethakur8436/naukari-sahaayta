from playwright.sync_api import Page
from app.apply_agent.base import BaseApplyAgent
from app.models.application import Application
import json
import os
from groq import Groq

class GreenhouseApplyAgent(BaseApplyAgent):
    def run_apply_flow(self, page: Page, application: Application, candidate_profile: dict, result: dict):
        url = application.job.url
        result["logs"].append(f"Navigating to {url}")
        page.goto(url)

        # Wait for the page JS (React/Vue/jQuery) to fully initialize before touching any fields
        page.wait_for_load_state("networkidle")

        # Take a screenshot BEFORE filling so we can debug the initial state
        try:
            page.screenshot(path=f"data/debug_initial_{application.id}.png")
        except: pass

        # Fill first name and immediately dispatch blur so the framework registers the change
        page.fill("input#first_name", candidate_profile.get("first_name", ""))
        page.locator("input#first_name").evaluate("el => el.dispatchEvent(new Event('blur', { bubbles: true }))")

        # Fill last name
        page.fill("input#last_name", candidate_profile.get("last_name", ""))
        page.locator("input#last_name").evaluate("el => el.dispatchEvent(new Event('blur', { bubbles: true }))")

        # Fill email
        page.fill("input#email", candidate_profile.get("email", ""))
        page.locator("input#email").evaluate("el => el.dispatchEvent(new Event('blur', { bubbles: true }))")

        # Fill phone
        page.fill("input#phone", candidate_profile.get("phone", ""))
        page.locator("input#phone").evaluate("el => el.dispatchEvent(new Event('blur', { bubbles: true }))")

        # Small wait to let any validation/re-render settle after filling standard fields
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
                    file_chooser = fc_info.value
                    file_chooser.set_files(application.tailored_resume_pdf_path)

        # Answer Custom Questions using Groq (free tier, 14,400 req/day)
        try:
            questions_data = page.evaluate("""() => {
                const fields = [];
                const elements = document.querySelectorAll('select:not([hidden]):not([style*="display: none"]), input[type="text"]:not([hidden]):not([style*="display: none"]), textarea:not([hidden]):not([style*="display: none"]), input[type="checkbox"]:not([hidden]):not([style*="display: none"]), input[type="radio"]:not([hidden]):not([style*="display: none"])');

                elements.forEach(el => {
                    if (['first_name', 'last_name', 'email', 'phone'].includes(el.id)) return;
                    if (el.className.includes('recaptcha') || el.id.includes('recaptcha')) return;
                    if (el.offsetWidth === 0 || el.offsetHeight === 0) return;

                    const container = el.closest('div.field, div.custom_question');
                    const labelEl = container?.querySelector('label') || document.querySelector(`label[for="${el.id}"]`);

                    let mainQuestionLabel = "";
                    if (['radio', 'checkbox', 'select'].includes(el.type) && container) {
                        const topLabel = container.querySelector('label');
                        if (topLabel) mainQuestionLabel = topLabel.innerText.trim();
                    }

                    if (el.tagName === 'SELECT' && !mainQuestionLabel) {
                        const parentDiv = el.closest('div.field, div.custom_question');
                        if (parentDiv) {
                            const potentialLabels = parentDiv.querySelectorAll('label');
                            for (let lbl of potentialLabels) {
                                if (lbl.innerText.trim() && lbl.innerText.trim() !== '*') {
                                    mainQuestionLabel = lbl.innerText.trim();
                                    break;
                                }
                            }
                        }
                    }

                    let label = labelEl ? labelEl.innerText.trim() : (el.name || el.id);
                    if (mainQuestionLabel && mainQuestionLabel !== label) label = `${mainQuestionLabel} - ${label}`;

                    // Check if field is required
                    const isRequired = el.required || !!container?.querySelector('label.required, [aria-required="true"], abbr[title="required"]') || label.includes('*');

                    let field_info = { id: el.id, name: el.name, label: label, type: el.type || el.tagName.toLowerCase(), required: isRequired };

                    if (el.tagName === 'SELECT') {
                        field_info.type = 'select';
                        // Include BOTH value and text so the LLM can match by label if needed
                        field_info.options = Array.from(el.options).map(o => ({ value: o.value, text: o.text })).filter(o => o.value);
                    }

                    if (field_info.type === 'radio' || field_info.type === 'checkbox') {
                        const existing = fields.find(f => f.name === field_info.name);
                        if (existing) {
                            if (!existing.options) existing.options = [];
                            existing.options.push({ value: el.value, id: el.id, label: label });
                            return;
                        } else {
                            field_info.options = [{ value: el.value, id: el.id, label: label }];
                        }
                    }

                    fields.push(field_info);
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
                3. If asked about work authorization, sponsorship, or visas, answer based on `work_auth`. If they require sponsorship now or in the future, select "Yes" for those questions. If they do not require sponsorship, select "No".
                4. If asked about gender, race/ethnicity, veteran, or disability status, use the exact values from `gender`, `hispanic`, `veteran`, and `disability` in the profile. Note that "No" might correspond to "No, I do not have a disability" or "I am not a protected veteran". Match the semantic meaning.
                5. If asked "How did you hear about us", say "LinkedIn".
                6. For `checkbox` types (like Data Privacy Consent, Acknowledgements), output the `id` of the checkbox to be checked. Look at the options array to find the correct `id`.
                7. For `radio` types, output the `id` of the option to be selected. Look closely at the `label` for each option to decide.
                8. If asked "Are you subject to any employment agreements", select the option corresponding to "No".
                9. If asked "Have you ever been employed by GitLab", select the option corresponding to "No".
                10. Answer "No" to any questions regarding non-compete agreements.
                11. Answer "No" or "None" to questions regarding reasonable accommodations for the interview process.

                IMPORTANT FOR SELECT FIELDS:
                - Each select field has an `options` array with BOTH `value` (the hidden attribute) and `text` (the visible label).
                - You MUST return the exact `value` string from the options array, NOT the text label.
                - Example: if options are [{{"value": "1", "text": "Yes"}}, {{"value": "0", "text": "No"}}] and the answer is No, return "0" not "No".
                - Always look at the options array and pick the value whose text best matches your intended answer.

                Form Fields to answer:
                {json.dumps(questions_data, indent=2)}

                Return a JSON object where keys are the field `id` (or `name` if id is empty), and values are the exact `value` to use.
                For select types, always return the `value` attribute from the options array.
                """

                client = Groq(api_key=os.getenv("GROQ_API_KEY"))
                chat_completion = client.chat.completions.create(
                    messages=[{"role": "user", "content": prompt}],
                    model="llama-3.3-70b-versatile",
                    response_format={"type": "json_object"},
                )
                answers = json.loads(chat_completion.choices[0].message.content)

                for field_id, answer_val in answers.items():
                    if not field_id: continue
                    try:
                        field_info = next((f for f in questions_data if f['id'] == field_id or f['name'] == field_id), None)

                        if not field_info:
                            for q in questions_data:
                                if q.get('options'):
                                    for opt in q['options']:
                                        if opt.get('id') == field_id:
                                            field_info = q
                                            break

                        if not field_info:
                            continue

                        field_type = field_info['type']

                        if field_type == 'select':
                            selector = f"[id='{field_info['id']}']" if "'" not in field_info['id'] else f"[name='{field_info['name']}']"
                            available_options = field_info.get('options', [])

                            # Primary: try the value returned by Groq directly
                            matched_value = None
                            for opt in available_options:
                                if str(opt['value']) == str(answer_val):
                                    matched_value = opt['value']
                                    break

                            # Fallback: if Groq returned a label text instead of value, match by text (case-insensitive)
                            if not matched_value:
                                for opt in available_options:
                                    if opt['text'].strip().lower() == str(answer_val).strip().lower():
                                        matched_value = opt['value']
                                        result["logs"].append(f"Label-matched '{answer_val}' → value='{matched_value}' for {field_id}")
                                        break

                            # Last resort: partial text match
                            if not matched_value:
                                for opt in available_options:
                                    if str(answer_val).strip().lower() in opt['text'].strip().lower():
                                        matched_value = opt['value']
                                        result["logs"].append(f"Partial-matched '{answer_val}' → value='{matched_value}' for {field_id}")
                                        break

                            if matched_value is not None:
                                page.locator(selector).select_option(value=str(matched_value), timeout=5000, force=True)
                                page.locator(selector).evaluate("""el => {
                                    el.dispatchEvent(new Event('change', { bubbles: true }));
                                    el.dispatchEvent(new Event('input', { bubbles: true }));
                                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                                }""")
                            else:
                                result["logs"].append(f"WARNING: Could not match any option for {field_id} with answer '{answer_val}'. Available: {available_options}")

                        elif field_type == 'checkbox':
                            checkbox_el = page.locator(f"[id='{answer_val}']")
                            checkbox_el.click(force=True, timeout=5000)
                        elif field_type == 'radio':
                            radio_el = page.locator(f"[id='{answer_val}']")
                            radio_el.click(force=True, timeout=5000)
                            radio_el.evaluate("el => el.dispatchEvent(new Event('change', { bubbles: true }))")
                        else:
                            selector = f"[id='{field_info['id']}']" if "'" not in field_info['id'] else f"[name='{field_info['name']}']"
                            page.locator(selector).fill(str(answer_val), timeout=5000)
                            page.locator(selector).evaluate("el => el.dispatchEvent(new Event('blur', { bubbles: true }))")

                        result["logs"].append(f"Filled {field_id} with: {answer_val}")
                    except Exception as e:
                        result["logs"].append(f"Failed to fill {field_id}: {str(e)}")

        except Exception as e:
            result["logs"].append(f"Error processing custom questions: {str(e)}")

        # Wait for any re-renders after filling all fields
        page.wait_for_timeout(2000)

        # Take a screenshot right before clicking submit
        try:
            page.screenshot(path=f"data/debug_before_submit_{application.id}.png", full_page=True)
            result["logs"].append(f"Saved debug screenshot: data/debug_before_submit_{application.id}.png")
        except: pass

        # Check for validation errors BEFORE submitting — do not submit if required fields are empty
        try:
            validation_errors = page.locator(".field_with_errors, .error, [class*='error']:visible, [class*='invalid']:visible").all_inner_texts()
            validation_errors = [e.strip() for e in validation_errors if e.strip() and "This field is required" in e]
            if validation_errors:
                result["logs"].append(f"BLOCKED SUBMIT: Validation errors still present: {validation_errors}")
                result["status"] = "VALIDATION_FAILED"
                return
        except: pass

        page.wait_for_timeout(1000)

        # Finally submit
        try:
            submit_btn = page.locator("input#submit_app, button#submit_app, button[type='submit']")
            if submit_btn.count() > 0:
                submit_btn.first.click()
                result["logs"].append("Clicked actual submit button! Application sent.")
                page.wait_for_timeout(4000)

                # Verify if we actually left the application page
                if "application" in page.url or "jobs" in page.url:
                    try:
                        error_texts = page.locator(".error-message, .field_with_errors").all_inner_texts()
                        if error_texts:
                            result["logs"].append(f"VALIDATION ERRORS FOUND AFTER SUBMIT: {error_texts}")
                            result["status"] = "VALIDATION_FAILED"
                    except: pass
            else:
                result["logs"].append("Could not find submit button. Submission failed.")
        except Exception as e:
            result["logs"].append(f"Error clicking submit button: {str(e)}")
