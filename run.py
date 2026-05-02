import subprocess
import sys
import time

def main():
    print("Starting FastAPI backend...")
    fastapi_process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.api.main:app", "--reload", "--port", "8000"])
    
    time.sleep(2)
    
    print("Starting Streamlit dashboard...")
    streamlit_process = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "dashboard/app.py", "--server.port", "8501"])
    
    try:
        fastapi_process.wait()
        streamlit_process.wait()
    except KeyboardInterrupt:
        print("Shutting down...")
        fastapi_process.terminate()
        streamlit_process.terminate()

if __name__ == "__main__":
    main()
