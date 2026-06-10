#!/usr/bin/env bash
# Stage the models needed for LTX-2.3 Image-to-Video (single-stage) into the
# weights dir that docker-compose mounts read-only at /models.
#
# Weights live OUTSIDE the repo (default ~/ltx2/models); .gitignore excludes
# models/ and *.safetensors so nothing here is ever committed.
#
# Idempotent: files already present are skipped / resumed. It will NOT
# re-download the ~51GB you already have. The only large NEW download is the
# Gemma-3-12B text encoder (currently missing), required by the local
# text-encode path (LTXVGemmaCLIPModelLoader / LTXAVTextEncoderLoader).

set -euo pipefail

MODELS_DIR="${MODELS_DIR:-$HOME/ltx2/models}"
LTX_REPO="Lightricks/LTX-2.3"
# Per ComfyUI-LTXVideo README, the local text encoder is this Gemma-3-12B repo.
GEMMA_REPO="google/gemma-3-12b-it-qat-q4_0-unquantized"
# ComfyUI text_encoders/ subfolder (mapped via comfyui/extra_model_paths.yaml).
GEMMA_DIR="$MODELS_DIR/text_encoders/gemma-3-12b-it-qat-q4_0-unquantized"

export HF_HUB_ENABLE_HF_TRANSFER="${HF_HUB_ENABLE_HF_TRANSFER:-1}"

if command -v hf >/dev/null 2>&1; then
  HF=(hf download)
elif command -v huggingface-cli >/dev/null 2>&1; then
  HF=(huggingface-cli download)
else
  echo "ERROR: install the HF CLI:  pip install 'huggingface_hub[hf_transfer]'" >&2
  exit 1
fi

mkdir -p "$MODELS_DIR" "$GEMMA_DIR"
echo "Models dir: $MODELS_DIR"

# --- LTX-2.3 weights for single-stage I2V (checkpoint = full 'dev', + distilled LoRA) ---
# NOTE: these match the filenames already on disk (the 1.0 / un-suffixed
# revision). Upstream now ships -1.1 variants; the workflow JSON points at the
# on-disk names. If a name 404s on a fresh machine, switch to the -1.1 variant
# here and in workflows/ltx23_i2v_api.json.
LTX_FILES=(
  "ltx-2.3-22b-dev.safetensors"
  "ltx-2.3-22b-distilled-lora-384.safetensors"
)
for f in "${LTX_FILES[@]}"; do
  if [[ -f "$MODELS_DIR/$f" ]]; then
    echo "✓ present, skipping: $f"
  else
    echo "↓ downloading: $f"
    "${HF[@]}" "$LTX_REPO" "$f" --local-dir "$MODELS_DIR"
  fi
done

# --- Gemma-3-12B text encoder (full snapshot; the missing piece) ---
if [[ -f "$GEMMA_DIR/tokenizer.model" ]] && \
   ls "$GEMMA_DIR"/model*.safetensors >/dev/null 2>&1 && \
   [[ -f "$GEMMA_DIR/preprocessor_config.json" ]]; then
  echo "✓ gemma text encoder present, skipping"
else
  echo "↓ downloading Gemma-3-12B text encoder into $GEMMA_DIR"
  "${HF[@]}" "$GEMMA_REPO" --local-dir "$GEMMA_DIR"
fi

echo
echo "Done. /models will see:"
echo "  checkpoints/loras -> $MODELS_DIR/*.safetensors"
echo "  text_encoders     -> $GEMMA_DIR"
ls -lah "$MODELS_DIR"
