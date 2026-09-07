"""
ServerMetry Agent self-updater.

Resolves the latest available version from both the GitHub Releases API and
AGENT_VERSION on `main` (whichever is higher), stages every file from the
matching version tag in a temp directory, validates all Python files with
ast.parse, and only swaps the staged files into place if everything fetched
and validated cleanly. The previous tree is backed up first and restored
automatically if the swap fails.
"""

import ast
import json
import os
import re
import shutil
import ssl
import time
import urllib.error
import urllib.request

from models.constants import AGENT_VERSION
from models.limits import UPDATE_FETCH_TIMEOUT
from models.paths import resolve_install_dir
from utils.lock import FileLock, atomic_write
from utils.logging import log_write

GITHUB_REPO = "FloBaMedia/servermetry-client"
GITHUB_RELEASES_LATEST_URL = "https://api.github.com/repos/{}/releases/latest".format(GITHUB_REPO)
GITHUB_RAW_ROOT = "https://raw.githubusercontent.com/{}".format(GITHUB_REPO)
GITHUB_MAIN_AGENT_DIR = GITHUB_RAW_ROOT + "/main/agent"
GITHUB_MAIN_VERSION_URL = GITHUB_MAIN_AGENT_DIR + "/models/constants.py"
UPDATE_CHECK_INTERVAL = 3600

_USER_AGENT = "ServerMetryAgent/{}".format(AGENT_VERSION)

AGENT_FILE = "agent.py"
_MODULE_FILES = [
    "client/__init__.py",
    "client/api.py",
    "models/__init__.py",
    "models/constants.py",
    "models/limits.py",
    "models/paths.py",
    "services/__init__.py",
    "services/config_applier.py",
    "services/linux.py",
    "services/darwin.py",
    "services/windows.py",
    "services/updater.py",
    "services/path_migration.py",
    "utils/__init__.py",
    "utils/config.py",
    "utils/logging.py",
    "utils/validation.py",
    "utils/lock.py",
    "utils/snapshot.py",
]
_ALL_FILES = [AGENT_FILE] + _MODULE_FILES


def _installed_path():
    candidate = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "agent.py")
    )
    if os.path.isfile(candidate):
        return candidate
    resolved = os.path.join(resolve_install_dir(), "agent.py")
    if os.path.isfile(resolved):
        return resolved
    return candidate


def _parse_version(content):
    m = re.search(r'AGENT_VERSION\s*=\s*["\']([^"\']+)["\']', content)
    return m.group(1) if m else None


def _version_tuple(version_str):
    try:
        return tuple(int(x) for x in version_str.strip().split("."))
    except Exception:
        return (0,)


def _fetch_with_status(url, timeout=UPDATE_FETCH_TIMEOUT):
    """Fetch a URL, returning (status_code_or_None, body_str)."""
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        log_write("WARNING", "Auto-update: failed to fetch {}: {}".format(url, e))
        return None, ""


def _fetch(url, timeout=UPDATE_FETCH_TIMEOUT):
    status, content = _fetch_with_status(url, timeout=timeout)
    # HTTP 200 with an empty body is success (e.g. empty __init__.py package markers).
    if status == 200:
        return True, content
    if status is not None:
        log_write("WARNING", "Auto-update: HTTP {} fetching {}".format(status, url))
    return False, ""


def _fetch_file(rel_path, version, log_fn=None):
    """
    Fetch a single agent file for the given version tag, falling back to
    `main` if the tag doesn't have it (e.g. 404). Returns (ok, content, url).

    Empty files (HTTP 200, 0-byte body) are valid — package ``__init__.py``
    markers are often empty. Treat only non-200 as failure.
    """
    tag_url = "{}/v{}/agent/{}".format(GITHUB_RAW_ROOT, version, rel_path)
    status, content = _fetch_with_status(tag_url)
    if status == 200:
        return True, content, tag_url

    if log_fn:
        log_fn(
            "Auto-update: {} not found on tag v{} (HTTP {}), falling back to main".format(
                rel_path, version, status
            )
        )

    main_url = "{}/{}".format(GITHUB_MAIN_AGENT_DIR, rel_path)
    status2, content2 = _fetch_with_status(main_url)
    if status2 == 200:
        return True, content2, main_url

    log_write(
        "WARNING",
        "Auto-update: could not fetch {} from tag v{} or main (HTTP {} / {})".format(
            rel_path, version, status, status2
        ),
    )
    return False, "", None


