"""
Club 24 Blink Tailgate Detection System
Continuous scanning + Email alerts during unstaffed hours
"""

import os
import json
import asyncio
import csv
import base64
import smtplib
import aiohttp
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import logging
from contextlib import asynccontextmanager

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
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_SENDER = "club24contactus@gmail.com"
PEOPLE_THRESHOLD = 2
CONFIDENCE_THRESHOLD = 0.5
SCAN_INTERVAL_SECONDS = 120  # Scan every 2 minutes

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


async def send_alert_email(camera_name, people_count, scores, recipient):
    """Send alert via Google Apps Script webhook"""
    try:
        now = now_et()
        subject = f"TAILGATE ALERT - {camera_name} - {people_count} people detected"
        html = f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
<div style="background:#E24B4A;color:white;padding:20px;border-radius:8px 8px 0 0;">
<h1 style="margin:0;font-size:22px;">TAILGATE ALERT</h1>
<p style="margin:5px 0 0;opacity:0.9;">Club 24 Security</p></div>
<div style="background:white;padding:20px;border:1px solid #ddd;border-radius:0 0 8px 8px;">
<div style="background:#FFF3F3;border-left:4px solid #E24B4A;padding:15px;margin-bottom:20px;">
<h2 style="margin:0 0 10px;color:#E24B4A;">{people_count} People Detected</h2>
<p style="margin:0;"><strong>Location:</strong> {camera_name}</p>
<p style="margin:5px 0;"><strong>Time:</strong> {now.strftime("%I:%M %p ET")}</p>
<p style="margin:5px 0;"><strong>Date:</strong> {now.strftime("%A, %B %d, %Y")}</p>
<p style="margin:5px 0;"><strong>Confidence:</strong> {", ".join([f"{s:.0%}" for s in scores])}</p>
</div>
<p style="color:#E24B4A;font-weight:bold;font-size:16px;">
ACTION REQUIRED: Open the Blink app to review footage now.</p>
<hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
<p style="color:#999;font-size:12px;">
Club 24 Tailgate Detection System | Mon-Fri 9pm-8am | Sat-Sun 3pm-8am ET</p>
</div></div>"""
        text = f"TAILGATE ALERT - {camera_name} - {people_count} people at {now.strftime('%I:%M %p ET')}"

        webhook_url = "https://script.google.com/macros/s/AKfycbzWr-xlOnq2ayUuFuU8ruJ3jRl4SItNRtmAD8wZY4vwq6AwTpw_XoVusyN5FjyyQSJ1/exec"

        payload = {
            "to": recipient,
            "subject": subject,
            "text": text,
            "html": html
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(webhook_url, json=payload) as resp:
                if resp.status == 200 or resp.status == 302:
                    logger.info(f"EMAIL SENT to {recipient} for {camera_name}")
                    return True
                else:
                    body = await resp.text()
                    logger.error(f"Email webhook error: {resp.status} - {body}")
                    return False
    except Exception as e:
        logger.error(f"Email error: {e}")
        return False
<body style="font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5;">
<div style="max-width: 600px; margin: 0 auto; background: white; border-radius: 8px; overflow: hidden;">
<div style="background: #E24B4A; color: white; padding: 20px;">
<h1 style="margin: 0; font-size: 22px;">TAILGATE ALERT</h1>
<p style="margin: 5px 0 0; opacity: 0.9;">Club 24 Security Monitoring</p>
</div>
<div style="padding: 20px;">
<div style="background: #FFF3F3; border-left: 4px solid #E24B4A; padding: 15px; margin-bottom: 20px;">
<h2 style="margin: 0 0 10px; color: #E24B4A;">{people_count} People Detected</h2>
<p style="margin: 0; color: #333;"><strong>Location:</strong> {location}</p>
<p style="margin: 5px 0; color: #333;"><strong>Time:</strong> {now.strftime("%I:%M %p ET")}</p>
<p style="margin: 5px 0; color: #333;"><strong>Date:</strong> {now.strftime("%A, %B %d, %Y")}</p>
<p style="margin: 5px 0; color: #333;"><strong>Confidence:</strong> {", ".join([f"{s:.0%}" for s in scores])}</p>
</div>
<p style="color: #E24B4A; font-weight: bold; font-size: 16px;">
ACTION REQUIRED: Open the Blink app to review footage now.
</p>
<hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
<p style="color: #999; font-size: 12px;">
Club 24 Tailgate Detection System<br>
Monitoring: Mon-Fri 9pm-8am | Sat-Sun 3pm-8am ET<br>
Alert threshold: {PEOPLE_THRESHOLD}+ people at front door
</p>
</div>
</div>
</body>
</html>"""

        raw_email = f"From: {GMAIL_SENDER}\r\nTo: {recipient}\r\nSubject: {subject}\r\nContent-Type: text/html; charset=utf-8\r\n\r\n{html_body}"
        encoded = base64.urlsafe_b64encode(raw_email.encode()).decode()

        # Use Gmail SMTP relay via aiohttp to an SMTP-to-HTTP bridge
        # Fallback: use a simple HTTP webhook approach
        # Direct approach: use smtplib in a thread to avoid blocking
        import concurrent.futures
        def _send_smtp():
            import smtplib as s
            from email.mime.text import MIMEText as MT
            from email.mime.multipart import MIMEMultipart as MM
            msg = MM("alternative")
            msg["Subject"] = subject
            msg["From"] = GMAIL_SENDER
            msg["To"] = recipient
            msg.attach(MT(html_body, "html"))
            with s.SMTP("smtp.gmail.com", 587, timeout=10) as srv:
                srv.ehlo()
                srv.starttls()
                srv.ehlo()
                srv.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
                srv.sendmail(GMAIL_SENDER, [recipient, GMAIL_SENDER], msg.as_string())
            return True

        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = await loop.run_in_executor(pool, _send_smtp)

        logger.info(f"EMAIL SENT to {recipient} for {camera_name}")
        return True
    except Exception as e:
        logger.error(f"Email error: {e}")
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
            logger.error(f"Thumbnail error: {e}")
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
            logger.error(f"Videos error: {e}")
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
            b64 = base64.b64encode(image_data).decode("utf-8")
            payload = {
                "requests": [{
                    "image": {"content": b64},
                    "features": [
                        {"type": "OBJECT_LOCALIZATION", "maxResults": 20}
                    ]
                }]
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, json=payload) as resp:
                    if resp.status != 200:
                        return {"people_count": 0, "error": f"API {resp.status}"}
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
            logger.error(f"Vision error: {e}")
            return {"people_count": 0, "error": str(e)}


