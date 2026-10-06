"""Host-root mapping and container-mode guards for Docker Compose."""

import os
import sys
import tempfile
import unittest
from unittest import mock

_AGENT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent")
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

from models.paths import host_path, host_root, in_container, mounts_path, proc_path  # noqa: E402
from services.config_applier import apply_config  # noqa: E402
from services import updater as u  # noqa: E402
from services.linux import _read_disk_usages, _read_pending_updates  # noqa: E402


def _clear_container_env():
    os.environ.pop("SERVERMETRY_HOST_ROOT", None)
    os.environ.pop("SERVERMETRY_CONTAINER", None)


class TestHostRoot(unittest.TestCase):
    def tearDown(self):
        _clear_container_env()

    def test_passthrough_without_root(self):
        _clear_container_env()
        self.assertEqual(host_root(), "")
        self.assertEqual(host_path("/proc/stat"), "/proc/stat")
        self.assertEqual(proc_path("stat"), "/proc/stat")
        self.assertEqual(mounts_path(), "/proc/mounts")
        self.assertFalse(in_container())

    def test_maps_under_mounted_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"SERVERMETRY_HOST_ROOT": tmp}):
                self.assertEqual(host_root(), tmp)
                self.assertEqual(host_path("/proc/stat"), tmp + "/proc/stat")
                self.assertEqual(proc_path("stat"), os.path.join(tmp, "proc", "stat"))
                self.assertTrue(in_container())

    def test_missing_root_falls_back(self):
        with mock.patch.dict(os.environ, {"SERVERMETRY_HOST_ROOT": "/nonexistent-host-root-xyz"}):
            self.assertEqual(host_root(), "")
            self.assertEqual(host_path("/proc/stat"), "/proc/stat")

    def test_container_flag_without_host_root(self):
        _clear_container_env()
        with mock.patch.dict(os.environ, {"SERVERMETRY_CONTAINER": "1"}):
            self.assertTrue(in_container())

    def test_mounts_path_prefers_pid1(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "proc", "1"))
            open(os.path.join(tmp, "proc", "1", "mounts"), "w").close()
            with mock.patch.dict(os.environ, {"SERVERMETRY_HOST_ROOT": tmp}):
                self.assertEqual(mounts_path(), os.path.join(tmp, "proc", "1", "mounts"))


class TestDiskUsagesHostRoot(unittest.TestCase):
    def tearDown(self):
        _clear_container_env()

    def test_statvfs_uses_host_prefixed_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "proc", "1"))
            data = os.path.join(tmp, "data")
            os.makedirs(data)
            with open(os.path.join(tmp, "proc", "1", "mounts"), "w") as f:
                f.write("/dev/sda1 /data ext4 rw,relatime 0 0\n")
            with mock.patch.dict(os.environ, {"SERVERMETRY_HOST_ROOT": tmp}):
                disks = _read_disk_usages()
            mountpoints = [d["mountpoint"] for d in disks]
            self.assertIn("/data", mountpoints)
            self.assertTrue(all(not p.startswith(tmp) for p in mountpoints))

    def test_pending_updates_skipped_with_host_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"SERVERMETRY_HOST_ROOT": tmp}):
                self.assertEqual(_read_pending_updates(), (None, None, None))


class TestContainerGuards(unittest.TestCase):
    def tearDown(self):
        _clear_container_env()

    def test_apply_config_skips_host_mutations(self):
        with mock.patch.dict(os.environ, {"SERVERMETRY_CONTAINER": "1"}):
            with mock.patch("services.config_applier.apply_timezone") as tz, \
                 mock.patch("services.config_applier.update_schedule") as sched:
                interval = apply_config({
                    "timezone": "Europe/Berlin",
                    "reportIntervalSeconds": 120,
                })
        tz.assert_not_called()
        sched.assert_not_called()
        self.assertEqual(interval, 120)

    def test_updater_skipped_in_container(self):
        with mock.patch.dict(os.environ, {"SERVERMETRY_CONTAINER": "1"}):
            result = u.check_and_update(force=True)
        self.assertEqual(result, "skipped")


if __name__ == "__main__":
    unittest.main()
