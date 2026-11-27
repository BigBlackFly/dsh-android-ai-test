"""Per-session Android screen recording via scrcpy on the host computer."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# 10 minutes. Every rotation means stopping one scrcpy and starting the next;
# the device-side scrcpy-server is sometimes not fully released yet, and the
# new instance then hits an access violation (0xC0000005) and the segment is
# lost. Longer segments mean most sessions never rotate at all. Measured
# bitrate is ~0.9 MB/min, so a 10-minute part is only ~9 MB.
ROTATE_AFTER_SECONDS = 600
# An MP4 with only the ftyp/mdat header is ~48 bytes; anything above this
# means the muxer has written real data.
HEADER_ONLY_BYTES = 1024
BIT_RATE = "2M"
MAX_SIZE = 1280
_JSON_LOCKS: dict[str, threading.Lock] = {}
_JSON_LOCKS_GUARD = threading.Lock()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve()).casefold()
    with _JSON_LOCKS_GUARD:
        lock = _JSON_LOCKS.setdefault(key, threading.Lock())
    with lock:
        # Unique temp names avoid worker/stop races; retry transient Windows locks.
        temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        payload = json.dumps(value, ensure_ascii=False, indent=2)
        for attempt in range(8):
            try:
                temp.write_text(payload, encoding="utf-8")
                os.replace(temp, path)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.025 * (attempt + 1))
            finally:
                # 成功路径已 os.replace 走了；走到这里说明这次没成，
                # 留着半截文件只会让下次 reconcile 把它当成品。
                temp.unlink(missing_ok=True)


def _ffprobe_path() -> str | None:
    found = shutil.which("ffprobe")
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages"
    matches = sorted(local.glob("Gyan.FFmpeg_*/*/bin/ffprobe.exe"), reverse=True) if local.is_dir() else []
    return str(matches[0]) if matches else None


def video_file_info(path: Path) -> dict:
    """Validate a finalized video with ffprobe; size is only a quick reject."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size <= 1024:
        return {"valid": False, "reason": "文件不存在或过小"}
    ffprobe = _ffprobe_path()
    if not ffprobe:
        return {"valid": False, "reason": "未找到 ffprobe，无法校验视频流"}
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height",
             "-of", "json", str(path)], capture_output=True, text=True, timeout=12,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
        )
        if result.returncode != 0:
            return {"valid": False, "reason": result.stderr.strip()[-500:] or "ffprobe 校验失败"}
        streams = json.loads(result.stdout).get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"
                      and int(s.get("width") or 0) > 0 and int(s.get("height") or 0) > 0), None)
        return ({"valid": True, "codec": video.get("codec_name"),
                 "width": video.get("width"), "height": video.get("height")}
                if video else {"valid": False, "reason": "文件中没有有效视频流"})
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        return {"valid": False, "reason": f"ffprobe 异常：{exc}"}


def _validated_segments(video_dir: Path, manifest: dict) -> list[dict]:
    valid = []
    for segment in manifest.get("segments") or []:
        name = str(segment.get("file") or "")
        if not re.fullmatch(r"part-\d+\.mp4", name):
            continue
        fp = (video_dir / name).resolve()
        if fp.parent != video_dir.resolve():
            continue
        info = video_file_info(fp)
        if info.get("valid"):
            valid.append({**segment, **{k: info[k] for k in ("codec", "width", "height") if k in info}})
    return valid


