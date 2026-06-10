"""Minimal torchaudio stub for the DGX Spark (aarch64) NGC build.

Why this exists:
  The NGC PyTorch 25.12 image ships torch 2.10.0a0 (dev) but a torchaudio whose
  compiled extension references a symbol removed from that torch
  (`torch_get_cuda_stream_from_pool`), so `import torchaudio` hard-fails:
      OSError: .../libtorchaudio.abi3.so: undefined symbol: torch_get_cuda_stream_from_pool
  No torchaudio wheel is built for this dev torch on aarch64.

  ComfyUI core hard-imports torchaudio at startup
  (comfy/ldm/lightricks/vae/audio_vae.py), even for video-only workflows, so the
  broken import crashes ComfyUI before it can listen.

What this does:
  Satisfies `import torchaudio` (and the `transforms`/`functional` submodules) so
  ComfyUI starts. The LTX-2.3 I2V workflow here is VIDEO-ONLY (no audio decode),
  so the real audio functions below are never reached. If an audio path ever does
  call them, it fails loudly rather than silently producing garbage.
"""

import sys
import types

__version__ = "0.0.0-stub-aarch64"


def _unavailable(*_args, **_kwargs):
    raise RuntimeError(
        "torchaudio is stubbed on this aarch64 build (audio disabled). "
        "This code path requires a working torchaudio, which is not available "
        "for the container's torch dev build."
    )


# transforms.MelSpectrogram — constructible (so module/class refs resolve), but
# calling it raises. Only audio decode would ever instantiate+call it.
class MelSpectrogram:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        _unavailable()

    def to(self, *args, **kwargs):
        return self


transforms = types.ModuleType("torchaudio.transforms")
transforms.MelSpectrogram = MelSpectrogram

functional = types.ModuleType("torchaudio.functional")
functional.resample = _unavailable

# Common top-level I/O entry points, in case anything references them.
load = _unavailable
save = _unavailable
info = _unavailable

sys.modules["torchaudio.transforms"] = transforms
sys.modules["torchaudio.functional"] = functional
