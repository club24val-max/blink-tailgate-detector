"""
Club 24 Blink Tailgate Detection System
Continuous scanning + Email alerts via Google Apps Script
"""

import os
import json
import asyncio
import csv
import base64
import aiohttp
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import logging

from aiohttp import ClientSession
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth
from fastapi import FastAPI, HTTPException
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BLINK_CREDS = os.getenv("BLINK_CREDS", "")
BLINK_CREDS_FILE = "./blink_credentials.json"
TAILGATE_LOG_CSV = "./tailgate_events.csv"
VISION_API_KEY = os.getenv("GOOGLE_VISION_API_KEY", "")
PEOPLE_THRESHOLD = 2
CONFIDENCE_THRESHOLD = 0.5
SCAN_INTERVAL_SECONDS = 120

EMAIL_WEBHOOK = "https://script.google.com/macros/s/AKfycbzWr-xlOnq2ayUuFuU8ruJ3jRl4SItNRtmAD8wZY4vwq6AwTpw_XoVusyN5FjyyQSJ1/exec"

ALERT_CAMERAS = {
    "Wallingford Front Door": "club24wf@gmail.com",
    "Torrington Main Door": "club24tor@gmail.com",
    "Front Desk Ridgefield": "club24rf@gmail.com",
    "Newtown Front Door": "club24nt@gmail.com",
    "New milford Front": "club24nm@gmail.com",
    "Middletown Front Desk": "club24mt@gmail.com",
    "Brookfield Door": "club24bf@gmail.com",
}

ET = timezone(timedelta(hours=-4))

def now_et():
    return datetime.now(ET)

def is_unstaffed_hours():
    now = now_et()
    hour = now.hour
    weekday = now.weekday()
    if weekday < 5:
        return hour >= 21 or hour < 8
    else:
        return hour >= 15 or hour < 8


