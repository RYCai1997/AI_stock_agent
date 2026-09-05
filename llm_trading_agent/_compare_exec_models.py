# -*- coding: utf-8 -*-
"""一次性对比：同参数、同数据下三档执行模型（intrabar / close / next_open）规则引擎回测差异。

关键注意（v7 日志「探针覆写配置经典坑」）：
- config.py 是 class Config + 模块底部单例 CFG = Config()；业务代码读的是【实例】。
- 覆写必须 `import config as m; m.CFG.X = ...`；直接 `import config as CFG; CFG.X=...` 改的是模块属性，无效。
- 三档对比必须规则引擎（确定性）：.env 的 BACKTEST_USE_LLM=true 会被
  `use_llm = args.use_llm or CFG.BACKTEST_USE_LLM` 拾起 → 每根真实调 LLM（烧钱+慢），务必显式置 False。
"""
import logging
import shutil
import types
from pathlib import Path

import config as m

m.CFG.BACKTEST_USE_LLM = False          # 强制规则引擎（确定性对比，防 LLM 随机性污染）
print("BACKTEST_USE_LLM =", m.CFG.BACKTEST_USE_LLM, flush=True)

import main

main.setup_logging()
logging.getLogger().setLevel(logging.WARNING)   # 静默 INFO 日志，报告从 md 读取
logging.getLogger("httpx").setLevel(logging.ERROR)

BASE = Path(m.CFG.BASE_DIR)
OUT = BASE / "runs"
OUT.mkdir(exist_ok=True)
args = types.SimpleNamespace(mode="backtest", once=False,
                             use_llm=False, plot=False)

rows: dict[str, str] = {}
for model in ("intrabar", "close", "next_open"):
    print(f"\n######## 执行模型 = {model} ########", flush=True)
    m.CFG.EXECUTION_MODEL = model          # 实例属性热切换 → _execution_model() getattr 现读
    main.run_backtest(args)
    src = BASE / "backtest_report.md"
    if src.exists():
        shutil.copy(src, OUT / f"exec_{model}_report.md")
        rows[model] = src.read_text(encoding="utf-8")
        print(f"  -> 报告已存 runs/exec_{model}_report.md", flush=True)
    else:
        rows[model] = ""
        print(f"  !! 报告缺失（{model}）", flush=True)

print("\n\n==================== 三档执行模型对比汇总 ====================")
for model, md in rows.items():
    print(f"\n----- {model} -----")
    if not md:
        print("  (无报告)")
        continue
    for line in md.splitlines():
        if line.startswith("| ") and "指标" not in line and "---" not in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            print(f"  {cells[0]}: {cells[1]}")
