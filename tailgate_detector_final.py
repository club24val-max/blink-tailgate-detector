"""
Club 24 Blink Tailgate Detection System
Uses blinkpy library for proper Blink authentication
"""

import os
import json
import asyncio
import csv
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict
import logging

from aiohttp import ClientSession
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BLINK_USERNAME = os.getenv("BLINK_USERNAME", "")
BLINK_PASSWORD = os.getenv("BLINK_PASSWORD", "")
BLINK_CREDS_FILE = "./blink_credentials.json"
TAILGATE_LOG_CSV = "./tailgate_events.csv"
PEOPLE_COUNT_THRESHOLD = 2
DETECTION_CONFIDENCE = 0.55


class TailgateEvent(BaseModel):
    location: str
    camera_name: str
    timestamp: str
    people_count: int
    confidence: float
    video_url: Optional[str] = None


class BlinkManager:
    def __init__(self):
        self.blink = None
        self.authenticated = False
        self.cameras_discovered = {}
        self.pending_2fa = False
    
    async def authenticate(self):
        try:
            self.blink = Blink(session=ClientSession())
            
            if Path(BLINK_CREDS_FILE).exists():
                logger.info("Loading saved Blink credentials...")
                auth = Auth(json.loads(Path(BLINK_CREDS_FILE).read_text()))
                self.blink.auth = auth
                await self.blink.start()
                self.authenticated = True
                logger.info("Blink authenticated from saved credentials")
                await self.discover_cameras()
                return True
            
            if not BLINK_USERNAME or not BLINK_PASSWORD:
                logger.warning("No Blink credentials provided")
                return False
            
            auth = Auth({"username": BLINK_USERNAME, "password": BLINK_PASSWORD})
            self.blink.auth = auth
            self.blink.auth.no_prompt = True
            
            try:
                await self.blink.start()
                self.authenticated = True
                logger.info("Blink authenticated successfully")
                await self.save_credentials()
                await self.discover_cameras()
                return True
            except Exception as e:
                err_str = str(e)
                if "2FA" in err_str or "pin" in err_str.lower() or "Unauthorized" in err_str:
                    self.pending_2fa = True
                    logger.info("2FA required - check your email for PIN")
                    return False
                else:
                    logger.error(f"Blink auth error: {e}")
                    return False
                    
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            return False
    
    async def verify_2fa(self, pin):
        try:
            if not self.blink:
                return False
            await self.blink.auth.send_auth_key(self.blink, pin)
            await self.blink.setup_post_verify()
            self.authenticated = True
            self.pending_2fa = False
            logger.info("2FA verified successfully")
            await self.save_credentials()
            await self.discover_cameras()
            return True
        except Exception as e:
            logger.error(f"2FA verification failed: {e}")
            return False
    
    async def save_credentials(self):
        try:
            if self.blink and self.blink.auth:
                creds = self.blink.auth.login_attributes
                Path(BLINK_CREDS_FILE).write_text(json.dumps(creds, indent=2))
                logger.info("Credentials saved")
        except Exception as e:
            logger.error(f"Failed to save credentials: {e}")
    
    async def discover_cameras(self):
        try:
            if not self.blink or not self.authenticated:
                return
            
            await self.blink.refresh()
            self.cameras_discovered = {}
            
            for sync_name, sync_module in self.blink.sync.items():
                logger.info(f"Sync Module: {sync_name} (ID: {sync_module.network_id})")
                self.cameras_discovered[sync_name] = {
                    "network_id": sync_module.network_id,
                    "armed": sync_module.arm,
                    "cameras": []
                }
            
            for cam_name, camera in self.blink.cameras.items():
                logger.info(f"Camera: {cam_name} (ID: {camera.camera_id})")
                for sync_name, sync_data in self.cameras_discovered.items():
                    if sync_data["network_id"] == camera.network_id:
                        sync_data["cameras"].append({
                            "name": cam_name,
                            "camera_id": camera.camera_id,
                            "network_id": camera.network_id,
                            "model": camera.camera_type,
                            "armed": camera.arm
                        })
                        break
            
            total = len(self.blink.cameras)
            logger.info(f"Discovered {total} camera(s) across {len(self.blink.sync)} sync module(s)")
            
        except Exception as e:
            logger.error(f"Camera discovery error: {e}")
    
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


class EventLogger:
    @staticmethod
    def log_tailgate_event(event):
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
                    "timestamp": event.timestamp,
                    "location": event.location,
                    "camera_name": event.camera_name,
                    "people_count": event.people_count,
                    "confidence": event.confidence,
                    "video_url": event.video_url or "N/A"
                })
            logger.info(f"Logged: {event.location} - {event.people_count} people")
        except Exception as e:
            logger.error(f"Logging error: {e}")


app = FastAPI(title="Club 24 Tailgate Detector")
blink_mgr = BlinkManager()
event_logger = EventLogger()


@app.on_event("startup")
async def startup_event():
    logger.info("Club 24 Tailgate Detector Starting...")
    await blink_mgr.authenticate()


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "blink_authenticated": blink_mgr.authenticated,
        "pending_2fa": blink_mgr.pending_2fa,
        "cameras_discovered": len(blink_mgr.cameras_discovered),
        "log_file": TAILGATE_LOG_CSV
    }


@app.post("/verify-2fa")
async def verify_2fa(pin: str):
    success = await blink_mgr.verify_2fa(pin)
    if success:
        return {"status": "verified", "cameras": blink_mgr.cameras_discovered}
    else:
        raise HTTPException(status_code=400, detail="2FA verification failed")


@app.get("/cameras")
async def list_cameras():
    if not blink_mgr.authenticated:
        return {
            "error": "Not authenticated",
            "pending_2fa": blink_mgr.pending_2fa,
            "hint": "POST to /verify-2fa?pin=YOUR_PIN if 2FA is pending"
        }
    return {
        "sync_modules": blink_mgr.cameras_discovered,
        "total_cameras": sum(
            len(s["cameras"]) for s in blink_mgr.cameras_discovered.values()
        )
    }


@app.get("/videos")
async def get_videos(camera: Optional[str] = None):
    if not blink_mgr.authenticated:
        return {"error": "Not authenticated"}
    videos = await blink_mgr.get_latest_videos(camera)
    return {"timestamp": datetime.now().isoformat(), "videos": videos}


@app.post("/check-tailgating")
async def check_tailgating(location: Optional[str] = None):
    if not blink_mgr.authenticated:
        return {
            "timestamp": datetime.now().isoformat(),
            "error": "Not authenticated with Blink",
            "pending_2fa": blink_mgr.pending_2fa,
            "hint": "POST to /verify-2fa?pin=YOUR_PIN"
        }
    results = []
    try:
        videos = await blink_mgr.get_latest_videos()
        for video in videos:
            results.append({
                "camera": video["camera_name"],
                "camera_id": video["camera_id"],
                "network_id": video["network_id"],
                "motion_detected": video["motion_detected"],
                "last_motion": video["last_motion"],
                "clip_available": video["clip"] is not None,
                "status": "motion_detected" if video["motion_detected"] else "clear"
            })
    except Exception as e:
        logger.error(f"Check error: {e}")
        return {"error": str(e)}
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
            with open(TAILGATE_LOG_CSV, "r") as f:
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
        with open(TAILGATE_LOG_CSV, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                stats["total_events"] += 1
                location = row.get("location", "Unknown")
                stats["by_location"][location] = stats["by_location"].get(location, 0) + 1
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=10000)
