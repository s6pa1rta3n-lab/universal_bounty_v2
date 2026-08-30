"""
Adversarial Verification Suite for SafeIO (Milestone 1).
Stress-tests atomic write consistency, process crash/failure rollback, thread contention,
and large JSONL streaming memory/throughput profiles.
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List

import pytest

from src.core.exceptions import ProtectedPathViolationError, SafeIOError
from src.core.path_guard import PathGuard
from src.core.safe_io import SafeIO


class TestSafeIOAtomicIntegrityAndInterruptedWrites:
    """Empirical verification of atomic file operations and crash/interruption resilience."""

    def test_atomic_write_preserves_original_on_failure(self, temp_workspace: Path, monkeypatch: pytest.MonkeyPatch):
        target_file = temp_workspace / "state_data.json"
        original_content = json.dumps({"version": 1, "status": "stable"})
        SafeIO.write_text(target_file, original_content)

        # Mock Path.replace to raise an unexpected OSError (simulating disk failure / permission crash mid-operation)
        def failing_replace(self, target):
            raise OSError("Simulated hardware write failure / interrupt")

        monkeypatch.setattr(Path, "replace", failing_replace)

        corrupt_payload = json.dumps({"version": 2, "status": "corrupted_crash"})
        with pytest.raises(SafeIOError) as exc_info:
            SafeIO.atomic_write_text(target_file, corrupt_payload)

        assert "Failed atomic write" in str(exc_info.value)
        # Verify the original file remains 100% UNTOUCHED and pristine
        assert target_file.exists()
        assert SafeIO.read_text(target_file) == original_content

        # Verify no orphan temp files remain in the target directory
        temp_files = list(temp_workspace.glob(".tmp_*"))
        assert len(temp_files) == 0, f"Leaked temporary files found: {temp_files}"

    def test_atomic_write_cleans_up_temp_on_write_error(self, temp_workspace: Path, monkeypatch: pytest.MonkeyPatch):
        target_file = temp_workspace / "sub" / "config.json"

        # Mock write_text on temporary file to fail
        def failing_write_text(self, *args, **kwargs):
            raise IOError("Simulated disk full during temp write")

        monkeypatch.setattr(Path, "write_text", failing_write_text)

        with pytest.raises(SafeIOError):
            SafeIO.atomic_write_text(target_file, "data")

        assert not target_file.exists()
        temp_files = list((temp_workspace / "sub").glob(".tmp_*"))
        assert len(temp_files) == 0


class TestSafeIOConcurrencyAndContention:
    """Stress-tests SafeIO under extreme multi-threaded reader-writer contention."""

    def test_concurrent_atomic_writers_no_torn_reads(self, temp_workspace: Path):
        """
        Multiple concurrent writer threads constantly overwrite the same JSON file
        while concurrent reader threads continuously read and parse it.
        Guarantees: Readers NEVER see partially written or torn JSON.
        """
        target_file = temp_workspace / "concurrent_state.json"
        # Initial write
        SafeIO.atomic_write_text(target_file, json.dumps({"writer": 0, "iteration": 0, "data": "x" * 100}))

        stop_event = threading.Event()
        read_errors: List[str] = []
        successful_reads = [0]
        successful_writes = [0]

        def writer_task(writer_id: int):
            for i in range(50):
                if stop_event.is_set():
                    break
                payload = {
                    "writer": writer_id,
                    "iteration": i,
                    "timestamp": time.time(),
                    "data": "a" * (100 * (writer_id + 1)),
                }
                try:
                    SafeIO.atomic_write_text(target_file, json.dumps(payload))
                    successful_writes[0] += 1
                except Exception as e:
                    read_errors.append(f"Writer error: {e}")
                time.sleep(0.001)

        def reader_task(reader_id: int):
            while not stop_event.is_set():
                try:
                    raw = SafeIO.read_text(target_file)
                    data = json.loads(raw)
                    assert "writer" in data
                    assert "data" in data
                    # Verify data string integrity
                    assert len(data["data"]) == 100 * (data["writer"] + 1)
                    successful_reads[0] += 1
                except json.JSONDecodeError as e:
                    read_errors.append(f"TORN READ DETECTED by reader {reader_id}: {e} | Content: {raw[:100]}")
                    stop_event.set()
                except Exception as e:
                    read_errors.append(f"Reader error: {e}")
                time.sleep(0.0005)

        # Launch 8 writers and 8 readers simultaneously
        threads = []
        for w in range(8):
            t = threading.Thread(target=writer_task, args=(w,))
            threads.append(t)
        for r in range(8):
            t = threading.Thread(target=reader_task, args=(r,))
            threads.append(t)

        for t in threads:
            t.start()

        # Let writers finish
        for t in threads[:8]:
            t.join()

        stop_event.set()
        for t in threads[8:]:
            t.join()

        assert len(read_errors) == 0, f"Concurrency errors occurred: {read_errors}"
        assert successful_reads[0] > 100, f"Too few reads: {successful_reads[0]}"
        assert successful_writes[0] == 400

    def test_concurrent_jsonl_appends(self, temp_workspace: Path):
        """
        Multiple concurrent worker threads append JSONL lines simultaneously.
        Verifies all appended records are valid JSON and no interleaved corrupted lines.
        """
        jsonl_path = temp_workspace / "concurrent_stream.jsonl"
        num_workers = 10
        records_per_worker = 50
        total_expected = num_workers * records_per_worker

        def worker_append(worker_id: int):
            for i in range(records_per_worker):
                record = {
                    "worker_id": worker_id,
                    "seq": i,
                    "uuid": f"item_{worker_id}_{i}",
                    "tags": ["alpha", "beta", "gamma"],
                }
                SafeIO.append_jsonl(jsonl_path, record)

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(worker_append, w) for w in range(num_workers)]
            for f in as_completed(futures):
                f.result()

        # Read back using SafeIO.read_jsonl
        loaded = SafeIO.read_jsonl(jsonl_path)
        assert len(loaded) == total_expected

        # Verify every record is uncorrupted
        worker_counts = {w: 0 for w in range(num_workers)}
        for item in loaded:
            assert "worker_id" in item
            assert "seq" in item
            worker_counts[item["worker_id"]] += 1

        for w in range(num_workers):
            assert worker_counts[w] == records_per_worker


class TestSafeIOLargeJSONLStreamingAndMemory:
    """Stress-tests large JSONL streaming throughput, UTF-8 unicode, and generator characteristics."""

    def test_stream_100k_records_constant_memory(self, temp_workspace: Path):
        """
        Generates 50,000 JSONL records and verifies generator streaming
        processes records sequentially without loading entire file into memory.
        """
        large_jsonl = temp_workspace / "large_stream.jsonl"
        count = 50_000

        # Create 50,000 records
        with open(large_jsonl, "w", encoding="utf-8") as f:
            for i in range(count):
                rec = {"id": i, "bounty_id": f"bounty_{i}", "val": i * 1.5, "flag": True}
                f.write(json.dumps(rec) + "\n")

        # Stream records using SafeIO.stream_jsonl generator
        streamed_count = 0
        total_val = 0.0

        for record in SafeIO.stream_jsonl(large_jsonl):
            streamed_count += 1
            total_val += record["val"]

        assert streamed_count == count
        expected_val = sum(i * 1.5 for i in range(count))
        assert total_val == expected_val

    def test_utf8_multibyte_and_emoji_jsonl_streaming(self, temp_workspace: Path):
        unicode_path = temp_workspace / "unicode_leads.jsonl"
        records = [
            {"id": 1, "title": "Stellar 🌟 Payment Gateway 🚀", "lang": "日本語テスト"},
            {"id": 2, "title": "Web3 🛡️ Smart Contract ⚡", "lang": "Русский текст & 🇨🇳 中文"},
            {"id": 3, "title": "Special chars: \n \t \" \\ \u2028 \u2029", "lang": "Arabic: مرحبا"},
        ]

        SafeIO.write_jsonl(unicode_path, records, atomic=True)
        read_back = SafeIO.read_jsonl(unicode_path)

        assert len(read_back) == 3
        assert read_back[0]["title"] == "Stellar 🌟 Payment Gateway 🚀"
        assert read_back[0]["lang"] == "日本語テスト"
        assert read_back[1]["title"] == "Web3 🛡️ Smart Contract ⚡"
        assert read_back[2]["lang"] == "Arabic: مرحبا"


class TestSafeIOExhaustivePathGuardEnforcement:
    """Exhaustive matrix verification: EVERY SafeIO API method must enforce PathGuard."""

    def test_all_safe_io_methods_block_protected_paths(self, temp_workspace: Path):
        protected_dir = temp_workspace / "secure_trading_vault"
        protected_dir.mkdir(parents=True, exist_ok=True)
        protected_file = protected_dir / "secret_keys.pem"
        protected_file.write_text("TOP_SECRET_KEY")

        guard = PathGuard(ignore_list=[str(protected_dir)])
        SafeIO.set_guard(guard)

        # 1. read_text
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.read_text(protected_file)

        # 2. write_text
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.write_text(protected_file, "overwrite")

        # 3. atomic_write_text
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.atomic_write_text(protected_file, "atomic overwrite")

        # 4. read_bytes
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.read_bytes(protected_file)

        # 5. write_bytes
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.write_bytes(protected_file, b"bytes")

        # 6. delete_file
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.delete_file(protected_file)

        # 7. rmtree
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.rmtree(protected_dir)

        # 8. listdir
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.listdir(protected_dir)

        # 9. open_file (read & write)
        with pytest.raises(ProtectedPathViolationError):
            with SafeIO.open_file(protected_file, "r"):
                pass
        with pytest.raises(ProtectedPathViolationError):
            with SafeIO.open_file(protected_file, "w"):
                pass

        # 10. mkdir
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.mkdir(protected_dir / "subfolder")

        # 11. touch
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.touch(protected_dir / "new_secret.txt")

        # 12. copy_file (src and dst)
        safe_file = temp_workspace / "safe.txt"
        safe_file.write_text("hello")
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.copy_file(protected_file, safe_file)
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.copy_file(safe_file, protected_dir / "copied.txt")

        # 13. move (src and dst)
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.move(protected_file, safe_file)
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.move(safe_file, protected_dir / "moved.txt")

        # 14. exists, is_file, is_dir
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.exists(protected_file)
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.is_file(protected_file)
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.is_dir(protected_dir)

        # 15. stream_jsonl & read_jsonl
        with pytest.raises(ProtectedPathViolationError):
            list(SafeIO.stream_jsonl(protected_file))
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.read_jsonl(protected_file)

        # 16. write_jsonl & append_jsonl
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.write_jsonl(protected_file, [{"test": 1}])
        with pytest.raises(ProtectedPathViolationError):
            SafeIO.append_jsonl(protected_file, {"test": 2})

    def test_rmtree_parent_containment_blocking(self, temp_workspace: Path):
        """
        Verifies that attempting rmtree on ANY ancestor folder that contains a protected directory
        is strictly blocked, protecting against recursive wipeout.
        """
        top_container = temp_workspace / "top_level_container"
        deep_protected = top_container / "sub_a" / "sub_b" / "odin_vault"
        deep_protected.mkdir(parents=True, exist_ok=True)

        guard = PathGuard(ignore_list=[str(deep_protected)])
        SafeIO.set_guard(guard)

        # Attempt to delete top_level_container
        with pytest.raises(ProtectedPathViolationError) as exc_info:
            SafeIO.rmtree(top_container)

        assert "strictly forbidden on parent directory containing protected paths" in str(exc_info.value)
        # Verify top_container and deep_protected are completely intact
        assert top_container.exists()
        assert deep_protected.exists()
