"""
Club 24 Blink Tailgate Detection System
Uses blinkpy with saved credentials
"""

import os
import json
import asyncio
import csv
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
PEOPLE_COUNT_THRESHOLD = 2


class BlinkManager:
    def __init__(self):
        self.blink = None
        self.authenticated = False
        self.cameras_discovered = {}
    
    async def authenticate(self):
        try:
            # Write creds from env var to file
            if BLINK_CREDS and not Path(BLINK_CREDS_FILE).exists():
                Path(BLINK_CREDS_FILE).write_text(BLINK_CREDS)
                logger.info("Wrote credentials from env var")
            
            if not Path(BLINK_CREDS_FILE).exists():
                logger.warning("No credentials file found")
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
                # Try 2FA flow if needed
                try:
                    pin_needed = "2FA" in str(type(e).__name__)
                    if pin_needed:
                        logger.error("2FA required - need fresh credentials")
                        return False
                except:
                    pass
                return False
            
            if self.blink.cameras:
                self.authenticated = True
                await self.discover_cameras()
                # Save refreshed credentials
                await self.blink.save(BLINK_CREDS_FILE)
                logger.info("Blink authenticated successfully")
                return True
            else:
                logger.warning("No cameras found after auth")
                return False
                    
        except Exception as e:
            logger.error(f"Authentication error: {e}")
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
            
            logger.info(f"Discovered {len(self.blink.cameras)} cameras across {len(self.blink.sync)} sync modules")
            
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
        except Exception as e:
            logger.error(f"Logging error: {e}")


app = FastAPI(title="Club 24 Tailgate Detector")
blink_mgr = BlinkManager()


@app.on_event("startup")
async def startup_event():
    logger.info("Club 24 Tailgate Detector Starting...")
    await blink_mgr.authenticate()


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "blink_authenticated": blink_mgr.authenticated,
        "cameras": len(blink_mgr.blink.cameras) if blink_mgr.blink and blink_mgr.blink.cameras else 0,
        "sync_modules": len(blink_mgr.blink.sync) if blink_mgr.blink and blink_mgr.blink.sync else 0,
        "account_id": blink_mgr.blink.account_id if blink_mgr.blink else None
    }


@app.get("/cameras")
async def list_cameras():
    if not blink_mgr.authenticated:
        return {"error": "Not authenticated"}
    return {
        "sync_modules": blink_mgr.cameras_discovered,
        "total_cameras": len(blink_mgr.blink.cameras)
    }


@app.get("/videos")
async def get_videos(camera: Optional[str] = None):
    if not blink_mgr.authenticated:
        return {"error": "Not authenticated"}
    videos = await blink_mgr.get_latest_videos(camera)
    return {"timestamp": datetime.now().isoformat(), "videos": videos}


@app.post("/check-tailgating")
async def check_tailgating():
    if not blink_mgr.authenticated:
        return {"error": "Not authenticated"}
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
                "status": "MOTION" if video["motion_detected"] else "clear"
            })
    except Exception as e:
        return {"error": str(e)}
    return {
        "timestamp": datetime.now().isoformat(),
        "checks_performed": len(results),
        "results": results
    }


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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=10000)
