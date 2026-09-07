"""Logging utilities for ServerMetry Agent."""

import os
import platform
import sys
from datetime import datetime

from models.paths import (
    LEGACY_LINUX_LOG_PATH,
    LINUX_LOG_PATH,
    legacy_windows_log_path,
    resolve_install_dir,
    windows_log_path,
)

_LOG_TO_STDERR = False


def _log_path():
    env_path = os.environ.get("SERVERMETRY_LOG_PATH", "").strip()
    if env_path:
        return env_path
    if platform.system() == "Windows":
        if os.path.isfile(windows_log_path()):
            return windows_log_path()
        if os.path.isfile(legacy_windows_log_path()):
            return legacy_windows_log_path()
        return windows_log_path()
    if os.path.isfile(LINUX_LOG_PATH):
        return LINUX_LOG_PATH
    if os.path.isfile(LEGACY_LINUX_LOG_PATH):
        return LEGACY_LINUX_LOG_PATH
    return LINUX_LOG_PATH


def _ensure_log_dir(path):
    log_dir = os.path.dirname(path)
    if log_dir and not os.path.isdir(log_dir):
        try:
            os.makedirs(log_dir, exist_ok=True)
        except OSError:
            pass


def log_write(level, message):
    """Write a log line to the agent log file and optionally stderr."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[{}] [{}] {}".format(ts, level, message)
    path = _log_path()
    _ensure_log_dir(path)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if _LOG_TO_STDERR:
        print(line, file=sys.stderr)


def log_debug(message, debug_flag=False):
    """Write a debug log line when debug mode is active."""
    global _LOG_TO_STDERR
    if debug_flag:
        _LOG_TO_STDERR = True
        log_write("DEBUG", message)
    elif os.environ.get("SERVERMETRY_DEBUG", "").strip().lower() in ("1", "true", "yes") or os.environ.get(
        "SERVERPULSE_DEBUG", ""
    ).strip().lower() in ("1", "true", "yes"):
        log_write("DEBUG", message)


def set_stderr_logging(enabled):
    global _LOG_TO_STDERR
    _LOG_TO_STDERR = enabled
