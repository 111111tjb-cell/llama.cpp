"""Verify safe checkpoint replacement and recovery with three server processes."""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def request(url, path, data=None):
    payload = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url + path, data=payload, headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=30)


@contextmanager
def server(binary, model, directory, stage):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ)
    env.pop("LLAMA_SERVER_DEBUG_FAKE_TIMING", None)
    with (directory / f"{stage}.log").open("wb") as log:
        process = subprocess.Popen([
            str(binary), "-m", str(model), "--host", "127.0.0.1", "--port", str(port),
            "-np", "1", "-c", "512", "-t", "4", "-ngl", "0", "--temp", "0",
            "--seed", "42", "--slot-save-path", str(directory), "--no-webui",
        ], stdout=log, stderr=log, env=env)
        url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"Server exited; see {directory / (stage + '.log')}")
                try:
                    with request(url, "/health") as response:
                        if response.status == 200:
                            break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Server did not become ready")
                    time.sleep(0.05)
            yield url, process.pid
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def call(url, path, data):
    with request(url, path, data) as response:
        return json.load(response)


def completion(url, pid, prompt):
    start = time.perf_counter()
    first_token_ms = None
    content = ""
    result = {}
    with request(url, "/completion", {
        "prompt": prompt, "id_slot": 0, "cache_prompt": True,
        "n_predict": 8, "temperature": 0, "stream": True,
    }) as response:
        for line in response:
            if not line.startswith(b"data: "):
                continue
            chunk = json.loads(line[6:])
            text = chunk.get("content", "")
            if text and first_token_ms is None:
                first_token_ms = (time.perf_counter() - start) * 1000
            content += text
            if chunk.get("stop"):
                result = chunk
    round_trip_ms = (time.perf_counter() - start) * 1000
    rss = None
    try:
        import psutil
        rss = psutil.Process(pid).memory_info().rss
    except ImportError:
        pass
    return {
        "pid": pid, "content": content, "first_token_ms": first_token_ms,
        "round_trip_ms": round_trip_ms,
        "prompt_tokens_processed": result["timings"]["prompt_n"],
        "rss_bytes_after_request": rss,
    }


def run(binary, model, directory):
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError("Use an empty directory for this demo")
    base = "The quick brown fox jumps over the lazy dog. " * 12
    follow_up = base + "And then the fox said"
    current, backup = directory / "current.bin", directory / "backup.bin"
    with server(binary, model, directory, "save") as (url, pid):
        save_pid = pid
        completion(url, pid, base)
        saved = call(url, "/slots/0?action=save", {"filename": current.name})
        original = digest_file(current)
        checkpoint_bytes = current.stat().st_size
        os.link(current, backup)
        completion(url, pid, "Tell me a story about a dragon.")
        call(url, "/slots/0?action=save", {"filename": current.name})
        backup_preserved = digest_file(backup) == original
        replacement_changed = digest_file(current) != original
    with server(binary, model, directory, "cold") as (url, pid):
        cold = completion(url, pid, follow_up)
    with server(binary, model, directory, "restore") as (url, pid):
        start = time.perf_counter()
        restored = call(url, "/slots/0?action=restore", {"filename": backup.name})
        restore_ms = (time.perf_counter() - start) * 1000
        resumed = completion(url, pid, follow_up)
    passed = (backup_preserved and replacement_changed
              and restored["n_restored"] == saved["n_saved"]
              and resumed["content"] == cold["content"]
              and resumed["prompt_tokens_processed"] < cold["prompt_tokens_processed"]
              and not list(directory.glob("*.tmp")))
    return {
        "passed": passed, "backup_preserved": backup_preserved,
        "replacement_changed": replacement_changed,
        "save_pid": save_pid,
        "model_sha256": digest_file(model),
        "checkpoint_bytes": checkpoint_bytes, "restore_ms": restore_ms,
        "cold": cold, "resumed": resumed,
        "note": "One small-model CPU run; timings are illustrative, not a performance claim.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    binary, model = args.server.resolve(), args.model.resolve()
    if args.directory:
        report = run(binary, model, args.directory.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="llama-checkpoint-") as temporary:
            report = run(binary, model, Path(temporary))
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
