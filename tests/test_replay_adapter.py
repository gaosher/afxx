"""Current AFX contracts, independent of workflow orchestration."""

import json
from pathlib import Path
import subprocess
import sys
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from afxx.afx import replay as adapter


def results(shapes=3):
    return [
        dict(
            id=backend,
            seed="123",
            backend=backend,
            status="ok",
            shape_results=[
                dict(
                    shape_index=i,
                    correctness="not_checked" if backend == "cpu" else "pass",
                )
                for i in range(shapes)
            ],
        )
        for backend in ("cpu", "af-mlir")
    ]


@pytest.mark.parametrize("count", [1, 2, 8])
def test_shape_counts_and_cpu_reference(count):
    report = adapter.summarize(results(count), [["123", count]])
    assert report["passed"], report
    assert adapter.normalized_seeds(json.dumps([["123", count]])) == [["123", count]]


@pytest.mark.parametrize("backend", ["cpu", "af-mlir"])
@pytest.mark.parametrize(
    "failure",
    [
        "fail",
        "error",
        "unknown",
        "execution_error",
        "missing_shape",
        "duplicate_shape",
        "missing_attempt",
        "extra_shape",
    ],
)
def test_reference_exception_does_not_hide_failures(backend, failure):
    attempts = results()
    row = next(row for row in attempts if row["backend"] == backend)
    if failure == "execution_error":
        row["status"] = "error"
    elif failure == "missing_shape":
        row["shape_results"].pop()
    elif failure == "duplicate_shape":
        row["shape_results"][1]["shape_index"] = 0
    elif failure == "extra_shape":
        row["shape_results"].append(dict(shape_index=3, correctness="pass"))
    elif failure == "missing_attempt":
        attempts.remove(row)
    else:
        row["shape_results"][0]["correctness"] = failure
    assert not adapter.summarize(attempts, [["123", 3]])["passed"]


def test_duplicate_and_unexpected_attempts():
    with pytest.raises(ValueError, match="duplicate"):
        adapter.summarize(results() + [results()[0]], [["123", 3]])
    extra = dict(results()[0], backend="eager-npu")
    assert not adapter.summarize(results() + [extra], [["123", 3]])["passed"]
    assert not adapter.summarize([], [["123", 3]])["passed"]


def test_result_cli_pagination_and_details(tmp_path):
    database = tmp_path / "results.sqlite"
    database.touch()
    calls = []

    def capture(argv):
        calls.append(argv)
        assert argv[:7] == [
            "/python",
            "-m",
            "autofusex",
            "fuzz",
            "maintenance",
            "results",
            str(database),
        ]
        if "--attempt" in argv:
            return json.dumps(next(row for row in results() if row["id"] == argv[-1]))
        return json.dumps(
            dict(items=[{"id": "cpu"}], next_cursor=200)
            if argv[-1] == "0"
            else dict(items=[{"id": "af-mlir"}], next_cursor=None)
        )

    assert adapter.read_results("/python", database, capture) == results()
    assert len(calls) == 4
    assert "--after" in calls[2] and calls[2][-1] == "200"


