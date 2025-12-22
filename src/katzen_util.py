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
