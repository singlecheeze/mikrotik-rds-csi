# Installs the verify-ocp-storage RouterOS script.
# Lab default pool: raid10. Edit poolSlot if needed.

/system/script/remove [find where name="verify-ocp-storage"]
/system/script/add name=verify-ocp-storage policy=read,write,policy,test source={
    :local poolSlot "raid10"
    :local expectedFs "xfs"
    :local expectedState "clean"

    :local rows [/disk print detail as-value where slot=$poolSlot]

    :if ([:len $rows] = 0) do={
        :error ($poolSlot . " not found.")
    }

    :local state ($rows->0->"state")
    :local fs ($rows->0->"fs")
    :local mounted ($rows->0->"mounted")
    :local size ($rows->0->"size")
    :local free ($rows->0->"free")

    :put "=================================================="
    :put " OpenShift Storage Datastore"
    :put "=================================================="
    :put ("Pool:       " . $poolSlot)
    :put ("State:      " . $state)
    :put ("Filesystem: " . $fs)
    :put ("Mounted:    " . $mounted)
    :put ("Size:       " . $size)
    :put ("Free:       " . $free)
    :put ""

    :if ($state != $expectedState) do={
        :error ($poolSlot . " is not " . $expectedState . ": " . $state)
    }

    :if ($fs != $expectedFs) do={
        :error ("Expected " . $expectedFs . " but found: " . $fs)
    }

    :if ($mounted != true) do={
        :error ($poolSlot . " is not mounted.")
    }

    :put "Datastore validation successful."
}
