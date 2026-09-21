import time

# Last time any real chat/room generation activity happened -- read by the
# memory-scan idle-detection loop, written from several places across
# chat_stream and the Athena-room turn loop. A plain module-level float
# works correctly across modules via attribute access (`activity.last_activity_ts
# = ...`) without needing `global` in the writer -- `global` only rebinds a
# name in the writer's OWN module, which would silently create a second,
# disconnected copy instead of updating this shared one.
last_activity_ts = time.time()


def touch():
    global last_activity_ts
    last_activity_ts = time.time()
