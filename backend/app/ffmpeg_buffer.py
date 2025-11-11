import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List

from .config import settings


class FFmpegBufferManager:
    """Manages an FFmpeg process that segments the live stream into MP3 files
    and provides utilities to retrieve the latest N minutes of audio.
    """

    def __init__(self) -> None:
        self.buffer_dir: Path = settings.BUFFER_DIR
        self.segment_seconds: int = settings.SEGMENT_SECONDS
        self.buffer_minutes: int = settings.BUFFER_MINUTES
        self.cleanup_margin_minutes: int = settings.CLEANUP_MARGIN_MINUTES
        self.ffmpeg_path: str = settings.FFMPEG_PATH
        self.stream_url: str = settings.STREAM_URL
        self.audio_bitrate_bps: int = settings.AUDIO_BITRATE

        self._stop_event = threading.Event()
        self._ffmpeg_process: subprocess.Popen | None = None
        self._monitor_thread: threading.Thread | None = None
        self._cleaner_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(target=self._run_monitor, daemon=True)
        self._monitor_thread.start()
        # Start cleaner thread
        self._cleaner_thread = threading.Thread(target=self._run_cleaner, daemon=True)
        self._cleaner_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        proc = self._ffmpeg_process
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass

    # ---------------------- Internal threads ----------------------
    def _run_monitor(self) -> None:
        while not self._stop_event.is_set():
            self._launch_ffmpeg()
            # Wait until process exits or stop requested
            while not self._stop_event.is_set():
                proc = self._ffmpeg_process
                if proc is None:
                    break
                if proc.poll() is not None:
                    # exited; check for errors
                    returncode = proc.returncode
                    stderr_data = b""
                    if proc.stderr:
                        try:
                            stderr_data = proc.stderr.read()
                        except Exception:
                            pass
                    if returncode != 0:
                        error_msg = stderr_data.decode("utf-8", errors="ignore")[:500] if stderr_data else "Unknown error"
                        print(f"[FFMPEG] Process exited with code {returncode}: {error_msg}")
                    else:
                        print(f"[FFMPEG] Process exited normally (code 0)")
                    # break to relaunch
                    break
                time.sleep(1)
            # Give a moment before relaunch
            if not self._stop_event.is_set():
                print(f"[FFMPEG] Waiting 2 seconds before relaunch...")
                time.sleep(2)

    def _launch_ffmpeg(self) -> None:
        # Ensure directory exists
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        # Build FFmpeg command to segment into MP3 chunks
        # Using strftime to include timestamps in filenames
        output_pattern = str(self.buffer_dir / "seg_%Y%m%d_%H%M%S.mp3")
        cmd = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            # Reconnect options for live HTTP streams
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_at_eof",
            "1",
            "-reconnect_delay_max",
            "5",
            "-i",
            self.stream_url,
            # Normalize to mp3 for consistent segments
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            "-ar",
            "44100",
            # Segmenting config
            "-f",
            "segment",
            "-segment_time",
            str(self.segment_seconds),
            "-reset_timestamps",
            "1",
            "-strftime",
            "1",
            output_pattern,
        ]
        try:
            print(f"[FFMPEG] Starting segmenter: segment_time={self.segment_seconds}s, output_pattern={output_pattern}")
            self._ffmpeg_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,  # Capture stderr to check for errors
            )
            print(f"[FFMPEG] Process started (PID: {self._ffmpeg_process.pid})")
        except FileNotFoundError as exc:
            # FFmpeg not installed or path invalid; sleep to avoid tight loop
            print(f"[FFMPEG] FFmpeg not found: {exc}")
            time.sleep(5)
        except Exception as exc:
            print(f"[FFMPEG] Failed to start FFmpeg: {exc}")
            time.sleep(5)

    def _run_cleaner(self) -> None:
        """Periodically remove old segments beyond the rolling window."""
        while not self._stop_event.is_set():
            try:
                self._cleanup_old_segments()
            except Exception:
                pass
            # Clean every 30 seconds
            self._stop_event.wait(30)

    def _cleanup_old_segments(self) -> None:
        keep_minutes = self.buffer_minutes + self.cleanup_margin_minutes
        # Use file mtime instead of parsed timestamps to avoid timezone issues
        # Calculate cutoff as seconds since epoch
        now_ts = time.time()
        cutoff_ts = now_ts - (keep_minutes * 60)
        
        all_segments = list(self._iter_segment_files())
        deleted_count = 0
        kept_count = 0
        
        for path in all_segments:
            try:
                # Use file modification time (mtime) which is always accurate
                mtime_ts = path.stat().st_mtime
                if mtime_ts < cutoff_ts:
                    # File is older than cutoff, delete it
                    path.unlink(missing_ok=True)
                    deleted_count += 1
                else:
                    kept_count += 1
            except Exception as e:
                print(f"[CLEANER] Error processing {path}: {e}")
                pass
        
        if deleted_count > 0:
            print(f"[CLEANER] Deleted {deleted_count} old segments (kept {kept_count}, cutoff: {keep_minutes} minutes ago)")
        elif len(all_segments) > 0:
            print(f"[CLEANER] Checked {len(all_segments)} segments, all within retention window (kept {kept_count})")

    # ---------------------- Public helpers ----------------------
    def recent_segments_for_minutes(self, minutes: int) -> List[Path]:
        """Return oldest-first list of segments covering the requested duration.
        
        Simple approach: exclude in-progress segments, then take the most recent
        N segments that would cover the requested time window.
        """
        print(f"[BUFFER] recent_segments_for_minutes called with minutes={minutes}, segment_seconds={self.segment_seconds}")
        
        if minutes <= 0:
            print(f"[BUFFER] Invalid minutes: {minutes}, returning empty list")
            return []
        
        # Exclude segments modified in the last 2 seconds (likely still being written)
        # Use time.time() for consistency with file mtime (both are epoch timestamps)
        now_ts = time.time()
        cutoff_ts = now_ts - 2.0
        
        stable_segments: List[tuple[Path, datetime]] = []
        all_files = list(self._iter_segment_files())
        print(f"[BUFFER] Found {len(all_files)} total segment files")
        
        for path in all_files:
            try:
                # Check file size first - exclude empty files
                stat_info = path.stat()
                file_size = stat_info.st_size
                if file_size == 0:
                    print(f"[BUFFER] Skipping empty file: {path.name}")
                    continue
                
                # Check if file is stable (not recently modified)
                mtime_ts = stat_info.st_mtime
                age_seconds = now_ts - mtime_ts
                if mtime_ts >= cutoff_ts:
                    print(f"[BUFFER] Skipping in-progress file: {path.name} (age: {age_seconds:.2f}s, size: {file_size} bytes)")
                    continue  # Skip in-progress files
                
                # Try to get timestamp from filename (more accurate)
                ts = self._timestamp_from_name(path.name)
                if ts is None:
                    # Fallback to mtime
                    ts = datetime.utcfromtimestamp(mtime_ts)
                
                stable_segments.append((path, ts))
                print(f"[BUFFER] Added stable segment: {path.name} (age: {age_seconds:.2f}s, size: {file_size} bytes)")
            except Exception as e:
                print(f"[BUFFER] Error processing {path}: {e}")
                continue
        
        print(f"[BUFFER] Found {len(stable_segments)} stable segments")
        
        if not stable_segments:
            print(f"[BUFFER] No stable segments found, returning empty list")
            return []
        
        # Sort by timestamp, newest first
        stable_segments.sort(key=lambda x: x[1], reverse=True)
        
        # Calculate how many segments we need
        target_seconds = minutes * 60
        segments_needed_raw = target_seconds / self.segment_seconds
        # Add just 1 extra segment as a small safety margin (instead of 20% + 2)
        # This should get us very close to the requested duration
        segments_needed = int(segments_needed_raw) + 1
        
        print(f"[BUFFER] Target: {target_seconds}s, Segments needed: {segments_needed} (raw: {segments_needed_raw:.2f})")
        print(f"[BUFFER] Available stable segments: {len(stable_segments)}")
        
        # Take the most recent N segments, but don't exceed what's available
        segments_to_take = min(segments_needed, len(stable_segments))
        selected = stable_segments[:segments_to_take]
        
        if len(selected) < segments_needed:
            print(f"[BUFFER] WARNING: Only {len(selected)} segments available, but {segments_needed} were requested")
            print(f"[BUFFER] This will result in ~{len(selected) * self.segment_seconds}s of audio instead of {target_seconds}s")
        else:
            print(f"[BUFFER] Selected {len(selected)} segments (requested {segments_needed})")
        
        # Sort oldest-first for proper concatenation order
        selected.sort(key=lambda x: x[1])
        
        if selected:
            oldest = selected[0][1]
            newest = selected[-1][1]
            span_seconds = (newest - oldest).total_seconds()
            print(f"[BUFFER] Selected segment span: {span_seconds:.2f}s (from {oldest.strftime('%H:%M:%S')} to {newest.strftime('%H:%M:%S')})")
        
        return [path for path, _ in selected]

    # ---------------------- Utilities ----------------------
    def _iter_segment_files(self) -> Iterable[Path]:
        if not self.buffer_dir.exists():
            return []
        return [p for p in self.buffer_dir.glob("seg_*.mp3") if p.is_file()]

    @staticmethod
    def _timestamp_from_name(name: str) -> datetime | None:
        # Expect format: seg_%Y%m%d_%H%M%S.mp3
        try:
            stem = name.split(".")[0]
            _, ts = stem.split("_", 1)
            return datetime.strptime(ts, "%Y%m%d_%H%M%S")
        except Exception:
            return None

    def _duration_for_file(self, path: Path) -> float:
        """Estimate duration from file size and configured bitrate.

        This avoids reliance on ffprobe timing headers which can be unreliable
        for very short MP3 segments. Falls back to configured segment_seconds
        if size is unavailable or bitrate is not positive.
        """
        try:
            size_bytes = path.stat().st_size
            if size_bytes > 0 and self.audio_bitrate_bps > 0:
                return (size_bytes * 8.0) / float(self.audio_bitrate_bps)
        except Exception:
            pass
        return float(self.segment_seconds)


# Singleton manager instance used by the app
buffer_manager = FFmpegBufferManager()

