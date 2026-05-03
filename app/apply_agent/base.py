from abc import ABC, abstractmethod
from playwright.sync_api import sync_playwright, Page
from app.models.application import Application

class BaseApplyAgent(ABC):
    def __init__(self, headless=True):
        self.headless = headless

    def apply(self, application: Application, candidate_profile: dict) -> dict:
        """
        Orchestrate the application process
        """
        result = {"status": "FAILED", "logs": [], "screenshot": None}
        try:
            with sync_playwright() as p:
                # Add slow_mo to slow down the automation so the user can visually track it
                browser = p.chromium.launch(headless=self.headless, slow_mo=500)
                context = browser.new_context()
                page = context.new_page()
                
                self.run_apply_flow(page, application, candidate_profile, result)
                
                # Take success screenshot
                screenshot_path = f"data/success_{application.id}.png"
                page.screenshot(path=screenshot_path)
                result["screenshot"] = screenshot_path
                result["status"] = "SUCCESS"
                
                browser.close()
        except Exception as e:
            result["logs"].append(f"Exception during apply: {str(e)}")
            print(f"Apply failed: {e}")
            
        return result

    @abstractmethod
    def run_apply_flow(self, page: Page, application: Application, candidate_profile: dict, result: dict):
        pass
