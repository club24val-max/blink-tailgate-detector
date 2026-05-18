"""
Club 24 Blink Tailgate Detection System
With Google Vision API - Scans during unstaffed hours only
"""

import os
import json
import asyncio
import csv
import base64
import aiohttp
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging

from aiohttp import ClientSession
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BLINK_CREDS = os.getenv("BLINK_CREDS", "")
BLINK_CREDS_FILE = "./blink_credentials.json"
TAILGATE_LOG_CSV = "./tailgate_events.csv"
VISION_API_KEY = os.getenv("GOOGLE_VISION_API_KEY", "")
PEOPLE_THRESHOLD = 2
CONFIDENCE_THRESHOLD = 0.5

# Only these front door cameras trigger alerts
ALERT_CAMERAS = [
    "Wallingford Front Door",
    "Torrington Main Door",
    "Front Desk Ridgefield",
    "Newtown Front Door",
    "New milford Front",
    "Middletown Front Desk",
    "Brookfield Door",
]

def is_unstaffed_hours():
    """Check if current time is during unstaffed hours (ET)
    Mon-Fri: 9pm - 8am
    Sat-Sun: 3pm - 8am
    """
    from datetime import timezone, timedelta
    et = timezone(timedelta(hours=-4))  # EDT
    now = datetime.now(et)
    hour = now.hour
    weekday = now.weekday()  # 0=Mon, 6=Sun
    
    if weekday < 5:  # Mon-Fri
        return hour >= 21 or hour < 8
    else:  # Sat-Sun
        return hour >= 15 or hour < 8


class BlinkManager:
    def __init__(self):
        self.blink = None
        self.authenticated = False
        self.cameras_discovered = {}
    
    async def authenticate(self):
        try:
            if BLINK_CREDS and not Path(BLINK_CREDS_FILE).exists():
                Path(BLINK_CREDS_FILE).write_text(BLINK_CREDS)
            if not Path(BLINK_CREDS_FILE).exists():
                return False
            creds = json.loads(Path(BLINK_CREDS_FILE).read_text())
            session = ClientSession()
            self.blink = Blink(session=session)
            auth = Auth(creds, no_prompt=True)
            self.blink.auth = auth
            try:
                await self.blink.start()
            except Exception as e:
                logger.warning(f"Start exception: {e}")
                return False
            if self.blink.cameras:
                self.authenticated = True
                await self.discover_cameras()
                await self.blink.save(BLINK_CREDS_FILE)
                logger.info("Blink authenticated")
                return True
            return False
        except Exception as e:
            logger.error(f"Auth error: {e}")
            return False
    
    async def discover_cameras(self):
        try:
            if not self.blink or not self.authenticated:
                return
            await self.blink.refresh()
            self.cameras_discovered = {}
            for sync_name, sync_module in self.blink.sync.items():
                self.cameras_discovered[sync_name] = {
                    "network_id": sync_module.network_id,
                    "armed": sync_module.arm,
                    "cameras": []
                }
            for cam_name, camera in self.blink.cameras.items():
                for sync_name, sync_data in self.cameras_discovered.items():
                    if sync_data["network_id"] == camera.network_id:
                        sync_data["cameras"].append({
                            "name": cam_name,
                            "camera_id": camera.camera_id,
                            "network_id": camera.network_id,
                            "model": camera.camera_type
                        })
                        break
            logger.info(f"Discovered {len(self.blink.cameras)} cameras")
        except Exception as e:
            logger.error(f"Discovery error: {e}")
    
    async def get_camera_thumbnail(self, camera_name):
        try:
            if not self.blink or camera_name not in self.blink.cameras:
                return None
            camera = self.blink.cameras[camera_name]
            tmp_path = f"/tmp/{camera_name.replace(' ', '_')}_thumb.jpg"
            await camera.image_to_file(tmp_path)
            if Path(tmp_path).exists():
                with open(tmp_path, "rb") as f:
                    return f.read()
            return None
        except Exception as e:
            logger.error(f"Thumbnail error for {camera_name}: {e}")
            return None
    
    async def get_latest_videos(self, camera_name=None):
        videos = []
        try:
            if not self.blink or not self.authenticated:
                return videos
            await self.blink.refresh()
            cameras_to_check = {}
            if camera_name and camera_name in self.blink.cameras:
                cameras_to_check = {camera_name: self.blink.cameras[camera_name]}
            else:
                cameras_to_check = self.blink.cameras
            for name, camera in cameras_to_check.items():
                videos.append({
                    "camera_name": name,
                    "camera_id": camera.camera_id,
                    "network_id": camera.network_id,
                    "clip": camera.clip,
                    "thumbnail": camera.thumbnail,
                    "last_motion": str(camera.last_motion) if camera.last_motion else None,
                    "motion_detected": camera.motion_detected
                })
            return videos
        except Exception as e:
            logger.error(f"Get videos error: {e}")
            return videos


