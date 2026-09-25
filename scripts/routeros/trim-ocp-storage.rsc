# Installs the trim-ocp-storage RouterOS script.
# The script refuses to TRIM unless the pool exists, is clean, is XFS, and is mounted.
# Lab default pool: raid10. Edit poolSlot if needed.

/system/script/remove [find where name="trim-ocp-storage"]
/system/script/add name=trim-ocp-storage policy=read,write,policy,test source={
    :local poolSlot "raid10"
    :local expectedFs "xfs"
    :local expectedState "clean"

    :local rows [/disk print detail as-value where slot=$poolSlot]

    :if ([:len $rows] = 0) do={
        :error ($poolSlot . " does not exist.")
    }

    :local state ($rows->0->"state")
    :local fs ($rows->0->"fs")
    :local mounted ($rows->0->"mounted")

    :if ($state != $expectedState) do={
        :error ("Skipping TRIM: " . $poolSlot . " state is " . $state)
    }

    :if ($fs != $expectedFs) do={
        :error ("Skipping TRIM: expected " . $expectedFs . " but found " . $fs)
    }

    :if ($mounted != true) do={
        :error ("Skipping TRIM: " . $poolSlot . " is not mounted.")
    }

    :log info ("Starting TRIM on OpenShift VM datastore " . $poolSlot)
    /disk trim $poolSlot
    :log info ("Completed TRIM on OpenShift VM datastore " . $poolSlot)
}
