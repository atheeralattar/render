from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, HttpUrl, validator
import uvicorn
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import os
import time
import uuid
import threading
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Any

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()  # Only log to stdout/stderr to avoid filling disk
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="FracFocus PDF Downloader API",
    description="API for downloading PDF disclosure forms from FracFocus",
    version="1.0.0"
)

# Use temporary directory for downloads
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/tmp/downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Store job statuses (with a maximum limit to prevent memory leaks)
MAX_JOBS = 50
jobs: Dict[str, Dict[str, Any]] = {}
# Store downloaded files with shorter expiration (1 hour)
downloads: Dict[str, Dict[str, Any]] = {}

# Track active downloads to limit concurrency
active_downloads = 0
MAX_CONCURRENT_DOWNLOADS = 2  # Limit to 2 concurrent downloads to save resources

# Configure Chrome options for Docker environment
def get_chrome_options(job_id: str):
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,720")  # Smaller window size
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-dev-tools")
    options.add_argument("--mute-audio")
    options.add_argument("--disable-software-rasterizer")
    
    # Set Chrome binary location explicitly
    options.binary_location = "/usr/bin/google-chrome"
    
    # Create job-specific download directory
    job_download_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_download_dir, exist_ok=True)
    
    # Chrome preferences for downloads
    prefs = {
        "download.default_directory": job_download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
        "download.open_pdf_in_system_reader": False,
        "profile.default_content_settings.popups": 0
    }
    options.add_experimental_option("prefs", prefs)
    
    return options

# Models
class DownloadRequest(BaseModel):
    well_url: HttpUrl
    
    @validator('well_url')
    def validate_well_url(cls, v):
        if not str(v).startswith('https://fracfocus.org/wells/'):
            raise ValueError('URL must be from fracfocus.org/wells/')
        return v

async def download_disclosure_pdf(well_url: str, job_id: str):
    """Background task to download PDF from FracFocus"""
    global active_downloads
    active_downloads += 1
    driver = None
    
    try:
        jobs[job_id]['status'] = 'downloading'
        logger.info(f"Job {job_id}: Starting download for {well_url}")
        
        # Get Chrome options for this job
        options = get_chrome_options(job_id)
        
        logger.info(f"Job {job_id}: Initializing Chrome webdriver")
        driver = webdriver.Chrome(options=options)
        
        driver.get(str(well_url))
        logger.info(f"Job {job_id}: Navigated to URL")
        
        # Wait for page to load
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        
        # Try various selectors to find the download button
        selectors = [
            "//button[contains(text(), 'Download PDF')]",
            "//a[contains(text(), 'Download PDF')]",
            "//button[contains(text(), 'PDF')]",
            "//a[contains(text(), 'PDF')]",
            "//a[contains(@href, 'pdf')]",
            "//button[contains(text(), 'Disclosure')]",
            "//a[contains(text(), 'Disclosure')]",
            "//button[contains(text(), 'Download')]",
            "//a[contains(text(), 'Download')]"
        ]
        
        download_button = None
        for selector in selectors:
            try:
                elements = driver.find_elements(By.XPATH, selector)
                if elements:
                    for i, element in enumerate(elements):
                        text = element.text
                        href = element.get_attribute('href') if element.tag_name == 'a' else None
                        logger.info(f"Job {job_id}: Found element {i+1}: Tag={element.tag_name}, Text='{text}', Href='{href}'")
                    
                    download_button = elements[0]
                    logger.info(f"Job {job_id}: Selected download button with selector: {selector}")
                    break
            except Exception as e:
                logger.warning(f"Job {job_id}: Error with selector {selector}: {str(e)}")
        
        if download_button:
            # Scroll to button and click
            driver.execute_script("arguments[0].scrollIntoView(true);", download_button)
            time.sleep(1)
            download_button.click()
            logger.info(f"Job {job_id}: Clicked download button")
            
            # Wait for download to complete (shorter time to save resources)
            time.sleep(10)
            
            # Find downloaded file
            job_download_dir = os.path.join(DOWNLOAD_DIR, job_id)
            files = os.listdir(job_download_dir)
            pdf_files = [f for f in files if f.lower().endswith('.pdf')]
            
            if pdf_files:
                file_path = os.path.join(job_download_dir, pdf_files[0])
                logger.info(f"Job {job_id}: Downloaded file: {file_path}")
                
                # Store file info with expiration (1 hour from now)
                downloads[job_id] = {
                    'file_path': file_path,
                    'expires_at': datetime.now() + timedelta(hours=1)
                }
                
                jobs[job_id]['status'] = 'completed'
                jobs[job_id]['file'] = pdf_files[0]
                jobs[job_id]['completed_at'] = datetime.now().isoformat()
            else:
                logger.error(f"Job {job_id}: No PDF files found after download attempt")
                jobs[job_id]['status'] = 'failed'
                jobs[job_id]['error'] = 'No PDF files found after download'
        else:
            logger.error(f"Job {job_id}: Could not find download button")
            jobs[job_id]['status'] = 'failed'
            jobs[job_id]['error'] = 'Could not find download button on page'
            
    except Exception as e:
        logger.error(f"Job {job_id}: Error downloading PDF: {str(e)}")
        jobs[job_id]['status'] = 'failed'
        jobs[job_id]['error'] = str(e)
    
    finally:
        if driver:
            driver.quit()
            logger.info(f"Job {job_id}: Browser closed")
        
        # Decrement active downloads counter
        active_downloads -= 1