class VisionAnalyzer:
    def __init__(self):
        self.api_key = VISION_API_KEY
        self.api_url = f"https://vision.googleapis.com/v1/images:annotate?key={self.api_key}"
        self.available = bool(self.api_key)
    
    async def count_people(self, image_data):
        if not self.available:
            return {"people_count": 0, "error": "No API key"}
        try:
            b64_image = base64.b64encode(image_data).decode("utf-8")
            payload = {
                "requests": [{
                    "image": {"content": b64_image},
                    "features": [
                        {"type": "OBJECT_LOCALIZATION", "maxResults": 20},
                        {"type": "LABEL_DETECTION", "maxResults": 10}
                    ]
                }]
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, json=payload) as resp:
                    if resp.status != 200:
                        error = await resp.text()
                        return {"people_count": 0, "error": f"API error: {resp.status}"}
                    data = await resp.json()
            response = data.get("responses", [{}])[0]
            people_count = 0
            person_scores = []
            objects_found = []
            for obj in response.get("localizedObjectAnnotations", []):
                name = obj.get("name", "").lower()
                score = obj.get("score", 0)
                objects_found.append({"name": obj.get("name"), "score": round(score, 3)})
                if name == "person" and score >= CONFIDENCE_THRESHOLD:
                    people_count += 1
                    person_scores.append(round(score, 3))
            labels = []
            for label in response.get("labelAnnotations", []):
                labels.append({
                    "name": label.get("description"),
                    "score": round(label.get("score", 0), 3)
                })
            is_tailgate = people_count >= PEOPLE_THRESHOLD
            return {
                "people_count": people_count,
                "person_scores": person_scores,
                "is_tailgate": is_tailgate,
                "objects_found": objects_found,
                "labels": labels,
                "threshold": PEOPLE_THRESHOLD
            }
        except Exception as e:
            logger.error(f"Vision error: {e}")
            return {"people_count": 0, "error": str(e)}


class EventLogger:
    @staticmethod
    def log_event(location, camera, people_count, confidence, video_url=None):
        file_exists = Path(TAILGATE_LOG_CSV).exists()
        try:
            with open(TAILGATE_LOG_CSV, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "timestamp", "location", "camera_name", "people_count",
                    "confidence", "video_url"
                ])
                if not file_exists:
                    writer.writeheader()
                writer.writerow({
                    "timestamp": datetime.now().isoformat(),
                    "location": location,
                    "camera_name": camera,
                    "people_count": people_count,
                    "confidence": confidence,
                    "video_url": video_url or "N/A"
                })
            logger.info(f"LOGGED: {location} - {camera} - {people_count} people")
        except Exception as e:
            logger.error(f"Logging error: {e}")


app = FastAPI(title="Club 24 Tailgate Detector")
blink_mgr = BlinkManager()
vision = VisionAnalyzer()
event_logger = EventLogger()


@app.on_event("startup")
async def startup_event():
    logger.info("Club 24 Tailgate Detector Starting...")
    await blink_mgr.authenticate()


@app.get("/health")
async def health_check():
    from datetime import timezone, timedelta
    et = timezone(timedelta(hours=-4))
    now = datetime.now(et)
    return {
        "status": "healthy",
        "blink_authenticated": blink_mgr.authenticated,
        "vision_api_configured": vision.available,
        "cameras": len(blink_mgr.blink.cameras) if blink_mgr.blink and blink_mgr.blink.cameras else 0,
        "account_id": blink_mgr.blink.account_id if blink_mgr.blink else None,
        "current_time_et": now.strftime("%Y-%m-%d %I:%M %p ET"),
        "is_unstaffed": is_unstaffed_hours(),
        "alert_cameras": len(ALERT_CAMERAS)
    }


@app.get("/cameras")
async def list_cameras():
    if not blink_mgr.authenticated:
        return {"error": "Not authenticated"}
    return {
        "sync_modules": blink_mgr.cameras_discovered,
        "total_cameras": len(blink_mgr.blink.cameras),
        "alert_cameras": ALERT_CAMERAS
    }


@app.get("/videos")
async def get_videos(camera: Optional[str] = None):
    if not blink_mgr.authenticated:
        return {"error": "Not authenticated"}
    videos = await blink_mgr.get_latest_videos(camera)
    return {"timestamp": datetime.now().isoformat(), "videos": videos}


