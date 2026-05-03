from playwright.sync_api import Page
from app.apply_agent.base import BaseApplyAgent
from app.models.application import Application
import json
import os
import google.generativeai as genai

class GreenhouseApplyAgent(BaseApplyAgent):
    def run_apply_flow(self, page: Page, application: Application, candidate_profile: dict, result: dict):
        url = application.job.url
        result["logs"].append(f"Navigating to {url}")
        page.goto(url)
        
        # Take a screenshot BEFORE filling so we can debug the initial state
        try:
            page.screenshot(path=f"data/debug_initial_{application.id}.png")
        except: pass
        
        # Fill first name
        page.fill("input#first_name", candidate_profile.get("first_name", ""))
        
        # Fill last name
        page.fill("input#last_name", candidate_profile.get("last_name", ""))
        
        # Fill email
        page.fill("input#email", candidate_profile.get("email", ""))
        
        # Fill phone
        page.fill("input#phone", candidate_profile.get("phone", ""))
        
        # Click outside to trigger validation on standard fields
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        
        # Upload resume
        if application.tailored_resume_pdf_path:
            # Greenhouse often has a file input for resume
            resume_input = page.locator("input[type='file'][name='job_application[answers_attributes][0][resume]']")
            if resume_input.count() > 0:
                resume_input.set_input_files(application.tailored_resume_pdf_path)
            else:
                # Alternative attach button
                attach_button = page.locator("button:has-text('Attach')").first
                if attach_button.count() > 0:
                    with page.expect_file_chooser() as fc_info:
                        attach_button.click()
                    file_chooser = fc_info.value
                    file_chooser.set_files(application.tailored_resume_pdf_path)
        
        # Answer Custom Questions using Gemini
        try:
            questions_data = page.evaluate("""() => {
                const fields = [];
                // Only find VISIBLE elements to avoid reCAPTCHA and hidden tokens
                const elements = document.querySelectorAll('select:not([hidden]):not([style*="display: none"]), input[type="text"]:not([hidden]):not([style*="display: none"]), textarea:not([hidden]):not([style*="display: none"]), input[type="checkbox"]:not([hidden]):not([style*="display: none"]), input[type="radio"]:not([hidden]):not([style*="display: none"])');
                
                elements.forEach(el => {
                    // Skip standard fields and invisible reCAPTCHA textareas
                    if (['first_name', 'last_name', 'email', 'phone'].includes(el.id)) return;
                    if (el.className.includes('recaptcha') || el.id.includes('recaptcha')) return;
                    if (el.offsetWidth === 0 || el.offsetHeight === 0) return;
                    
                    const container = el.closest('div.field, div.custom_question');
                    const labelEl = container?.querySelector('label') || document.querySelector(`label[for="${el.id}"]`);
                    
                    // Extract main question label for grouped elements
                    let mainQuestionLabel = "";
                    if (['radio', 'checkbox', 'select'].includes(el.type) && container) {
                        const topLabel = container.querySelector('label'); // Usually the first label in the div is the question
                        if (topLabel) {
                            mainQuestionLabel = topLabel.innerText.trim();
                        }
                    }
                    
                    // Specific fix for Greenhouse dropdowns (Select2)
                    // Sometimes the visible dropdown has a sibling or parent label that is structured differently
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
                    
                    let field_info = { id: el.id, name: el.name, label: label, type: el.type || el.tagName.toLowerCase() };
                    
                    if (el.tagName === 'SELECT') {
                        field_info.type = 'select'; // Ensure it's marked as select explicitly
                        field_info.options = Array.from(el.options).map(o => ({ value: o.value, text: o.text })).filter(o => o.value);
                    }
                    
                    // Group radio buttons and checkboxes by name so we don't send duplicates
                    if (field_info.type === 'radio' || field_info.type === 'checkbox') {
                        const existing = fields.find(f => f.name === field_info.name);
                        if (existing) {
                            if (!existing.options) existing.options = [];
                            existing.options.push({ value: el.value, id: el.id, label: label });
                            return; // Don't add a new field, just appended to existing
                        } else {
                            field_info.options = [{ value: el.value, id: el.id, label: label }];
                        }
                    }
                    
                    fields.push(field_info);
                });
                return fields;
            }""")
            
            if questions_data:
                result["logs"].append(f"Found {len(questions_data)} custom questions. Asking Gemini for answers...")
                
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
                
                Form Fields to answer:
                {json.dumps(questions_data, indent=2)}
                
                Return a JSON object where keys are the field `id` (or `name` if id is empty or it's a radio/checkbox group), and values are the exact string to fill in or the exact `id` to click.
                For `select` types, you MUST provide the exact `value` from the provided options array. DO NOT provide the label text, provide the hidden `value` attribute.
                """
                
                genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
                model = genai.GenerativeModel('gemini-2.5-pro', generation_config={"response_mime_type": "application/json"})
                response = model.generate_content(prompt)
                
                answers = json.loads(response.text)
                
                for field_id, answer_val in answers.items():
                    if not field_id: continue
                    try:
                        # Find the field type from our scraped data
                        field_info = next((f for f in questions_data if f['id'] == field_id or f['name'] == field_id), None)
                        
                        # In case Gemini returns the ID of a specific radio button instead of the group name
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
                            # For select, Gemini must return the value, not the text
                            selector = f"[id='{field_info['id']}']" if "'" not in field_info['id'] else f"[name='{field_info['name']}']"
                            # In Greenhouse, some selects are styled with select2 or chosen plugins.
                            # Standard select_option works on the hidden select, but we should force it.
                            page.locator(selector).select_option(value=str(answer_val), timeout=5000, force=True)
                            
                            # Fire multiple events to ensure React/Vue/jQuery plugins catch the change
                            page.locator(selector).evaluate("""el => {
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('blur', { bubbles: true }));
                            }""")
                        elif field_type == 'checkbox':
                            # Gemini now returns the ID of the checkbox it wants to check
                            checkbox_el = page.locator(f"[id='{answer_val}']")
                            checkbox_el.click(force=True, timeout=5000)
                        elif field_type == 'radio':
                            # For radio, Gemini should return the ID of the specific radio button
                            # Wait for the specific radio element, ensure it's visible, and click its parent label or itself
                            radio_el = page.locator(f"[id='{answer_val}']")
                            # Radio buttons in Greenhouse are often hidden visually with CSS, and you must click the label
                            radio_el.click(force=True, timeout=5000)
                            
                            # Fire change event on radio
                            radio_el.evaluate("el => el.dispatchEvent(new Event('change', { bubbles: true }))")
                        else:
                            selector = f"[id='{field_info['id']}']" if "'" not in field_info['id'] else f"[name='{field_info['name']}']"
                            page.locator(selector).fill(str(answer_val), timeout=5000)
                            
                            # Fire blur event for text inputs in case of validation triggers
                            page.locator(selector).evaluate("el => el.dispatchEvent(new Event('blur', { bubbles: true }))")
                            
                        result["logs"].append(f"Filled {field_id} with: {answer_val}")
                    except Exception as e:
                        result["logs"].append(f"Failed to fill {field_id}: {str(e)}")
                        
        except Exception as e:
            result["logs"].append(f"Error processing custom questions: {str(e)}")
            
        # Take a screenshot right before clicking submit so we can see what was missed
        try:
            page.screenshot(path=f"data/debug_before_submit_{application.id}.png", full_page=True)
            result["logs"].append(f"Saved debug screenshot: data/debug_before_submit_{application.id}.png")
        except: pass
            
        page.wait_for_timeout(3000) # Wait a moment to ensure UI updates
        
        # Finally submit (Actual submission enabled)
        try:
            submit_btn = page.locator("input#submit_app, button#submit_app, button[type='submit']")
            if submit_btn.count() > 0:
                submit_btn.first.click()
                result["logs"].append("Clicked actual submit button! Application sent.")
                # Wait briefly for success page to load so the screenshot captures it
                page.wait_for_timeout(4000)
                
                # Verify if we actually left the application page
                if "application" in page.url or "jobs" in page.url:
                    # We might still be on the same page due to a validation error
                    try:
                        error_texts = page.locator(".error-message, .field_with_errors").all_inner_texts()
                        if error_texts:
                            result["logs"].append(f"VALIDATION ERRORS FOUND ON PAGE: {error_texts}")
                    except: pass
            else:
                result["logs"].append("Could not find submit button. Submission failed.")
        except Exception as e:
            result["logs"].append(f"Error clicking submit button: {str(e)}")
