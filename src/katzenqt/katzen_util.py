import asyncio
import logging

logger = logging.getLogger("katzen.util")

def create_task(coro):
    """Wrapper around asyncio.create_task() that logs exceptions"""
    def throw_if_needed(task):
        if task.cancelled():
            return  # cancellation is expected on shutdown, not an error
        exc = task.exception()
        if exc is not None:
            # Log the failure but do NOT re-raise: a done-callback's raise
            # only surfaces as a spurious asyncio "Exception in callback"
            # traceback (seen on transient kpclientd link drops during a
            # bounce). logger.error's exc_info already preserves the
            # traceback through the normal logging configuration.
            logger.error("create_task: unhandled exception in %s", task, exc_info=exc)
    task = asyncio.create_task(coro)
    task.add_done_callback(throw_if_needed)
    return task


# Note: the BACAP Idx64 counter used to live in a local `bacap_idx64()`
# helper here; it peeked at the first 8 bytes of the MessageBoxIndex blob
# as a little-endian uint64, which coupled the Python code to
# hpqc/bacap/bacap.go's binary layout. Replaced by the daemon-owned
# ThinClient.get_message_box_index_counter() API so MessageBoxIndex stays
# opaque on this side.