def _state_path():
    return os.path.join(os.path.dirname(_installed_path()), ".update_check_ts")


def _lock_path():
    return os.path.join(os.path.dirname(_installed_path()), ".update.lock")


def _read_state():
    try:
        import json as _json
        with open(_state_path(), "r") as f:
            raw = f.read().strip()
        try:
            data = _json.loads(raw)
            if isinstance(data, dict):
                return data
            # json.loads succeeded but returned a number (legacy plain-float format)
            return {"ts": float(data), "remote_version": None}
        except Exception:
            return {"ts": float(raw), "remote_version": None}
    except Exception:
        return {"ts": 0.0, "remote_version": None}


def _read_last_check_ts():
    return _read_state().get("ts", 0.0)


def _read_last_remote_version():
    return _read_state().get("remote_version")


def _write_last_check_ts(remote_version=None):
    import json as _json
    try:
        dir_path = os.path.dirname(_state_path())
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path)
        existing = _read_state()
        data = {
            "ts": time.time(),
            "remote_version": remote_version if remote_version is not None else existing.get("remote_version"),
        }
        with open(_state_path(), "w") as f:
            _json.dump(data, f)
    except Exception as e:
        log_write("WARNING", "Auto-update: could not write state file: {}".format(e))


def _resolve_latest_version(log_fn=None):
    """
    Determine the latest available version by checking BOTH the GitHub
    Releases API (`tag_name`, e.g. "v1.4.1") and AGENT_VERSION in
    constants.py on `main`, then returning the higher semver. If one
    source fails, the other is used alone.
    Returns the version string (without leading "v") or None.
    """
    release_version = None
    main_version = None

    if log_fn:
        log_fn("Fetching latest release info from {}".format(GITHUB_RELEASES_LATEST_URL))

    status, content = _fetch_with_status(GITHUB_RELEASES_LATEST_URL)
    if status == 200 and content:
        try:
            data = json.loads(content)
            tag = (data.get("tag_name") or "").strip()
        except Exception:
            tag = ""
        version = tag[1:] if tag.startswith("v") else tag
        if version and re.match(r"^\d+(\.\d+)*$", version):
            release_version = version
        elif log_fn:
            log_fn("Auto-update: releases API returned no usable tag_name")
    elif log_fn:
        log_fn("Auto-update: releases API unavailable (HTTP {})".format(status))

    version_url = "{}?t={}".format(GITHUB_MAIN_VERSION_URL, int(time.time()))
    if log_fn:
        log_fn("Auto-update: fetching version from {}".format(version_url))
    ok, version_content = _fetch(version_url)
    if ok and version_content:
        main_version = _parse_version(version_content)
    elif log_fn:
        log_fn("Auto-update: could not fetch version from main")

    if release_version and main_version:
        if _version_tuple(main_version) > _version_tuple(release_version):
            if log_fn:
                log_fn(
                    "Auto-update: main (v{}) is newer than release (v{})".format(
                        main_version, release_version
                    )
                )
            return main_version
        if log_fn:
            log_fn(
                "Auto-update: using release v{} (main is v{})".format(
                    release_version, main_version
                )
            )
        return release_version

    return release_version or main_version


