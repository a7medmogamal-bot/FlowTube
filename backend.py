"""
FlowTube Backend - Vercel Serverless
"""

import os
import uuid
import asyncio
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, Field, validator
from dotenv import load_dotenv

import yt_dlp
import sqlite3
from contextlib import contextmanager
import time
import tempfile

load_dotenv()

# ============ Configuration ============
class Config:
    APP_ENV = os.getenv("APP_ENV", "production")
    SECRET_KEY = os.getenv("SECRET_KEY", "dev_secret_key")
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./flowtube.db")
    TEMP_DIR = os.getenv("TEMP_DIR", tempfile.gettempdir() + "/flowtube")
    MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", "104857600"))
    JOB_TIMEOUT = int(os.getenv("JOB_TIMEOUT", "600"))
    RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "20"))
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")

config = Config()
Path(config.TEMP_DIR).mkdir(parents=True, exist_ok=True)

# ============ Database (in-memory for serverless) ============
class Database:
    def __init__(self):
        self.db_path = config.DATABASE_URL.replace("sqlite:///", "")
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    plan TEXT DEFAULT 'free',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    url TEXT NOT NULL,
                    quality TEXT,
                    status TEXT DEFAULT 'queued',
                    progress INTEGER DEFAULT 0,
                    file_path TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP,
                    error_message TEXT
                )
            """)
    
    def get_user(self, user_id: str) -> Optional[Dict]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(row) if row else None
    
    def create_user(self, user_id: str, plan: str = "free") -> Dict:
        with self.get_connection() as conn:
            conn.execute("INSERT OR IGNORE INTO users (id, plan) VALUES (?, ?)", (user_id, plan))
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(row)
    
    def create_job(self, user_id: str, url: str, quality: str) -> Dict:
        job_id = str(uuid.uuid4())
        expires_at = datetime.utcnow() + timedelta(seconds=config.JOB_TIMEOUT)
        with self.get_connection() as conn:
            conn.execute(
                "INSERT INTO jobs (id, user_id, url, quality, status, expires_at) VALUES (?, ?, ?, ?, 'queued', ?)",
                (job_id, user_id, url, quality, expires_at)
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row)
    
    def get_job(self, job_id: str) -> Optional[Dict]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None
    
    def update_job(self, job_id: str, **kwargs):
        with self.get_connection() as conn:
            fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
            values = list(kwargs.values()) + [job_id]
            conn.execute(f"UPDATE jobs SET {fields} WHERE id = ?", values)

db = Database()

# ============ Models ============
class InfoRequest(BaseModel):
    url: str
    
    @validator('url')
    def validate_url(cls, v):
        if not v or len(v) > 2000:
            raise ValueError('Invalid URL')
        try:
            parsed = urlparse(v)
            if parsed.scheme not in ['http', 'https']:
                raise ValueError('Invalid URL scheme')
            if not parsed.netloc:
                raise ValueError('Invalid URL')
        except Exception:
            raise ValueError('Invalid URL')
        return v

class DownloadRequest(BaseModel):
    url: str
    quality: str = Field(default="360p")
    
    @validator('url')
    def validate_url(cls, v):
        if not v or len(v) > 2000:
            raise ValueError('Invalid URL')
        try:
            parsed = urlparse(v)
            if parsed.scheme not in ['http', 'https']:
                raise ValueError('Invalid URL scheme')
            if not parsed.netloc:
                raise ValueError('Invalid URL')
        except Exception:
            raise ValueError('Invalid URL')
        return v
    
    @validator('quality')
    def validate_quality(cls, v):
        if v not in ['360p', '480p', '720p', '1080p']:
            raise ValueError('Invalid quality')
        return v

# ============ Services ============
class ContentPolicyService:
    ALLOWED_DOMAINS = ['youtube.com', 'youtu.be', 'vimeo.com']
    
    @staticmethod
    def is_url_allowed(url: str) -> bool:
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower().replace('www.', '')
            return any(domain == d or domain.endswith('.' + d) for d in ContentPolicyService.ALLOWED_DOMAINS)
        except Exception:
            return False

class SubscriptionService:
    FREE_QUALITIES = ['360p', '480p']
    PLUS_QUALITIES = ['360p', '480p', '720p', '1080p']
    
    @staticmethod
    def get_user_plan(user_id: str) -> str:
        user = db.get_user(user_id)
        return user.get('plan', 'free') if user else 'free'
    
    @staticmethod
    def can_access_quality(user_id: str, quality: str) -> bool:
        plan = SubscriptionService.get_user_plan(user_id)
        if plan == 'plus':
            return quality in SubscriptionService.PLUS_QUALITIES
        return quality in SubscriptionService.FREE_QUALITIES

class DownloadService:
    @staticmethod
    def get_video_info(url: str) -> Dict[str, Any]:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'noplaylist': True,
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                title = info.get('title', 'Unknown Title')
                duration = info.get('duration', 0)
                thumbnail = info.get('thumbnail', '')
                source = info.get('extractor_key', 'Unknown')
                
                formats = info.get('formats', [])
                qualities = set()
                for fmt in formats:
                    height = fmt.get('height')
                    if height:
                        if height >= 1080:
                            qualities.add('1080p')
                        elif height >= 720:
                            qualities.add('720p')
                        elif height >= 480:
                            qualities.add('480p')
                        elif height >= 360:
                            qualities.add('360p')
                
                available_qualities = sorted(list(qualities), key=lambda q: int(q.replace('p', '')), reverse=True)
                
                return {
                    'title': title,
                    'duration': duration,
                    'thumbnail': thumbnail,
                    'source': source,
                    'qualities': available_qualities
                }
        except Exception:
            raise HTTPException(status_code=400, detail={
                'success': False,
                'error': {
                    'code': 'CONTENT_UNAVAILABLE',
                    'message': 'هذا المحتوى غير متاح للتحميل.'
                }
            })
    
    @staticmethod
    def process_download(job_id: str, url: str, quality: str):
        try:
            db.update_job(job_id, status='downloading', progress=10)
            
            job_dir = Path(config.TEMP_DIR) / job_id
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
                'max_filesize': config.MAX_FILE_SIZE,
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                db.update_job(job_id, status='downloading', progress=30)
                ydl.download([url])
                db.update_job(job_id, status='processing', progress=60)
            
            files = [f for f in job_dir.iterdir() if f.is_file()]
            if files:
                files.sort(key=lambda f: f.stat().st_size, reverse=True)
                file_path = str(files[0])
                db.update_job(job_id, status='completed', progress=100, file_path=file_path)
            else:
                db.update_job(job_id, status='failed', error_message='Download failed')
                
        except Exception as e:
            db.update_job(job_id, status='failed', error_message='Processing failed')

# ============ FastAPI App ============
app = FastAPI(title="FlowTube API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============ Helpers ============
def get_user_id(request: Request) -> str:
    user_id = request.headers.get('X-User-ID', 'demo_user')
    user = db.get_user(user_id)
    if not user:
        db.create_user(user_id)
    return user_id

# ============ API Routes ============
@app.get("/")
async def root():
    return {"success": True, "message": "FlowTube API"}

@app.post("/api/info")
async def get_video_info(request: InfoRequest):
    if not ContentPolicyService.is_url_allowed(request.url):
        return JSONResponse(status_code=400, content={
            "success": False,
            "error": {"code": "UNSUPPORTED_SOURCE", "message": "هذا المصدر غير مدعوم."}
        })
    
    try:
        info = DownloadService.get_video_info(request.url)
        return {"success": True, "data": info}
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content=e.detail)
    except Exception:
        return JSONResponse(status_code=500, content={
            "success": False,
            "error": {"code": "PROCESSING_FAILED", "message": "فشلت المعالجة."}
        })

@app.post("/api/download")
async def download_content(request: DownloadRequest, http_request: Request):
    if not ContentPolicyService.is_url_allowed(request.url):
        return JSONResponse(status_code=400, content={
            "success": False,
            "error": {"code": "UNSUPPORTED_SOURCE", "message": "هذا المصدر غير مدعوم."}
        })
    
    user_id = get_user_id(http_request)
    
    if not SubscriptionService.can_access_quality(user_id, request.quality):
        return JSONResponse(status_code=403, content={
            "success": False,
            "error": {"code": "QUALITY_REQUIRES_PLUS", "message": f"جودة {request.quality} تتطلب اشتراك Plus."}
        })
    
    job = db.create_job(user_id, request.url, request.quality)
    
    # Run download synchronously for serverless
    DownloadService.process_download(job['id'], request.url, request.quality)
    
    updated_job = db.get_job(job['id'])
    return {"success": True, "data": {"job_id": job['id'], "status": updated_job['status']}}

@app.get("/api/progress/{job_id}")
async def get_job_progress(job_id: str):
    job = db.get_job(job_id)
    if not job:
        return JSONResponse(status_code=404, content={
            "success": False,
            "error": {"code": "JOB_EXPIRED", "message": "انتهت صلاحية المهمة."}
        })
    
    return {
        "success": True,
        "data": {
            "job_id": job['id'],
            "status": job['status'],
            "progress": job['progress'] or 0,
            "download_url": f"/api/file/{job['id']}" if job['status'] == 'completed' and job.get('file_path') else None
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

# ============ Vercel Handler ============
from mangum import Mangum
handler = Mangum(app)
