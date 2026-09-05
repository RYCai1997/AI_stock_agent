# LLM 驱动量化交易 Agent（多币种 · 多周期 · Paper Trading）

> 按所选周期（默认 4H，支持 15m/30m/1h/2h/4h/6h/8h/12h/1d）拉取 Binance 行情 →
> 计算技术指标 → 生成「战情简报」→ 交给 **DeepSeek**（deepseek-chat / deepseek-reasoner，
> 走 OpenAI 兼容端点、requests 裸调、免装 openai SDK）按**交易宪法**决策 →
> 脚本层强制风控撮合 → 自动执行模拟交易。内置完整回测引擎与
> 「防 LLM 幻觉」多重防线。支持 BTC/ETH/DOGE/SOL 自由切换，界面内嵌
> 权益曲线 / 买入持有 / 价格走势实时图表。

---

## 1. 快速开始（三步）

```bash
# ① 进入项目目录并安装依赖（或双击 install_deps.bat）
cd llm_trading_agent
python -m pip install -r requirements.txt

# ② 配置 API 密钥：把 .env.example 复制为 .env，填入 DeepSeek 密钥
#    复制 .env.example .env   （Windows）
#    编辑 .env：填 DEEPSEEK_API_KEY（申请 https://platform.deepseek.com/api_keys）

# ③ 运行
python main.py --mode backtest   # 模式2：回测（不填密钥也能跑，走内置规则引擎）
python main.py --mode live       # 模式1：实时模拟（每4小时自动运行）
```

Windows 用户可直接双击：`install_deps.bat` → `run_backtest.bat` / `run_live.bat`。

> **✨ 最省事的入口：图形启动面板**
> 双击 **`Launch_Agent.bat`**（或在桌面上用面板自带的「桌面快捷方式」按钮生成图标），
> 即可打开中文图形控制台。面板左侧可：
> - **选标的 / 切片周期 / 回测范围 / 实盘起点**：BTC·ETH·DOGE·SOL 自由切换（也可手输任意 Binance 交易对）；
>   切片间隔 15m~1d 可选；回测可设起止日期（超 1000 切片自动截取最近段并在日志/报告标注实际起点）；
> - **填 API Key / 选模型**：固定 DeepSeek（OpenAI 兼容、免装 SDK），可切换 deepseek-chat / deepseek-reasoner，密钥只写 .env、界面掩码显示；
> - **勾选做空开关**（默认关闭=纯现货做多，见 §6）；
>
> 右侧为**内嵌图表 + 运行日志**：回测结束自动绘制「策略权益 vs 买入持有 + 价格走势」三线图；
> 实盘循环若设置了实盘起点，先以**规则引擎快速回放补齐历史曲线**（只影响显示、不调用 LLM、不烧 token），
> 之后每来一个新切片曲线**实时刷新**。图表可一键导出 PNG。
> 面板仅依赖 Python 自带 tkinter（Anaconda 开箱即用），首次使用先跑一次 `install_deps.bat`。

> **没有 API 密钥也能跑通全流程**：程序会自动降级到内置的「宪法机械执行引擎」
> （确定性规则，离线可用）。填入密钥后自动切换为真实 LLM 决策。

---

## 2. 两种运行模式

| 模式 | 命令 | 说明 |
|---|---|---|
| 实时模拟（默认） | `python main.py --mode live` | 启动后立即执行一个切片，之后按 `INTERVAL` 周期自动运行（默认 4 小时）；若设了 `LIVE_START` 会先从该时间点用规则引擎预热补齐历史曲线（不烧 token）再进入实时；默认每 2 分钟巡检一次止损；日志写入 `trading.log` |
| 一次性回测 | `python main.py --mode backtest` | 从 `START_DATE` 到 `BACKTEST_END`（默认到现在）逐根 K 线回放，输出绩效报告；区间超过 `MAX_BACKTEST_BARS`(默认 1000) 个切片时**自动截取最近段**并在日志与报告标注实际起点 |

