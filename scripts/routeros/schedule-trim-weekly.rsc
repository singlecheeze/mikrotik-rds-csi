# Installs/replaces a weekly scheduler entry that runs trim-ocp-storage at 03:00.
# Import trim-ocp-storage.rsc first.

/system/scheduler/remove [find where name="trim-ocp-storage-weekly"]
/system/scheduler/add \
    name=trim-ocp-storage-weekly \
    interval=7d \
    start-time=03:00:00 \
    on-event="/system/script/run trim-ocp-storage" \
    policy=read,write,policy,test
