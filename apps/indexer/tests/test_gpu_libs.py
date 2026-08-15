"""engine.ensure_gpu_libs() must not break torch — verification discipline §16.

onnxruntime-gpu's CUDA provider links against cuBLAS/cuDNN DLLs that ship in
separate ``nvidia-*-cu12`` wheels, and nothing puts them on Windows' DLL search
path by itself. Left alone, session creation does not raise: onnxruntime logs
to stderr, silently returns a CPU-bound session, and ``get_available_providers()``
keeps advertising CUDA (it only ever meant "compiled with CUDA support"). The
app therefore reported GPU while running WD14 at 0.84s/image instead of
0.041s — a 20x slowdown that described itself as working.

The obvious fix — load those DLLs ourselves — has a trap that is worse than
the bug, and this test exists because it was walked straight into. Torch
ships its *own* ``torch/lib/{cublas64_12,cudnn64_9,...}.dll``, the same file
names at whatever versions that torch build was compiled against. Windows
resolves a DLL name to whatever copy is already loaded, so preloading the
standalone wheel copies first rebinds torch onto mismatched libraries and the
next ``import torch`` dies with ``OSError(22, 'The specified procedure could
not be found.', None, 127)``. In the real app that turned every caption job
into an error while the fix "passed" every existing test — none of them
imports torch after touching the GPU loader.

So the contract is: when torch is installed, torch owns the CUDA runtime and
ensure_gpu_libs() must keep its hands off. Both halves are checked here.

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_gpu_libs
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run() -> int:
    from indexer import engine

    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    check("torch" not in sys.modules,
          "torch is not imported yet (the ordering under test)")

    # Must never raise, on any machine — including one with no GPU, no NVIDIA
    # wheels, and no torch at all.
    try:
        engine.ensure_gpu_libs(["CUDAExecutionProvider", "CPUExecutionProvider"])
        check(True, "ensure_gpu_libs() completes on this machine")
    except Exception as exc:                       # pragma: no cover
        check(False, f"ensure_gpu_libs() raised {exc!r}")
        return 1

    # Idempotent: called once per engine construction, several engines per file.
    try:
        engine.ensure_gpu_libs(["CUDAExecutionProvider"])
        check(True, "a second call is a no-op, not a second round of DLL loads")
    except Exception as exc:                       # pragma: no cover
        check(False, f"the second call raised {exc!r}")

    # The regression itself. Asserted as "did it load anything?" rather than
    # "did torch survive?", deliberately: whether a competing copy actually
    # breaks torch depends on which CUDA minor versions happen to be installed
    # side by side, so a survival check passes on a machine whose two sets
    # happen to match and silently stops guarding anything. The contract does
    # not depend on that — when torch is present it owns the CUDA runtime, and
    # ensure_gpu_libs() must load nothing at all.
    if importlib.util.find_spec("torch") is None:
        print("  --  torch is not installed here — DLL-conflict check skipped")
    else:
        import ctypes
        loaded: list[str] = []
        real_cdll = ctypes.CDLL

        class RecordingCDLL(ctypes.CDLL):
            def __init__(self, name, *a, **kw):
                loaded.append(str(name))
                super().__init__(name, *a, **kw)

        engine._DLL_DIRS_ADDED = False
        ctypes.CDLL = RecordingCDLL
        try:
            engine.ensure_gpu_libs(["CUDAExecutionProvider", "CPUExecutionProvider"])
        finally:
            ctypes.CDLL = real_cdll
        check(not loaded,
              "with torch installed, ensure_gpu_libs() loads no CUDA DLLs of its own"
              + (f" (loaded {len(loaded)}: {[n.rsplit(chr(92), 1)[-1] for n in loaded[:4]]})"
                 if loaded else ""))

        try:
            import torch
            check(True, f"torch still imports afterwards ({torch.__version__})")
            available = torch.cuda.is_available()
            check(True, f"torch.cuda.is_available() answers without error ({available})")
            if available:
                x = torch.zeros(8, device="cuda")
                check(float(x.sum()) == 0.0, "a real CUDA tensor op still works")
        except OSError as exc:
            check(False, f"ensure_gpu_libs() broke torch: {exc!r}")
        except Exception as exc:                   # pragma: no cover
            check(False, f"torch failed after ensure_gpu_libs(): {exc!r}")

    # A provider list with no CUDA in it must not drag a CUDA runtime into the
    # process at all.
    import ctypes as _ctypes
    engine._DLL_DIRS_ADDED = False
    seen: list[str] = []
    real = _ctypes.CDLL

    class Recorder(_ctypes.CDLL):
        def __init__(self, name, *a, **kw):
            seen.append(str(name))
            super().__init__(name, *a, **kw)

    _ctypes.CDLL = Recorder
    try:
        engine.ensure_gpu_libs(["DmlExecutionProvider", "CPUExecutionProvider"])
    finally:
        _ctypes.CDLL = real
    check(not seen, "a non-CUDA provider list loads no CUDA libraries")

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s)")
        return 1
    print("RESULT: PASS — GPU DLL loading does not conflict with torch")
    return 0


if __name__ == "__main__":
    sys.exit(run())