辅助参数：

- `--once`：live 模式只跑一个切片就退出（联调用，跳过预热）
- `--use-llm`：回测时逐根 K 线调用**真实 LLM**（⚠️ 每切片 1 次 API，费用与耗时巨大，默认关闭）
- `--plot`：回测结束后额外绘制权益曲线图 `backtest_equity_chart.png`（需 `pip install matplotlib`，缺失时自动跳过）

**通用化回测参数（任意切币/切周期/切区间，仅 `backtest` 档生效）**：
`--symbol BTCUSDT`、`--interval 1d`、`--start YYYY-MM-DD`、`--end YYYY-MM-DD`

**引擎护栏开关（V10 Guardrail 策略，见 `STRATEGY_v10_guardrail.md`）**：
`--v10-rl1 breach --v10-rl1-cool 20 --v10-rl2`
——① 收盘 < EMA50 强制平仓（结构止损）② 离场后冷却 20 根禁 BUY ③ 弱市（收盘 < EMA50）BUY 压杠杆至 1x。默认全关，对旧路径零影响。

**一键 A/B 对照（护栏 on/off 各跑一次自动出对比表）**：
`python run_strategy_ab.py --symbol ETHUSDT --interval 1d --start 2021-10-01 --end 2022-12-31 [--use-llm]`

回测产出：控制台报告 + `backtest_report.md`（绩效指标 + 风控拦截统计）+ `backtest_equity.csv`（逐 K 线权益曲线）+ `runs/curve_backtest.csv`（供面板内嵌图表：time/equity/price）+（可选）权益曲线图 PNG。实盘曲线实时写入 `runs/live_curve.csv`。

---

## 3. 项目结构

```
llm_trading_agent/
├── config.py          # 配置管理（.env 加载、交易参数、标的/周期/区间、LLM选择、文件路径）
├── data_fetcher.py    # Binance K线获取（多域名故障切换、symbol/interval/日期分页）+ pandas 原生指标
├── prompt_builder.py  # 战情简报构建（内嵌【交易宪法】System Prompt，一字不差；标题随标的/周期）
├── llm_client.py      # DeepSeek 调用（requests 裸调 OpenAI 兼容端点）+ 防御性JSON解析 + 规则引擎
├── order_executor.py  # 订单执行与风控（仓位上限/止损/熔断/宪法拦截）+ state.json
├── main.py            # 主循环（按所选周期调度 + live/backtest 双模式 + 预热回放 + 绩效报告）
├── run_strategy_ab.py # V10 护栏一键 A/B 对照（护栏 on/off 各跑一次自动出对比表）
├── STRATEGY_v10_guardrail.md  # V10 Guardrail 通用策略规格（规则/参数/实证/边界/自测入口）
├── state.json         # 持久化状态（持仓/盈亏/交易历史，实盘模式自动读写；删除即重置账户）
├── requirements.txt   # 依赖清单
├── .env / .env.example  # API 密钥与参数（.env 不入库）
├── install_deps.bat   # 一键装依赖（双击）
├── run_live.bat       # 双击跑实盘模拟
├── run_backtest.bat   # 双击跑回测
├── Launch_Agent.bat   # 🖱 双击打开图形启动面板（无黑框，推荐入口）
├── launcher_gui.pyw   # 图形启动面板本体（tkinter 深色控制台 + matplotlib 内嵌图表）
├── runs/              # 任务日志副本 + curve_backtest.csv / live_curve.csv（面板图表数据源）
└── README.md
```

---

## 4. 架构与数据流

```
Binance API ──> data_fetcher.fetch_klines()   （最近500根 / 历史分页）
                     │
              calculate_indicators()          （EMA200/RSI14/BOLL/MACD/量比，纯pandas）
                     │
              get_latest_slice()  →  最新4H切片
                     │
        prompt_builder.build_full_prompt()    （交易宪法 system + 战情简报 user）
                     │
        llm_client.call_llm()                 （temperature=0.1, max_tokens=300）
                     │  action / confidence / stop_loss / reasoning
                     │  spot模式由引擎把信心映射为10→25→50→75%目标仓位
                     ▼
        order_executor.execute_decision()     （强制风控，见 §6）
                     │
             更新现金/币量/均价/盈亏 → 写回 state.json → 日志
```

