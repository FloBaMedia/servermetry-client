"""Linux metric collectors for ServerPulse Agent."""

import json
import os
import platform
import subprocess
import time
from models.constants import SKIP_FILESYSTEMS, DISK_PREFIXES
from models.limits import (
    CPU_HZ_DEFAULT,
    CPU_SNAP_INTERVAL_SEC,
    DISK_MAX_ENTRIES,
    NETWORK_MAX_ENTRIES,
    STATE_ENCODING,
    TOP_PROCESS_LIMIT,
)
from models.paths import host_path, host_root, mounts_path, proc_path
from services.storage import read_disk_health as _read_disk_health
from utils.lock import FileLock
from utils.logging import log_write
from utils.snapshot import CpuSnapStore

_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CPU_SNAP_FILE = os.path.join(_AGENT_DIR, ".cpu_snap")
_LOCK_FILE = os.path.join(_AGENT_DIR, ".cpu_snap.lock")
_APT_CACHE_FILE = os.path.join(_AGENT_DIR, ".apt_cache")
_APT_CACHE_TTL = 300  # 5 minutes

_snap_store = CpuSnapStore(_CPU_SNAP_FILE)


def _parse_proc_stat():
    try:
        with open(proc_path("stat"), "r") as f:
            for line in f:
                if line.startswith("cpu "):
                    return [int(x) for x in line.split()[1:]]
    except Exception:
        pass
    return None


