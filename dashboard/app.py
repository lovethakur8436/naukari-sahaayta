import streamlit as st
import requests
import json
import pandas as pd

API_URL = "http://localhost:8000"

st.set_page_config(page_title="Job Auto-Apply Dashboard", layout="wide")
st.title("Job Application Automation System")

tab1, tab2, tab3 = st.tabs(["Jobs", "Applications", "Candidate Profile & Setup"])

with tab3:
    st.header("Candidate Profile Setup")
    
    col1, col2, col3 = st.columns(3)
    with col1:
        st.subheader("Basic Info")
        first_name = st.text_input("First Name", "Luv")
        last_name = st.text_input("Last Name", "Kumar")
        email = st.text_input("Email", "luvkumar8436@gmail.com")
        phone = st.text_input("Phone", "+91-7689961477")
        
    with col2:
        st.subheader("Links")
        linkedin_url = st.text_input("LinkedIn", "linkedin.com/in/luv-kumar-06975b175")
        github_url = st.text_input("GitHub", "https://github.com/lovethakur8436")
        portfolio_url = st.text_input("Portfolio / Website", "")
        
    with col3:
        st.subheader("Demographics & Status")
        location_input = st.text_input("Current Location", "Hyderabad, India")
        work_auth = st.selectbox("Work Authorization (US)", ["Authorized", "Require Sponsorship", "N/A"], index=2)
        gender = st.selectbox("Gender", ["Male", "Female", "Non-binary", "Decline to self-identify"], index=0)
        hispanic = st.selectbox("Hispanic/Latino", ["Yes", "No", "Decline to self-identify"], index=1)
        veteran = st.selectbox("Veteran Status", ["I am not a protected veteran", "Decline to self-identify"], index=0)
        disability = st.selectbox("Disability Status", ["No, I don't have a disability", "Decline to self-identify"], index=0)
        
    st.subheader("Base Resume Configurations")
    colA, colB = st.columns(2)
    with colA:
        base_resume_text = st.text_area("Base Resume (Text format for matching)", height=200, value="""Professional Summary
Java Software Engineer with 3.5+ years at Wells Fargo designing and maintaining Spring Boot microservices for high-volume financial transaction processing. Experienced in REST API development, Hibernate/Spring Data JPA, OAuth2/JWT security, and cloud-native deployments on AWS. Hands-on with CI/CD pipelines, Ansible-based infrastructure automation, and full-stack delivery using React.js. Proven ability to ship AI-powered products end-to-end in Agile, regulated banking environments.

Technical Skills
Languages: Java, Python, JavaScript, TypeScript, SQL, Bash
Backend: Spring Boot, Spring Data JPA, Hibernate, REST API, Node.js, Express.js
Core Java: Multithreading, Concurrency (ExecutorService, ConcurrentHashMap), Collections, Java 8+ Streams & Lambdas
Microservices: Spring Boot Microservices, Circuit Breaker (Resilience4j), API Gateway, Event-Driven Architecture
Frontend: React.js, Vite, HTML5, CSS3, JSON
AI & APIs: Google Gemini API, REST API Integration, GenAI Prompt Engineering
DevOps: Ansible, Jenkins CI/CD, GitHub Actions, Docker, Kubernetes, Linux/VM Administration
Cloud: AWS (EC2, S3, Lambda), Firebase Hosting, Infrastructure-as-Code
Security & Practices: OAuth2, JWT, OWASP Standards, TDD (JUnit/PyTest), Agile/Scrum
Databases & Monitoring: PostgreSQL, MongoDB, Redis, Grafana, Splunk

Professional Experience
Wells Fargo Hyderabad, India
Software Engineer Nov 2022 – Present
– Developed Java utility modules and REST API integrations within Spring Boot microservices for high-volume financial transaction processing; used Spring Data JPA and Hibernate for ORM-based data access; diagnosed and resolved production issues in a regulated banking environment.
– Built thread-safe Java components using ConcurrentHashMap and ExecutorService for parallel data processing within high-throughput financial services, improving reliability under concurrent load.
– Designed and maintained Ansible-based automation frameworks for configuration management, environment provisioning, and Java application deployment across enterprise financial infrastructure — reducing manual provisioning effort by 40% and cutting setup time from days to hours.
– Built Python automation tools for log parsing, automated health-check pipelines, and report generation — improving team productivity by 30% across critical support functions.
– Developed Jenkins CI/CD pipelines integrating Ansible and Java build workflows (Maven/Gradle); enforced zero-downtime deployment standards and automated test execution across development and staging environments.
– Built application monitoring using Grafana and Splunk dashboards to detect system degradation proactively; reduced mean time-to-detect (MTTD) for service incidents by 35% across financial platform services.
– Applied OAuth2/JWT security standards and OWASP guidelines in API access management; participated in compliance reviews and security audits aligned with enterprise data governance policies.
– Collaborated in Agile/Scrum sprints; authored technical documentation and contributed to design reviews — mentoring 2 junior engineers and reducing onboarding time by 25%.

Airveda Remote
Software Engineer Intern Mar 2022 – May 2022
– Built REST API integrations and UI components in JavaScript; increased unit test coverage by 25%, reducing production regression defects in data ingestion pipelines.

Projects
AI Trip Planner | React.js, Vite, Google Gemini API, Firebase Hackathon · Live ↑
– Integrated Google Gemini API to generate personalised day-by-day trip plans; built session context handling to maintain trip history; deployed responsive React.js + Vite frontend on Firebase Hosting — demonstrating full ownership of design, API integration, and cloud deployment.

InfraBoard — Ansible Ops Dashboard | Python, FastAPI, React.js, MongoDB, Docker, AWS EC2 In Progress
– Building a full-stack dashboard exposing Ansible playbook execution via FastAPI REST API; React.js frontend shows real-time run status and logs; JWT-secured endpoints with MongoDB persistence, Dockerized and deployed on AWS EC2.

Education
Dr. A.P.J. Abdul Kalam Technical University Lucknow, India
Bachelor of Technology, Computer Science Engineering 2018 – 2022""")
        
    base_resume_json = st.text_area("Base Resume JSON (For Tailoring)", height=300, value='''{
  "personal": {"name": "Luv Kumar", "phone": "+91-7689961477", "email": "luvkumar8436@gmail.com", "linkedin": "linkedin.com/in/luv-kumar-06975b175", "github": "github.com/lovethakur8436"},
  "summary": "Java Software Engineer with 3.5+ years at Wells Fargo designing and maintaining Spring Boot microservices for high-volume financial transaction processing. Experienced in REST API development, Hibernate/Spring Data JPA, OAuth2/JWT security, and cloud-native deployments on AWS.",
  "experience": [
    {
      "company": "Wells Fargo",
      "title": "Software Engineer",
      "location": "Hyderabad, India",
      "dates": "Nov 2022 – Present",
      "bullets": [
        "Developed Java utility modules and REST API integrations within Spring Boot microservices for high-volume financial transaction processing; used Spring Data JPA and Hibernate for ORM-based data access.",
        "Built thread-safe Java components using ConcurrentHashMap and ExecutorService for parallel data processing within high-throughput financial services, improving reliability under concurrent load.",
        "Designed and maintained Ansible-based automation frameworks for configuration management, environment provisioning, and Java application deployment across enterprise financial infrastructure — reducing manual provisioning effort by 40%.",
        "Built Python automation tools for log parsing, automated health-check pipelines, and report generation — improving team productivity by 30%.",
        "Developed Jenkins CI/CD pipelines integrating Ansible and Java build workflows (Maven/Gradle); enforced zero-downtime deployment standards.",
        "Built application monitoring using Grafana and Splunk dashboards to detect system degradation proactively; reduced mean time-to-detect (MTTD) for service incidents by 35%.",
        "Applied OAuth2/JWT security standards and OWASP guidelines in API access management.",
        "Collaborated in Agile/Scrum sprints; authored technical documentation and contributed to design reviews — mentoring 2 junior engineers."
      ]
    },
    {
      "company": "Airveda",
      "title": "Software Engineer Intern",
      "location": "Remote",
      "dates": "Mar 2022 – May 2022",
      "bullets": [
        "Built REST API integrations and UI components in JavaScript; increased unit test coverage by 25%, reducing production regression defects in data ingestion pipelines."
      ]
    }
  ],
  "projects": [
    {
      "name": "AI Trip Planner",
      "dates": "Hackathon",
      "bullets": [
        "Integrated Google Gemini API to generate personalised day-by-day trip plans; built session context handling to maintain trip history.",
        "Deployed responsive React.js + Vite frontend on Firebase Hosting."
      ]
    },
    {
      "name": "InfraBoard — Ansible Ops Dashboard",
      "dates": "In Progress",
      "bullets": [
        "Building a full-stack dashboard exposing Ansible playbook execution via FastAPI REST API.",
        "React.js frontend shows real-time run status and logs; JWT-secured endpoints with MongoDB persistence, Dockerized and deployed on AWS EC2."
      ]
    }
  ],
  "skills": [
    {"category": "Languages", "items": ["Java", "Python", "JavaScript", "TypeScript", "SQL", "Bash"]},
    {"category": "Backend", "items": ["Spring Boot", "Spring Data JPA", "Hibernate", "REST API", "Node.js", "Express.js"]},
    {"category": "Core Java", "items": ["Multithreading", "Concurrency", "Collections", "Java 8+ Streams & Lambdas"]},
    {"category": "Microservices", "items": ["Spring Boot Microservices", "Circuit Breaker", "API Gateway", "Event-Driven Architecture"]},
    {"category": "Frontend", "items": ["React.js", "Vite", "HTML5", "CSS3", "JSON"]},
    {"category": "AI & APIs", "items": ["Google Gemini API", "REST API Integration", "GenAI Prompt Engineering"]},
    {"category": "DevOps", "items": ["Ansible", "Jenkins CI/CD", "GitHub Actions", "Docker", "Kubernetes", "Linux"]},
    {"category": "Cloud", "items": ["AWS (EC2, S3, Lambda)", "Firebase Hosting"]},
    {"category": "Databases", "items": ["PostgreSQL", "MongoDB", "Redis", "Grafana", "Splunk"]}
  ]
}''')

    st.subheader("Ingest Jobs")
    greenhouse_input = st.text_input(
        "Greenhouse companies to ingest (comma-separated slugs)",
        value="gitlab",
        help="e.g. gitlab, stripe, figma, notion"
    )
    greenhouse_tokens = [t.strip() for t in greenhouse_input.split(",") if t.strip()]

    if st.button("Trigger Ingest"):
        with st.spinner("Ingesting jobs..."):
            res = requests.post(
                f"{API_URL}/jobs/ingest",
                json={"greenhouse": greenhouse_tokens, "lever": []},
                headers={"Content-Type": "application/json"}
            )
            data = res.json()
            if res.status_code == 200:
                st.success(data.get("message", "Ingested successfully"))
                st.caption(f"Companies scraped: {', '.join(data.get('companies_scraped', []))}")
            else:
                st.error(f"Error {res.status_code}: {data.get('detail', str(data))}")
        
    if st.button("Trigger Global Match"):
        with st.spinner("Matching in progress (processing 5 jobs)..."):
            res = requests.post(f"{API_URL}/applications/match", json={"base_resume": base_resume_text})
            if res.status_code == 200:
                st.success(res.json()["message"])
            else:
                st.error(f"Error matching: {res.text}")

