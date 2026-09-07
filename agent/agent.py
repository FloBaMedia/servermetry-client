#!/usr/bin/env python3
"""ServerMetry Agent — run with -h or --help for usage."""

import json
import os
import platform
import signal
import socket
import subprocess
import sys
import time


def _bootstrap():
    """Download any missing module files from GitHub before imports run."""
    import ssl
    import urllib.request

    _BOOTSTRAP_BASE = "https://raw.githubusercontent.com/FloBaMedia/servermetry-client/main/agent"
    _BOOTSTRAP_FILES = [
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

    script_dir = os.path.dirname(os.path.abspath(__file__))
    missing = [
        f for f in _BOOTSTRAP_FILES
        if not os.path.isfile(os.path.join(script_dir, f.replace("/", os.sep)))
    ]
    if not missing:
        return

    print("Bootstrap: downloading {} missing module file(s)...".format(len(missing)))
    ctx = ssl.create_default_context()
    failed = []
    for rel_path in missing:
        url = _BOOTSTRAP_BASE + "/" + rel_path
        dest = os.path.join(script_dir, rel_path.replace("/", os.sep))
        dest_dir = os.path.dirname(dest)
        if dest_dir and not os.path.isdir(dest_dir):
            os.makedirs(dest_dir)
        try:
            with urllib.request.urlopen(url, timeout=15, context=ctx) as resp:
                content = resp.read()
            with open(dest, "wb") as f:
                f.write(content)
            print("  + {}".format(rel_path))
        except Exception as e:
            print("  ERROR: could not download {}: {}".format(rel_path, e))
            failed.append(rel_path)

    if failed:
        print("Bootstrap failed for: {}".format(", ".join(failed)))
        print("Please check your internet connection or install manually.")
        sys.exit(1)

    print("Bootstrap complete.\n")


_bootstrap()

from models.constants import AGENT_VERSION, DEFAULT_API_URL
from models.limits import SCRIPT_EXEC_TIMEOUT, STATE_ENCODING
from utils.config import ensure_config, load_config
from models.paths import in_container
from utils.lock import FileLock, atomic_write
from utils.logging import log_debug, log_write, set_stderr_logging

_CONFIG_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".config_state")
_CONFIG_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".config_state.lock")


def _load_config_state():
    """Return (configChangedAt, config, services, loaded).

    ``loaded`` is True when a state file was read successfully — even if the
    cached config dict is empty or configChangedAt is null. Callers must use
    that flag for bootstrap, not truthiness of ``config``.
    """
    try:
        with open(_CONFIG_STATE_FILE, "r", encoding=STATE_ENCODING) as f:
            data = json.load(f)
        config = data.get("config") or {}
        if not isinstance(config, dict):
            config = {}
        services = data.get("services") or []
        if not isinstance(services, list):
            services = []
        return data.get("configChangedAt"), config, services, True
    except Exception:
        return None, {}, [], False


def _save_config_state(config_changed_at, config_dict, services=None):
    try:
        atomic_write(_CONFIG_STATE_FILE, json.dumps({
            "configChangedAt": config_changed_at,
            "config": config_dict,
            "services": services or [],
        }), encoding=STATE_ENCODING)
    except Exception as e:
        log_write("WARNING", "config_state: could not write state file: {}".format(e))


def _check_service_port(port, protocol=None, timeout=3):
    """TCP connect check. Returns ("up", None) or ("down", error_string)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return "up", None
    except socket.timeout:
        return "down", "Connection timed out"
    except ConnectionRefusedError:
        return "down", "Connection refused"
    except OSError as e:
        return "down", str(e)


HELP_TEXT = """\
ServerMetry Agent {version}

Usage: python3 agent.py [OPTIONS]

Options:
  (none)                          Send metrics once and exit
  --info                          Show agent version and configuration info
  --check                         Collect and display metrics locally, no upload
  --dry-run                       Collect metrics, print JSON — no HTTP request
  --check-update                  Check if a newer agent version is available
  --update                        Download and apply the latest agent version now
  --update-status                 Show auto-update schedule (last check, next check)
  --config-status                 Show local config + last applied server config
  --discover-ports                Scan all listening TCP ports and report to server
  --apply-template <id>           Fetch and execute a config template by ID
    --schedule <cron|remove>      Schedule or remove the template as a cron job
  --config <path>                 Path to config file (default: /etc/servermetry/agent.conf)
  --no-apply-config               Skip fetching and applying remote config changes
  --loop                          Keep running; report on an interval (for Docker)
  --debug                         Enable verbose debug logging
  -h, --help                      Show this help message