def recording_status(video_dir: Path) -> dict:
    try:
        value = json.loads((video_dir / "recording.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def reconcile_recording(video_dir: Path) -> dict:
    """Repair stale recording metadata only after confirming worker is gone."""
    video_dir = Path(video_dir).resolve()
    status = recording_status(video_dir)
    if status.get("state") not in ("recording", "partial"):
        return status
    pid = status.get("worker_pid")
    if not pid:
        return status
    try:
        alive = _pid_alive(int(pid))
    except (TypeError, ValueError):
        alive = False
    if alive:
        return status
    _finalize_orphaned_worker(video_dir, status)
    return recording_status(video_dir)


def _find_scrcpy() -> str | None:
    found = shutil.which("scrcpy")
    if found:
        return found
    # Workbuddy/IDE-launched processes may not inherit the user's WinGet PATH.
    roots = [Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages"]
    for root in roots:
        if root.is_dir():
            matches = sorted(root.glob("Genymobile.scrcpy_*/*/scrcpy.exe"), reverse=True)
            if matches:
                return str(matches[0])
    return None


def start_video_recording(serial: str | None, record_dir: Path,
                          screen_size: tuple[int, int] | None = None) -> dict:
    """Start a detached host worker; recording failures do not block tests."""
    if not serial:
        return {"started": False, "reason": "设备 serial 不可用"}
    scrcpy = _find_scrcpy()
    if not scrcpy:
        return {"started": False, "reason": "未找到 scrcpy；请将 scrcpy 加入 PATH 或安装 WinGet 版本"}
    record_dir = Path(record_dir).resolve()
    video_dir = record_dir / "video"
    # 先看一眼有没有正在跑的：此刻还不用建目录，否则被拒绝的 start
    # 会在 run_history 里留下一个空 video/，看上去像"录过但没录到东西"。
    if video_dir.is_dir() and recording_status(video_dir).get("state") == "recording":
        return {"started": True, "already_running": True, "note": "本会话录屏已在运行"}
    video_dir.mkdir(parents=True, exist_ok=True)
    (video_dir / "stop.flag").unlink(missing_ok=True)
    cmd = [sys.executable, str(Path(__file__).resolve()), "--worker",
           "--serial", serial, "--video-dir", str(video_dir), "--scrcpy", scrcpy]
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        with (video_dir / "worker.log").open("ab") as log:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    creationflags=flags,
                                    start_new_session=(os.name != "nt"))
    except OSError as exc:
        return {"started": False, "reason": f"录屏进程启动失败：{exc}"}
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        status = recording_status(video_dir)
        if status.get("state") == "recording":
            return {"started": True, "serial": serial, "bit_rate": BIT_RATE,
                    "max_size": MAX_SIZE, "pid": proc.pid, "method": "scrcpy"}
        if status.get("state") == "error":
            return {"started": False, "reason": status.get("error") or "scrcpy 启动失败"}
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    return {"started": False,
            "reason": recording_status(video_dir).get("error") or "等待 scrcpy 录屏启动超时",
            "pid": proc.pid}


def _stop_result(video_dir: Path, status: dict) -> dict:
    """Summarise a finished recording.

    `stopped` means "the recording was wrapped up and its segments are usable",
    NOT "every frame was captured". A `partial` run (scrcpy died mid-way) still
    has playable segments, so it must not be reported as a failure -- that is
    what used to make healthy recordings look broken. `complete` carries the
    distinction; `reason` only appears when something actually went wrong.
    """
    manifest = _read_manifest(video_dir)
    state = status.get("state")
    segments = len(_validated_segments(video_dir, manifest))
    stopped = state in ("complete", "partial") and segments > 0
    result = {
        "stopped": stopped,
        "complete": state == "complete",
        "state": state,
        "segments": segments,
        "index_file": str(video_dir / "index.json"),
    }
    if state == "partial":
        result["note"] = "录制中途中断，已保留可用片段（不代表录制完整）"
    # ★ 「失败」必须有 reason，且不能自相矛盾：
    #   旧写法在 state=complete、segments=0 时给出 stopped=false + complete=true 且无 reason，
    #   调用方拿到两个对立字段却没有任何解释。
    if not stopped:
        error = status.get("error") or "未找到有效视频片段"
        if state == "complete":
            error = f"录制已收尾，但没有可用的视频片段（segments=0）"
        result["reason"] = error
    elif status.get("error"):
        result["error"] = status["error"]
    return result


def stop_video_recording(record_dir: Path, timeout: float = 45) -> dict:
    video_dir = Path(record_dir).resolve() / "video"
    status = reconcile_recording(video_dir)
    if not status:
        return {"stopped": False, "complete": False, "reason": "没有录屏记录"}
    if status.get("state") in ("complete", "error", "partial"):
        return _stop_result(video_dir, status)
    (video_dir / "stop.flag").touch()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = recording_status(video_dir)
        if status.get("state") in ("complete", "error", "partial"):
            return _stop_result(video_dir, status)
        if status.get("worker_pid") and not _pid_alive(int(status["worker_pid"])):
            break
        time.sleep(0.25)
    _finalize_orphaned_worker(video_dir)
    status = recording_status(video_dir)
    if status.get("state") in ("complete", "error", "partial"):
        return _stop_result(video_dir, status)
    return {"stopped": False, "complete": False,
            "reason": "录屏收尾超时；保留已落盘片段"}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _finalize_orphaned_worker(video_dir: Path, status: dict | None = None) -> None:
    """Recover/index a finalized scrcpy file if its worker was reaped by Windows."""
    status = status or recording_status(video_dir)
    manifest = _read_manifest(video_dir) or {
        "version": 2, "method": "scrcpy",
        "serial": status.get("serial"),
        "max_size": MAX_SIZE, "bit_rate": BIT_RATE, "segments": [],
    }
    known = {str(item.get("file")) for item in manifest.get("segments", [])}
    for fp in sorted(video_dir.glob("part-*.mp4")):
        if fp.name not in known and video_file_info(fp).get("valid"):
            manifest.setdefault("segments", []).append({
                "file": fp.name, "started_at": _now(),
                "start_requested_at": _now(), "ended_at": _now(),
                "size_bytes": fp.stat().st_size,
            })
    manifest["segments"] = _validated_segments(video_dir, manifest)
    manifest["errors"] = list(manifest.get("errors") or [])
    _write_json(video_dir / "index.json", manifest)
    if manifest.get("segments"):
        _write_json(video_dir / "recording.json", {
            "state": "partial", "method": "scrcpy",
            "serial": manifest.get("serial"), "finished_at": _now(),
            "segments": len(manifest["segments"]),
            "error": status.get("error") or "worker 意外退出；已恢复可播放片段",
            "note": "异常退出后恢复片段；不代表录制完整",
        })
    else:
        _write_json(video_dir / "recording.json", {
            "state": "error", "method": "scrcpy", "serial": manifest.get("serial"),
            "finished_at": _now(), "error": status.get("error") or "worker 已退出，未找到有效视频",
            "segments": 0,
        })


def _read_manifest(video_dir: Path) -> dict:
    try:
        data = json.loads((video_dir / "index.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _launch_segment(scrcpy: str, serial: str, current_file: Path, video_dir: Path,
                    stop_flag: Path) -> subprocess.Popen:
    """Start scrcpy for one segment and wait until it is actually writing.

    scrcpy occasionally dies during start-up on Windows with an access
    violation (0xC0000005) before a single frame is written. That is transient,
    so one retry is attempted before giving up.
    """
    attempts = 2
    last_error = "scrcpy 未能开始录制"
    for attempt in range(1, attempts + 1):
        # 录到文件就够，不要 scrcpy 的实时窗口：它会浮在桌面上挡住被测界面、
        # 白占一份 adb 视频通道，而且取证截图里一旦把它拍进去就是噪声。
        # 排查时用 worker.log，不看那个窗口。
        cmd = [scrcpy, "--serial", serial, "--no-window", "--record", str(current_file),
               "--record-format", "mp4", "--video-bit-rate", BIT_RATE,
               "--max-size", str(MAX_SIZE), "--capture-orientation=@"]
        log_path = video_dir / "worker.log"
        with log_path.open("ab") as log:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=log,
                                    start_new_session=(os.name != "nt"))
        deadline = time.monotonic() + 20
        started = False
        # Same criterion the code used before the retry was added: the record
        # file appearing while scrcpy is still alive. Requiring a minimum size
        # here made healthy starts look like failures (the device can take a
        # while to emit the first frame on a still screen).
        while time.monotonic() < deadline and not stop_flag.exists():
            if current_file.exists() and proc.poll() is None:
                started = True
                break
            if proc.poll() is not None:
                last_error = f"scrcpy 提前退出，exit_code={proc.returncode}"
                break
            time.sleep(0.2)
        if started:
            if attempt > 1:
                _write_json(video_dir / "startup_retry.json",
                            {"attempt": attempt, "recovered": True, "first_error": last_error})
            return proc
        # Clean up the failed attempt before retrying.
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        # Remove whatever the failed attempt left (48-byte header or empty).
        if current_file.exists():
            current_file.unlink(missing_ok=True)
        if stop_flag.exists():
            break
        if attempt < attempts:
            _write_json(video_dir / "startup_retry.json",
                        {"attempt": attempt + 1, "recovered": False, "first_error": last_error})
            time.sleep(1.0)
    raise RuntimeError(last_error + f"（已重试 {attempts} 次）")


def _run_worker(serial: str, video_dir: Path, scrcpy: str) -> int:
    video_dir = video_dir.resolve()
    video_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"version": 2, "method": "scrcpy", "serial": serial,
                "max_size": MAX_SIZE, "bit_rate": BIT_RATE, "segments": []}
    proc: subprocess.Popen | None = None
    current_file: Path | None = None
    segment_started: str | None = None
    segment_no = 0
    try:
        while True:
            # Re-check right before spawning: the stop flag can land while we are
            # tearing down the previous scrcpy, and launching a new segment then
            # would immediately be killed -> a 0-second hollow part-N file.
            if (video_dir / "stop.flag").exists():
                break
            segment_no += 1
            ext = "mp4"
            filename = f"part-{segment_no:03d}.{ext}"
            current_file = video_dir / filename
            segment_started = _now()
            try:
                proc = _launch_segment(scrcpy, serial, current_file, video_dir,
                                       video_dir / "stop.flag")
            except RuntimeError:
                # Stop requested while starting -- not an error.
                if (video_dir / "stop.flag").exists():
                    current_file = None
                    break
                raise
            _write_json(video_dir / "recording.json", {
                "state": "recording", "method": "scrcpy", "serial": serial,
                "worker_pid": os.getpid(), "scrcpy_pid": proc.pid,
                "segment": segment_no, "started_at": segment_started,
                "max_size": MAX_SIZE, "bit_rate": BIT_RATE,
            })
            rotate_at = time.monotonic() + ROTATE_AFTER_SECONDS
            self_exit: int | None = None
            while time.monotonic() < rotate_at and not (video_dir / "stop.flag").exists():
                code = proc.poll()
                if code is not None:
                    # Remember why it ended; whether that is an error depends on
                    # whether WE were stopping it (see below), not on the code.
                    self_exit = code
                    break
                time.sleep(0.25)
            if self_exit is not None and not (video_dir / "stop.flag").exists():
                # Died on its own before the rotation deadline -- real interruption.
                raise RuntimeError(f"scrcpy 自行退出，exit_code={self_exit}")
            # We stopped it on purpose (rotation or stop flag); scrcpy returns a
            # non-zero code after the device server is signalled -- that is normal.
            _stop_scrcpy_process(proc, serial)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            proc = None
            info = video_file_info(current_file)
            if info.get("valid"):
                manifest["segments"].append({
                    "file": filename, "started_at": segment_started,
                    "start_requested_at": segment_started, "ended_at": _now(),
                    "size_bytes": current_file.stat().st_size,
                    **{k: info[k] for k in ("codec", "width", "height") if k in info},
                })
                _write_json(video_dir / "index.json", manifest)
            else:
                manifest.setdefault("errors", []).append(
                    f"{filename} 视频校验失败：{info.get('reason')}")
                # Keep the manifest and on-disk part list consistent; invalid
                # startup/stop remnants are not replayable evidence.
                current_file.unlink(missing_ok=True)
            current_file = None
            if (video_dir / "stop.flag").exists():
                break

        manifest["segments"] = _validated_segments(video_dir, manifest)
        if not manifest["segments"]:
            raise RuntimeError("录屏结束但没有保存有效视频片段")
        _write_json(video_dir / "index.json", manifest)
        _write_json(video_dir / "recording.json", {
            "state": "complete", "method": "scrcpy", "serial": serial,
            "finished_at": _now(), "segments": len(manifest["segments"]),
        })
        return 0
    except Exception as exc:
        if proc and proc.poll() is None:
            _stop_scrcpy_process(proc, serial)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        # The recorder may have finalized a usable MP4 before returning a
        # non-zero status. Trust ffprobe-valid output, keep the error detail.
        info = video_file_info(current_file) if current_file else {}
        if info.get("valid"):
            existing = {x.get("file") for x in manifest.get("segments", [])}
            if current_file.name not in existing:
                manifest["segments"].append({
                    "file": current_file.name, "started_at": segment_started or _now(),
                    "start_requested_at": segment_started or _now(), "ended_at": _now(),
                    "size_bytes": current_file.stat().st_size,
                    **{k: info[k] for k in ("codec", "width", "height") if k in info},
                })
        elif current_file:
            # Exceptional shutdown can also leave an empty or corrupt part.
            current_file.unlink(missing_ok=True)
        manifest["segments"] = _validated_segments(video_dir, manifest)
        _write_json(video_dir / "index.json", manifest)
        _write_json(video_dir / "recording.json", {
            "state": "partial" if manifest["segments"] else "error",
            "method": "scrcpy", "serial": serial,
            "finished_at": _now(), "error": str(exc),
            "segments": len(manifest["segments"]),
        })
        return 1


def _stop_scrcpy_process(proc: subprocess.Popen, serial: str | None = None) -> None:
    """Stop scrcpy so its muxer flushes the MP4/MKV trailer.

    Frames only reach the host once scrcpy finalises the file, so a plain
    terminate()/kill() loses the whole recording (the MP4 stays at its 48-byte
    header). On Windows the reliable way to make scrcpy finalise is to stop the
    on-device server: scrcpy then prints "Recording complete" and exits.
    CTRL_BREAK to the host process does NOT work -- scrcpy is a GUI subsystem
    binary with no console control handler, so the event is ignored.
    """
    if proc.poll() is not None:
        return
    interrupted = False
    if serial:
        interrupted = _interrupt_device_server(serial)
    if not interrupted:
        # Fallback for non-USB setups / older behaviour.
        try:
            if os.name == "nt":
                attached = ctypes.windll.kernel32.AttachConsole(proc.pid)
                if not attached:
                    raise OSError(ctypes.get_last_error(), "AttachConsole failed")
                try:
                    ctypes.windll.kernel32.SetConsoleCtrlHandler(None, True)
                    if not ctypes.windll.kernel32.GenerateConsoleCtrlEvent(signal.CTRL_BREAK_EVENT, 0):
                        raise OSError(ctypes.get_last_error(), "GenerateConsoleCtrlEvent failed")
                finally:
                    ctypes.windll.kernel32.FreeConsole()
                    ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
            else:
                proc.send_signal(signal.SIGINT)
        except (OSError, AttributeError):
            pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def _interrupt_device_server(serial: str) -> bool:
    """SIGINT the scrcpy-server on the device so the host muxer finalises."""
    try:
        result = subprocess.run(
            ["adb", "-s", serial, "shell", "pkill -SIGINT -f com.genymobile.scrcpy.Server"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                       if os.name == "nt" else 0),
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--serial")
    parser.add_argument("--video-dir", type=Path)
    parser.add_argument("--scrcpy")
    args = parser.parse_args()
    if args.worker:
        if not args.serial or not args.video_dir or not args.scrcpy:
            raise SystemExit("worker needs --serial, --video-dir and --scrcpy")
        raise SystemExit(_run_worker(args.serial, args.video_dir, args.scrcpy))


if __name__ == "__main__":
    main()
