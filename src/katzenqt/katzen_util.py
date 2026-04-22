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


def bacap_idx64(message_box_index: bytes) -> int:
    """Return the BACAP Idx64 counter from a 104-byte MessageBoxIndex blob.

    The layout is fixed by hpqc/bacap/bacap.go's
    ``MessageBoxIndex.MarshalBinary``: the first 8 bytes are ``Idx64`` as a
    little-endian ``uint64``, followed by three 32-byte fields
    (CurBlindingFactor, CurEncryptionKey, HKDFState). Ordering the indexes
    needs the integer view — byte comparison on a little-endian encoding
    gives the wrong answer (``0x00ff`` lex-compares below ``0x0100``).
    """
    return int.from_bytes(message_box_index[:8], 'little')