def _parse_proc_diskstats():
    result = {}
    try:
        with open(proc_path("diskstats"), "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 10:
                    continue
                devname = parts[2]
                if not any(devname.startswith(p) for p in DISK_PREFIXES):
                    continue
                if devname.startswith("nvme"):
                    if "p" in devname.split("n")[-1]:
                        continue
                elif devname[-1].isdigit():
                    continue
                result[devname] = (int(parts[5]), int(parts[9]))
    except Exception:
        pass
    return result


def _calc_cpu_delta(snap0, snap1):
    if snap0 is None or snap1 is None or len(snap0) < 4 or len(snap1) < 4:
        return 0.0
    idle0 = snap0[3] + (snap0[4] if len(snap0) > 4 else 0)
    idle1 = snap1[3] + (snap1[4] if len(snap1) > 4 else 0)
    total0 = sum(snap0)
    total1 = sum(snap1)
    delta_total = total1 - total0
    delta_idle = idle1 - idle0
    if delta_total <= 0:
        return 0.0
    return round((1.0 - delta_idle / delta_total) * 100.0, 2)


def _calc_io_delta(snap0, snap1, elapsed):
    read_sectors = 0
    write_sectors = 0
    all_devs = set(snap0.keys()) | set(snap1.keys())
    for dev in all_devs:
        r0, w0 = snap0.get(dev, (0, 0))
        r1, w1 = snap1.get(dev, (0, 0))
        read_sectors += max(0, r1 - r0)
        write_sectors += max(0, w1 - w0)
    if elapsed <= 0:
        return 0.0, 0.0
    read_kbps = round((read_sectors * 512 / 1024) / elapsed, 2)
    write_kbps = round((write_sectors * 512 / 1024) / elapsed, 2)
    return read_kbps, write_kbps


def _take_proc_snapshot():
    return _parse_proc_stat(), _parse_proc_diskstats(), time.time()


def _calc_deltas(snap0, snap1):
    cpu0, disk0, t0 = snap0
    cpu1, disk1, t1 = snap1
    elapsed = t1 - t0
    cpu_pct = _calc_cpu_delta(cpu0, cpu1)
    io_read, io_write = _calc_io_delta(disk0, disk1, elapsed)
    return cpu_pct, io_read, io_write


def _read_cpu_cores():
    try:
        count = 0
        with open(proc_path("cpuinfo"), "r") as f:
            for line in f:
                if line.startswith("processor"):
                    count += 1
        return max(count, 1)
    except Exception as e:
        log_write("WARNING", "cpu_cores unavailable: {}".format(e))
        return 0


def _read_cpu_info():
    model = None
    mhz = None
    threads = 0
    try:
        with open(proc_path("cpuinfo"), "r") as f:
            for line in f:
                if line.startswith("processor"):
                    threads += 1
                elif model is None and line.startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                elif mhz is None and line.startswith("cpu MHz"):
                    try:
                        mhz = int(float(line.split(":", 1)[1].strip()))
                    except (ValueError, OverflowError):
                        pass
    except Exception as e:
        log_write("WARNING", "cpu_info unavailable: {}".format(e))
    return model, mhz, max(threads, 1)


def _read_load_avg():
    try:
        with open(proc_path("loadavg"), "r") as f:
            parts = f.read().split()
            return float(parts[0]), float(parts[1]), float(parts[2])
    except Exception as e:
        log_write("WARNING", "loadavg unavailable: {}".format(e))
        return 0.0, 0.0, 0.0


def _read_memory():
    result = {"MemTotal": 0, "MemAvailable": 0, "SwapTotal": 0, "SwapFree": 0}
    try:
        with open(proc_path("meminfo"), "r") as f:
            for line in f:
                for key in result:
                    if line.startswith(key + ":"):
                        result[key] = int(line.split()[1])
    except Exception as e:
        log_write("WARNING", "meminfo unavailable: {}".format(e))
        return 0, 0, 0.0, 0, 0

    total_mb = result["MemTotal"] // 1024
    available_mb = result["MemAvailable"] // 1024
    used_mb = total_mb - available_mb
    usage_pct = round((used_mb / total_mb) * 100.0, 2) if total_mb > 0 else 0.0
    swap_total_mb = result["SwapTotal"] // 1024
    swap_used_mb = (result["SwapTotal"] - result["SwapFree"]) // 1024
    return total_mb, used_mb, usage_pct, swap_total_mb, swap_used_mb


def _read_disk_usages():
    result = []
    seen_mountpoints = set()
    seen_fs_ids = set()
    try:
        with open(mounts_path(), "r") as f:
            mounts = f.readlines()
    except Exception as e:
        log_write("WARNING", "disk: cannot read {}: {}".format(mounts_path(), e))
        return result

    for line in mounts:
        if len(result) >= DISK_MAX_ENTRIES:
            break
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mountpoint, fs_type = parts[0], parts[1], parts[2]
        if fs_type in SKIP_FILESYSTEMS:
            continue
        if mountpoint in seen_mountpoints:
            continue
        seen_mountpoints.add(mountpoint)
        try:
            st = os.statvfs(host_path(mountpoint))
            if st.f_blocks == 0:
                continue
            fs_id = (st.f_blocks, st.f_frsize)
            if fs_id in seen_fs_ids:
                continue
            seen_fs_ids.add(fs_id)
            total_gb = round((st.f_blocks * st.f_frsize) / (1024 ** 3), 2)
            free_gb = round((st.f_bavail * st.f_frsize) / (1024 ** 3), 2)
            used_gb = round(total_gb - free_gb, 2)
            usage_pct = round((used_gb / total_gb) * 100.0, 2) if total_gb > 0 else 0.0
            result.append({
                "mountpoint": mountpoint,
                "totalGb": total_gb,
                "usedGb": used_gb,
                "usagePercent": usage_pct,
                "filesystem": fs_type,
            })
        except Exception as e:
            log_write("WARNING", "disk: statvfs({}) failed: {}".format(mountpoint, e))
            continue

    return result


def _run_cmd(args, timeout=5):
    try:
        return subprocess.check_output(
            args, stderr=subprocess.DEVNULL, timeout=timeout, universal_newlines=True,
        )
    except Exception:
        return ""


def _read_iface_addresses():
    """Map interface name -> [{address, family}] using `ip -j addr` or `ip -o addr`."""
    mapping = {}
    raw_json = _run_cmd(["ip", "-j", "addr"], timeout=3)
    if raw_json:
        try:
            for iface in json.loads(raw_json):
                name = iface.get("ifname") or iface.get("name")
                if not name or name == "lo":
                    continue
                addrs = []
                for a in iface.get("addr_info") or []:
                    local = a.get("local")
                    family = a.get("family")
                    if not local:
                        continue
                    if family not in ("inet", "inet6"):
                        continue
                    # Skip link-local IPv6
                    if family == "inet6" and local.lower().startswith("fe80:"):
                        continue
                    addrs.append({"address": local, "family": family})
                if addrs:
                    mapping[name] = addrs
            return mapping
        except Exception as e:
            log_write("WARNING", "network: ip -j addr parse failed: {}".format(e))

    raw = _run_cmd(["ip", "-o", "addr", "show"], timeout=3)
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        name = parts[1]
        family = parts[2]
        cidr = parts[3]
        if name == "lo" or family not in ("inet", "inet6"):
            continue
        address = cidr.split("/")[0]
        if family == "inet6" and address.lower().startswith("fe80:"):
            continue
        mapping.setdefault(name, []).append({"address": address, "family": family})
    return mapping


def _read_network_interfaces():
    result = []
    addr_map = _read_iface_addresses()
    try:
        with open(proc_path("net/dev"), "r") as f:
            lines = f.readlines()
    except Exception as e:
        log_write("WARNING", "network: cannot read /proc/net/dev: {}".format(e))
        return result

    for line in lines[2:]:
        if len(result) >= NETWORK_MAX_ENTRIES:
            break
        if ":" not in line:
            continue
        name, data = line.split(":", 1)
        name = name.strip()
        if name == "lo":
            continue
        fields = data.split()
        if len(fields) < 10:
            continue
        try:
            result.append({
                "name": name,
                "rxBytes": int(fields[0]),
                "rxPackets": int(fields[1]),
                "txBytes": int(fields[8]),
                "txPackets": int(fields[9]),
                "addresses": addr_map.get(name, []),
            })
        except (ValueError, IndexError) as e:
            log_write("WARNING", "network: parse error for {}: {}".format(name, e))

    return result


def _read_top_processes(limit=TOP_PROCESS_LIMIT):
    try:
        def _read_proc_stats():
            stats = {}
            for pid in os.listdir(proc_path()):
                if not pid.isdigit():
                    continue
                try:
                    with open(proc_path("{}/stat".format(pid)), "r") as f:
                        parts = f.read().split()
                    name = parts[1].strip("()")
                    utime = int(parts[13])
                    stime = int(parts[14])
                    stats[pid] = {"name": name, "ticks": utime + stime}
                except Exception:
                    pass
            return stats

        snap0 = _read_proc_stats()
        time.sleep(CPU_SNAP_INTERVAL_SEC)
        snap1 = _read_proc_stats()

        mem_total = 0
        try:
            with open(proc_path("meminfo"), "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        mem_total = int(line.split()[1]) * 1024
                        break
        except Exception:
            pass

        results = []
        for pid, s1 in snap1.items():
            if pid not in snap0:
                continue
            delta = s1["ticks"] - snap0[pid]["ticks"]
            cpu_pct = round((delta / CPU_HZ_DEFAULT) / CPU_SNAP_INTERVAL_SEC * 100.0, 1)
            mem_mb = 0.0
            user = ""
            try:
                with open(proc_path("{}/status".format(pid)), "r") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            mem_mb = round(int(line.split()[1]) / 1024.0, 1)
                        elif line.startswith("Uid:"):
                            uid = line.split()[1]
                            try:
                                import pwd
                                user = pwd.getpwuid(int(uid)).pw_name
                            except Exception:
                                user = uid
            except Exception:
                pass
            results.append({
                "pid": int(pid),
                "name": s1["name"],
                "cpuPercent": cpu_pct,
                "memMb": mem_mb,
                "user": user,
            })

        results.sort(key=lambda p: p["cpuPercent"], reverse=True)
        return results[:limit]
    except Exception as e:
        log_write("WARNING", "top_processes unavailable: {}".format(e))
        return []


def _read_process_count():
    try:
        return sum(1 for d in os.listdir(proc_path()) if d.isdigit())
    except Exception as e:
        log_write("WARNING", "process_count unavailable: {}".format(e))
        return 0


def _read_open_files():
    try:
        with open(proc_path("sys/fs/file-nr"), "r") as f:
            return int(f.read().split()[0])
    except Exception as e:
        log_write("WARNING", "open_files unavailable: {}".format(e))
        return 0


def _read_os_info():
    try:
        with open(host_path("/etc/os-release"), "r") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return platform.system() + " " + platform.release()


def _read_uptime():
    try:
        with open(proc_path("uptime"), "r") as f:
            return int(float(f.read().split()[0]))
    except Exception as e:
        log_write("WARNING", "uptime unavailable: {}".format(e))
        return 0


def _read_pending_updates():
    """Return (count, security_count, packages) from apt, with 30-min file cache."""
    if host_root():
        # Container apt would list the image, not the host. Skip rather than lie.
        return None, None, None
    try:
        now = time.time()
        try:
            with open(_APT_CACHE_FILE, "r") as f:
                cached = json.load(f)
            if now - cached.get("ts", 0) < _APT_CACHE_TTL:
                return cached["count"], cached["security_count"], cached["packages"]
        except Exception:
            pass

        result = subprocess.run(
            ["apt", "list", "--upgradable"],
            capture_output=True, text=True, timeout=30,
        )
        packages = []
        for line in result.stdout.splitlines():
            if line.startswith("Listing") or not line.strip():
                continue
            try:
                # Format: name/repo version arch [upgradable from: old_version]
                pkg_part, rest = line.split("/", 1)
                tokens = rest.split()
                repo = tokens[0] if tokens else ""
                new_ver = tokens[1] if len(tokens) > 1 else ""
                old_ver = ""
                if "upgradable from:" in line:
                    old_ver = line.split("upgradable from:")[-1].strip().rstrip("]")
                is_security = "-security" in repo
                packages.append({
                    "name": pkg_part.strip(),
                    "currentVersion": old_ver,
                    "newVersion": new_ver,
                    "security": is_security,
                })
            except Exception:
                continue

        count = len(packages)
        security_count = sum(1 for p in packages if p["security"])
        try:
            with open(_APT_CACHE_FILE, "w") as f:
                json.dump({"ts": now, "count": count, "security_count": security_count, "packages": packages}, f)
        except Exception:
            pass
        return count, security_count, packages
    except FileNotFoundError:
        return None, None, None
    except Exception as e:
        log_write("WARNING", "apt check failed: {}".format(e))
        return None, None, None


def read_listening_ports():
    """Return sorted list of listening TCP ports via /proc/net/tcp[6]."""
    ports = set()
    for path in [proc_path("net/tcp"), proc_path("net/tcp6")]:
        try:
            with open(path, "r") as f:
                lines = f.readlines()
            for line in lines[1:]:
                parts = line.split()
                if len(parts) < 4:
                    continue
                if parts[3] != "0A":  # 0A = LISTEN
                    continue
                port_hex = parts[1].split(":")[-1]
                port = int(port_hex, 16)
                if 0 < port <= 65535:
                    ports.add(port)
        except Exception:
            pass
    return [{"port": p, "protocol": "TCP"} for p in sorted(ports)]


def collect_linux_metrics():
    from models.constants import AGENT_VERSION

    configured_root = (os.environ.get("SERVERMETRY_HOST_ROOT") or "").strip()
    if configured_root and not host_root():
        log_write(
            "WARNING",
            "SERVERMETRY_HOST_ROOT={!r} is set but not mounted; "
            "collecting container metrics instead of host metrics".format(configured_root),
        )

    with FileLock(_LOCK_FILE, timeout=30) as lock:
        if not lock._acquired:
            log_write("WARNING", "cpu_snap locked by another process, skipping snapshot update")

    snap0 = _take_proc_snapshot()

    prev_snap = _snap_store.load()
    if prev_snap is not None:
        prev_fields, prev_ts = prev_snap
        cpu_avg_1min = _calc_cpu_delta(prev_fields, snap0[0])
        cpu_avg_1min = round(max(0.0, min(100.0, cpu_avg_1min)), 2)
    else:
        cpu_avg_1min = None
    _snap_store.save(snap0[0], snap0[2])

    cpu_cores = _read_cpu_cores()
    cpu_model, cpu_mhz, cpu_threads = _read_cpu_info()
    load1, load5, load15 = _read_load_avg()
    mem_total, mem_used, mem_pct, swap_total, swap_used = _read_memory()
    disks = _read_disk_usages()
    networks = _read_network_interfaces()
    disk_health = _read_disk_health()
    proc_count = _read_process_count()
    top_processes = _read_top_processes()
    open_files = _read_open_files()
    os_info = _read_os_info()
    uptime = _read_uptime()
    kernel = platform.release()

    snap1 = _take_proc_snapshot()
    cpu_pct, io_read, io_write = _calc_deltas(snap0, snap1)
    cpu_pct = max(0.0, min(100.0, cpu_pct))

    pending_count, pending_security_count, pending_packages = _read_pending_updates()

    return {
        "os": os_info,
        "kernelVersion": kernel,
        "uptimeSeconds": uptime,
        "agentVersion": AGENT_VERSION,
        "cpuModel": cpu_model,
        "cpuMhz": cpu_mhz,
        "cpuThreads": cpu_threads,
        "cpuUsagePercent": cpu_pct,
        "cpuAvg1MinPercent": cpu_avg_1min,
        "cpuCores": cpu_cores,
        "loadAvg1": load1,
        "loadAvg5": load5,
        "loadAvg15": load15,
        "memTotalMb": mem_total,
        "memUsedMb": mem_used,
        "memUsagePercent": mem_pct,
        "swapTotalMb": swap_total,
        "swapUsedMb": swap_used,
        "diskUsages": disks,
        "networkInterfaces": networks,
        "diskHealth": disk_health,
        "processCount": proc_count,
        "topProcesses": top_processes,
        "openFiles": open_files,
        "ioReadKbps": io_read,
        "ioWriteKbps": io_write,
        "pendingUpdates": pending_count,
        "pendingSecurityUpdates": pending_security_count,
        "pendingPackages": pending_packages,
    }