def update_status(auto_updates_enabled=None):
    """Print auto-update schedule status. No network requests.

    "Latest" is the version remembered from the last successful check
    (``.update_check_ts``), not a live GitHub lookup — use ``--check-update``
    for that. When the check is overdue we never claim "up to date" from stale
    cache, which previously contradicted a live ``--check-update``.
    """
    import datetime

    last_ts = _read_last_check_ts()
    last_remote = _read_last_remote_version()
    now = time.time()
    overdue = last_ts > 0.0 and (now - last_ts) >= UPDATE_CHECK_INTERVAL

    print("Auto-Update Status")
    print("  Version         : v{}".format(AGENT_VERSION))
    if not last_remote:
        print("  Latest          : unknown (not yet checked)")
    elif _version_tuple(last_remote) > _version_tuple(AGENT_VERSION):
        print("  Latest          : v{} (update available)".format(last_remote))
    elif overdue:
        # Cache says equal/older but the check itself is stale — do not claim
        # "up to date" (live GitHub may already be newer; see --check-update).
        print(
            "  Latest          : v{} (from last check; stale — run --check-update)".format(
                last_remote
            )
        )
    else:
        print("  Latest          : v{} (up to date)".format(last_remote))

    if auto_updates_enabled is not None:
        print("  Auto-updates    : {}".format("enabled" if auto_updates_enabled else "disabled"))

    if auto_updates_enabled is False:
        if last_ts == 0.0:
            print("  Last check      : never")
        else:
            last_dt = datetime.datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M:%S")
            print("  Last check      : {}".format(last_dt))
        print("  Next check      : will not run until auto-updates are enabled")
    elif last_ts == 0.0:
        print("  Last check      : never")
        print("  Next check      : on next metric report")
    else:
        elapsed = now - last_ts
        remaining = max(0.0, UPDATE_CHECK_INTERVAL - elapsed)
        last_dt = datetime.datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M:%S")
        if elapsed < 60:
            elapsed_str = "{:.0f}s ago".format(elapsed)
        elif elapsed < 3600:
            elapsed_str = "{:.0f} min ago".format(elapsed / 60)
        else:
            elapsed_str = "{:.1f}h ago".format(elapsed / 3600)

        if remaining == 0:
            next_str = "overdue (will run on next metric report)"
        elif remaining < 60:
            next_str = "in {:.0f}s".format(remaining)
        elif remaining < 3600:
            next_str = "in {:.0f} min".format(remaining / 60)
        else:
            next_str = "in {:.1f}h".format(remaining / 3600)

        print("  Last check      : {} ({})".format(last_dt, elapsed_str))
        print("  Next check      : {}".format(next_str))

    print("  Check interval  : {}h".format(UPDATE_CHECK_INTERVAL // 3600))
    print("")
    print("  --check-update   check latest version without updating")
    print("  --update         force an immediate update now")


def check_version(log_debug_fn=None):
    """Fetch remote version and print comparison. No writes, no update."""
    print("Checking for updates...")

    remote_version = _resolve_latest_version(log_fn=log_debug_fn)
    if not remote_version:
        print("ERROR: Could not reach GitHub or resolve the latest version.")
        return False

    print("  Installed : v{}".format(AGENT_VERSION))
    print("  Available : v{}".format(remote_version))

    if _version_tuple(remote_version) <= _version_tuple(AGENT_VERSION):
        print("  Status    : Up to date")
    else:
        print("  Status    : Update available (run with --update to apply)")

    return True


def _restore_backup(backup_dir, install_dir):
    """Best-effort restore of every file that was backed up before the swap."""
    try:
        for root, _dirs, files in os.walk(backup_dir):
            for name in files:
                src = os.path.join(root, name)
                rel = os.path.relpath(src, backup_dir)
                dest = os.path.join(install_dir, rel)
                dest_dir = os.path.dirname(dest)
                if dest_dir and not os.path.isdir(dest_dir):
                    os.makedirs(dest_dir)
                shutil.copy2(src, dest)
        log_write("INFO", "Auto-update: restored previous version from backup after failed swap")
    except Exception as e:
        log_write("ERROR", "Auto-update: failed to restore backup: {}".format(e))


def _stage_and_apply_update(remote_version, log_debug_fn=None):
    """
    Fetch + validate every agent file into a staging directory. Only if ALL
    files fetch and validate cleanly do we back up the current tree and swap
    the staged files in. Returns "updated" or "skipped" (no partial update).
    """
    target_path = _installed_path()
    install_dir = os.path.dirname(target_path)
    staging_dir = os.path.join(install_dir, ".update_staging")
    backup_dir = os.path.join(install_dir, ".update_backup")

    if os.path.isdir(staging_dir):
        shutil.rmtree(staging_dir, ignore_errors=True)
    os.makedirs(staging_dir)

    try:
        staged_content = {}

        for rel_path in _ALL_FILES:
            ok, content, source_url = _fetch_file(rel_path, remote_version, log_fn=log_debug_fn)
            if not ok:
                log_write(
                    "ERROR",
                    "Auto-update: failed to fetch {} for v{} – aborting update (no partial update)".format(
                        rel_path, remote_version
                    ),
                )
                return "skipped"

            if rel_path.endswith(".py"):
                try:
                    ast.parse(content)
                except SyntaxError as e:
                    log_write(
                        "ERROR",
                        "Auto-update: {} (from {}) has a syntax error – aborting update: {}".format(
                            rel_path, source_url, e
                        ),
                    )
                    return "skipped"

            staged_dest = os.path.join(staging_dir, rel_path.replace("/", os.sep))
            staged_dest_dir = os.path.dirname(staged_dest)
            if staged_dest_dir and not os.path.isdir(staged_dest_dir):
                os.makedirs(staged_dest_dir)
            with open(staged_dest, "w", encoding="utf-8") as f:
                f.write(content)
            staged_content[rel_path] = content
            if log_debug_fn:
                log_debug_fn("Auto-update: staged {} from {}".format(rel_path, source_url))

        # Every file fetched + validated successfully — back up the current
        # tree before touching anything, then swap in the staged files.
        if os.path.isdir(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)
        os.makedirs(backup_dir)

        for rel_path in _ALL_FILES:
            current_path = os.path.join(install_dir, rel_path.replace("/", os.sep))
            if os.path.isfile(current_path):
                backup_dest = os.path.join(backup_dir, rel_path.replace("/", os.sep))
                backup_dest_dir = os.path.dirname(backup_dest)
                if backup_dest_dir and not os.path.isdir(backup_dest_dir):
                    os.makedirs(backup_dest_dir)
                shutil.copy2(current_path, backup_dest)

        try:
            for rel_path in _ALL_FILES:
                dest = os.path.join(install_dir, rel_path.replace("/", os.sep))
                dest_dir = os.path.dirname(dest)
                if dest_dir and not os.path.isdir(dest_dir):
                    os.makedirs(dest_dir)
                atomic_write(dest, staged_content[rel_path], encoding="utf-8")

            log_write(
                "INFO",
                "Auto-update: successfully updated to v{} (backup: {})".format(remote_version, backup_dir),
            )
            return "updated"

        except PermissionError:
            log_write(
                "WARNING",
                "Auto-update: no write permission for {} – run agent as root/admin".format(install_dir),
            )
            _restore_backup(backup_dir, install_dir)
            return "skipped"
        except Exception as e:
            log_write("ERROR", "Auto-update: failed to apply staged update: {} – restoring backup".format(e))
            _restore_backup(backup_dir, install_dir)
            return "skipped"
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def check_and_update(log_debug_fn=None, force=False):
    try:
        from models.paths import in_container
    except ImportError:
        in_container = lambda: False  # noqa: E731
    if in_container():
        if log_debug_fn:
            log_debug_fn("Auto-update: skipped in container mode (rebuild the image)")
        log_write("INFO", "Auto-update: skipped in container mode")
        return "skipped"

    elapsed = time.time() - _read_last_check_ts()
    if not force and elapsed < UPDATE_CHECK_INTERVAL:
        if log_debug_fn:
            log_debug_fn(
                "Auto-update: skipping check ({:.0f}s / {}s since last check)".format(
                    elapsed, UPDATE_CHECK_INTERVAL
                )
            )
        return "skipped"

    lock = FileLock(_lock_path(), timeout=60)
    if not lock.acquire(blocking=False):
        if log_debug_fn:
            log_debug_fn("Auto-update: already running, skipping")
        return "skipped"

    try:
        remote_version = _resolve_latest_version(log_fn=log_debug_fn)
        if not remote_version:
            log_write("WARNING", "Auto-update: could not resolve latest version – skipping")
            # Still advance the interval so a failing resolve cannot hammer
            # GitHub on every metric report (unauthenticated rate limit: 60/h).
            _write_last_check_ts()
            return "skipped"

        if log_debug_fn:
            log_debug_fn(
                "Auto-update: local={}, remote={}".format(AGENT_VERSION, remote_version)
            )

        if _version_tuple(remote_version) <= _version_tuple(AGENT_VERSION):
            log_write("INFO", "Auto-update: already up to date (v{})".format(AGENT_VERSION))
            _write_last_check_ts(remote_version=remote_version)
            return "up_to_date"

        log_write(
            "INFO",
            "Auto-update: new version available ({} → {}), staging…".format(
                AGENT_VERSION, remote_version
            ),
        )

        result = _stage_and_apply_update(remote_version, log_debug_fn=log_debug_fn)

        # Always record that we attempted this remote version (success or not).
        # That keeps --update-status honest ("update available") and enforces the
        # 1h interval so failed applies/network blips do not retry every minute.
        _write_last_check_ts(remote_version=remote_version)

        if result == "updated":
            try:
                from services.path_migration import migrate_install_paths
                migrate_install_paths()
            except Exception as e:
                log_write("WARNING", "Auto-update: path migration skipped: {}".format(e))

        return result
    finally:
        lock.release()