async def send_alert_email(camera_name, people_count, scores, recipient, motion_time=None):
    try:
        now = now_et()
        time_str = now.strftime("%I:%M %p ET")
        date_str = now.strftime("%A, %B %d, %Y")
        conf_str = ", ".join([str(round(s * 100)) + "%" for s in scores])

        subject = "TAILGATE ALERT - " + camera_name + " - " + str(people_count) + " people detected"

        html = (
            '<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">'
            '<div style="background:#E24B4A;color:white;padding:20px;border-radius:8px 8px 0 0;">'
            '<h1 style="margin:0;font-size:22px;">TAILGATE ALERT</h1>'
            '<p style="margin:5px 0 0;opacity:0.9;">Club 24 Security</p></div>'
            '<div style="background:white;padding:20px;border:1px solid #ddd;border-radius:0 0 8px 8px;">'
            '<div style="background:#FFF3F3;border-left:4px solid #E24B4A;padding:15px;margin-bottom:20px;">'
            '<h2 style="margin:0 0 10px;color:#E24B4A;">' + str(people_count) + ' People Detected</h2>'
            '<p style="margin:0;"><strong>Location:</strong> ' + camera_name + '</p>'
            '<p style="margin:5px 0;"><strong>Time:</strong> ' + time_str + '</p>'
            '<p style="margin:5px 0;"><strong>Date:</strong> ' + date_str + '</p>'
            '<p style="margin:5px 0;"><strong>Confidence:</strong> ' + conf_str + '</p>'
            + ('<p style="margin:5px 0;"><strong>Motion Detected:</strong> ' + str(motion_time) + '</p>' if motion_time else '')
            '</div>'
            '<p style="color:#E24B4A;font-weight:bold;font-size:16px;">'
            'ACTION REQUIRED: Open the Blink app to review footage now.</p>'
            '<hr style="border:none;border-top:1px solid #eee;margin:20px 0;">'
            '<p style="color:#999;font-size:12px;">'
            'Club 24 Tailgate Detection System</p>'
            '</div></div>'
        )

        text = "TAILGATE ALERT - " + camera_name + " - " + str(people_count) + " people at " + time_str + (" | Motion: " + str(motion_time) if motion_time else "")

        payload = {
            "to": recipient,
            "subject": subject,
            "text": text,
            "html": html
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(EMAIL_WEBHOOK, json=payload, allow_redirects=True) as resp:
                logger.info("Email webhook response: " + str(resp.status))
                if resp.status == 200 or resp.status == 302:
                    logger.info("EMAIL SENT to " + recipient + " for " + camera_name)
                    return True
                else:
                    body = await resp.text()
                    logger.error("Email webhook error: " + str(resp.status) + " - " + body)
                    return False
    except Exception as e:
        logger.error("Email error: " + str(e))
        return False


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
                logger.warning("Start exception: " + str(e))
                return False
            if self.blink.cameras:
                self.authenticated = True
                await self.discover_cameras()
                await self.blink.save(BLINK_CREDS_FILE)
                logger.info("Blink authenticated")
                return True
            return False
        except Exception as e:
            logger.error("Auth error: " + str(e))
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
                    "cameras": []
                }
            for cam_name, camera in self.blink.cameras.items():
                for sync_name, sync_data in self.cameras_discovered.items():
                    if sync_data["network_id"] == camera.network_id:
                        sync_data["cameras"].append({
                            "name": cam_name,
                            "camera_id": camera.camera_id,
                            "network_id": camera.network_id
                        })
                        break
            logger.info("Discovered " + str(len(self.blink.cameras)) + " cameras")
        except Exception as e:
            logger.error("Discovery error: " + str(e))

    async def get_camera_thumbnail(self, camera_name):
        try:
            if not self.blink or camera_name not in self.blink.cameras:
                return None
            camera = self.blink.cameras[camera_name]
            tmp_path = "/tmp/" + camera_name.replace(" ", "_") + "_thumb.jpg"
            await camera.image_to_file(tmp_path)
            if Path(tmp_path).exists():
                with open(tmp_path, "rb") as fh:
                    return fh.read()
            return None
        except Exception as e:
            logger.error("Thumbnail error: " + str(e))
            return None

    async def get_latest_videos(self, camera_name=None):
        videos = []
        try:
            if not self.blink or not self.authenticated:
                return videos
            await self.blink.refresh()
            cams = {}
            if camera_name and camera_name in self.blink.cameras:
                cams = {camera_name: self.blink.cameras[camera_name]}
            else:
                cams = self.blink.cameras
            for name, camera in cams.items():
                videos.append({
                    "camera_name": name,
                    "camera_id": camera.camera_id,
                    "network_id": camera.network_id,
                    "clip": camera.clip,
                    "last_motion": str(camera.last_motion) if camera.last_motion else None,
                    "motion_detected": camera.motion_detected
                })
            return videos
        except Exception as e:
            logger.error("Videos error: " + str(e))
            return videos


class VisionAnalyzer:
    def __init__(self):
        self.api_key = VISION_API_KEY
        self.api_url = "https://vision.googleapis.com/v1/images:annotate?key=" + self.api_key
        self.available = bool(self.api_key)

    async def count_people(self, image_data):
        if not self.available:
            return {"people_count": 0, "error": "No API key"}
        try:
            b64 = base64.b64encode(image_data).decode("utf-8")
            payload = {
                "requests": [{
                    "image": {"content": b64},
                    "features": [{"type": "OBJECT_LOCALIZATION", "maxResults": 20}]
                }]
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, json=payload) as resp:
                    if resp.status != 200:
                        return {"people_count": 0, "error": "API " + str(resp.status)}
                    data = await resp.json()
            response = data.get("responses", [{}])[0]
            people_count = 0
            scores = []
            for obj in response.get("localizedObjectAnnotations", []):
                if obj.get("name", "").lower() == "person" and obj.get("score", 0) >= CONFIDENCE_THRESHOLD:
                    people_count += 1
                    scores.append(round(obj["score"], 3))
            return {
                "people_count": people_count,
                "person_scores": scores,
                "is_tailgate": people_count >= PEOPLE_THRESHOLD
            }
        except Exception as e:
            logger.error("Vision error: " + str(e))
            return {"people_count": 0, "error": str(e)}


