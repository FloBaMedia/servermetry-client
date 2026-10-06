"""
ServerMetry config applier – applies server configuration to the local system.
All functions are best-effort: failures are logged but never abort the agent run.
"""

import os
import platform
import re
import subprocess
import sys
import tempfile

from models.limits import CONFIG_APPLIER_TIMEOUT, EXTRA_CMD_TIMEOUT, STATE_ENCODING
from models.paths import (
    CRON_MARKER,
    LEGACY_CRON_MARKER,
    LEGACY_TEMPLATE_CRON_MARKER,
    LEGACY_WINDOWS_TASK_NAME,
    TEMPLATE_CRON_MARKER,
    WINDOWS_TASK_NAME,
)
from utils.lock import atomic_write
from utils.logging import log_write
from utils.validation import (
    resolve_windows_timezone,
    validate_and_sanitize_dns,
    validate_and_sanitize_interval,
    validate_and_sanitize_ntp,
    validate_and_sanitize_timezone,
)


def _run(cmd, timeout=CONFIG_APPLIER_TIMEOUT):
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        return result.returncode == 0, stdout, stderr
    except subprocess.TimeoutExpired:
        return False, "", "timeout after {}s".format(timeout)
    except Exception as e:
        return False, "", str(e)


def apply_timezone(timezone):
    if not timezone:
        return True

    valid, tz = validate_and_sanitize_timezone(timezone)
    if not valid:
        log_write("WARNING", "Invalid timezone rejected: {}".format(timezone))
        return False

    if platform.system() == "Windows":
        win_tz = resolve_windows_timezone(tz)
        if not win_tz:
            log_write(
                "WARNING",
                "No Windows timezone mapping for '{}' – set a Windows ID "
                "(e.g. 'W. Europe Standard Time') or a known IANA zone".format(tz),
            )
            return False
        # Escape single quotes for PowerShell string literal
        safe_win_tz = win_tz.replace("'", "''")
        ok, _, err = _run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Set-TimeZone -Id '{}'".format(safe_win_tz)]
        )
        if ok:
            log_write("INFO", "Timezone set to {} (Windows ID: {})".format(tz, win_tz))
        else:
            log_write("WARNING", "Failed to set timezone {} ({}): {}".format(tz, win_tz, err))
        return ok

    ok, _, err = _run(["timedatectl", "set-timezone", tz])
    if ok:
        log_write("INFO", "Timezone set to {}".format(tz))
    else:
        log_write("WARNING", "Failed to set timezone {}: {}".format(tz, err))
    return ok


def apply_locale(locale):
    if not locale:
        return True
    if platform.system() != "Linux":
        return True

    safe_locale = "".join(c for c in locale if c.isalnum() or c in "_-.")
    if safe_locale != locale or len(safe_locale) > 100:
        log_write("WARNING", "Invalid locale rejected: {}".format(locale))
        return False

    ok, _, err = _run(["localectl", "set-locale", "LANG={}".format(safe_locale)])
    if ok:
        log_write("INFO", "Locale set to {}".format(safe_locale))
    else:
        log_write("WARNING", "Failed to set locale {}: {}".format(safe_locale, err))
    return ok


def apply_ntp(custom_ntp):
    if not custom_ntp:
        return True

    valid, ntp = validate_and_sanitize_ntp(custom_ntp)
    if not valid:
        log_write("WARNING", "Invalid NTP server rejected: {}".format(custom_ntp))
        return False

    if platform.system() == "Windows":
        ok, _, err = _run([
            "w32tm", "/config",
            "/manualpeerlist:{}".format(ntp),
            "/syncfromflags:manual",
            "/reliable:YES",
            "/update",
        ])
        if ok:
            _run(["net", "stop", "w32time"])
            _run(["net", "start", "w32time"])
    else:
        conf_path = "/etc/systemd/timesyncd.conf"
        try:
            try:
                with open(conf_path, "r", encoding=STATE_ENCODING) as f:
                    content = f.read()
            except FileNotFoundError:
                content = "[Time]\n"

            safe_ntp = "".join(c for c in ntp if c.isalnum() or c in ".-_")
            if safe_ntp != ntp:
                log_write("WARNING", "NTP server sanitized: {} -> {}".format(ntp, safe_ntp))
                ntp = safe_ntp

            if re.search(r"^NTP\s*=", content, re.MULTILINE):
                content = re.sub(
                    r"^NTP\s*=.*$",
                    "NTP={}".format(ntp),
                    content,
                    flags=re.MULTILINE,
                )
            elif "[Time]" in content:
                content = content.replace("[Time]", "[Time]\nNTP={}".format(ntp), 1)
            else:
                content += "\n[Time]\nNTP={}\n".format(ntp)

            atomic_write(conf_path, content, encoding=STATE_ENCODING)
            ok, _, err = _run(["systemctl", "restart", "systemd-timesyncd"])
        except PermissionError:
            log_write("WARNING", "No permission to update timesyncd.conf – run agent as root")
            return False
        except Exception as e:
            log_write("WARNING", "Failed to configure NTP: {}".format(e))
            return False

    if ok:
        log_write("INFO", "NTP server set to {}".format(ntp))
    else:
        log_write("WARNING", "Failed to configure NTP {}: {}".format(ntp, err))
    return ok


