# RouterOS storage preparation and maintenance helpers

These files are optional RDS administration helpers. They are **not required by the CSI pods at runtime**. They capture the RouterOS storage setup and maintenance workflow used while validating the driver.

The examples default to the validated lab layout:

- physical disks: `nvme1` through `nvme8`
- RAID10 implementation: four RAID1 mirrors striped by a top-level RAID0
- top-level slot: `raid10`
- mirror slots: `raid10-m0` through `raid10-m3`
- RAID0 chunk: `256K`
- XFS label: `ocp-storage`
- NVMe/TCP data address in the manual LUN example: `172.16.100.125`
- NVMe/TCP port: `4420`

Edit the variables near the top of each RouterOS script before use on a different RDS.

## Files

| File | Purpose |
|---|---|
| `build-ocp-raid10.rsc` | Adds the `build-ocp-raid10` RouterOS script. Validates the eight disks, creates four RAID1 mirrors under a RAID0 top-level array, assigns the disk pairs, and waits for `clean` state. |
| `format-xfs.commands.txt` | Manual destructive XFS format command. Kept out of an auto-run script because RouterOS requires confirmation. |
| `verify-ocp-storage.rsc` | Adds `verify-ocp-storage`, which checks the configured pool is present, `clean`, XFS, and mounted. |
| `trim-ocp-storage.rsc` | Adds `trim-ocp-storage`, with safety checks before issuing `/disk trim`. |
| `schedule-trim-weekly.rsc` | Adds the weekly 03:00 scheduler entry used in the lab. Import the TRIM script first. |
| `create-ocp-vm-lun-example.rsc` | Legacy/manual file-backed NVMe/TCP LUN creator retained for troubleshooting. Dynamic CSI provisioning normally replaces this workflow. |
| `wipe-quick-nvme.commands.txt` | Manual, destructive `wipe-quick` commands for the eight lab disks. Intended only for complete rebuild/reset scenarios. |

## Importing an `.rsc` helper

Upload the `.rsc` file to the RDS, then import it. Example:

```routeros
/import file-name=build-ocp-raid10.rsc
/system/script/run build-ocp-raid10
```

For the validation helper:

```routeros
/import file-name=verify-ocp-storage.rsc
/system/script/run verify-ocp-storage
```

For weekly TRIM:

```routeros
/import file-name=trim-ocp-storage.rsc
/import file-name=schedule-trim-weekly.rsc
```

Run TRIM once manually before relying on the scheduler:

```routeros
/system/script/run trim-ocp-storage
```

## Lab build sequence

For a new/empty RDS using the validated eight-disk layout:

1. If reusing disks, manually clear old metadata with `wipe-quick-nvme.commands.txt` after removing old RAID objects.
2. Import `build-ocp-raid10.rsc` and run `build-ocp-raid10`.
3. Wait for the script to report all four mirrors and the top-level array as `clean`.
4. Run the command in `format-xfs.commands.txt` and manually confirm the destructive format.
5. Import and run `verify-ocp-storage.rsc`.
6. Import `trim-ocp-storage.rsc`; optionally import `schedule-trim-weekly.rsc` after a successful manual TRIM.
7. Configure the CSI driver to point at the resulting pool and NVMe/TCP data address.

The CSI driver itself creates and removes file-backed NVMe/TCP volumes through REST; `create-ocp-vm-lun-example.rsc` is therefore only a manual troubleshooting/reference tool.

## RouterOS references

- ROSE storage / nested RAID / NVMe over TCP: https://help.mikrotik.com/docs/spaces/ROS/pages/259031065/ROSE-storage
- Disk commands, including `trim`: https://help.mikrotik.com/docs/spaces/ROS/pages/91193346/Disks

The lab used RouterOS 7.24.4. Review current RouterOS documentation before applying these helpers to a substantially different release.
