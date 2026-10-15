# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""module11/demo/state.py : flock + temp file + os.replace,
lock-free readers, a 10 s lock timeout, crash-safe lock release, and the shared demo wording."""
import fcntl
import json
import multiprocessing as mp
import os
import signal
import time

import pytest

from module11.demo import state, wording

CTX = mp.get_context("fork")


def _writer(path, key, n):
    for i in range(n):
        state.update(lambda doc, i=i: {**doc, key: i}, path)


def _reader(path, stop_at, errors):
    while time.time() < stop_at:
        try:
            state.read(path)
        except ValueError:
            errors.value += 1


def test_two_writers_keep_both_keys_and_a_reader_never_sees_a_torn_file(tmp_path):
    path = tmp_path / "demo-state.json"
    errors = CTX.Value("i", 0)
    reader = CTX.Process(target=_reader, args=(path, time.time() + 6, errors))
    reader.start()
    writers = [CTX.Process(target=_writer, args=(path, key, 200)) for key in ("a", "b")]
    for w in writers:
        w.start()
    for w in writers:
        w.join(60)
        assert w.exitcode == 0
    reader.join(30)
    assert errors.value == 0
    doc = json.loads(path.read_text())
    assert doc["a"] == 199 and doc["b"] == 199


def _hold(path, seconds, ready):
    fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    ready.set()
    time.sleep(seconds)


def test_a_lock_held_too_long_raises_and_leaves_the_file_unchanged(tmp_path):
    path = tmp_path / "demo-state.json"
    state.update(lambda doc: {"traffic": {"rolloutId": "r-1"}}, path)
    before = path.read_bytes()
    ready = CTX.Event()
    holder = CTX.Process(target=_hold, args=(path, 30, ready))
    holder.start()
    try:
        assert ready.wait(10)
        started = time.monotonic()
        with pytest.raises(state.DemoStateError):
            state.update(lambda doc: {**doc, "x": 1}, path, timeout=10)
        assert 9.5 <= time.monotonic() - started < 15
        assert path.read_bytes() == before
    finally:
        holder.kill()
        holder.join()


def test_a_sigkilled_holder_releases_the_lock(tmp_path):
    path = tmp_path / "demo-state.json"
    ready = CTX.Event()
    holder = CTX.Process(target=_hold, args=(path, 300, ready))
    holder.start()
    assert ready.wait(10)
    os.kill(holder.pid, signal.SIGKILL)
    holder.join()
    state.update(lambda doc: {**doc, "after": True}, path, timeout=2)
    assert state.read(path) == {"after": True}
    assert (tmp_path / "demo-state.json.lock").exists()       # created once, never deleted


def test_a_missing_file_reads_as_empty(tmp_path):
    assert state.read(tmp_path / "absent.json") == {}
    assert state.update(lambda doc: {**doc, "k": 1}, tmp_path / "new" / "demo-state.json") == {"k": 1}


def test_a_failing_update_function_leaves_the_file_unchanged(tmp_path):
    path = tmp_path / "demo-state.json"
    state.set_key("k", 1, path)
    with pytest.raises(ZeroDivisionError):
        state.update(lambda doc: {**doc, "k": 1 / 0}, path)
    assert state.read(path) == {"k": 1}
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_payments_tweak_is_one_shared_constant():
    prompt = "You are the Companion. Keep the diagnosis brief: at most 60 words. Cite evidence."
    edited = wording.apply_edit(prompt, wording.PAYMENTS_TWEAK)
    assert edited.count(wording.PAYMENTS_TWEAK) == 1
    assert edited.startswith("You are the Companion. Keep the diagnosis brief: at most 60 words. "
                             + wording.PAYMENTS_TWEAK + " Cite")
    with pytest.raises(ValueError):
        wording.apply_edit("no anchor here", wording.PAYMENTS_TWEAK)
