import asyncio
import traceback

def create_task(coro):
    """Wrapper around asyncio.create_task() that logs exceptions"""
    def throw_if_needed(task):
        try:
            result = task.result()
        except Exception:
            print(f"create_task {task.exception()}")
            traceback.print_exc()
            raise
    task = asyncio.create_task(coro)
    task.add_done_callback(throw_if_needed)
    return task


# Note: the BACAP Idx64 counter used to live in a local `bacap_idx64()`
# helper here; it peeked at the first 8 bytes of the MessageBoxIndex blob
# as a little-endian uint64, which coupled the Python code to
# hpqc/bacap/bacap.go's binary layout. Replaced by the daemon-owned
# ThinClient.get_message_box_index_counter() API so MessageBoxIndex stays
# opaque on this side.
