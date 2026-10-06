# RAID / NVMe health monitoring (agent 1.4.9)

## What this fixes

A missing mirror member must be visible even when the remaining SSD reports SMART
PASSED. SMART and array redundancy are independent checks. No reconstruction,
formatting, disk removal, pool import, scrub or self-test is performed by the agent.

- Linux md: missing `[U_]` slots, `[expected/active]` mismatch, `(F)` members,
  inactive arrays, and single-member configured mirrors. Healthy resync/check is
  not itself a disk failure. Inactive IMSM/DDF metadata containers are excluded.
- ZFS: `zpool status -P` in the C locale reports DEGRADED/FAULTED/OFFLINE/UNAVAIL
  pools and known permanent data errors, including missing NVMe mirror members.
- SMART JSON: nonzero smartctl health bitmasks no longer discard diagnostics.
  NVMe critical warnings, media errors and exhausted endurance, plus ATA pending /
  offline-uncorrectable sectors, trigger attention even if overall SMART passes.
  These findings warrant investigation, not an automatic diagnosis that the
  physical drive is irreparably defective. Media-error counters may be historical.
- `raidCheckStatus` and `smartCheckStatus`: collected/unavailable/error. Missing
  tools, permissions, malformed output, timeouts and incomplete scans do not count
  as recovery. At most 32 drives; SMART work has a 20-second total deadline.

## Alerts

No manually created rule is required: a reported positive RAID or disk-health
count opens a CRITICAL RAID_DEGRADED / DISK_FAILING alert. Notifications use existing
global channels; email/chat delivery still requires configured channels. Concurrent
metric ingests are serialized per server/type to prevent duplicate default alerts.
Existing configured storage rules take precedence (including explicit disabled
rules as opt-outs). Test mode and maintenance keep their existing semantics.

A zero count resolves a default alert only when the relevant collector has usable,
nonempty, complete data. Missing telemetry or empty inventory never silently clears
an open hardware alert. Removing a monitored array intentionally may require manual
resolution. Unknown data is also preserved for ordinary storage rules.

## Installation and release

Deploy API support, then release/install the paired agent 1.4.9. Check Coolify's
LATEST_AGENT_VERSION override if one is configured. Create the client GitHub release
after merge; do not deploy unmerged agent files directly to production servers.
Version 1.4.8 is already used by the separate Docker-agent branch, hence 1.4.9.
All bootstrap/updater/install file lists include the new storage module.

Native Linux requires `smartmontools` with device-read permissions. ZFS hosts need
their distribution's `zpool` utility and permissions to query pools. The agent does
not install/change a host's ZFS stack. In host-root container mode, mounted mdstat
is usable; container SMART/ZFS queries are deliberately marked unavailable rather
than misrepresented as host health. Docker-agent integration remains separate.

## Verification

- Client: `python3 -m pytest tests/ -q`; `python3 -m bandit -r agent -c .bandit -ll -ii`;
  `shellcheck -S error -x agent/*.sh`.
- API: `bun run lint`, `bun run test`, `bun run build`.
- `scripts/e2e/storage-health.mjs` uses only the fixed Dev API and an existing QA key,
  creates/deletes its own server, and verifies critical alert, deduplication,
  unknown-data retention, healthy recovery and maintenance suppression.
- On the affected host, read `cat /proc/mdstat`, `zpool status -P` (if ZFS),
  `smartctl --scan -j`, and `smartctl -j -H -A -i /dev/DEVICE`. smartctl can return
  nonzero *because it detected a health problem*. Never run destructive repair
  commands merely to test monitoring.

## Remaining coverage / operational action

The exact cause and RAID type on node05 have not been verified by direct host
inspection. Software cannot infer a missing historical disk if neither RAID
metadata nor a prior stable-ID inventory records that it should exist.

Further coverage needs: persistent serial/WWN inventory for entirely disappeared
standalone disks, hardware-controller-specific tools, Btrfs/LVM RAID, stalled
resilver/rebuild and overdue scrubs, growing ZFS checksum/I/O counters, SMART trends,
filesystem read-only/I/O errors, inode exhaustion, thin-pool exhaustion and backup
age/restore verification. Arbitrary NVMe thermal thresholds need device-specific
limits; critical firmware warning bits are already used here.

For a long-degraded mirror: verify current backups first, identify the exact
failed/missing member and controller/path condition, replace/rebuild under a
separate maintenance procedure, then confirm full redundancy. This monitoring
change neither repairs the RAID nor recovers the missing redundancy.