with tab1:
    st.header("Ingested Jobs")
    try:
        jobs = requests.get(f"{API_URL}/jobs", params={"limit": 1000}).json()
        if jobs:
            st.caption(f"Total jobs in DB: {len(jobs)}")
            df_jobs = pd.DataFrame(jobs)
            st.dataframe(df_jobs[["id", "portal", "company", "title", "location"]], use_container_width=True)
        else:
            st.info("No jobs found. Go to Setup to ingest.")
    except Exception as e:
        st.error(f"Failed to connect to API: {e}")

with tab2:
    st.header("Application Queue")
    
    if st.button("Refresh Applications"):
        st.rerun()
        
    try:
        apps = requests.get(f"{API_URL}/applications").json()
        jobs_req = requests.get(f"{API_URL}/jobs", params={"limit": 1000}).json()
        job_map = {j["id"]: j for j in jobs_req} if jobs_req else {}
        
        if apps:
            for app in apps:
                job_info = job_map.get(app['job_id'], {})
                title = job_info.get("title", "Unknown Role")
                company = job_info.get("company", "Unknown Company")
                location = job_info.get("location", "Unknown Location")
                
                with st.expander(f"App #{app['id']} | {company} - {title} | {location} | Fit: {app['fit_score']}"):
                    st.write(f"**Role Details:** {title} @ {company} ({location})")
                    
                    status_color = "blue"
                    if app['status'] == 'AUTO_APPLIED': status_color = "green"
                    elif app['status'] == 'FAILED': status_color = "red"
                    st.markdown(f"**Status:** :{status_color}[{app['status']}]")
                    
                    full_analysis = app.get('fit_analysis', '')
                    short_analysis = full_analysis.split('\n')[0] if full_analysis else ""
                    if len(short_analysis) > 120:
                        short_analysis = short_analysis[:117] + "..."
                        
                    st.write(f"**Fit Analysis:** {short_analysis}")
                    
                    colA, colB = st.columns(2)
                    with colA:
                        if st.button(f"Tailor Resume #{app['id']}"):
                            res = requests.post(f"{API_URL}/applications/{app['id']}/tailor", json=json.loads(base_resume_json))
                            st.success("Tailored!")
                    with colB:
                        if st.button(f"Auto-Apply #{app['id']}"):
                            profile = {
                                "first_name": first_name,
                                "last_name": last_name,
                                "email": email,
                                "phone": phone,
                                "linkedin": linkedin_url,
                                "github": github_url,
                                "portfolio": portfolio_url,
                                "location": location_input,
                                "work_auth": work_auth,
                                "gender": gender,
                                "hispanic": hispanic,
                                "veteran": veteran,
                                "disability": disability
                            }
                            res = requests.post(f"{API_URL}/applications/{app['id']}/apply", json=profile)
                            st.success("Apply process started! Wait a few seconds and click 'Refresh Applications' above.")
                            
                    if app.get('submission_log_json_path'):
                        try:
                            with open(app['submission_log_json_path'], 'r') as f:
                                log_data = json.load(f)
                            st.write("**Apply Logs:**")
                            for log in log_data.get('logs', []):
                                st.code(log, language='text')
                        except Exception:
                            pass
        else:
            st.info("No applications generated yet.")
    except Exception as e:
        st.error(f"Failed to connect to API: {e}")
