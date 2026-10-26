"""为可能阻塞在系统驱动中的串口打开操作提供有界等待。"""

import threading
import time


_pending = set()
_pending_lock = threading.Lock()


class SerialOpenBlocked(RuntimeError):
    """驱动打开未返回，或仍有上次打开等待清理。"""


def open_serial_bounded(factory, port, timeout, check_stop):
    key = port.strip().casefold()
    with _pending_lock:
        if key in _pending:
            raise SerialOpenBlocked(f"串口 {port} 上一次驱动打开操作尚未返回，正在等待其结束并清理")
        _pending.add(key)
    ready = threading.Event()
    lock = threading.Lock()
    state = {"abandoned": False}

    def open_port():
        connection = None
        try:
            connection = factory()
            with lock:
                if not state["abandoned"]:
                    state["connection"] = connection
                    connection = None
        except Exception as exc:
            with lock:
                state["error"] = exc
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                with _pending_lock:
                    _pending.discard(key)
                ready.set()

    thread = threading.Thread(target=open_port, name=f"open-{port}", daemon=True)
    try:
        thread.start()
    except Exception:
        with _pending_lock:
            _pending.discard(key)
        raise
    deadline = time.monotonic() + timeout
    try:
        while True:
            check_stop()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SerialOpenBlocked(
                    f"打开串口 {port} 的驱动调用超过 {timeout:g} 秒未返回；"
                    "读取 timeout 不限制打开耗时，迟到的连接将自动关闭"
                )
            if ready.wait(min(0.05, remaining)):
                check_stop()
                with lock:
                    if "error" in state:
                        raise state["error"]
                    return state.pop("connection")
    finally:
        with lock:
            state["abandoned"] = True
            connection = state.pop("connection", None)
        if connection is not None:
            connection.close()
