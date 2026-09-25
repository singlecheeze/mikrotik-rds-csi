# Legacy/manual NVMe/TCP LUN helper used before dynamic CSI provisioning.
#
# The CSI driver makes this script unnecessary for normal PVC provisioning, but it
# is retained as a useful troubleshooting/example tool for creating one file-backed
# block volume directly on the RDS.
#
# Lab defaults shown below match the validated environment. Edit before use.

/system/script/remove [find where name="create-ocp-vm-lun"]
/system/script/add name=create-ocp-vm-lun policy=read,write,policy,test source={
    :local poolSlot "raid10"
    :local poolPath "raid10"
    :local volumeName "vm-storage-001"
    :local volumeSize "1T"
    :local targetAddress "172.16.100.125"
    :local targetPort 4420
    :local nqn "nqn.2026-09.com.mikrotik:rds2216.vm-storage-001"

    :local backingFile ($poolPath . "/" . $volumeName . ".img")
    :local raidRows [/disk print detail as-value where slot=$poolSlot]

    :if ([:len $raidRows] = 0) do={
        :error ($poolSlot . " does not exist.")
    }

    :local raidState ($raidRows->0->"state")
    :local raidFs ($raidRows->0->"fs")
    :local mounted ($raidRows->0->"mounted")

    :if ($raidState != "clean") do={
        :error ($poolSlot . " is not clean: " . $raidState)
    }

    :if ($raidFs != "xfs") do={
        :error ($poolSlot . " is not XFS: " . $raidFs)
    }

    :if ($mounted != true) do={
        :error ($poolSlot . " is not mounted.")
    }

    :if ([:len [/disk find where slot=$volumeName]] > 0) do={
        :error ("Disk object already exists: " . $volumeName)
    }

    :put "=================================================="
    :put " Creating OpenShift Virtualization block volume"
    :put "=================================================="
    :put ("Name:    " . $volumeName)
    :put ("Size:    " . $volumeSize)
    :put ("Backing: " . $backingFile)
    :put ("NQN:     " . $nqn)
    :put ""

    /disk add \
        type=file \
        slot=$volumeName \
        file-path=$backingFile \
        file-size=$volumeSize \
        mount-filesystem=no \
        mount-read-only=no \
        compress=no

    :delay 3s

    :local lun [/disk find where slot=$volumeName]
    :if ([:len $lun] = 0) do={
        :error "File-backed block volume creation failed."
    }

    /disk set $lun \
        nvme-tcp-export=yes \
        nvme-tcp-server-port=$targetPort \
        nvme-tcp-server-nqn=$nqn

    :delay 3s

    :local rows [/disk print detail as-value where slot=$volumeName]
    :local exported ($rows->0->"nvme-tcp-export")
    :local actualNqn ($rows->0->"nvme-tcp-server-nqn")
    :local port ($rows->0->"nvme-tcp-server-port")

    :if ($exported != true) do={
        :error "NVMe/TCP export was not enabled."
    }

    :put ""
    :put "=================================================="
    :put " VM BLOCK VOLUME READY"
    :put "=================================================="
    :put ("Target: " . $targetAddress . ":" . $port)
    :put ("NQN:    " . $actualNqn)
    :put ("Size:   " . $volumeSize)
    :put ("File:   " . $backingFile)
    :put ""
    :put "Linux discovery command:"
    :put ("nvme discover -t tcp -a " . $targetAddress . " -s " . $port)
    :put ""
}
