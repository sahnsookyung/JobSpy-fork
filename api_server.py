import uuid
import logging
from typing import List, Optional, Dict, Any, Union
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

# Import JobSpy core
from jobspy.model import ScraperInput, Site, JobResponse, DescriptionFormat
from jobspy.scrapers.tokyodev import TokyoDev
from jobspy.scrapers.japandev import JapanDev
from jobspy.indeed import Indeed
from jobspy.linkedin import LinkedIn
from jobspy.glassdoor import Glassdoor
from jobspy.ziprecruiter import ZipRecruiter
from jobspy.google import Google
from jobspy.bayt import BaytScraper
from jobspy.naukri import Naukri
from jobspy.bdjobs import BDJobs

# Scraper-specific enums/filters are no longer needed at the API level
# Scrapers handle their own validation

SCRAPER_MAPPING = {
    Site.LINKEDIN: LinkedIn,
    Site.INDEED: Indeed,
    Site.ZIP_RECRUITER: ZipRecruiter,
    Site.GLASSDOOR: Glassdoor,
    Site.GOOGLE: Google,
    Site.BAYT: BaytScraper,
    Site.NAUKRI: Naukri,
    Site.BDJOBS: BDJobs,
    Site.TOKYODEV: TokyoDev,
    Site.JAPANDEV: JapanDev,
}

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api_server")

app = FastAPI(title="JobSpy Scraper API")

# --- In-Memory Job Store (Replace with Redis in Prod) ---
# Format: { "task_id": { "status": "processing" | "completed" | "failed", "data": ... } }
JOB_STORE: Dict[str, Dict[str, Any]] = {}

# --- Request Models ---

class ScrapeRequest(BaseModel):
    site_name: str = Field(..., description="The site to scrape, e.g., 'tokyodev', 'japandev', 'indeed'")
    search_term: Optional[str] = None
    location: Optional[str] = None
    results_wanted: int = 20
    is_remote: bool = False
    
    # Generic options dict for scraper-specific arguments
    # Scrapers handle validation - any exceptions will be caught and returned to caller
    options: Optional[Dict[str, Any]] = None

# --- Background Worker Function ---

def run_scraper_task(task_id: str, request: ScrapeRequest):
    """
    Executes the scraping logic in a background thread/process.
    """
    try:
        logger.info(f"Task {task_id}: Starting scrape for {request.site_name}")
        
        # 1. Map String to Site Enum
        try:
            site_enum = Site[request.site_name.upper()]
        except KeyError:
            raise ValueError(f"Invalid site name: {request.site_name}")

        # 2. Setup Base Input
        scraper_input = ScraperInput(
            site_type=[site_enum],
            search_term=request.search_term,
            location=request.location,
            results_wanted=request.results_wanted,
            is_remote=request.is_remote,
            description_format=DescriptionFormat.MARKDOWN
        )

        scraper = None
        results = None

        # 3. Dispatch to Specific Scraper
        scraper_class = SCRAPER_MAPPING.get(site_enum)
        if not scraper_class:
            raise NotImplementedError(f"Scraper for {site_enum.name} not configured in API")

        scraper = scraper_class()
        
        # Pass all options directly to scraper - it handles validation
        scrape_kwargs = request.options or {}
        results = scraper.scrape(scraper_input, **scrape_kwargs)

        # 4. Save Success Result
        # Convert Pydantic models to dict for JSON serialization
        jobs_data = [job.dict() for job in results.jobs]
        
        JOB_STORE[task_id] = {
            "status": "completed",
            "count": len(jobs_data),
            "data": jobs_data
        }
        logger.info(f"Task {task_id}: Completed with {len(jobs_data)} jobs")

    except Exception as e:
        logger.error(f"Task {task_id}: Failed - {str(e)}")
        JOB_STORE[task_id] = {
            "status": "failed",
            "error": str(e)
        }

# --- Endpoints ---

@app.post("/scrape", status_code=202)
async def submit_scrape_job(request: ScrapeRequest, background_tasks: BackgroundTasks):
    """
    Submits a scraping job. Returns a Task ID immediately.
    
    Returns:
        202: Job accepted and queued for processing
        400: Invalid request (bad site name, invalid parameters)
    """
    # Validate site name before queuing
    try:
        site_enum = Site[request.site_name.upper()]
    except KeyError:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid site name: '{request.site_name}'. Valid options: {[s.value for s in Site]}"
        )
    
    # Validate scraper is configured
    if site_enum not in SCRAPER_MAPPING:
        raise HTTPException(
            status_code=400,
            detail=f"Scraper for '{request.site_name}' is not configured in this API"
        )
    
    task_id = str(uuid.uuid4())
    
    # Initialize status
    JOB_STORE[task_id] = {"status": "processing"}
    
    # Add to background queue
    background_tasks.add_task(run_scraper_task, task_id, request)
    
    return {
        "task_id": task_id,
        "status": "processing",
        "message": "Job submitted successfully. Poll /status/{task_id} for results."
    }

@app.get("/status/{task_id}")
async def check_job_status(task_id: str):
    """
    Check the status of a job.
    
    Returns:
        200: Job completed successfully (includes job data)
        202: Job still processing
        404: Task ID not found
        500: Job failed (includes error details)
    """
    job = JOB_STORE.get(task_id)
    
    if not job:
        raise HTTPException(status_code=404, detail="Task ID not found")
    
    status = job.get("status")
    
    if status == "failed":
        raise HTTPException(
            status_code=500,
            detail={
                "error": job.get("error", "Unknown error"),
                "task_id": task_id
            }
        )
    elif status == "processing":
        return {"status": "processing", "task_id": task_id}
    else:  # completed
        return job

@app.get("/health")
def health():
    return {"status": "ok", "jobs_in_memory": len(JOB_STORE)}
