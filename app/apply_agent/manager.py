import json
from sqlalchemy.orm import Session
from app.models.application import Application
from app.apply_agent.greenhouse import GreenhouseApplyAgent
# Import others as needed

def process_application(db: Session, application: Application, candidate_profile: dict):
    if application.job.portal.lower() == "greenhouse":
        # Launch headed (non-headless) mode so the user can watch the browser automation live
        agent = GreenhouseApplyAgent(headless=False)
    else:
        print(f"No agent configured for {application.job.portal}")
        return
        
    result = agent.apply(application, candidate_profile)
    
    # Save logs
    log_path = f"data/apply_log_{application.id}.json"
    with open(log_path, 'w') as f:
        json.dump(result, f, indent=2)
        
    application.submission_log_json_path = log_path
    application.screenshot_path = result.get("screenshot")
    application.status = "AUTO_APPLIED" if result["status"] == "SUCCESS" else "FAILED"
    
    db.commit()