**交易日历**：K 线时间统一换算为北京时间；「当日亏损熔断」按北京时间自然日滚动。

---

## 5. 指标说明（pandas 原生实现，无需 TA-Lib）

| 指标 | 参数 | 实现要点 |
|---|---|---|
| EMA200 | 周期 200 | `ewm(span=200, adjust=False, min_periods=200)`，不足 200 根为 NaN（预热） |
| RSI | 14 | Wilder 平滑（`alpha=1/14`），与 TA-Lib 高度一致；全涨无下跌时按 100 处理 |
| 布林带 | 20, 2σ | 总体标准差（`ddof=0`，同国内行情软件惯例） |
| MACD | 12, 26, 9 | DIF=EMA12−EMA26，DEA=DIF 的 EMA9，柱体=(DIF−DEA)×2 |
| 量比 | 20 | 当前量 ÷ 20 期均量（放量/缩量判断） |

> 全部指标均为**因果滤波器**：第 i 根只依赖前 i 根数据，因此回测中全量预计算后
> 逐根回放**不会引入未来函数**。

---

## 6. 强制风控（防 LLM 幻觉/抽风的最后防线）

无论 LLM 输出什么，脚本层在 `order_executor.py` 中强制执行：

| 防线 | 规则 |
|---|---|
| JSON 解析失败 / API 异常 | 自动降级 `HOLD`，记录错误，程序不中断；网络重试 3 次（2s/4s 指数退避） |
| 单笔上限 | 实际下单量 = min(LLM建议, **5%**, 剩余仓位空间) |
| 总仓位硬顶 | 持仓市值不得超过总权益的 **20%** |
| 宪法拦截 | 价格 < EMA200 且 LLM 输出 BUY → 强制改 HOLD 并记录「宪法拦截」；对称地，价格 > EMA200 且输出 SELL 开空 → 同样拦截（仅开启做空后生效） |
| 累计亏损熔断 | 累计盈亏 < −2% → 强制忽略所有 BUY |
| 当日熔断 | 当日亏损超 −2% → 当天剩余时间所有 BUY 被拦截（按北京时间日） |
| 统一止损 | 持仓均价回撤 2% 自动 CLOSE；实盘模式每 2 分钟额外巡检一次最新价 |

**现货语义（默认）**：`TRADING_MODE=spot` 时只持有标的现货或现金，固定 1×，不做空、无强平、无 funding。中/高信心首次 BUY 只建立 10% 试探仓；持仓盈利且价格、EMA50、MACD 确认后，才按信心逐级增加至 25% / 50% / 75%。低信心 BUY 不开仓；SELL 按信心减仓，高信心 SELL 全部回到现金。

**旧合约路径（仅供复现）**：先设 `TRADING_MODE=futures`，再设 `ALLOW_SHORT=true` 才允许做空。旧路径仍保留，但不是当前策略研究主线。
- 该路径继续使用逐仓保证金、可配置杠杆、强平和历史 funding 账本；其配置及旧报告不能与现货结果混合比较。

---