class EventLogger:
    @staticmethod
    def log_event(location, camera, people_count, confidence, email_sent=False):
        file_exists = Path(TAILGATE_LOG_CSV).exists()
        try:
            with open(TAILGATE_LOG_CSV, "a", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=[
                    "timestamp", "location", "camera_name", "people_count",
                    "confidence", "email_sent"
                ])
                if not file_exists:
                    writer.writeheader()
                writer.writerow({
                    "timestamp": now_et().isoformat(),
                    "location": location,
                    "camera_name": camera,
                    "people_count": people_count,
                    "confidence": confidence,
                    "email_sent": email_sent
                })
        except Exception as e:
            logger.error("Log error: " + str(e))


blink_mgr = BlinkManager()
vision_api = VisionAnalyzer()
event_logger = EventLogger()
scan_stats = {"total_scans": 0, "last_scan": None, "alerts_sent": 0, "running": False}


async def continuous_scan_loop():
    scan_stats["running"] = True
    logger.info("Continuous scan loop started")

    while True:
        try:
            if not is_unstaffed_hours():
                scan_stats["running"] = False
                logger.info("Staffed hours - sleeping 5 min")
                await asyncio.sleep(300)
                continue

            if not blink_mgr.authenticated:
                logger.warning("Not authenticated - retrying in 60s")
                await asyncio.sleep(60)
                continue

            scan_stats["running"] = True
            logger.info("Scanning cameras at " + now_et().strftime("%I:%M %p ET"))

            await blink_mgr.blink.refresh()

            for cam_name in ALERT_CAMERAS:
                try:
                    if cam_name not in blink_mgr.blink.cameras:
                        continue

                    image_data = await blink_mgr.get_camera_thumbnail(cam_name)
                    if not image_data:
                        continue

                    result = await vision_api.count_people(image_data)
                    people = result.get("people_count", 0)
                    scores = result.get("person_scores", [])

                    if result.get("is_tailgate"):
                        recipient = ALERT_CAMERAS[cam_name]
                        avg_conf = sum(scores) / max(len(scores), 1)
                        motion_time = str(blink_mgr.blink.cameras[cam_name].last_motion) if blink_mgr.blink.cameras[cam_name].last_motion else None

                        email_sent = await send_alert_email(cam_name, people, scores, recipient, motion_time)

                        event_logger.log_event(
                            location=cam_name, camera=cam_name,
                            people_count=people, confidence=round(avg_conf, 3),
                            email_sent=email_sent
                        )
                        scan_stats["alerts_sent"] += 1
                        logger.warning("TAILGATE: " + cam_name + " - " + str(people) + " people - email: " + str(email_sent))

                except Exception as e:
                    logger.error("Scan error " + cam_name + ": " + str(e))

            scan_stats["total_scans"] += 1
            scan_stats["last_scan"] = now_et().isoformat()

            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

        except Exception as e:
            logger.error("Loop error: " + str(e))
            await asyncio.sleep(30)


app = FastAPI(title="Club 24 Tailgate Detector")


@app.on_event("startup")
async def startup_event():
    logger.info("Club 24 Tailgate Detector Starting...")
    await blink_mgr.authenticate()
    asyncio.create_task(continuous_scan_loop())
    logger.info("Background scan loop started")


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "blink_authenticated": blink_mgr.authenticated,
        "vision_api": vision_api.available,
        "email_webhook": bool(EMAIL_WEBHOOK),
        "cameras": len(blink_mgr.blink.cameras) if blink_mgr.blink and blink_mgr.blink.cameras else 0,
        "account_id": blink_mgr.blink.account_id if blink_mgr.blink else None,
        "current_time_et": now_et().strftime("%I:%M %p ET - %A"),
        "is_unstaffed": is_unstaffed_hours(),
        "scan_loop_running": scan_stats["running"],
        "total_scans": scan_stats["total_scans"],
        "last_scan": scan_stats["last_scan"],
        "alerts_sent": scan_stats["alerts_sent"]
    }