class EventLogger:
    @staticmethod
    def log_event(location, camera, people_count, confidence, email_sent=False):
        file_exists = Path(TAILGATE_LOG_CSV).exists()
        try:
            with open(TAILGATE_LOG_CSV, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=[
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
            logger.error(f"Log error: {e}")


# Global state
blink_mgr = BlinkManager()
vision = VisionAnalyzer()
event_logger = EventLogger()
scan_task = None
scan_stats = {"total_scans": 0, "last_scan": None, "alerts_sent": 0, "running": False}


async def continuous_scan_loop():
    """Background loop that scans cameras during unstaffed hours"""
    scan_stats["running"] = True
    logger.info("Continuous scan loop started")

    while True:
        try:
            if not is_unstaffed_hours():
                scan_stats["running"] = False
                logger.info(f"Staffed hours - sleeping 5 min ({now_et().strftime('%I:%M %p ET')})")
                await asyncio.sleep(300)
                continue

            if not blink_mgr.authenticated:
                logger.warning("Not authenticated - retrying in 60s")
                await asyncio.sleep(60)
                continue

            scan_stats["running"] = True
            logger.info(f"Scanning all cameras ({now_et().strftime('%I:%M %p ET')})")

            await blink_mgr.blink.refresh()

            for cam_name in ALERT_CAMERAS:
                try:
                    if cam_name not in blink_mgr.blink.cameras:
                        continue

                    image_data = await blink_mgr.get_camera_thumbnail(cam_name)
                    if not image_data:
                        continue

                    result = await vision.count_people(image_data)
                    people = result.get("people_count", 0)
                    scores = result.get("person_scores", [])

                    if result.get("is_tailgate"):
                        recipient = ALERT_CAMERAS[cam_name]
                        avg_conf = sum(scores) / max(len(scores), 1)

                        email_sent = await send_alert_email(cam_name, people, scores, recipient)

                        event_logger.log_event(
                            location=cam_name, camera=cam_name,
                            people_count=people, confidence=round(avg_conf, 3),
                            email_sent=email_sent
                        )
                        scan_stats["alerts_sent"] += 1
                        logger.warning(f"TAILGATE: {cam_name} - {people} people - email: {email_sent}")

                except Exception as e:
                    logger.error(f"Scan error {cam_name}: {e}")

            scan_stats["total_scans"] += 1
            scan_stats["last_scan"] = now_et().isoformat()
            logger.info(f"Scan complete. Total: {scan_stats['total_scans']}, Alerts: {scan_stats['alerts_sent']}")

            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

        except Exception as e:
            logger.error(f"Loop error: {e}")
            await asyncio.sleep(30)


app = FastAPI(title="Club 24 Tailgate Detector")


@app.on_event("startup")
async def startup_event():
    global scan_task
    logger.info("Club 24 Tailgate Detector Starting...")
    await blink_mgr.authenticate()
    scan_task = asyncio.create_task(continuous_scan_loop())
    logger.info("Background scan loop started")


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "blink_authenticated": blink_mgr.authenticated,
        "vision_api": vision.available,
        "email_configured": bool(GMAIL_APP_PASSWORD),
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


@app.get("/videos")
async def get_videos(camera: Optional[str] = None):
    if not blink_mgr.authenticated:
        return {"error": "Not authenticated"}
    videos = await blink_mgr.get_latest_videos(camera)
    return {"timestamp": now_et().isoformat(), "videos": videos}


@app.post("/analyze-camera")
async def analyze_camera(camera_name: str):
    if not blink_mgr.authenticated:
        return {"error": "Not authenticated"}
    if not vision.available:
        return {"error": "Vision API not configured"}
    image_data = await blink_mgr.get_camera_thumbnail(camera_name)
    if not image_data:
        return {"error": f"No image from {camera_name}"}
    result = await vision.count_people(image_data)
    return {"camera": camera_name, "timestamp": now_et().isoformat(), "analysis": result}


@app.post("/scan-all")
async def scan_all_cameras():
    """Force scan all cameras right now"""
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
                results.append({"camera": cam_name, "status": "no_image"})
                continue
            analysis = await vision.count_people(image_data)
            people = analysis.get("people_count", 0)
            scores = analysis.get("person_scores", [])
            is_alert_cam = cam_name in ALERT_CAMERAS
            is_tailgate = analysis.get("is_tailgate", False)

            status = "clear"
            if is_tailgate and is_alert_cam:
                status = "TAILGATE_ALERT"
                recipient = ALERT_CAMERAS[cam_name]
                avg_conf = sum(scores) / max(len(scores), 1)
                email_sent = await send_alert_email(cam_name, people, scores, recipient)
                event_logger.log_event(cam_name, cam_name, people, round(avg_conf, 3), email_sent)
                alerts.append({"camera": cam_name, "people": people, "email_sent": email_sent})
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
    return {
        "unstaffed_hours": {
            "mon_fri": "9:00 PM - 8:00 AM ET",
            "sat_sun": "3:00 PM - 8:00 AM ET"
        },
        "scan_interval": f"Every {SCAN_INTERVAL_SECONDS} seconds",
        "alert_cameras": ALERT_CAMERAS,
        "people_threshold": PEOPLE_THRESHOLD,
        "currently_unstaffed": is_unstaffed_hours(),
        "email_from": GMAIL_SENDER,
        "mode": "Continuous background scanning"
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=10000)