## 7. .env 参数一览

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 空 | DeepSeek 密钥（申请 https://platform.deepseek.com/api_keys） |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | DeepSeek OpenAI 兼容端点（requests 裸调，一般无需改） |
| `LLM_PROVIDER` | `deepseek` | 当前仅支持 `deepseek`（保留字段便于未来扩展） |
| `LLM_MODEL` | `deepseek-chat` | `deepseek-chat`（通用）或 `deepseek-reasoner`（推理、更慢） |
| `SYMBOL` | `BTCUSDT` | 交易对：BTC/ETH/DOGE/SOL 下拉可选，也可手输任意 Binance 交易对 |
| `INTERVAL` | `4h` | 切片/K线周期：`15m/30m/1h/2h/4h/6h/8h/12h/1d` |
| `TRADING_MODE` | `spot` | `spot`=现货 long/flat 主线；`futures`=旧合约复现路径 |
| `SPOT_MAX_EXPOSURE_PERCENT` | `75` | 现货最大市值仓位，至少保留约25%现金 |
| `SPOT_PROBE_PERCENT` | `10` | 中/高信心首次 BUY 的试探仓位 |
| `SPOT_SIZING_MODE` | `confidence` | `fixed` 为固定仓对照；`confirmed` 为实验性 10→25→50→75% 价格确认仓位 |
| `SPOT_RISK_BUDGET_PERCENT` | `2.0` | confirmed 模式按结构止损距离限制单仓权益风险 |
| `SPOT_MEDIUM_TARGET_PERCENT` | `25` | 中信心确认后的目标仓位上限 |
| `SPOT_HIGH_STEP_PERCENT` | `50` | 高信心中间档，下一档为最大75% |
| `SPOT_MIN_REBALANCE_PERCENT` | `5` | 目标变化不足5个百分点不成交，降低震荡磨损 |
| `START_DATE` | `2025-08-10` | 回测起始日期（超上限会自动后移） |
| `BACKTEST_END` | 空 | 回测截止日期（空=到现在） |
| `LIVE_START` | 空 | 实盘时间起点：设了则在实盘循环启动时用规则引擎预热补齐该起点→当前的历史曲线（不烧 token） |
| `MAX_BACKTEST_BARS` | `1000` | 回测/预热切片上限，超过自动截取最近段并在日志与报告标注实际起点 |
| `MAX_POSITION_PERCENT` | `20` | 旧合约路径的单仓保证金上限（%） |
| `SINGLE_ORDER_PERCENT` | `5` | 旧合约路径单次保证金上限（%） |
| `STOP_LOSS_RATIO` | `0.02` | 止损比例 |
| `DAILY_LOSS_LIMIT_PERCENT` | `2.0` | 当日亏损熔断线（%） |
| `OVERALL_LOSS_LIMIT_PERCENT` | `5.0` | 交易门控峰值回撤熔断线（%） |
| `EQUITY_DRAWDOWN_LIMIT_PERCENT` | `15.0` | 账户高水位回撤停机线；触发后需人工审查复位 |
| `ALLOW_SHORT` | `false` | 仅 `TRADING_MODE=futures` 时有效；spot 始终禁止做空 |
| `CAPITAL` | `100000` | 模拟本金（USDT） |
| `BINANCE_BASE_URLS` | `api.binance.com,data-api.binance.vision` | 行情端点，自动故障切换 |
| `BACKTEST_USE_LLM` | `false` | 回测是否调真实 LLM |

---

## 8. 常见问题

**Q1：Binance 请求超时/返回空？**
部分网络会屏蔽 `api.binance.com`。程序内置备用端点 `data-api.binance.vision` 自动切换；
仍不行可在 `.env` 中自定义 `BINANCE_BASE_URLS`（如走代理域名），或为 Python 配置系统代理。

**Q2：调用 DeepSeek 报连接错误 / 401？**
DeepSeek 为国内直连（一般无需代理）。报 401 请检查 `.env` 中 `DEEPSEEK_API_KEY`
是否粘贴完整；报网络错误可设置环境变量 `HTTPS_PROXY` 后重试。
本项目用 requests 直连 DeepSeek 的 OpenAI 兼容端点，**无需安装 openai/anthropic SDK**。

**Q3：回测数据不足 / EMA200 全为 NaN？**
EMA200 需要至少 200 根 K 线（4H≈33 天；周期越短所需天数越少，如 1H≈9 天）。
若 `START_DATE` 距今太近，请把日期往前调。实盘模式固定拉取最近 500 根，已足够。

