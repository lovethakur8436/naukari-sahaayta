from playwright.sync_api import Page
from app.apply_agent.base import BaseApplyAgent
from app.models.application import Application

class GreenhouseApplyAgent(BaseApplyAgent):
    def run_apply_flow(self, page: Page, application: Application, candidate_profile: dict, result: dict):
        url = application.job.url
        result["logs"].append(f"Navigating to {url}")
        page.goto(url)
        
        # Fill first name
        page.fill("input#first_name", candidate_profile.get("first_name", ""))
        
        # Fill last name
        page.fill("input#last_name", candidate_profile.get("last_name", ""))
        
        # Fill email
        page.fill("input#email", candidate_profile.get("email", ""))
        
        # Fill phone
        page.fill("input#phone", candidate_profile.get("phone", ""))
        
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
        
        # Custom questions logic can be added here
        # E.g., filling out required text areas or selects using LLM
        
        # Finally submit (Commented out to prevent actual submission during testing)
        # page.click("input#submit_app")
        result["logs"].append("Filled Greenhouse form successfully (Submit simulated)")
