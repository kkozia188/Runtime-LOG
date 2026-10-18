"""The v1 segment format is frozen by tests/fixtures/shm_format_v1.bin.

robot_driver keeps a copy of shm_ring.hpp (rt_control_semantic_components) and the same golden file;
each side checks its writer against it, so the two copies cannot drift apart unnoticed.
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from runtime_log.shm import ShmReader

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/fixtures/shm_format_v1.bin"
GOLDEN_SHA256 = "ec82a6d3875406e4f5ddb6237c45b65d9f4d70710225cec4089af5e21645e367"


def test_golden_file_is_the_frozen_v1_sample():
    assert hashlib.sha256(GOLDEN.read_bytes()).hexdigest() == GOLDEN_SHA256


@pytest.mark.skipif(shutil.which("g++") is None, reason="needs g++")
def test_writer_produces_the_golden_bytes(tmp_path):
    binary = tmp_path / "format_sample"
    subprocess.run(
        ["g++", "-O2", "-std=c++17", "-Wall", "-Wextra", "-Werror", '-DSHM_RING_HEADER="runtime_log_recorder/shm_ring.hpp"',
         "-I", str(ROOT / "ros/runtime_log_recorder/include"), str(ROOT / "tests/cpp/format_sample.cpp"), "-o", str(binary)],
        check=True,
    )
    out = tmp_path / "sample.bin"
    subprocess.run([str(binary), str(out)], check=True)
    assert out.read_bytes() == GOLDEN.read_bytes()


def test_reader_understands_the_golden_sample(tmp_path):
    path = tmp_path / "golden"
    shutil.copy(GOLDEN, path)
    reader = ShmReader(path)
    assert reader.fields == ["a", "b/c"] and reader.writer_name == "format" and reader.info == "check"
    assert reader.slot_count == 4 and reader.writer_state == "active"
    batch = reader.read()
    assert batch.seqs == [1, 2] and batch.values == [(1.5, -2.0), (3.0, 4.0)] and batch.times_ns == [1000, 2000]
