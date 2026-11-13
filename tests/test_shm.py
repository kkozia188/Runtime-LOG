"""C++ writer (shm_ring.hpp via shm_bench) <-> Python reader interoperability and torn-read safety."""

import os
import select
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from runtime_log.shm import SHM_DIR, ShmReader

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "ros/runtime_log_recorder"

pytestmark = pytest.mark.skipif(shutil.which("g++") is None or not SHM_DIR.is_dir(), reason="needs g++ and /dev/shm")


@pytest.fixture(scope="module")
def bench(tmp_path_factory):
    binary = tmp_path_factory.mktemp("bin") / "shm_bench"
    subprocess.run(
        ["g++", "-O2", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(PKG / "include"), str(PKG / "tools/shm_bench.cpp"), "-o", str(binary)],
        check=True,
    )
    return binary


def segment_name(tag: str) -> str:
    return f"/runtime-log.test-{os.getpid()}-{tag}"


def test_header_fields_and_values(bench):
    name = segment_name("basic")
    try:
        out = subprocess.run([str(bench), "--name", name, "--fields", "30", "--slots", "64", "--cycles", "50", "--period-us", "0", "--pattern"],
                             check=True, capture_output=True, text=True).stdout
        assert "write_ns" in out
        reader = ShmReader(SHM_DIR / name[1:])
        assert reader.writer_pid > 0 and reader.writer_name == "shm_bench" and reader.writer_state == "inactive"
        assert reader.fields[0] == "axis0/v0" and reader.fields[13] == "axis1/v1" and len(reader.fields) == 30
        assert reader.slot_size % 64 == 0 and reader.header_size % 4096 == 0
        batch = reader.read()
        assert batch.head == 50
        assert batch.seqs == list(range(50 - 64 + 2 if 50 > 62 else 1, 51))
        for seq, values in zip(batch.seqs, batch.values):
            assert values == tuple(seq * 1000.0 + i for i in range(30))
        assert reader.read().seqs == []  # nothing new
        reader.close()
    finally:
        (SHM_DIR / name[1:]).unlink(missing_ok=True)


def test_reader_reports_overwritten_cycles_as_gaps(bench):
    name = segment_name("gap")
    try:
        subprocess.run([str(bench), "--name", name, "--fields", "4", "--slots", "16", "--cycles", "100", "--period-us", "0", "--pattern"], check=True, capture_output=True)
        reader = ShmReader(SHM_DIR / name[1:])
        reader.next = 1  # pretend we had read nothing since the start
        batch = reader.read()
        assert batch.seqs == list(range(86, 101)) and batch.gaps == 85
    finally:
        (SHM_DIR / name[1:]).unlink(missing_ok=True)


def test_concurrent_reading_never_returns_torn_slots(bench):
    name = segment_name("race")
    process = subprocess.Popen([str(bench), "--name", name, "--fields", "64", "--slots", "32", "--period-us", "0", "--pattern", "--stream"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    reader = None
    try:
        # A fixed cycle count may finish before the reader is scheduled on a fast host.
        assert select.select([process.stdout], [], [], 5)[0], "writer did not become ready"
        assert process.stdout.readline() == b"ready\n"
        reader = ShmReader(SHM_DIR / name[1:])
        process.stdin.write(b"start\n")
        process.stdin.flush()
        valid = gaps = 0
        end = time.monotonic() + 10
        previous = 0
        while valid <= 1000 and time.monotonic() < end:
            assert process.poll() is None, "stress writer exited early"
            batch = reader.read()
            gaps += batch.gaps
            for seq, values in zip(batch.seqs, batch.values):
                assert seq > previous
                previous = seq
                assert values == tuple(seq * 1000.0 + i for i in range(64)), f"torn slot {seq}"
                valid += 1
        assert valid > 1000
        assert gaps > 0  # Exercise real overwrites, not only the uncontended path.
    finally:
        process.kill()
        process.wait()
        process.stdin.close()
        process.stdout.close()
        if reader is not None:
            reader.close()
        (SHM_DIR / name[1:]).unlink(missing_ok=True)