"""


def parse_args():
    """Minimal arg parsing without argparse."""
    args = sys.argv[1:]

    if "-h" in args or "--help" in args:
        print(HELP_TEXT.format(version=AGENT_VERSION))
        sys.exit(0)

    info = "--info" in args
    config_status = "--config-status" in args
    dry_run = "--dry-run" in args
    check = "--check" in args
    check_update = "--check-update" in args
    force_update = "--update" in args
    update_status = "--update-status" in args
    debug = "--debug" in args
    no_apply_config = "--no-apply-config" in args
    discover_ports = "--discover-ports" in args
    loop = "--loop" in args
    config_path = None
    template_id = None
    schedule = None
    if "--config" in args:
        idx = args.index("--config")
        if idx + 1 < len(args):
            config_path = args[idx + 1]
    if "--apply-template" in args:
        idx = args.index("--apply-template")
        if idx + 1 < len(args):
            template_id = args[idx + 1]
    if "--schedule" in args:
        idx = args.index("--schedule")
        if idx + 1 < len(args):
            schedule = args[idx + 1]

    _KNOWN_FLAGS = {
        "--info", "--config-status", "--dry-run", "--check", "--check-update", "--update",
        "--update-status", "--debug", "--no-apply-config", "--discover-ports", "--loop",
        "--config", "--apply-template", "--schedule", "-h", "--help",
    }
    _VALUE_FLAGS = {"--config", "--apply-template", "--schedule"}
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in _VALUE_FLAGS:
            skip_next = True
            continue
        if arg.startswith("-") and arg not in _KNOWN_FLAGS:
            print("Unknown option: {}\n".format(arg))
            print(HELP_TEXT.format(version=AGENT_VERSION))
            sys.exit(1)

    return info, config_status, dry_run, check, check_update, force_update, update_status, debug, config_path, template_id, schedule, no_apply_config, discover_ports, loop


def _print_check(metrics):
    """Print a compact human-readable summary of collected metrics."""
    disks = metrics.get("diskUsages", [])
    nets = metrics.get("networkInterfaces", [])
    top = metrics.get("topProcesses", [])
    print("  OS:        {}".format(metrics.get("os", "?")))
    print("  Kernel:    {}".format(metrics.get("kernelVersion", "?")))
    print("  CPU:       {}% (cores: {}, threads: {})".format(
        metrics.get("cpuUsagePercent", 0),
        metrics.get("cpuCores", "?"),
        metrics.get("cpuThreads", "?"),
    ))
    print("  RAM:       {}% ({}/{} MB)".format(
        metrics.get("memUsagePercent", 0),
        metrics.get("memUsedMb", 0),
        metrics.get("memTotalMb", 0),
    ))
    print("  Disks:     {} found{}".format(
        len(disks),
        " — " + ", ".join(
            "{} ({:.0f}%)".format(d["mountpoint"], d["usagePercent"]) for d in disks[:3]
        ) if disks else "",
    ))
    print("  Network:   {} interface(s)".format(len(nets)))
    print("  Processes: {}".format(metrics.get("processCount", 0)))
    if metrics.get("pendingUpdates") is not None:
        print("  Updates:   {} pending ({} security)".format(
            metrics.get("pendingUpdates", 0),
            metrics.get("pendingSecurityUpdates", 0),
        ))
    if top:
        print("  Top proc:  {} ({:.1f}% CPU)".format(top[0]["name"], top[0]["cpuPercent"]))


def execute_script(script_content, log_debug_fn=None):
    import hashlib
    script_hash = hashlib.sha256(script_content.encode("utf-8", errors="replace")).hexdigest()

    if log_debug_fn:
        log_debug_fn("Executing script ({} chars)".format(len(script_content)))

    log_write("INFO", "Executing server setup script (SHA256: {})".format(script_hash))

    is_windows = platform.system() == "Windows"
    if is_windows:
        cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-Command", script_content]
        shell = False
    else:
        cmd = ["bash", "-c", script_content]
        shell = False

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SCRIPT_EXEC_TIMEOUT,
            shell=shell,
        )
        result.stdout = result.stdout.decode("utf-8", errors="replace")
        result.stderr = result.stderr.decode("utf-8", errors="replace")
        if result.stdout:
            for line in result.stdout.splitlines():
                log_write("INFO", "[script] {}".format(line))
        if result.stderr:
            for line in result.stderr.splitlines():
                log_write("WARNING", "[script] {}".format(line))

        success = result.returncode == 0
        if success:
            log_write("INFO", "Script executed successfully (exit code: {})".format(result.returncode))
        else:
            log_write("ERROR", "Script failed with exit code: {}".format(result.returncode))

        return success, result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        log_write("ERROR", "Script execution timed out after {} seconds".format(SCRIPT_EXEC_TIMEOUT))
        return False, "", "Timeout after {} seconds".format(SCRIPT_EXEC_TIMEOUT), -1
    except Exception as e:
        log_write("ERROR", "Script execution failed: {}".format(e))
        return False, "", str(e), -1


def apply_template_script(api_url, api_key, template_id, server_id, log_debug_fn=None):
    from client.api import apply_template

    log_write("INFO", "Fetching template {} for server {}...".format(template_id, server_id))
    success, result, err = apply_template(api_url, api_key, template_id, server_id, log_debug_fn=log_debug_fn)

    if not success:
        log_write("ERROR", "Failed to fetch template from API: {}".format(err))
        return False

    data = result.get("data", {}) if isinstance(result, dict) else {}
    script_content = data.get("scriptContent") if isinstance(data, dict) else None
    if not script_content and isinstance(result, dict):
        script_content = result.get("scriptContent")
    if not script_content:
        log_write("INFO", "Template has no scriptContent to execute")
        return True

    return execute_script(script_content, log_debug_fn=log_debug_fn)[0]


_STOP = False


def _request_stop(signum, frame):
    global _STOP
    _STOP = True


def _report_interval(remote_config):
    env = os.environ.get("SERVERMETRY_INTERVAL", "").strip()
    if env:
        try:
            return max(10, min(3600, int(env)))
        except ValueError:
            pass
    interval = (remote_config or {}).get("reportIntervalSeconds") or 60
    try:
        return max(10, min(3600, int(interval)))
    except (TypeError, ValueError):
        return 60


def _sleep_interruptible(seconds):
    end = time.time() + max(0, float(seconds))
    while not _STOP:
        remaining = end - time.time()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def _collect_metrics():
    _system = platform.system()
    if _system == "Windows":
        from services.windows import collect_windows_metrics
        return collect_windows_metrics()
    if _system == "Darwin":
        from services.darwin import collect_darwin_metrics
        return collect_darwin_metrics()
    from services.linux import collect_linux_metrics
    return collect_linux_metrics()


def _attach_service_statuses(metrics, stored_services):
    if not stored_services:
        return
    service_statuses = []
    for svc in stored_services:
        port = svc.get("port")
        if not port:
            continue
        protocol = (svc.get("protocol") or "TCP").upper()
        if protocol == "UDP":
            continue  # UDP requires app-level probing; skip silently
        status, error = _check_service_port(int(port), protocol)
        entry = {"serviceId": svc["id"], "status": status}
        if error:
            entry["error"] = error
        service_statuses.append(entry)
    if service_statuses:
        metrics["serviceStatuses"] = service_statuses


def _metrics_cycle(api_url, api_key, debug, no_apply_config):
    """Collect metrics, POST them, apply config/updates. Returns (ok, remote_config)."""
    skip_config = no_apply_config
    config_lock = FileLock(_CONFIG_LOCK_FILE, timeout=30)
    lock_held = config_lock.acquire(blocking=False)
    if not lock_held:
        log_write("WARNING", "Config state locked by another process, skipping config update")
        skip_config = True

    remote_config = {}
    try:
        if skip_config:
            stored_changed_at, remote_config, stored_services, config_state_loaded = None, {}, [], False
        else:
            stored_changed_at, remote_config, stored_services, config_state_loaded = _load_config_state()

        metrics = _collect_metrics()
        log_debug("Metrics collected successfully", debug_flag=debug)
        _attach_service_statuses(metrics, stored_services)

        from client.api import post_metrics
        ok, config_changed_at, enable_auto_updates, commands, post_err = post_metrics(
            api_url, api_key, metrics, log_debug_fn=lambda msg: log_debug(msg, debug_flag=debug)
        )
        if not ok:
            log_write("ERROR", "Failed to post metrics: {}".format(post_err))

        needs_config_fetch = (not config_state_loaded) or (config_changed_at != stored_changed_at)
        if ok and not skip_config and needs_config_fetch:
            from client.api import get_config
            from services.config_applier import apply_config

            if not config_state_loaded:
                log_debug("No cached config — fetching for the first time", debug_flag=debug)
            else:
                log_debug("Config changed on server — re-fetching", debug_flag=debug)

            config_ok, fetched_config, fetched_services = get_config(
                api_url, api_key, log_debug_fn=lambda msg: log_debug(msg, debug_flag=debug)
            )
            if config_ok:
                remote_config = fetched_config if isinstance(fetched_config, dict) else {}
                stored_services = fetched_services if isinstance(fetched_services, list) else []
                if remote_config:
                    apply_config(remote_config, log_debug_fn=lambda msg: log_debug(msg, debug_flag=debug))
                _save_config_state(config_changed_at, remote_config, stored_services)
            else:
                log_debug("Could not fetch config from server", debug_flag=debug)

        if enable_auto_updates is None:
            enable_auto_updates = remote_config.get("enableAutoUpdates")
        if ok and not skip_config and enable_auto_updates is not False:
            from services.updater import check_and_update
            check_and_update(log_debug_fn=lambda msg: log_debug(msg, debug_flag=debug))

        if ok and "discover-ports" in commands:
            if platform.system() != "Linux":
                log_write("WARNING", "Server requested port scan but --discover-ports is Linux only — skipping")
            else:
                log_write("INFO", "Server requested port scan — running")
                try:
                    from services.linux import read_listening_ports
                    from client.api import post_discovered_ports
                    _ports = read_listening_ports()
                    post_discovered_ports(
                        api_url, api_key, _ports,
                        log_debug_fn=lambda msg: log_debug(msg, debug_flag=debug),
                    )
                    log_write("INFO", "Port scan complete — {} port(s) reported".format(len(_ports)))
                except Exception as _e:
                    log_write("WARNING", "Server-requested port scan failed: {}".format(_e))

        return ok, remote_config
    finally:
        if lock_held:
            config_lock.release()


def main():
    info, config_status, dry_run, check, check_update, force_update, show_update_status, cli_debug, config_override, template_id, schedule, no_apply_config, discover_ports, loop = parse_args()
    DEBUG = cli_debug
    if in_container():
        set_stderr_logging(True)

    try:
        from services.path_migration import migrate_install_paths
        migrate_install_paths()
    except ImportError:
        pass

    values, conf_path = load_config(config_override)

    if info:
        api_url = values.get("api_url", DEFAULT_API_URL)
        server_id = values.get("server_id", "(not configured)")
        print("ServerMetry Agent")
        print("  Version:    {}".format(AGENT_VERSION))
        print("  Python:     {}".format(platform.python_version()))
        print("  Platform:   {} {} ({})".format(
            platform.system(), platform.release(), platform.machine()
        ))
        print("  Hostname:   {}".format(platform.node()))
        print("  Config:     {}".format(conf_path))
        print("  API URL:    {}".format(api_url))
        print("  Server ID:  {}".format(server_id))
        sys.exit(0)

    if config_status:
        stored_changed_at, remote_config, stored_services, config_state_loaded = _load_config_state()
        print("ServerMetry Agent — Config Status")
        print("")
        print("Local config ({})".format(conf_path or "env vars"))
        print("  API URL:      {}".format(values.get("api_url", DEFAULT_API_URL)))
        print("  Server ID:    {}".format(values.get("server_id", "(not set)")))
        print("")
        if config_state_loaded:
            # Match runtime gate: missing enableAutoUpdates => enabled (opt-out).
            auto_updates_on = remote_config.get("enableAutoUpdates") is not False
            print("Server config (last fetched: {})".format(stored_changed_at or "unknown"))
            print("  Auto-updates: {}".format("enabled" if auto_updates_on else "disabled"))
            print("  Report every: {}s".format(remote_config.get("reportIntervalSeconds", 60)))
            print("  Timezone:     {}".format(remote_config.get("timezone") or "(not set)"))
            print("  Locale:       {}".format(remote_config.get("locale") or "(not set)"))
            if stored_services:
                print("  Services:     {}".format(len(stored_services)))
        else:
            print("Server config: not yet fetched (run agent once to sync)")
        sys.exit(0)

    if show_update_status:
        try:
            from services.updater import update_status
        except ImportError:
            print("ERROR: services/updater.py is missing. Run with --update to bootstrap.")
            sys.exit(1)
        _, cached_config, _, config_state_loaded = _load_config_state()
        if config_state_loaded:
            # Match runtime: missing key => enabled; only explicit False disables.
            auto_updates = cached_config.get("enableAutoUpdates") is not False
        else:
            auto_updates = None
        update_status(auto_updates_enabled=auto_updates)
        sys.exit(0)

    if check_update or force_update:
        try:
            from services.updater import check_and_update, check_version
        except ImportError:
            print(
                "ERROR: services/updater.py is missing. Run:\n"
                "  curl -o /etc/servermetry/services/updater.py "
                "https://raw.githubusercontent.com/FloBaMedia/servermetry-client/main/agent/services/updater.py"
            )
            sys.exit(1)

        if check_update:
            ok = check_version(log_debug_fn=lambda msg: log_debug(msg, debug_flag=cli_debug) if cli_debug else None)
            sys.exit(0 if ok else 1)

        print("Checking for updates (forced)...")
        result = check_and_update(
            force=True,
            log_debug_fn=lambda msg: log_debug(msg, debug_flag=cli_debug) if cli_debug else None,
        )
        if result == "updated":
            print("Agent updated successfully. Restart the agent to use the new version.")
            sys.exit(0)
        elif result == "up_to_date":
            print("Agent is already up to date.")
            sys.exit(0)
        else:
            print("Update failed or was skipped. Check logs for details.")
            sys.exit(1)

    if values.get("debug"):
        DEBUG = True

    if DEBUG:
        log_debug("ServerMetry Agent {} starting (debug mode)".format(AGENT_VERSION), debug_flag=DEBUG)
        log_debug("Platform: {} {}".format(platform.system(), platform.release()), debug_flag=DEBUG)
        log_debug("dry_run={}".format(dry_run), debug_flag=DEBUG)
        if template_id:
            log_debug("template_id={}".format(template_id), debug_flag=DEBUG)

    if not dry_run and not check:
        values = ensure_config(values, conf_path, config_override)

    api_url = values.get("api_url", DEFAULT_API_URL)
    api_key = values.get("api_key", "")

    if template_id:
        if schedule is not None:
            from services.config_applier import schedule_template
            ok = schedule_template(template_id, schedule)
            sys.exit(0 if ok else 1)

        server_id = values.get("server_id", "")
        if not server_id:
            log_write("ERROR", "server_id not configured. Cannot apply template.")
            sys.exit(1)
        ok = apply_template_script(
            api_url, api_key, template_id, server_id, log_debug_fn=lambda msg: log_debug(msg, debug_flag=DEBUG)
        )
        sys.exit(0 if ok else 1)

    if discover_ports:
        _system = platform.system()
        if _system != "Linux":
            log_write("ERROR", "--discover-ports is only supported on Linux")
            sys.exit(1)
        from services.linux import read_listening_ports
        from client.api import post_discovered_ports
        ports = read_listening_ports()
        print("Found {} listening TCP port(s): {}".format(
            len(ports), ", ".join(str(p["port"]) for p in ports) if ports else "none"
        ))
        ok, err = post_discovered_ports(api_url, api_key, ports)
        if ok:
            print("Reported to server successfully.")
        else:
            log_write("ERROR", "Failed to report ports: {}".format(err))
        sys.exit(0 if ok else 1)

    if loop and (check or dry_run):
        log_write("WARNING", "--loop is ignored together with --check or --dry-run")
        loop = False

    if check or dry_run:
        metrics = _collect_metrics()
        log_debug("Metrics collected successfully", debug_flag=DEBUG)
        if check:
            _print_check(metrics)
            sys.exit(0)
        print(json.dumps(metrics, indent=2))
        sys.exit(0)

    def _run_once():
        try:
            return _metrics_cycle(api_url, api_key, DEBUG, no_apply_config)
        except Exception as e:
            log_write("ERROR", "Metrics cycle failed: {}".format(e))
            return False, {}

    ok, remote_config = _run_once()
    if not loop:
        sys.exit(0 if ok else 1)

    try:
        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)
    except (ValueError, OSError):
        pass

    log_write("INFO", "Loop mode started (interval from config or SERVERMETRY_INTERVAL)")
    while not _STOP:
        interval = _report_interval(remote_config)
        log_debug("Sleeping {}s until next report".format(interval), debug_flag=DEBUG)
        _sleep_interruptible(interval)
        if _STOP:
            break
        ok, remote_config = _run_once()
    log_write("INFO", "Loop mode stopped")
    sys.exit(0)


if __name__ == "__main__":
    main()