def test_missing_database_not_created(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(ValueError, match="missing"):
        adapter.read_results(
            "/python", path, lambda argv: pytest.fail("must not query")
        )
    assert not path.exists()


@pytest.mark.parametrize("cursor", [0, -1, True, "200"])
def test_bad_result_cursor_rejected(tmp_path, cursor):
    path = tmp_path / "results.sqlite"
    path.touch()

    def capture(argv):
        return json.dumps(
            results()[0]
            if "--attempt" in argv
            else dict(items=[{"id": "cpu"}], next_cursor=cursor)
        )

    with pytest.raises(ValueError, match="cursor"):
        adapter.read_results("/python", path, capture)


@pytest.mark.parametrize("batch", [False, True])
def test_command_contract(batch, tmp_path):
    options = {
        "compile_threads": 4,
        "workers": 2,
        "batch_timeout_seconds": 300,
        "devices": ["npu:0", "npu:1"],
    }
    normalized = [["123", 8], ["456", 1]] if batch else [["123", 2]]
    argv = adapter.replay_command(
        "/venv/bin/python", options, normalized, tmp_path, batch
    )
    assert argv[:4] == ["/venv/bin/python", "-m", "autofusex", "fuzz"]
    assert argv[4] == ("run" if batch else "replay")
    assert argv[argv.index("--database") + 1] == str(tmp_path / "results.sqlite")
    assert argv[argv.index("--scheduling") + 1] == "staged"
    assert ("--timeout" in argv) == batch
    assert argv.count("--device") == (2 if batch else 1)
    assert "--save" in argv


def test_seed_command_uses_installed_current_contract(tmp_path):
    package = tmp_path / "autofusex/fuzz/client/generation"
    package.mkdir(parents=True)
    for directory in (package, *list(package.parents)[:3]):
        (directory / "__init__.py").touch()
    (package.parent / "contracts.py").write_text("""class Seed:
    @classmethod
    def parse(cls, text):
        if text == 'retired': raise ValueError('unsupported seed')
        s = cls(); s.value = text; s.dynamic = text != '1'; return s
    def __str__(self): return self.value
""")
    (package / "sampling.py").write_text("""from types import SimpleNamespace
def decode(seed): return SimpleNamespace(dynamic_variants=int(str(seed))), 42
""")
    command = adapter.seed_command(sys.executable, ["1", "2", "3", "8"])
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert adapter.normalized_seeds(result.stdout) == [
        ["1", 1],
        ["2", 2],
        ["3", 3],
        ["8", 8],
    ]
    assert (
        subprocess.run(
            adapter.seed_command(sys.executable, ["retired"]),
            cwd=tmp_path,
            capture_output=True,
        ).returncode
        != 0
    )


@pytest.mark.parametrize(
    "response",
    [
        "",
        "[]",
        '[["123", 0]]',
        '[["123", 9]]',
        '[["123", true]]',
        '[["123", 1], ["123", 1]]',
    ],
)
def test_invalid_seed_response_rejected(response):
    with pytest.raises(ValueError):
        adapter.normalized_seeds(response)


def test_requested_seeds_and_environment(tmp_path):
    seeds = tmp_path / "seeds.txt"
    seeds.write_text("123\n\n456\n")
    assert adapter.requested_seeds(None, seeds) == ["123", "456"]
    with pytest.raises(ValueError):
        adapter.requested_seeds("invalid seed", None)
    env = adapter.replay_environment(tmp_path, True)
    assert env["AUTOFUSEX_HOME"] == str(tmp_path)
    assert "AUTOFUSEDBG_HOME" not in env
    assert "--debug_dir=" in env["AUTOFUSE_DFX_FLAGS"]


@pytest.mark.parametrize("value", ["0", "-1", "true", "[]", ""])
def test_auto_devices_rejects_no_or_invalid_devices(value):
    with pytest.raises(ValueError):
        adapter.visible_device_count(value)


def test_auto_devices_uses_logical_runtime_count():
    assert adapter.visible_device_count("driver diagnostic\n2\n") == 2
    assert "torch.npu.device_count()" in adapter.device_command("/target/python")[2]


@pytest.mark.parametrize("dump", [False, True])
def test_diagnostics_are_scoped_to_replay(tmp_path, monkeypatch, dump):
    import os
    monkeypatch.setenv("AUTOFUSE_DFX_FLAGS", "parent-flags")
    before = dict(os.environ)
    results = tmp_path / "results"
    env = adapter.replay_environment(results, dump)
    assert dict(os.environ) == before
    assert env["TORCHINDUCTOR_CACHE_DIR"] == str(results / "cache")
    if dump:
        assert env["TORCH_COMPILE_DEBUG"] == "1"
        assert env["DUMP_GE_GRAPH"] == "1"
        assert env["DUMP_GRAPH_LEVEL"] == "1"
        assert Path(env["DUMP_GRAPH_PATH"]) == results / "dump" / "ge"
        assert Path(env["DUMP_GRAPH_PATH"]).is_dir()
        assert "--codegen_compile_debug=true;" in env["AUTOFUSE_DFX_FLAGS"]
        assert f"--debug_dir={results / 'dump'};" in env["AUTOFUSE_DFX_FLAGS"]
        assert env["ASCEND_GLOBAL_LOG_LEVEL"] == "0"
        assert env["ASCEND_SLOG_PRINT_TO_STDOUT"] == "1"
    else:
        assert not results.exists()
        assert set(env) == {"AUTOFUSEX_HOME", "TORCHINDUCTOR_CACHE_DIR", "AUTOFUSE_DFX_FLAGS"}
    assert "ASCEND_LAUNCH_BLOCKING" not in env
    assert "TORCHINDUCTOR_FORCE_DISABLE_CACHES" not in env


def test_evidence_returns_text_and_missing_directories(tmp_path):
    job = tmp_path / "job"
    results = tmp_path / "results"
    job.mkdir()
    (job / "job.log").write_text("failure stack")
    (results / "dump").mkdir(parents=True)
    (results / "dump" / "lowering.mlir").write_text("module {}")
    (results / "sync.json").write_text('SECRET')
    (results / "dump" / "outside.log").symlink_to(job / "job.log")
    data = adapter.collect_evidence(job, results, True)
    assert {row["excerpt"] for row in data["files"]} == {"failure stack", "module {}"}
    assert not data["directories"]["torch_debug"]["exists"]
    assert not data["truncated"]
    assert all(Path(row["path"]).is_absolute() for row in data["files"])


def test_evidence_bounds_output_and_preserves_log_tail(tmp_path):
    job = tmp_path / "job"
    results = tmp_path / "results"
    job.mkdir()
    results.mkdir()
    (job / "job.log").write_text("x" * 10000 + "last error")
    for index in range(150):
        (results / f"{index:03}.log").write_text("a" * 10000)
    data = adapter.collect_evidence(job, results, True)
    assert len(data["files"]) == 128 and data["truncated"]
    assert sum(len(row.get("excerpt", "")) for row in data["files"]) <= 65536
    assert data["files"][0]["excerpt"].endswith("last error")
    assert data["files"][0]["excerpt_truncated"]
