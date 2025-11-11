from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from typing import AsyncIterator, Iterable, List

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .config import settings
from .ffmpeg_buffer import buffer_manager


app = FastAPI(title="Live Audio Proxy with Rolling Buffer")


# CORS for local frontend dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup() -> None:
    # Start the FFmpeg segmenter/cleaner threads
    buffer_manager.start()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    buffer_manager.stop()


# ---------------------- Live proxy ----------------------
async def _iter_upstream(url: str) -> AsyncIterator[bytes]:
    # Stream chunks from upstream and yield to client
    timeout = httpx.Timeout(None)
    headers = {"Icy-MetaData": "1"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        while True:
            try:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(8192):
                        if chunk:
                            yield chunk
                        # Allow cancellation
                        await asyncio.sleep(0)
            except (httpx.HTTPError, httpx.TransportError):
                # brief backoff then reconnect
                await asyncio.sleep(1)
                continue
            break


@app.get("/live")
async def live() -> StreamingResponse:
    # Default to mp3; if upstream provides content-type we could reflect it.
    return StreamingResponse(_iter_upstream(settings.STREAM_URL), media_type="audio/mpeg")


# ---------------------- Download last N minutes ----------------------
def _concat_stream(file_list: List[Path]) -> Iterable[bytes]:
    if not file_list:
        print("[CONCAT] Empty file list, returning empty")
        return []
    
    print(f"[CONCAT] Concatenating {len(file_list)} files")
    
    # Filter out empty files before concatenation
    valid_files = []
    for p in file_list:
        if not p.exists():
            print(f"[CONCAT] Skipping non-existent file: {p.name}")
            continue
        size = p.stat().st_size
        if size == 0:
            print(f"[CONCAT] Skipping empty file: {p.name}")
            continue
        valid_files.append(p)
    
    if not valid_files:
        print("[CONCAT] No valid files to concatenate!")
        return []
    
    if len(valid_files) < len(file_list):
        print(f"[CONCAT] Filtered {len(file_list) - len(valid_files)} empty/invalid files, using {len(valid_files)} valid files")
    
    for i, p in enumerate(valid_files[:5]):  # Log first 5
        print(f"[CONCAT]   File {i+1}: {p.name} ({p.stat().st_size} bytes)")
    if len(valid_files) > 5:
        print(f"[CONCAT]   ... and {len(valid_files) - 5} more files")
    
    # Create concat demuxer list file (most reliable method)
    # Use absolute paths with forward slashes and proper escaping
    list_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".txt", newline="\n", encoding="utf-8"
        ) as f:
            list_path = Path(f.name)
            for p in valid_files:
                # Convert to absolute path and use forward slashes
                abs_path = p.resolve().as_posix()
                # Escape single quotes and backslashes for concat demuxer
                escaped = abs_path.replace("'", "'\\''").replace("\\", "/")
                f.write(f"file '{escaped}'\n")
        
        print(f"[CONCAT] Created concat list file: {list_path}")
        
        # Build FFmpeg command using concat demuxer
        cmd = [
            settings.FFMPEG_PATH,
            "-hide_banner",
            "-loglevel",
            "error",  # Only show errors
            "-nostdin",
            "-f",
            "concat",
            "-safe",
            "0",  # Allow absolute paths
            "-i",
            str(list_path),
            # Use copy codec for speed (segments are already MP3)
            "-c",
            "copy",
            "-f",
            "mp3",
            "pipe:1",
        ]
        
        print(f"[CONCAT] Running FFmpeg: {' '.join(cmd[:8])}... (truncated)")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # Capture stderr for debugging
        )
        print(f"[CONCAT] FFmpeg process started (PID: {proc.pid})")
    except Exception as exc:
        print(f"[CONCAT] FFmpeg setup failed: {exc}")
        if list_path and list_path.exists():
            list_path.unlink()
        raise HTTPException(status_code=500, detail=f"FFmpeg setup failed: {exc}")

    def _gen() -> Iterable[bytes]:
        stderr_data = b""
        bytes_yielded = 0
        try:
            assert proc.stdout is not None
            assert proc.stderr is not None
            
            # Read stdout in chunks
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                bytes_yielded += len(chunk)
                yield chunk
            
            print(f"[CONCAT] Yielded {bytes_yielded} bytes total")
            
            # Read any remaining stderr (for error detection)
            stderr_data = proc.stderr.read()
            if stderr_data:
                stderr_text = stderr_data.decode("utf-8", errors="ignore")
                print(f"[CONCAT] FFmpeg stderr: {stderr_text[:500]}")
        except Exception as e:
            print(f"[CONCAT] Read exception: {e}")
            # If read fails, try to get stderr for debugging
            if proc.stderr:
                try:
                    stderr_data = proc.stderr.read()
                except Exception:
                    pass
            raise HTTPException(
                status_code=500,
                detail=f"FFmpeg read failed: {e}. stderr: {stderr_data.decode('utf-8', errors='ignore')[:200]}",
            )
        finally:
            # Cleanup
            try:
                returncode = proc.wait(timeout=5)
                print(f"[CONCAT] FFmpeg process finished with returncode {returncode}")
                if returncode != 0 and stderr_data:
                    error_msg = stderr_data.decode("utf-8", errors="ignore")[:500]
                    print(f"[CONCAT] FFmpeg error (returncode {returncode}): {error_msg}")
            except subprocess.TimeoutExpired:
                print("[CONCAT] FFmpeg process timeout, killing")
                proc.kill()
                proc.wait()
            except Exception as e:
                print(f"[CONCAT] Cleanup exception: {e}")
                try:
                    proc.kill()
                except Exception:
                    pass
            # Remove temp list file
            if list_path and list_path.exists():
                try:
                    list_path.unlink()
                    print(f"[CONCAT] Removed temp list file")
                except Exception as e:
                    print(f"[CONCAT] Failed to remove temp file: {e}")

    return _gen()


@app.get("/download")
def download(minutes: int = Query(2, ge=1, le=30)) -> StreamingResponse:
    """Download the last N minutes of audio as a single MP3 file."""
    print(f"[DOWNLOAD] Requested minutes: {minutes} (type: {type(minutes)})")
    files = buffer_manager.recent_segments_for_minutes(minutes)
    print(f"[DOWNLOAD] Selected {len(files)} segment files")
    if not files:
        raise HTTPException(status_code=503, detail="Buffer not ready yet; please try again shortly")

    return StreamingResponse(
        _concat_stream(files),
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": f"attachment; filename=last-{minutes}-minutes.mp3",
            # No content-length because we're streaming
            "Cache-Control": "no-store",
        },
    )


@app.get("/debug/segments")
def debug_segments(minutes: int = Query(1, ge=1)):
    files = buffer_manager.recent_segments_for_minutes(minutes)
    return {
        "count": len(files),
        "files": [
            {
                "name": f.name,
                "size_bytes": f.stat().st_size if f.exists() else 0,
                "mtime": f.stat().st_mtime if f.exists() else 0,
                "path": str(f),
            }
            for f in files
        ],
    }


@app.get("/")
def root():
    return {"status": "ok"}


