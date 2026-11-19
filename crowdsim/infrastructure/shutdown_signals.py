"""Process-local shutdown handlers; never change defaults outside this process."""
from contextlib import contextmanager
import signal
import threading

@contextmanager
def shutdown_signals(callback):
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    signals = (signal.SIGINT, signal.SIGTERM)
    previous = {s: signal.getsignal(s) for s in signals}
    mask = None
    def handler(signum, _frame):
        callback()
    try:
        for s in signals:
            signal.signal(s, handler)
        if hasattr(signal, 'pthread_sigmask'):
            mask = signal.pthread_sigmask(signal.SIG_UNBLOCK, set(signals))
        yield
    finally:
        for s, old in previous.items():
            signal.signal(s, old)
        if mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
