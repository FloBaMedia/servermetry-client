import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch, mock_open
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "agent"))
from services import storage


class StorageTests(unittest.TestCase):
    def test_missing_nvme_mirror_member_even_without_failed_flag(self):
        for detail in ["100 blocks [2/1] [U_]", "100 blocks [2/1]"]:
            raid = storage.parse_mdstat("md0 : active raid1 nvme0n1p3[0]\n " + detail)[0]
            self.assertTrue(raid["degraded"])
            self.assertEqual(raid["devices"], ["nvme0n1p3"])

    def test_faulted_member_and_inactive_array(self):
        for header in ["active raid1 nvme0n1p3[0] nvme1n1p3[1](F)", "inactive nvme0n1p3[0](S)"]:
            self.assertTrue(storage.parse_mdstat("md0 : " + header)[0]["degraded"])

    def test_recovery_preserves_degradation_but_healthy_resync_is_not_failure(self):
        for marker, degraded in [("[2/1] [U_]", True), ("[2/2] [UU]", False)]:
            raid = storage.parse_mdstat("md0 : active raid1 sda[0] sdb[1]\n" + marker + "\nresync = 25%")[0]
            self.assertEqual(raid["degraded"], degraded)
            self.assertEqual(raid["state"], "recovering")

    def test_single_member_mirror_has_no_redundancy(self):
        self.assertTrue(storage.parse_mdstat("md0 : active raid1 sda[0]\n[1/1] [U]")[0]["degraded"])

    def test_imsm_metadata_container_is_not_a_failed_array(self):
        self.assertFalse(storage.parse_mdstat("md127 : inactive sda[0](S)\n100 blocks super external:imsm")[0]["degraded"])

    def test_no_md_arrays(self):
        self.assertEqual(storage.parse_mdstat("Personalities : [raid1]\nunused devices: <none>"), [])

    def test_zfs_degraded_mirror_and_healthy_pool(self):
        pools = storage.parse_zpool_status("""  pool: rpool
 state: DEGRADED
config:
 NAME STATE READ WRITE CKSUM
 rpool DEGRADED 0 0 0
 mirror-0 DEGRADED 0 0 0
 /dev/nvme0n1p3 ONLINE 0 0 0
 /dev/nvme1n1p3 UNAVAIL 0 0 0
errors: No known data errors
  pool: backup
 state: ONLINE
 /dev/sda ONLINE 0 0 0
errors: No known data errors
""")
        self.assertTrue(pools[0]["degraded"])
        self.assertEqual(pools[0]["failedDevices"], ["/dev/nvme1n1p3"])
        self.assertFalse(pools[1]["degraded"])

    def test_zfs_permanent_data_errors(self):
        self.assertTrue(storage.parse_zpool_status("pool: rpool\nstate: ONLINE\nerrors: Permanent errors have been detected")[0]["degraded"])

    @patch("services.storage.subprocess.run")
    def test_smart_failed_exit_code_preserves_json(self, run):
        raw = json.dumps({"smart_status": {"passed": False}})
        run.return_value = subprocess.CompletedProcess([], 8, raw)
        result, code = storage.run_readonly(["smartctl", "-j", "/dev/nvme0"], 1)
        self.assertEqual(code, 8)
        self.assertEqual(storage.smart_disk(json.loads(result), "/dev/nvme0")["health"], "FAILED")
        self.assertFalse(run.call_args.kwargs["check"])

    def test_nvme_health_and_endurance_flags(self):
        for log in [{"critical_warning": 1}, {"media_errors": 1}, {"percentage_used": 100}]:
            self.assertEqual(storage.smart_disk({"smart_status": {"passed": True}, "nvme_smart_health_information_log": log}, "/dev/nvme0")["health"], "FAILED")
        self.assertEqual(storage.smart_disk({}, "/dev/nvme0")["health"], "UNKNOWN")

    def test_ata_pending_sectors(self):
        data = {"smart_status": {"passed": True}, "ata_smart_attributes": {"table": [{"id": 197, "raw": {"value": 2}}]}}
        self.assertEqual(storage.smart_disk(data, "/dev/sda")["health"], "FAILED")

    @patch("services.storage.shutil.which", return_value="/usr/sbin/smartctl")
    @patch("services.storage.run_readonly")
    def test_collection_exit_bits_and_device_types(self, run, _which):
        run.side_effect = [(json.dumps({"devices": [{"name": "/dev/nvme0", "type": "nvme"}]}), 0), (json.dumps({"smart_status": {"passed": False}}), 8)]
        disks, status = storage.read_smart_disks()
        self.assertEqual(status, "collected")
        self.assertEqual(disks[0]["health"], "FAILED")
        self.assertIn("nvme", run.call_args.args[0])
        run.side_effect = [(json.dumps({"devices": [{"name": "/dev/sda"}]}), 0), ('{}', 2)]
        disks, status = storage.read_smart_disks()
        self.assertEqual(status, "error")
        self.assertEqual(disks[0]["health"], "UNKNOWN")

    @patch("services.storage.shutil.which", return_value=None)
    def test_missing_smartctl_is_unknown_not_healthy(self, _which):
        self.assertEqual(storage.read_smart_disks(), ([], "unavailable"))

    @patch.dict(os.environ, {"SERVERMETRY_HOST_ROOT": "/host"})
    @patch("builtins.open", new_callable=mock_open, read_data="md0 : active raid1 nvme0n1p3[0]\n[2/1] [U_]")
    def test_host_mdstat_does_not_claim_container_smart_health(self, opened):
        data = storage.read_disk_health()
        opened.assert_called_once_with("/host/proc/mdstat", encoding="utf-8")
        self.assertEqual(data["raidDegradedCount"], 1)
        self.assertEqual(data["smartCheckStatus"], "unavailable")

    def test_updater_and_installers_include_storage_module(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        for name in ["agent/agent.py", "agent/install.sh", "agent/install-windows.ps1", "agent/services/updater.py"]:
            self.assertIn('"services/storage.py"', (root / name).read_text())


@patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "SERVERMETRY_HOST_ROOT": ""})
@patch("services.storage.shutil.which")
@patch("services.storage.run_readonly")
@patch("services.storage.read_smart_disks", return_value=([], "unavailable"))
@patch("builtins.open", new_callable=mock_open, read_data="Personalities :")
def test_cron_resolves_zpool_in_sbin(_open, _smart, run, which):
    which.side_effect = lambda name, path: "/usr/sbin/" + name if "/usr/sbin" in path.split(os.pathsep) else None
    run.return_value = ("  pool: rpool\n state: ONLINE\nerrors: No known data errors\n", 0)
    result = storage.read_disk_health()
    assert result["raidCheckStatus"] == "collected"
    assert result["raids"][0]["name"] == "zfs:rpool"
    run.assert_called_once_with(["/usr/sbin/zpool", "status", "-P"], 5)
    assert storage.find_storage_tool("smartctl") == "/usr/sbin/smartctl"
