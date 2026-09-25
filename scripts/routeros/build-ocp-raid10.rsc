# MikroTik RDS / RouterOS RAID10 builder
#
# Creates the RAID layout validated in the OpenShift lab:
#   nvme1 + nvme2 -> RAID1 mirror 0
#   nvme3 + nvme4 -> RAID1 mirror 1
#   nvme5 + nvme6 -> RAID1 mirror 2
#   nvme7 + nvme8 -> RAID1 mirror 3
#   four mirrors  -> RAID0 top-level array
#
# Lab geometry:
#   top-level RAID0 chunk: 256K
#   full stripe width:      1 MiB
#   compression:            disabled
#
# IMPORTANT:
# - This script is intended for EMPTY/UNASSIGNED disks.
# - It does not wipe disks and it refuses to overwrite existing RAID slot names.
# - Edit the variables in the script source if your device names or RAID names differ.
# - Formatting is intentionally a separate manual step; see format-xfs.commands.txt.

/system/script/remove [find where name="build-ocp-raid10"]
/system/script/add name=build-ocp-raid10 policy=read,write,policy,test source={
    :local topRaid "raid10"
    :local mirror0 "raid10-m0"
    :local mirror1 "raid10-m1"
    :local mirror2 "raid10-m2"
    :local mirror3 "raid10-m3"
    :local chunkSize "256K"

    :local d1 "nvme1"
    :local d2 "nvme2"
    :local d3 "nvme3"
    :local d4 "nvme4"
    :local d5 "nvme5"
    :local d6 "nvme6"
    :local d7 "nvme7"
    :local d8 "nvme8"

    :put "=================================================="
    :put " Building OpenShift Virtualization RAID10"
    :put "=================================================="
    :put ""
    :put "Checking NVMe devices..."

    :foreach d in={$d1;$d2;$d3;$d4;$d5;$d6;$d7;$d8} do={
        :local rows [/disk print detail as-value where slot=$d]
        :if ([:len $rows] = 0) do={
            :error ("Required disk not found: " . $d)
        }
        :put ("  OK: " . $d)
    }

    :foreach r in={$topRaid;$mirror0;$mirror1;$mirror2;$mirror3} do={
        :if ([:len [/disk find where slot=$r]] > 0) do={
            :error ("Refusing to continue because RAID slot already exists: " . $r)
        }
    }

    :put ""
    :put "All NVMe devices passed validation."
    :put ""

    :put ("Creating " . $topRaid . " RAID0...")
    /disk add type=raid slot=$topRaid raid-type=0 raid-device-count=4 raid-chunk-size=$chunkSize compress=no

    :put ("Creating " . $mirror0 . "...")
    /disk add type=raid slot=$mirror0 raid-type=1 raid-device-count=2 raid-master=$topRaid raid-role=0 mount-filesystem=no compress=no

    :put ("Creating " . $mirror1 . "...")
    /disk add type=raid slot=$mirror1 raid-type=1 raid-device-count=2 raid-master=$topRaid raid-role=1 mount-filesystem=no compress=no

    :put ("Creating " . $mirror2 . "...")
    /disk add type=raid slot=$mirror2 raid-type=1 raid-device-count=2 raid-master=$topRaid raid-role=2 mount-filesystem=no compress=no

    :put ("Creating " . $mirror3 . "...")
    /disk add type=raid slot=$mirror3 raid-type=1 raid-device-count=2 raid-master=$topRaid raid-role=3 mount-filesystem=no compress=no

    :put ""
    :put ("Assigning " . $d1 . " + " . $d2 . " -> " . $mirror0)
    /disk set [find where slot=$d1] raid-master=$mirror0 raid-role=0 mount-filesystem=no
    /disk set [find where slot=$d2] raid-master=$mirror0 raid-role=1 mount-filesystem=no

    :put ("Assigning " . $d3 . " + " . $d4 . " -> " . $mirror1)
    /disk set [find where slot=$d3] raid-master=$mirror1 raid-role=0 mount-filesystem=no
    /disk set [find where slot=$d4] raid-master=$mirror1 raid-role=1 mount-filesystem=no

    :put ("Assigning " . $d5 . " + " . $d6 . " -> " . $mirror2)
    /disk set [find where slot=$d5] raid-master=$mirror2 raid-role=0 mount-filesystem=no
    /disk set [find where slot=$d6] raid-master=$mirror2 raid-role=1 mount-filesystem=no

    :put ("Assigning " . $d7 . " + " . $d8 . " -> " . $mirror3)
    /disk set [find where slot=$d7] raid-master=$mirror3 raid-role=0 mount-filesystem=no
    /disk set [find where slot=$d8] raid-master=$mirror3 raid-role=1 mount-filesystem=no

    :put ""
    :put "RAID layout created."
    :put ""
    :put "=================================================="
    :put " Waiting for RAID1 synchronization"
    :put "=================================================="
    :put ""

    :local allClean false
    :while ($allClean = false) do={
        :local r0 [/disk print detail as-value where slot=$mirror0]
        :local r1 [/disk print detail as-value where slot=$mirror1]
        :local r2 [/disk print detail as-value where slot=$mirror2]
        :local r3 [/disk print detail as-value where slot=$mirror3]
        :local rt [/disk print detail as-value where slot=$topRaid]

        :local s0 ($r0->0->"state")
        :local s1 ($r1->0->"state")
        :local s2 ($r2->0->"state")
        :local s3 ($r3->0->"state")
        :local st ($rt->0->"state")

        :put "--------------------------------------------------"
        :put ($mirror0 . " = " . $s0)
        :put ($mirror1 . " = " . $s1)
        :put ($mirror2 . " = " . $s2)
        :put ($mirror3 . " = " . $s3)
        :put ($topRaid . " = " . $st)

        :if (($s0 = "clean") && ($s1 = "clean") && ($s2 = "clean") && ($s3 = "clean") && ($st = "clean")) do={
            :set allClean true
        } else={
            :delay 10s
        }
    }

    :put ""
    :put "=================================================="
    :put " RAID10 SYNCHRONIZATION COMPLETE"
    :put "=================================================="
    :put ""
    :put ("  " . $mirror0 . " = clean")
    :put ("  " . $mirror1 . " = clean")
    :put ("  " . $mirror2 . " = clean")
    :put ("  " . $mirror3 . " = clean")
    :put ("  " . $topRaid . " = clean")
    :put ""
    :put "RAID geometry:"
    :put "  RAID1 pairs:       4"
    :put ("  RAID0 chunk:       " . $chunkSize)
    :put "  Full stripe width: 1 MiB"
    :put "  Compression:       disabled"
    :put ""
    :put "Next command (DESTRUCTIVE; requires confirmation):"
    :put ""
    :put ("/disk format " . $topRaid . " file-system=xfs label=ocp-storage mbr-partition-table=no")
    :put ""
}
