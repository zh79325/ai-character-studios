#!/usr/bin/env bash
# 下载 faster-whisper large-v3 到本地 gitee 仓库并推送。可重复执行（断点续传）。
# 用完可删。用法： bash prepare-speech-model.sh
set -euo pipefail

REPO="/Users/eleme/Desktop/ai-game/local-speech-recognition-model"
PY="/Users/eleme/Desktop/ai-game/dudu/server/.venv/bin/python"

cd "$REPO"

echo "== 1/4 下载模型（hf-mirror，禁用 xet，断点续传）=="
HF_HUB_DISABLE_XET=1 HF_ENDPOINT=https://hf-mirror.com "$PY" - <<'PYEOF'
from huggingface_hub import snapshot_download
p = snapshot_download(
    repo_id="Systran/faster-whisper-large-v3",
    local_dir=".",
    ignore_patterns=[".gitattributes", "README.md", ".git*"],
)
print("下载完成:", p)
PYEOF

echo "== 2/4 LFS 追踪大文件 =="
git lfs install --local
git lfs track "*.bin" "model.bin"
git add .gitattributes

echo "== 3/4 提交 =="
rm -rf .cache
git add -A
git status -s
git commit -m "add faster-whisper large-v3 model" || echo "（无改动可提交，跳过）"

echo "== 4/4 推送到 gitee =="
git push origin master

echo "== 完成。LFS 清单： =="
git lfs ls-files
