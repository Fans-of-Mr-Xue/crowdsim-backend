"""Production shutdown guards: process-local signals and owned worker draining."""
import signal
import threading
from types import SimpleNamespace
from crowdsim.infrastructure.shutdown_signals import shutdown_signals
from crowdsim.experiments.orchestrator import ExperimentOrchestrator
from crowdsim.experiments.repository import CrowdSimRepository
from crowdsim.experiments.controller_registry import ControllerRegistry

class Gateway:
    def __init__(self): self.closed = 0; self.listeners = []
    def add_listener(self, x): self.listeners.append(x)
    def remove_listener(self, x): self.listeners.remove(x)
    def close(self): self.closed += 1


def test_signal_mask_changes_only_term_int_and_restores_handlers():
    before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    called = []
    with shutdown_signals(lambda: called.append(True)):
        after = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        assert after == before - {signal.SIGINT, signal.SIGTERM}
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert called == [True]
    assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == before
    assert all(signal.getsignal(s) == h for s, h in handlers.items())


def test_worker_is_joined_and_no_run_executes_after_close():
    g = Gateway(); o = ExperimentOrchestrator(CrowdSimRepository(), g, ControllerRegistry())
    o._ensure_worker()
    assert o._worker.is_alive()
    o.close()
    assert not o._worker.is_alive() and g.closed == 1 and not g.listeners


def test_close_wakes_owned_terminal_without_marking_completed():
    g = Gateway(); repo = CrowdSimRepository(); o = ExperimentOrchestrator(repo, g, ControllerRegistry())
    terminal = threading.Event()
    o._active = {'terminal': terminal, 'accept_frames': True}
    o.close()
    assert terminal.is_set() and o._active['shutdown_requested']
    assert not o._active['accept_frames']


def test_non_main_thread_does_not_install_or_change_signals():
    out = []
    def work():
        before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        with shutdown_signals(lambda: out.append('unexpected')):
            out.append(signal.pthread_sigmask(signal.SIG_BLOCK, set()) == before)
    t = threading.Thread(target=work); t.start(); t.join()
    assert out == [True]


def test_close_does_not_execute_already_queued_recovery(monkeypatch):
    import queue
    g = Gateway(); o = ExperimentOrchestrator(CrowdSimRepository(), g, ControllerRegistry())
    called = []
    monkeypatch.setattr(o, '_restore_observation_run', lambda item: called.append(item))
    o._queue.put({'recover': True})
    o._shutdown.set()
    o._queue.put(None)
    o._worker_loop()
    assert called == [] and o._processing is None
    assert o._queue.unfinished_tasks == 0
