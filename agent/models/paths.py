"""Install paths and branding constants for ServerMetry Agent."""

import os
import platform

# ServerMetry (current)
LINUX_INSTALL_DIR = "/etc/servermetry"
WINDOWS_INSTALL_DIR = r"C:\ProgramData\ServerMetry"
USER_CONFIG_DIR = os.path.expanduser("~/.config/servermetry")

# Legacy ServerPulse paths (migrated on agent update)
LEGACY_LINUX_INSTALL_DIR = "/etc/serverpulse"
LEGACY_WINDOWS_INSTALL_DIR = r"C:\ProgramData\ServerPulse"
LEGACY_USER_CONFIG_DIR = os.path.expanduser("~/.config/serverpulse")

CONFIG_SECTION = "servermetry"
LEGACY_CONFIG_SECTION = "serverpulse"

CRON_MARKER = "servermetry/agent.py"
LEGACY_CRON_MARKER = "serverpulse/agent.py"

WINDOWS_TASK_NAME = "ServerMetryAgent"
LEGACY_WINDOWS_TASK_NAME = "ServerPulseAgent"

LINUX_LOG_PATH = "/var/log/servermetry-agent.log"
LEGACY_LINUX_LOG_PATH = "/var/log/serverpulse-agent.log"

WINDOWS_LOG_PATH = os.path.join(WINDOWS_INSTALL_DIR, "agent.log")
LEGACY_WINDOWS_LOG_PATH = os.path.join(LEGACY_WINDOWS_INSTALL_DIR, "agent.log")

TEMPLATE_CRON_MARKER = "# servermetry-template-"
LEGACY_TEMPLATE_CRON_MARKER = "# serverpulse-template-"

MIGRATION_MARKER = ".servermetry-migrated"


def install_dir():
    """Preferred install directory for the current platform."""
    if platform.system() == "Windows":
        return WINDOWS_INSTALL_DIR
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return LINUX_INSTALL_DIR
    return USER_CONFIG_DIR


def legacy_install_dir():
    """Legacy ServerPulse install directory for the current platform."""
    if platform.system() == "Windows":
        return LEGACY_WINDOWS_INSTALL_DIR
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return LEGACY_LINUX_INSTALL_DIR
    return LEGACY_USER_CONFIG_DIR


def resolve_install_dir():
    """Return the directory that currently holds agent.py, preferring ServerMetry."""
    new_dir = install_dir()
    legacy_dir = legacy_install_dir()
    if os.path.isfile(os.path.join(new_dir, "agent.py")):
        return new_dir
    if os.path.isfile(os.path.join(legacy_dir, "agent.py")):
        return legacy_dir
    return new_dir


def agent_conf_path(base_dir=None):
    return os.path.join(base_dir or resolve_install_dir(), "agent.conf")


def agent_py_path(base_dir=None):
    return os.path.join(base_dir or resolve_install_dir(), "agent.py")


def windows_log_path():
    return WINDOWS_LOG_PATH


def legacy_windows_log_path():
    return LEGACY_WINDOWS_LOG_PATH


# --- Container / host-root mapping (Docker Compose) ---

_HOST_ROOT_ENV = "SERVERMETRY_HOST_ROOT"
_CONTAINER_ENV = "SERVERMETRY_CONTAINER"
_TRUTHY = ("1", "true", "yes")


def host_root():
    """Return the mounted host rootfs, or ``""`` when not in host-root mode."""
    root = (os.environ.get(_HOST_ROOT_ENV) or "").strip().rstrip("/")
    if root and os.path.isdir(root):
        return root
    return ""


def in_container():
    """True when running as a containerized agent (Compose / Docker)."""
    if os.environ.get(_CONTAINER_ENV, "").strip().lower() in _TRUTHY:
        return True
    return bool(host_root())


def host_path(path):
    """Translate an absolute host path into the container namespace."""
    root = host_root()
    if not root:
        return path
    if not path:
        return root
    if not path.startswith("/"):
        path = "/" + path
    return root + path


def proc_path(rel=""):
    """Path under the host's ``/proc`` (or the container's if no host root)."""
    base = host_path("/proc")
    if not rel:
        return base
    return os.path.join(base, rel.lstrip("/"))


def mounts_path():
    """File that lists the *host* mount table when ``HOST_ROOT`` is set.

    ``/proc/self/mounts`` follows the container mount namespace even when
    ``/proc`` is bind-mounted. PID 1 on the host typically has the real table.
    """
    if host_root():
        candidate = proc_path("1/mounts")
        if os.path.isfile(candidate):
            return candidate
        return proc_path("mounts")
    return "/proc/mounts"
