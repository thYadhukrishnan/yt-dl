import os
import re
import uuid
import tempfile
import logging
from fastapi import FastAPI, BackgroundTasks, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="YouTube & Media Downloader API", version="1.0.0")

# Setup static files directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

class DownloadRequest(BaseModel):
    url: str

def cleanup_file(file_path: str):
    """Safely remove temporary file after response is sent."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"Cleaned up temp file: {file_path}")
    except Exception as e:
        logger.error(f"Failed to delete temp file {file_path}: {e}")

def sanitize_filename(filename: str) -> str:
    """Sanitize string to be safe for HTTP headers and filenames."""
    cleaned = re.sub(r'[\\/*?:"<>|]', "", filename)
    return cleaned[:150].strip() or "media_file"

@app.get("/", response_class=FileResponse)
async def serve_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Index page not found")
    return FileResponse(index_path)

@app.post("/api/download")
async def download_media(request: DownloadRequest, background_tasks: BackgroundTasks):
    url = request.url.strip()
    if not url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="URL field cannot be empty.")

    # Ensure /tmp directory exists or fallback to system temp directory
    temp_dir = "/tmp"
    if not os.path.exists(temp_dir):
        try:
            os.makedirs(temp_dir, exist_ok=True)
        except Exception:
            temp_dir = tempfile.gettempdir()

    file_id = str(uuid.uuid4())
    outtmpl_pattern = os.path.join(temp_dir, f"{file_id}.%(ext)s")
    expected_mp4_path = os.path.join(temp_dir, f"{file_id}.mp4")

    ydl_opts = {
        'format': 'bestvideo*+bestaudio/best/b',
        'merge_output_format': 'mp4',
        'outtmpl': outtmpl_pattern,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['mweb', 'ios', 'android', 'web']
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
        }
    }

    # Check for cookies file (Render Secret File path included)
    cookie_locations = [
        "/etc/secrets/cookies.txt",
        os.path.join(BASE_DIR, "..", "cookies.txt"),
        os.path.join(BASE_DIR, "cookies.txt"),
        "/app/cookies.txt",
        "cookies.txt"
    ]
    cookie_path = next((path for path in cookie_locations if os.path.exists(path)), None)
    if cookie_path:
        # Copy to writable temp_dir because yt-dlp attempts to update cookies during download
        writable_cookie_path = os.path.join(temp_dir, "active_cookies.txt")
        try:
            import shutil
            shutil.copyfile(cookie_path, writable_cookie_path)
            ydl_opts['cookiefile'] = writable_cookie_path
            logger.info(f"Copied cookies from {cookie_path} to writable path: {writable_cookie_path}")
        except Exception as e:
            logger.error(f"Failed to copy cookies file: {e}")
            ydl_opts['cookiefile'] = cookie_path



    try:
        logger.info(f"Starting download for URL: {url}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            raw_title = info.get('title', 'video') if info else 'video'
            safe_title = sanitize_filename(raw_title)
            download_filename = f"{safe_title}.mp4"

        # Locate actual output file
        actual_path = expected_mp4_path
        if not os.path.exists(actual_path):
            candidates = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if f.startswith(file_id)]
            if candidates:
                actual_path = candidates[0]
            else:
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Downloaded file missing from temp storage.")

        # Register background cleanup task
        background_tasks.add_task(cleanup_file, actual_path)

        return FileResponse(
            path=actual_path,
            filename=download_filename,
            media_type="video/mp4",
            headers={"Content-Disposition": f'attachment; filename="{download_filename}"'}
        )

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp download error: {e}")
        # Clean user-facing error message
        err_msg = str(e).split('\n')[0]
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Media extraction failed: {err_msg}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Server error: {str(e)}")
