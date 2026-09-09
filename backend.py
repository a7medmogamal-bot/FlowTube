from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
from mangum import Mangum
from urllib.parse import urlparse
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from pathlib import Path
import os
import uuid
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager

import yt_dlp

TEMP_DIR = os.path.join(tempfile.gettempdir(), "flowtube")
MAX_FILE_SIZE = 104857600
Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)

class Database:
    def __init__(self):
        self.db_path = os.path.join(tempfile.gettempdir(), "flowtube.db")
        self.init_db()
    
    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    
    def init_db(self):
        with self.get_connection() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, plan TEXT DEFAULT 'free')")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    url TEXT,
                    quality TEXT,
                    status TEXT DEFAULT 'queued',
                    progress INTEGER DEFAULT 0,
                    file_path TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP
                )
            """)
    
    def get_user(self, user_id):
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(row) if row else None
    
    def create_user(self, user_id, plan="free"):
        with self.get_connection() as conn:
            conn.execute("INSERT OR IGNORE INTO users (id, plan) VALUES (?, ?)", (user_id, plan))
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(row)
    
    def create_job(self, user_id, url, quality):
        job_id = str(uuid.uuid4())
        expires_at = datetime.utcnow() + timedelta(seconds=600)
        with self.get_connection() as conn:
            conn.execute(
                "INSERT INTO jobs (id, user_id, url, quality, status, expires_at) VALUES (?, ?, ?, ?, 'queued', ?)",
                (job_id, user_id, url, quality, expires_at)
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row)
    
    def get_job(self, job_id):
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None
    
    def update_job(self, job_id, **kwargs):
        with self.get_connection() as conn:
            fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
            values = list(kwargs.values()) + [job_id]
            conn.execute(f"UPDATE jobs SET {fields} WHERE id = ?", values)

db = Database()

class InfoRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    quality: str = "360p"

class ContentPolicyService:
    ALLOWED_DOMAINS = ['youtube.com', 'youtu.be', 'vimeo.com']
    
    @staticmethod
    def is_url_allowed(url):
        try:
            domain = urlparse(url).netloc.lower().replace('www.', '')
            return any(domain == d or domain.endswith('.' + d) for d in ContentPolicyService.ALLOWED_DOMAINS)
        except:
            return False

class SubscriptionService:
    FREE_QUALITIES = ['360p', '480p']
    PLUS_QUALITIES = ['360p', '480p', '720p', '1080p']
    
    @staticmethod
    def can_access_quality(user_id, quality):
        user = db.get_user(user_id)
        plan = user.get('plan', 'free') if user else 'free'
        if plan == 'plus':
            return quality in SubscriptionService.PLUS_QUALITIES
        return quality in SubscriptionService.FREE_QUALITIES

class DownloadService:
    @staticmethod
    def get_video_info(url):
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'noplaylist': True,
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                formats = info.get('formats', [])
                qualities = set()
                for fmt in formats:
                    h = fmt.get('height')
                    if h:
                        if h >= 1080:
                            qualities.add('1080p')
                        elif h >= 720:
                            qualities.add('720p')
                        elif h >= 480:
                            qualities.add('480p')
                        elif h >= 360:
                            qualities.add('360p')
                
                return {
                    'title': info.get('title', 'Unknown'),
                    'duration': info.get('duration', 0),
                    'thumbnail': info.get('thumbnail', ''),
                    'source': info.get('extractor_key', 'Unknown'),
                    'qualities': sorted(list(qualities), key=lambda q: int(q.replace('p', '')), reverse=True)
                }
        except Exception:
            return None
    
    @staticmethod
    def process_download(job_id, url, quality):
        try:
            db.update_job(job_id, status='downloading', progress=10)
            
            job_dir = Path(TEMP_DIR) / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            file_id = uuid.uuid4().hex[:16]
            
            quality_map = {
                '360p': 'best[height<=360]/best',
                '480p': 'best[height<=480]/best',
                '720p': 'best[height<=720]/best',
                '1080p': 'best[height<=1080]/best'
            }
            
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,
                'format': quality_map.get(quality, 'best'),
                'outtmpl': str(job_dir / f"{file_id}.%(ext)s"),
                'max_filesize': MAX_FILE_SIZE,
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                db.update_job(job_id, status='downloading', progress=30)
                ydl.download([url])
                db.update_job(job_id, status='processing', progress=60)
            
            files = [f for f in job_dir.iterdir() if f.is_file()]
            if files:
                files.sort(key=lambda f: f.stat().st_size, reverse=True)
                db.update_job(job_id, status='completed', progress=100, file_path=str(files[0]))
            else:
                db.update_job(job_id, status='failed')
                
        except Exception:
            db.update_job(job_id, status='failed')

app = FastAPI(title="FlowTube API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {"success": True, "message": "FlowTube API is running!"}

@app.get("/api/health")
async def health():
    return {"success": True, "status": "healthy"}

@app.post("/api/info")
async def get_video_info(request: InfoRequest):
    if not ContentPolicyService.is_url_allowed(request.url):
        return JSONResponse(status_code=400, content={
            "success": False,
            "error": {"code": "UNSUPPORTED_SOURCE", "message": "هذا المصدر غير مدعوم."}
        })
    
    info = DownloadService.get_video_info(request.url)
    if not info:
        return JSONResponse(status_code=400, content={
            "success": False,
            "error": {"code": "CONTENT_UNAVAILABLE", "message": "هذا المحتوى غير متاح للتحميل."}
        })
    
    return {"success": True, "data": info}

@app.post("/api/download")
async def download_content(request: DownloadRequest):
    if not ContentPolicyService.is_url_allowed(request.url):
        return JSONResponse(status_code=400, content={
            "success": False,
            "error": {"code": "UNSUPPORTED_SOURCE", "message": "هذا المصدر غير مدعوم."}
        })
    
    user_id = "demo_user"
    user = db.get_user(user_id)
    if not user:
        db.create_user(user_id)
    
    if not SubscriptionService.can_access_quality(user_id, request.quality):
        return JSONResponse(status_code=403, content={
            "success": False,
            "error": {"code": "QUALITY_REQUIRES_PLUS", "message": f"جودة {request.quality} تتطلب اشتراك Plus."}
        })
    
    job = db.create_job(user_id, request.url, request.quality)
    
    DownloadService.process_download(job['id'], request.url, request.quality)
    
    updated_job = db.get_job(job['id'])
    
    if updated_job['status'] == 'completed':
        return {
            "success": True,
            "data": {
                "job_id": job['id'],
                "status": "completed",
                "download_url": f"/api/file/{job['id']}"
            }
        }
    
    return {
        "success": True,
        "data": {
            "job_id": job['id'],
            "status": updated_job['status']
        }
    }

@app.get("/api/file/{job_id}")
async def get_file(job_id: str):
    job = db.get_job(job_id)
    if not job or job['status'] != 'completed' or not job.get('file_path'):
        return JSONResponse(status_code=404, content={
            "success": False,
            "error": {"code": "JOB_EXPIRED", "message": "الملف غير موجود."}
        })
    
    file_path = job['file_path']
    if not os.path.exists(file_path):
        return JSONResponse(status_code=404, content={
            "success": False,
            "error": {"code": "JOB_EXPIRED", "message": "الملف غير موجود."}
        })
    
    return FileResponse(file_path, filename=os.path.basename(file_path), media_type='application/octet-stream')

handler = Mangum(app)