**Q4：回测为什么不默认调用真实 LLM？**
一次回放最多 1000 个切片 = 至多 1000 次 API 调用，费用与耗时都很大。
默认用确定性规则引擎验证流水线与风控；想评估 LLM 决策质量可 `--use-llm`（建议先缩小日期区间）。

**Q5：如何重置实盘账户？**
删除 `state.json` 后重新运行 live 即回到初始本金 10 万 USDT（面板状态卡同步归零）。

**Q6：交易决策日志在哪？**
控制台 + `trading.log`（10MB 轮转，保留 3 份）。

**Q7：回测区间太长会被怎样处理？**
自动截取**最近 1000 个切片**（`MAX_BACKTEST_BARS` 可调），在运行日志、回测报告与
面板日志中都会标注实际起点（如“起点自动调整为 2026-03-20”），不会静默跑超长区间。

**Q8：内嵌图表的曲线从哪来？**
回测结束自动绘制 `runs/curve_backtest.csv`（time/equity/price）；实盘每切片实时追加
`runs/live_curve.csv`，面板每 2 秒检测新节点自动重绘。若设了实盘起点，启动时先用
规则引擎回放补齐历史段（只影响显示，不调用 LLM、不消耗 token），随后实时切片在
曲线右端逐个追加。图表上红线=策略权益、蓝虚线=买入持有（以首行价为基准）、黄线=价格。

---

## 9. 免责声明

本项目仅用于**策略研究与学习**，所有交易均为模拟（Paper Trading），不涉及真实资金，
不构成任何投资建议。加密货币价格波动剧烈，请勿据此进行真实交易。

---

## 10. ETF Rotation V1（独立研究，不接订单）

`portfolio_core/` 和 `run_etf_rotation.py` 提供零 LLM、只做多的多ETF组合回测。当前规则为：

- 6个月动量与12-1个月动量各占50%；
- 价格位于200日均线上方且综合动量为正才可入选；
- 月末选Top N，下一交易日收盘执行；
- 单ETF最高30%，未分配部分保留现金；
- 同时比较70%、90%、100%总目标仓位与SPY买入持有。

联网获取Yahoo复权收盘价：

```powershell
python run_etf_rotation.py --start 2010-01-01 --end 2026-09-05
```

也可使用本地CSV，避免数据源变化。每个文件命名为 `SYMBOL.csv`，包含 `date,adjusted_close`：

```powershell
python run_etf_rotation.py --data-dir D:\path\to\prices
```

回归测试：

```powershell
python -m unittest test_etf_rotation.py
```

ETF重仓股新闻接口位于 `portfolio_core/news_overlay.py`。持仓事实必须带披露日期与来源；LLM只允许提供新闻风险复核，不直接生成仓位或交易指令。完整变更边界见 `CHANGELOG_2026-09-05_ETF_ROTATION_V1.md`。

五ETF的EMA200突破与5%峰值回撤退出是单独实验，不改变轮动基线：

```powershell
python run_etf_ema200_trailing_experiment.py --prices runs\etf_rotation_v1_YYYYMMDD_HHMMSS\adjusted_close.csv
```

规则、结果和未修改范围见 `CHANGELOG_2026-09-05_ETF_EMA200_TRAILING5_EXPERIMENT.md`。

10%峰值回撤与2%固定亏损止损的A/B实验：

```powershell
python run_etf_ema200_trailing10_stop2_experiment.py --prices runs\etf_rotation_v1_YYYYMMDD_HHMMSS\adjusted_close.csv
```

详细结果见 `CHANGELOG_2026-09-05_ETF_EMA200_TRAILING10_STOP2.md`。

A/H股方向ETF的2%、3%、5%止损比较：

```powershell
python run_etf_ah_ema200_stop_sweep.py --prices runs\etf_rotation_v1_YYYYMMDD_HHMMSS\adjusted_close.csv
```

结果与适用性判断见 `CHANGELOG_2026-09-05_ETF_AH_STOP_SWEEP.md`。