def apply_dns(custom_dns):
    if not custom_dns or not isinstance(custom_dns, list):
        return True

    valid, dns_list = validate_and_sanitize_dns(custom_dns)
    if not valid:
        log_write("WARNING", "Invalid DNS list rejected: {}".format(custom_dns))
        return True

    if platform.system() == "Windows":
        dns_args = ", ".join("'{}'".format(d) for d in dns_list)
        ps_cmd = (
            "$adapter = (Get-NetAdapter | Where-Object {{ $_.Status -eq 'Up' }} "
            "| Select-Object -First 1).Name; "
            "Set-DnsClientServerAddress -InterfaceAlias $adapter -ServerAddresses ({})".format(dns_args)
        )
        ok, _, err = _run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd]
        )
    else:
        try:
            lines = ["# Managed by ServerMetry Agent\n"]
            for dns in dns_list:
                lines.append("nameserver {}\n".format(dns))
            atomic_write("/etc/resolv.conf", "".join(lines), encoding=STATE_ENCODING)
            ok, err = True, ""
        except PermissionError:
            log_write("WARNING", "No permission to update /etc/resolv.conf – run agent as root")
            return False
        except Exception as e:
            log_write("WARNING", "Failed to update DNS: {}".format(e))
            return False

    if ok:
        log_write("INFO", "DNS servers set to {}".format(", ".join(dns_list)))
    else:
        log_write("WARNING", "Failed to set DNS servers: {}".format(err))
    return ok


def _interval_to_cron(interval_seconds):
    minutes = max(1, round(interval_seconds / 60))
    if minutes == 1:
        return "* * * * *"
    if minutes <= 30 and 60 % minutes == 0:
        return "*/{} * * * *".format(minutes)
    if minutes <= 60:
        return "0 * * * *"
    hours = round(minutes / 60)
    if hours == 1:
        return "0 * * * *"
    if hours <= 12 and 24 % hours == 0:
        return "0 */{} * * *".format(hours)
    return "0 0 * * *"


def update_schedule(interval_seconds):
    valid, interval = validate_and_sanitize_interval(interval_seconds)
    if not valid:
        log_write("WARNING", "Invalid interval rejected: {}".format(interval_seconds))
        return True

    if interval == 60:
        return True

    if platform.system() == "Windows":
        task_name = WINDOWS_TASK_NAME
        interval_minutes = max(1, round(interval / 60))
        ps_cmd = (
            "$t = Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue; "
            "if ($t) {{ "
            "  $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) "
            "    -RepetitionInterval (New-TimeSpan -Minutes {minutes}) "
            "    -RepetitionDuration (New-TimeSpan -Days 9999); "
            "  Set-ScheduledTask -TaskName '{task}' -Trigger $trigger | Out-Null; "
            "  Write-Output 'updated'; "
            "}} else {{ Write-Output 'not_found'; }}"
        ).format(task=task_name, minutes=interval_minutes)

        ok, stdout, err = _run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd]
        )
        if "updated" in stdout:
            log_write("INFO", "Scheduled task interval updated to {} minutes".format(interval_minutes))
            return True
        if "not_found" in stdout:
            log_write("WARNING", "Scheduled task '{}' not found; cannot update interval".format(task_name))
            return False
        log_write("WARNING", "Failed to update scheduled task: {}".format(err))
        return False
    else:
        cron_expr = _interval_to_cron(interval)
        _, current_cron, _ = _run(["crontab", "-l"])

        lines = current_cron.splitlines() if current_cron else []
        new_lines = []
        updated = False

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue
            if CRON_MARKER in line or LEGACY_CRON_MARKER in line:
                parts = stripped.split()
                if len(parts) >= 6:
                    cmd_part = " ".join(parts[5:])
                    cmd_part = cmd_part.replace(LEGACY_CRON_MARKER, CRON_MARKER)
                    new_lines.append("{} {}".format(cron_expr, cmd_part))
                    updated = True
                    continue
            new_lines.append(line)

        if not updated:
            log_write("WARNING", "ServerMetry cron entry not found; cannot update interval")
            return False

        new_cron = "\n".join(new_lines) + "\n"
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".cron", delete=False, encoding=STATE_ENCODING) as tmp:
                tmp.write(new_cron)
                tmp_path = tmp.name
            ok, _, err = _run(["crontab", tmp_path])
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        if ok:
            log_write("INFO", "Cron interval updated to '{}' ({} seconds)".format(cron_expr, interval))
            return True
        log_write("WARNING", "Failed to update crontab: {}".format(err))
        return False


