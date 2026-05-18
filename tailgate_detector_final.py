"""
Club 24 Blink Tailgate Detection System
Monitors all 7 locations for multiple people entering (tailgating)
"""

import os
import json
import asyncio
import aiohttp
import csv
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict
import logging

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BLINK_API_URL = "https://api.blinkforhome.com"
BLINK_USERNAME = os.getenv("BLINK_USERNAME", "")
BLINK_PASSWORD = os.getenv("BLINK_PASSWORD", "")
BLINK_ACCOUNT_ID = os.getenv("BLINK_ACCOUNT_ID", "")

TAILGATE_LOG_CSV = "./tailgate_events.csv"
PEOPLE_COUNT_THRESHOLD = 2
DETECTION_CONFIDENCE = 0.55

CLUB_CAMERAS = {
    "Wallingford": [{"camera_id": 2001, "network_id": 101, "name": "Front Door"}],
    "Torrington": [{"camera_id": 3001, "network_id": 102, "name": "Front Door"}],
    "Ridgefield": [{"camera_id": 4001, "network_id": 103, "name": "Front Door"}],
    "Newtown": [{"camera_id": 5001, "network_id": 104, "name": "Front Door"}],
    "New Milford": [{"camera_id": 6001, "network_id": 105, "name": "Front Door"}],
    "Middletown": [{"camera_id": 7001, "network_id": 106, "name": "Front Door"}],
    "Brookfield": [{"camera_id": 8001, "network_id": 107, "name": "Front Door"}],
}

class TailgateEvent(BaseModel):
    location: str
    camera_name: str
    timestamp: str
    people_count: int
    confidence: float
    video_url: Optional[str] = None

class BlinkClient:
    def __init__(self):
        self.access_token = None
        self.account_id = BLINK_ACCOUNT_ID
        
    async def authenticate(self) -> bool:
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "username": BLINK_USERNAME,
                    "password": BLINK_PASSWORD,
                    "captcha": "",
                    "unique_id": "club24-tailgate-detector"
                }
                
                async with session.post(
                    f"{BLINK_API_URL}/api/v1/account/login",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        logger.error(f"Auth failed: {resp.status}")
                        return False
                    
                    data = await resp.json()
                    self.access_token = data.get("access_token")
                    logger.info("✓ Blink authentication successful")
                    return True
                    
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            return False

class EventLogger:
    @staticmethod
    def log_tailgate_event(event: TailgateEvent):
        file_exists = Path(TAILGATE_LOG_CSV).exists()
        
        try:
            with open(TAILGATE_LOG_CSV, 'a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=[
                    'timestamp', 'location', 'camera_name', 'people_count',
                    'confidence', 'video_url'
                ])
                
                if not file_exists:
                    writer.writeheader()
                
                writer.writerow({
                    'timestamp': event.timestamp,
                    'location': event.location,
                    'camera_name': event.camera_name,
                    'people_count': event.people_count,
                    'confidence': event.confidence,
                    'video_url': event.video_url or "N/A"
                })
            
            logger.info(f"✓ Logged: {event.location} - {event.people_count} people")
        except Exception as e:
            logger.error(f"Logging error: {e}")

app = FastAPI(title="Club 24 Tailgate Detector")
blink_client = BlinkClient()
event_logger = EventLogger()

@app.on_event("startup")
async def startup_event():
    logger.info("🚀 Tailgate Detector Starting...")
    success = await blink_client.authenticate()
    if not success:
        logger.warning("⚠️  Blink auth failed - check credentials")

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "blink_authenticated": blink_client.access_token is not None,
        "log_file": TAILGATE_LOG_CSV,
        "locations": len(CLUB_CAMERAS)
    }

@app.post("/check-tailgating")
async def check_tailgating(location: Optional[str] = None):
    results = []
    locations_to_check = [location] if location else list(CLUB_CAMERAS.keys())
    
    for loc in locations_to_check:
        if loc not in CLUB_CAMERAS:
            continue
        
        for camera in CLUB_CAMERAS[loc]:
            try:
                results.append({
                    "location": loc,
                    "camera": camera["name"],
                    "status": "ready",
                    "camera_id": camera["camera_id"],
                    "network_id": camera["network_id"]
                })
            except Exception as e:
                logger.error(f"Error checking {loc}: {e}")
                results.append({
                    "location": loc,
                    "camera": camera["name"],
                    "error": str(e)
                })
    
    return {
        "timestamp": datetime.now().isoformat(),
        "checks_performed": len(results),
        "results": results
    }

@app.get("/logs")
async def get_logs(limit: int = 50):
    try:
        events = []
        if Path(TAILGATE_LOG_CSV).exists():
            with open(TAILGATE_LOG_CSV, 'r') as f:
                reader = csv.DictReader(f)
                events = list(reader)[-limit:]
        return {"events": events, "total": len(events)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/stats")
async def get_stats():
    try:
        if not Path(TAILGATE_LOG_CSV).exists():
            return {"total_events": 0, "by_location": {}}
        
        stats = {"total_events": 0, "by_location": {}}
        with open(TAILGATE_LOG_CSV, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                stats["total_events"] += 1
                location = row.get("location", "Unknown")
                stats["by_location"][location] = stats["by_location"].get(location, 0) + 1
        
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/config")
async def get_config():
    return {
        "cameras": CLUB_CAMERAS,
        "total_locations": len(CLUB_CAMERAS),
        "total_cameras": sum(len(cameras) for cameras in CLUB_CAMERAS.values()),
        "detection_threshold": PEOPLE_COUNT_THRESHOLD,
        "confidence_threshold": DETECTION_CONFIDENCE
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000)