@app.post("/api/download", status_code=202)
async def start_download(request: DownloadRequest, background_tasks: BackgroundTasks):
    """Start a download job"""
    global active_downloads
    
    # Check if we've reached the maximum concurrent downloads
    if active_downloads >= MAX_CONCURRENT_DOWNLOADS:
        raise HTTPException(
            status_code=429,
            detail="Too many active downloads. Please try again later."
        )
    
    # Check if we've reached the maximum number of stored jobs
    if len(jobs) >= MAX_JOBS:
        # Remove the oldest completed job
        oldest_job_id = None
        oldest_time = datetime.now()
        
        for job_id, job in jobs.items():
            if job['status'] in ['completed', 'failed']:
                created_at = datetime.fromisoformat(job['created_at'])
                if created_at < oldest_time:
                    oldest_time = created_at
                    oldest_job_id = job_id
        
        if oldest_job_id:
            # Remove the job
            del jobs[oldest_job_id]
            # Also remove from downloads if present
            if oldest_job_id in downloads:
                try:
                    # Remove the file
                    file_path = downloads[oldest_job_id]['file_path']
                    if os.path.exists(file_path):
                        os.remove(file_path)
                except Exception as e:
                    logger.error(f"Error removing file for job {oldest_job_id}: {str(e)}")
                del downloads[oldest_job_id]
        else:
            # If we can't find an old job to remove, reject the request
            raise HTTPException(
                status_code=429,
                detail="Maximum number of jobs reached. Please try again later."
            )
    
    job_id = str(uuid.uuid4())
    
    # Initialize job status
    jobs[job_id] = {
        'id': job_id,
        'well_url': str(request.well_url),
        'status': 'queued',
        'created_at': datetime.now().isoformat()
    }
    
    # Start download in background
    background_tasks.add_task(download_disclosure_pdf, request.well_url, job_id)
    
    return {
        'job_id': job_id,
        'status': 'queued',
        'message': 'Download job started'
    }

@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    """Check status of a download job"""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    
    return jobs[job_id]

@app.get("/api/downloads/{job_id}")
async def download_file(job_id: str):
    """Download the PDF file"""
    if job_id not in downloads:
        raise HTTPException(status_code=404, detail="Download not found or expired")
    
    file_info = downloads[job_id]
    
    # Check expiration
    if datetime.now() > file_info['expires_at']:
        # Remove expired download
        try:
            file_path = file_info['file_path']
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            logger.error(f"Error removing expired file for job {job_id}: {str(e)}")
        
        del downloads[job_id]
        raise HTTPException(status_code=410, detail="Download link expired")
    
    return FileResponse(
        path=file_info['file_path'],
        filename=os.path.basename(file_info['file_path']),
        media_type='application/pdf'
    )

@app.get("/api/health")
async def health_check():
    """API health check endpoint"""
    # Try to get Chrome version
    chrome_version = None
    try:
        import subprocess
        chrome_version_bytes = subprocess.check_output(["/usr/bin/google-chrome", "--version"])
        chrome_version = chrome_version_bytes.decode("utf-8").strip()
    except Exception as e:
        logger.error(f"Error getting Chrome version: {str(e)}")
    
    # Get disk usage of /tmp
    tmp_usage = None
    try:
        import shutil
        total, used, free = shutil.disk_usage("/tmp")
        tmp_usage = {
            "total_gb": round(total / (1024**3), 2),
            "used_gb": round(used / (1024**3), 2),
            "free_gb": round(free / (1024**3), 2),
            "percent_used": round((used / total) * 100, 2)
        }
    except Exception as e:
        logger.error(f"Error getting disk usage: {str(e)}")
    
    return {
        'status': 'ok',
        'timestamp': datetime.now().isoformat(),
        'active_jobs': active_downloads,
        'completed_jobs': sum(1 for job in jobs.values() if job['status'] == 'completed'),
        'failed_jobs': sum(1 for job in jobs.values() if job['status'] == 'failed'),
        'chrome_version': chrome_version,
        'tmp_disk_usage': tmp_usage,
        'max_concurrent_downloads': MAX_CONCURRENT_DOWNLOADS,
        'max_stored_jobs': MAX_JOBS
    }

def start_cleanup_thread():
    """Start a background thread to clean up old jobs and downloads"""
    def cleanup_old_jobs():
        while True:
            time.sleep(300)  # Run every 5 minutes
            
            now = datetime.now()
            
            # Remove expired downloads
            for job_id in list(downloads.keys()):
                if now > downloads[job_id]['expires_at']:
                    file_path = downloads[job_id]['file_path']
                    try:
                        if os.path.exists(file_path):
                            os.remove(file_path)
                        download_dir = os.path.dirname(file_path)
                        if os.path.exists(download_dir) and not os.listdir(download_dir):
                            os.rmdir(download_dir)
                    except Exception as e:
                        logger.error(f"Error cleaning up files for {job_id}: {str(e)}")
                    
                    del downloads[job_id]
                    logger.info(f"Cleaned up expired download: {job_id}")
    
    cleanup_thread = threading.Thread(target=cleanup_old_jobs)
    cleanup_thread.daemon = True
    cleanup_thread.start()

@app.on_event("startup")
async def startup_event():
    # Start cleanup thread
    start_cleanup_thread()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)