# ── Template Scheduling ───────────────────────────────────────────────────────


def _validate_cron_expr(expr):
    parts = expr.strip().split()
    if len(parts) != 5:
        return False
    allowed = set("0123456789*/-,")
    return all(all(c in allowed for c in p) for p in parts)


def _write_crontab(lines):
    new_cron = "\n".join(lines) + "\n"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".cron", delete=False, encoding=STATE_ENCODING) as tmp:
            tmp.write(new_cron)
            tmp_path = tmp.name
        return _run(["crontab", tmp_path])
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def schedule_template(template_id, cron_expr):
    """
    Install or update a cron job for a template script.
    Pass cron_expr='remove' to unschedule.
    Only supported on Linux.
    """
    safe_id = "".join(c for c in template_id if c.isalnum() or c == "-")
    if safe_id != template_id or not safe_id:
        log_write("WARNING", "Invalid template_id rejected: {}".format(template_id))
        return False

    if cron_expr == "remove":
        return unschedule_template(template_id)

    if platform.system() != "Linux":
        log_write("WARNING", "Template scheduling via --schedule is only supported on Linux")
        return False

    if not _validate_cron_expr(cron_expr):
        log_write("WARNING", "Invalid cron expression: {}".format(cron_expr))
        return False

    agent_path = os.path.realpath(sys.argv[0])
    legacy_marker = "{}{}".format(LEGACY_TEMPLATE_CRON_MARKER, safe_id)
    marker = "{}{}".format(TEMPLATE_CRON_MARKER, safe_id)
    cron_line = "{} python3 {} --apply-template {}  {}".format(
        cron_expr, agent_path, safe_id, marker
    )

    _, current_cron, _ = _run(["crontab", "-l"])
    lines = current_cron.splitlines() if current_cron else []
    new_lines = []
    updated = False
    for line in lines:
        if marker in line or legacy_marker in line:
            new_lines.append(cron_line)
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(cron_line)

    ok, _, err = _write_crontab(new_lines)
    if ok:
        log_write("INFO", "Template {} scheduled: {}".format(safe_id, cron_expr))
    else:
        log_write("WARNING", "Failed to schedule template {}: {}".format(safe_id, err))
    return ok


def unschedule_template(template_id):
    """Remove the cron entry for a template. Only supported on Linux."""
    safe_id = "".join(c for c in template_id if c.isalnum() or c == "-")
    legacy_marker = "{}{}".format(LEGACY_TEMPLATE_CRON_MARKER, safe_id)
    marker = "{}{}".format(TEMPLATE_CRON_MARKER, safe_id)

    if platform.system() != "Linux":
        log_write("WARNING", "Template unscheduling is only supported on Linux")
        return False

    _, current_cron, _ = _run(["crontab", "-l"])
    lines = current_cron.splitlines() if current_cron else []
    new_lines = [l for l in lines if marker not in l and legacy_marker not in l]

    if len(new_lines) == len(lines):
        log_write("INFO", "No scheduled cron entry found for template {}".format(safe_id))
        return True

    ok, _, err = _write_crontab(new_lines)
    if ok:
        log_write("INFO", "Template {} unscheduled".format(safe_id))
    else:
        log_write("WARNING", "Failed to unschedule template {}: {}".format(safe_id, err))
    return ok


def apply_config(config, log_debug_fn=None):
    if not config:
        return 60

    timezone = config.get("timezone")
    locale = config.get("locale")
    custom_ntp = config.get("customNtp")
    custom_dns = config.get("customDns")
    interval = config.get("reportIntervalSeconds", 60) or 60

    try:
        from models.paths import in_container
    except ImportError:
        in_container = lambda: False  # noqa: E731
    if in_container():
        log_write("INFO", "Container mode: skipping host timezone/locale/NTP/DNS/cron")
        return interval

    if log_debug_fn:
        log_debug_fn(
            "Applying config: timezone={}, locale={}, ntp={}, dns={}, interval={}s".format(
                timezone, locale, custom_ntp, custom_dns, interval
            )
        )

    if timezone:
        apply_timezone(timezone)
    if locale:
        apply_locale(locale)
    if custom_ntp:
        apply_ntp(custom_ntp)
    if custom_dns:
        apply_dns(custom_dns)
    if interval != 60:
        update_schedule(interval)

    return interval
