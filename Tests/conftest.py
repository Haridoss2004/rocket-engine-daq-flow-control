import os
import shutil
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from Configuration.defaults import default_config          # noqa: E402
from Core.engine import DaqEngine                           # noqa: E402
from State_Machine.sequencer import State                   # noqa: E402


def fast_config(results_dir):
    """Default configuration with shorter sequence timings so the tests run quickly."""
    cfg = default_config()
    cfg["logging"]["results_dir"] = results_dir
    cfg["logging"]["hf_log_mode"] = "fft_channel"
    cfg["sequence"].update(hold_duration_s=2.0, ramp_duration_s=2.0)
    return cfg


@pytest.fixture
def tmpdir_results():
    d = tempfile.mkdtemp(prefix="rocketdaq_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def cfg(tmpdir_results):
    return fast_config(tmpdir_results)


@pytest.fixture
def engine(cfg, tmpdir_results):
    e = DaqEngine(cfg, root_dir=tmpdir_results)
    yield e
    e.stop()


def goto(e, state, timeout=30.0):
    """Drive the synchronous engine until `state` is reached (issuing operator commands)."""
    order = [State.PRECHECK, State.READY, State.START, State.RAMP, State.HOLD, State.SHUTDOWN,
             State.COMPLETE]
    if e.state == State.IDLE and (state in order or state == State.ABORT):
        e.run_for(0.2)
        ok, msg = e.cmd_start()
        assert ok, msg
    end = e.t + timeout
    fired = False
    while e.t < end and e.state != state:
        if e.state == State.READY and not fired and (state == State.ABORT or
                                                     order.index(state) > order.index(State.READY)):
            ok, msg = e.cmd_fire()
            assert ok, msg
            fired = True
        e.run_for(e.dt_block)
    assert e.state == state, f"expected {state.value}, engine is in {e.state.value}"
    return e
