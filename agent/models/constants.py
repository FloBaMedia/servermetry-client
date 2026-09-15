AGENT_VERSION = "1.4.9"
LOG_MAX_BYTES = 1_048_576  # 1 MB
DEFAULT_API_URL = "https://api.servermetry.com"

SKIP_FILESYSTEMS = {
    "tmpfs", "devtmpfs", "sysfs", "proc", "cgroup", "cgroup2",
    "pstore", "bpf", "tracefs", "securityfs", "debugfs",
    "hugetlbfs", "mqueue", "fusectl", "configfs", "ramfs",
    "devpts", "overlay", "squashfs", "autofs", "rpc_pipefs",
    # Container / VM virtual filesystems (can produce hundreds of entries on Docker hosts)
    "shm", "nsfs", "efivarfs", "binfmt_misc", "selinuxfs",
}

DISK_PREFIXES = ("sd", "nvme", "vd", "xvd", "hd")