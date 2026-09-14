"""Read-only Linux storage health. No repair, scrub, resync or SMART self-tests."""
import json
import os
import re
import shutil
import subprocess
import time


def parse_mdstat(content):
    arrays = []
    current = None
    for line in content.splitlines():
        line = line.strip()
        match = re.match(r"^(md\S+)\s*:\s*(.*)$", line)
        if match:
            parts = match.group(2).split()
            members = re.findall(r"(\S+?)\[\d+\]((?:\([A-Z]\))*)", line)
            failed = [name for name, flags in members if "(F)" in flags]
            active = "active" in parts
            current = {
                "name": match.group(1), "level": next((p for p in parts if p.startswith("raid")), "unknown"),
                "state": "active" if active else "inactive",
                "devices": [name for name, _ in members],
                "failedDevices": failed, "degraded": bool(failed) or not active,
            }
            arrays.append(current)
        elif current and not line.startswith(("Personalities", "unused")):
            counts = re.search(r"\[(\d+)/(\d+)\]", line)
            slots = re.search(r"\[([U_]+)\]", line)
            if (counts and (int(counts[2]) < int(counts[1]) or (current["level"] in ("raid1", "raid5", "raid6", "raid10") and int(counts[1]) < 2))) or (slots and "_" in slots[1]):
                current["degraded"] = True
            if "super external:imsm" in line or "super external:ddf" in line:
                current.update(level="container", degraded=False)
            if re.search(r"\b(recovery|resync|reshape|check|repair)\b", line):
                current["state"] = "recovering"
        if current and current["degraded"] and current["state"] == "active":
            current["state"] = "degraded"
    return arrays


def parse_zpool_status(content):
    """LC_ALL=C zpool status -P. Pool state is authoritative, not disk count."""
    pools = []
    current = None
    for line in content.splitlines():
        match = re.match(r"\s*pool:\s*(\S+)\s*$", line)
        if match:
            current = {"name": "zfs:" + match[1], "level": "zfs", "state": "unknown",
                       "devices": [], "failedDevices": [], "degraded": False}
            pools.append(current)
        elif current:
            state = re.match(r"\s*state:\s*(\S+)", line)
            if state:
                current["state"] = state[1].lower()
                current["degraded"] = state[1] != "ONLINE"
            if line.strip().startswith("errors:") and "No known data errors" not in line:
                current.update(degraded=True, state="data-errors")
            member = re.match(r"\s*(\S+)\s+(ONLINE|DEGRADED|FAULTED|OFFLINE|UNAVAIL|REMOVED)\b", line)
            if member and (member[1].startswith("/dev/") or member[1].isdigit()):
                current["devices"].append(member[1])
                if member[2] != "ONLINE":
                    current["failedDevices"].append(member[1])
    return pools


def run_readonly(args, timeout):
    # smartctl uses bitmask exit codes for actual disk failures; retain stdout.
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=timeout, universal_newlines=True, check=False,
                                env={**os.environ, "LC_ALL": "C"})
        return result.stdout, result.returncode
    except (OSError, subprocess.TimeoutExpired):
        return "", -1


def smart_disk(data, device):
    status = data.get("smart_status", {}).get("passed")
    nvme = data.get("nvme_smart_health_information_log", {})
    failed = status is False or bool(nvme.get("critical_warning", 0))
    # Media/data-integrity errors and exhausted endurance warrant intervention
    # even when firmware still reports overall SMART PASSED.
    failed = failed or nvme.get("media_errors", 0) > 0 or nvme.get("percentage_used", 0) >= 100
    attributes = data.get("ata_smart_attributes", {}).get("table", [])
    failed = failed or any(a.get("id") in (197, 198) and a.get("raw", {}).get("value", 0) > 0 for a in attributes)
    health = "FAILED" if failed else "PASSED" if status is True else "UNKNOWN"
    temperature = data.get("temperature", {}).get("current")
    return {"name": device, "model": data.get("model_name"), "health": health,
            "temperatureC": temperature if isinstance(temperature, (int, float)) else None}


def read_smart_disks():
    if not shutil.which("smartctl"):
        return [], "unavailable"
    deadline = time.monotonic() + 20
    raw, code = run_readonly(["smartctl", "--scan", "-j"], 3)
    try:
        devices = json.loads(raw).get("devices", [])
    except (ValueError, TypeError, AttributeError):
        return [], "error"
    if code != 0 or not isinstance(devices, list) or not devices:
        return [], "unavailable"
    disks = []
    state = "collected"
    seen = set()
    for entry in devices:
        if not isinstance(entry, dict):
            state = "error"
            continue
        device = entry.get("name", "")
        if not isinstance(device, str) or not device.startswith("/dev/") or device in seen:
            continue
        seen.add(device)
        if len(disks) >= 32 or time.monotonic() >= deadline:
            state = "error"
            break
        args = ["smartctl", "-j", "-H", "-A", "-i"]
        if isinstance(entry.get("type"), str):
            args += ["-d", entry["type"]]
        raw, code = run_readonly(args + [device], max(0.1, min(3, deadline - time.monotonic())))
        try:
            disk = smart_disk(json.loads(raw), device)
        except (ValueError, TypeError, AttributeError):
            disk = {"name": device, "health": "UNKNOWN", "model": None, "temperatureC": None}
        # Bits 0..2 mean invocation/device/command failures, not trustworthy success.
        if code < 0 or code & 7 or disk["health"] == "UNKNOWN":
            state = "error"
            if disk["health"] != "FAILED":
                disk["health"] = "UNKNOWN"
        disks.append(disk)
    return disks, state if disks else "unavailable"


def read_disk_health():
    root = os.environ.get("SERVERMETRY_HOST_ROOT", "").rstrip("/")
    raids = []
    raid_status = "collected"
    try:
        with open(root + "/proc/mdstat", encoding="utf-8") as handle:
            raids = parse_mdstat(handle.read())
    except FileNotFoundError:
        pass  # md module is optional (ZFS-only machines need no mdstat).
    except OSError:
        raid_status = "error"
    # A container's zpool/smartctl view may differ from the mounted host. Do not
    # report that as host health; mdstat can still be read from the host mount.
    if root:
        raid_status = "unavailable"
        disks, smart_status = [], "unavailable"
    else:
        if shutil.which("zpool"):
            output, code = run_readonly(["zpool", "status", "-P"], 5)
            pools = parse_zpool_status(output)
            raids.extend(pools)
            if code != 0 or (not pools and "no pools available" not in output) or any(p["state"] == "unknown" for p in pools):
                raid_status = "error"
        elif os.path.isdir("/sys/module/zfs"):
            raid_status = "unavailable"
        disks, smart_status = read_smart_disks()
    return {"raids": raids, "disks": disks,
            "raidDegradedCount": sum(bool(r["degraded"]) for r in raids),
            "diskUnhealthyCount": sum(d["health"] == "FAILED" for d in disks),
            "raidCheckStatus": raid_status, "smartCheckStatus": smart_status}
