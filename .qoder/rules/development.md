---
trigger: always_on
description: 开发纪律：读写代码的工具用法、验证命令、汇报口径
---

# 开发纪律

任何时候在本仓库开发都遵守以下几条。

## 读代码

- 先 `grep_code` 定位，只有需要看整体结构时才 `Read`。
- `Read` 会把请求范围自动扩到 200-300 行，本仓库单文件常在 500-1000 行且注释密度高，一次全量读代价很大。同一份内容不要读第二遍。
- 已经在上下文里的内容不重复拉取；需要确认某处现值用 `grep -n` 或 `sed -n '起,止p'`。

## 改代码

- `SearchReplace` 的 `original_text` 收到 1-3 行唯一锚点，不要贴大段上下文。
- 相关的多处改动合并进同一次调用，不拆成连续多次。
- 报匹配失败后**不要**立刻用 `Read` 复核：它会返回编辑前的缓存，看着像整批回滚。用 `sed -n` 读真实文件，确认哪几条已经写入——多条替换不保证原子，可能已部分落盘。
- 同一处失败两次就换 `python3 -c` 按 Unicode 码位替换，别继续重试。长中文片段（破折号、全角引号、书名号）最容易匹配失败。

## 验证

改动期间只跑受影响的文件，收尾再跑一次全量。命令一律接 `| tail -N`，不要把完整输出灌进上下文。

服务端（`cd server`）：

```bash
.venv/bin/python -m pytest tests/test_xxx.py -q | tail -5   # 改动期
.venv/bin/python -m pytest -q | tail -3                     # 收尾
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format src tests
```

前端（`cd app`）：

```bash
npx tsc -p tsconfig.web.json --noEmit
npx vitest run 2>&1 | tail -8
npx eslint src/路径
npx prettier --check src/路径
```

改完 Python 跑 ruff，改完 TS/TSX 跑 tsc + prettier + eslint。`mypy` 当前存量报错很多，只看自己新增的那几行。

## 汇报

只写可操作内容：改了什么、怎么验证、还剩什么。不写设计理由复述、不写背景铺垫、不做长篇总结。
