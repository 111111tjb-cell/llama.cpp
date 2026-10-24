# Safe slot checkpoint replacement

This fork improves the reliability of saving llama-server slot state to disk. The file contains the target state plus any context checkpoint and draft-state appendices. Previously, saving over an existing file opened the destination in `wb` mode before the complete replacement was available. An interrupted write could destroy the previous save, and hard-linked backups were overwritten through the same inode.

## Scope and design

The server now writes to a temporary file in the destination directory. It publishes that file only after the target state and both appendices have been saved successfully. A write or replacement error keeps the existing destination and removes the temporary file. Existing file permission bits are copied before the writer runs. Windows read-only temporary files are made removable during failure cleanup.

The implementation extends the existing `fs_write_atomic` helper with a callback that receives the temporary path. Its string-writing callers keep using the same interface. Slot serialization still streams to disk; the change does not buffer an additional full state in RAM. The public C API, REST response shape, and slot file format are unchanged.

The original direction was [disk checkpoint offloading, issue #20697](https://github.com/ggml-org/llama.cpp/issues/20697). Upstream already has [persistent prompt-cache work in PR #28092](https://github.com/ggml-org/llama.cpp/pull/28092) and slot checkpoint appendices in the current source. This fork builds on that existing serialization with a focused reliability fix. It does not introduce an automatic cache tier, an eviction policy, or a new cache directory option.

Base: `3f03b17724ad8b69040825f45c43c59b092a0244`.

## Run the demo

Build the server using the [upstream build instructions](docs/build.md). A typical CPU build is:

```sh
cmake -S . -B build -DLLAMA_BUILD_TESTS=ON
cmake --build build --config Release --target llama-server test-server-component
```

Download the same 1.2 MB test model used by the regression tests:

```sh
curl -L -o stories260K-f32.gguf https://huggingface.co/ggml-org/test-model-stories260K/resolve/479896ec924af6d40fd419ab8f4d1eb2101de00d/stories260K-f32.gguf
python examples/checkpoint_save.py --server build/bin/llama-server --model stories260K-f32.gguf
```

On Windows use the path to `llama-server.exe`, which may be under `build/bin/Release` with a Visual Studio generator. Python 3.10 or newer is needed. The demo uses the standard library; installing `psutil` adds optional RSS measurements.

The demo starts three independent server processes on loopback. It saves a checkpoint, creates a hard-link backup, changes the conversation and saves again. It then compares a cold request with recovery of the backup in a new process. A successful run prints `"passed": true`, `"backup_preserved": true`, and identical generated text with fewer prompt tokens processed after recovery. Use `--directory <empty-directory>` to retain the files and server logs for inspection. The filesystem must support hard links for this particular reproduction.

File hashing uses bounded chunks, including when a larger model is supplied.

## Validation

- The new hard-link regression failed against the unmodified upstream server: the backup bytes changed after overwriting the current save.
- The fixed server passed the selected text-only slot suite, including ordinary save/restore, legacy token payloads, draft-state restoration, SWA checkpoints, non-ASCII filenames, damaged appendices, and draft KV type changes.
- The component suite exercises injected writer failure, replacement failure, temporary-file cleanup, visibility of the previous contents during writing, and permission preservation. Windows read-only destination cleanup is covered.
- The model-resolution tests check the existing callers of the shared atomic-write helper.
- Python type checking uses the repository-pinned ty 0.0.88. Changed files were checked for LF newlines, trailing whitespace, and final newlines.

See [VALIDATION_RESULTS.json](VALIDATION_RESULTS.json) for exact counts, model hashes, environment, build flags, and the measured demo run. Local testing used Windows, Clang 23.1.4 from LLVM-MinGW, CMake 4.4.4, CPU-only builds, an i9-14900HX, and approximately 32 GiB of RAM. HTTPS support was disabled for the test build; models were downloaded separately and existing test presets were redirected to those exact local files. Other test preset downloads were disabled.

The small-model demo checks correctness. Its latency measurements are noisy and do not establish a performance improvement. The existing cache reduces prompt recomputation; this patch makes replacing its saved state safer. The recorded run includes the restore cost separately, so it must not be omitted from an end-to-end latency comparison.

## Limits

- No full upstream CI, GPU backend, Linux/macOS, or large production-model validation was performed. Six multimodal slot tests were excluded from the selected local suite.
- Atomic replacement needs space for a complete new file while the old one still exists. Writers to the same destination are not coordinated across processes; the last successful replacement wins.
- Filesystem rename semantics apply. Readers or read-only destinations can prevent replacement on Windows; the previous checkpoint is retained on such errors.
- File permission bits are preserved, while ownership and platform-specific ACL metadata follow filesystem creation rules.
- There is no new `fsync` or directory-sync guarantee for sudden power loss. Terminating the process can leave a temporary file, while cleanup of ordinary errors is handled by the helper.

## Attribution

The original llama.cpp history, sources, and MIT license are preserved. The design, implementation, tests, and demo in this fork were developed with Codex assistance. No upstream PR or automated communication to maintainers was submitted.