@app.post("/analyze-camera")
async def analyze_camera(camera_name: str):
    if not blink_mgr.authenticated:
        return {"error": "Not authenticated"}
    if not vision.available:
        return {"error": "Vision API not configured"}
    image_data = await blink_mgr.get_camera_thumbnail(camera_name)
    if not image_data:
        return {"error": f"Could not get image from {camera_name}"}
    result = await vision.count_people(image_data)
    if result.get("is_tailgate"):
        avg_conf = sum(result.get("person_scores", [0])) / max(len(result.get("person_scores", [1])), 1)
        event_logger.log_event(
            location=camera_name, camera=camera_name,
            people_count=result["people_count"], confidence=round(avg_conf, 3)
        )
    return {
        "camera": camera_name,
        "timestamp": datetime.now().isoformat(),
        "analysis": result
    }


@app.post("/scan-all")
async def scan_all_cameras():
    """Scan ALL cameras - analyzes all but only alerts on front door cameras"""
    if not blink_mgr.authenticated:
        return {"error": "Not authenticated"}
    if not vision.available:
        return {"error": "Vision API not configured"}
    
    await blink_mgr.blink.refresh()
    results = []
    alerts = []
    
    for cam_name, camera in blink_mgr.blink.cameras.items():
        try:
            image_data = await blink_mgr.get_camera_thumbnail(cam_name)
            if not image_data:
                results.append({"camera": cam_name, "status": "no_image", "people_count": 0})
                continue
            
            analysis = await vision.count_people(image_data)
            is_tailgate = analysis.get("is_tailgate", False)
            people_count = analysis.get("people_count", 0)
            is_alert_camera = cam_name in ALERT_CAMERAS
            
            result = {
                "camera": cam_name,
                "is_alert_camera": is_alert_camera,
                "status": "TAILGATE_ALERT" if (is_tailgate and is_alert_camera) else ("people_detected" if is_tailgate else "clear"),
                "people_count": people_count,
                "person_scores": analysis.get("person_scores", []),
                "motion_detected": camera.motion_detected
            }
            results.append(result)
            
            if is_tailgate and is_alert_camera:
                avg_conf = sum(analysis.get("person_scores", [0])) / max(len(analysis.get("person_scores", [1])), 1)
                event_logger.log_event(
                    location=cam_name, camera=cam_name,
                    people_count=people_count, confidence=round(avg_conf, 3)
                )
                alerts.append(result)
                logger.warning(f"TAILGATE: {cam_name} - {people_count} people!")
            
        except Exception as e:
            logger.error(f"Error scanning {cam_name}: {e}")
            results.append({"camera": cam_name, "status": "error", "error": str(e)})
    
    return {
        "timestamp": datetime.now().isoformat(),
        "is_unstaffed_hours": is_unstaffed_hours(),
        "cameras_scanned": len(results),
        "tailgate_alerts": len(alerts),
        "alerts": alerts,
        "results": results
    }


@app.post("/scheduled-scan")
async def scheduled_scan():
    """Called by cron job - only scans during unstaffed hours"""
    if not is_unstaffed_hours():
        return {
            "timestamp": datetime.now().isoformat(),
            "status": "skipped",
            "reason": "Currently staffed hours - no scan needed"
        }
    return await scan_all_cameras()


@app.post("/check-tailgating")
async def check_tailgating():
    """Alias for scan-all"""
    return await scan_all_cameras()


@app.get("/logs")
async def get_logs(limit: int = 50):
    events = []
    if Path(TAILGATE_LOG_CSV).exists():
        with open(TAILGATE_LOG_CSV, "r") as f:
            reader = csv.DictReader(f)
            events = list(reader)[-limit:]
    return {"events": events, "total": len(events)}


@app.get("/stats")
async def get_stats():
    if not Path(TAILGATE_LOG_CSV).exists():
        return {"total_events": 0, "by_location": {}}
    stats = {"total_events": 0, "by_location": {}}
    with open(TAILGATE_LOG_CSV, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            stats["total_events"] += 1
            loc = row.get("location", "Unknown")
            stats["by_location"][loc] = stats["by_location"].get(loc, 0) + 1
    return stats


@app.get("/schedule")
async def get_schedule():
    """Show the scanning schedule"""
    return {
        "unstaffed_hours": {
            "mon_fri": "9:00 PM - 8:00 AM ET",
            "sat_sun": "3:00 PM - 8:00 AM ET"
        },
        "alert_cameras": ALERT_CAMERAS,
        "people_threshold": PEOPLE_THRESHOLD,
        "currently_unstaffed": is_unstaffed_hours(),
        "scan_endpoint": "/scheduled-scan (use with cron job)"
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=10000)