@app.get("/cameras")
async def list_cameras():
    if not blink_mgr.authenticated:
        return {"error": "Not authenticated"}
    return {
        "total_cameras": len(blink_mgr.blink.cameras),
        "alert_cameras": ALERT_CAMERAS,
        "sync_modules": blink_mgr.cameras_discovered
    }


@app.post("/analyze-camera")
async def analyze_camera(camera_name: str):
    if not blink_mgr.authenticated:
        return {"error": "Not authenticated"}
    image_data = await blink_mgr.get_camera_thumbnail(camera_name)
    if not image_data:
        return {"error": "No image from " + camera_name}
    result = await vision_api.count_people(image_data)
    return {"camera": camera_name, "timestamp": now_et().isoformat(), "analysis": result}


@app.post("/scan-all")
async def scan_all_cameras():
    if not blink_mgr.authenticated:
        return {"error": "Not authenticated"}

    await blink_mgr.blink.refresh()
    results = []
    alerts = []

    for cam_name, camera in blink_mgr.blink.cameras.items():
        try:
            image_data = await blink_mgr.get_camera_thumbnail(cam_name)
            if not image_data:
                results.append({"camera": cam_name, "status": "no_image"})
                continue
            analysis = await vision_api.count_people(image_data)
            people = analysis.get("people_count", 0)
            scores = analysis.get("person_scores", [])
            is_alert_cam = cam_name in ALERT_CAMERAS
            is_tailgate = analysis.get("is_tailgate", False)

            status = "clear"
            if is_tailgate and is_alert_cam:
                status = "TAILGATE_ALERT"
                recipient = ALERT_CAMERAS[cam_name]
                avg_conf = sum(scores) / max(len(scores), 1)
                motion_time = str(camera.last_motion) if camera.last_motion else None
                email_sent = await send_alert_email(cam_name, people, scores, recipient, motion_time)
                event_logger.log_event(cam_name, cam_name, people, round(avg_conf, 3), email_sent)
                alerts.append({"camera": cam_name, "people": people, "email_sent": email_sent, "motion_time": motion_time})
            elif is_tailgate:
                status = "people_detected"

            results.append({
                "camera": cam_name,
                "is_alert_camera": is_alert_cam,
                "status": status,
                "people_count": people,
                "person_scores": scores
            })
        except Exception as e:
            results.append({"camera": cam_name, "status": "error", "error": str(e)})

    return {
        "timestamp": now_et().isoformat(),
        "is_unstaffed": is_unstaffed_hours(),
        "cameras_scanned": len(results),
        "tailgate_alerts": len(alerts),
        "alerts": alerts,
        "results": results
    }


@app.post("/scheduled-scan")
async def scheduled_scan():
    if not is_unstaffed_hours():
        return {"status": "skipped", "reason": "Staffed hours"}
    return await scan_all_cameras()


@app.get("/logs")
async def get_logs(limit: int = 50):
    events = []
    if Path(TAILGATE_LOG_CSV).exists():
        with open(TAILGATE_LOG_CSV, "r") as fh:
            reader = csv.DictReader(fh)
            events = list(reader)[-limit:]
    return {"events": events, "total": len(events)}


@app.get("/stats")
async def get_stats():
    if not Path(TAILGATE_LOG_CSV).exists():
        return {"total_events": 0, "by_location": {}}
    stats = {"total_events": 0, "by_location": {}}
    with open(TAILGATE_LOG_CSV, "r") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            stats["total_events"] += 1
            loc = row.get("location", "Unknown")
            stats["by_location"][loc] = stats["by_location"].get(loc, 0) + 1
    return stats


@app.get("/schedule")
async def get_schedule():
    return {
        "unstaffed_hours": {
            "mon_fri": "9:00 PM to 8:00 AM ET",
            "sat_sun": "3:00 PM to 8:00 AM ET"
        },
        "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
        "alert_cameras": ALERT_CAMERAS,
        "people_threshold": PEOPLE_THRESHOLD,
        "currently_unstaffed": is_unstaffed_hours(),
        "email_webhook": "Google Apps Script",
        "mode": "Continuous background scanning"
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=10000)
