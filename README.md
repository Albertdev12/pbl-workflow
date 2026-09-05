# 模拟证券投资大赛 · 全自动工作流（同花顺模拟炒股配套）

> 📱 想在电脑不开机时也自动采集、用手机（鸿蒙6浏览器）查看数据？见 [README_CLOUD.md](README_CLOUD.md) —— 免费云端方案（GitHub Actions + 手机网页仪表盘），已含部署步骤与测试结论。

依据《PBL项目一：模拟证券投资大赛学生任务书》构建，实现 **数据采集 → 选股 → 分析 → 规则校验 → 决策 → 记录/成果生成 → 自动验收 → 提交打包** 全流程自动化。全部真实行情数据驱动，**每交易日运行2次**（14:00盘中风控 + 16:00收盘全流程）+ 周六周报。

## 一、快速开始

```bash
cd workflow
python main.py init      # 首次运行：建账 + 市场观察 + 候选池 + 初始建仓 + 全部成果文档
python main.py daily     # 收盘全流程（16:00 由计划任务自动执行）
python main.py eod        # 收盘全流程聚合命令（16:00 自动执行）
python main.py data|market|pool|riskwatch|close|review|dailyreport  # 分时段子命令，可单独手动运行
python main.py weekly    # 周报（周六10:00 自动执行）
python main.py report    # 仅根据已有数据重新生成全部成果文档
python main.py validate  # 自动验收：核对任务书成果清单完成度
python main.py package   # 打包最终提交包 zip
python main.py confirm D2026090401 [更多ID]   # 人工确认决策（PENDING→CONFIRMED）
```

定时任务（已注册9个，统一写入 `workflow/run.log`）：

| 时间 | 任务名 | 命令 | 内容 |
|---|---|---|---|
| 14:00 | 模拟炒股工作流_1_盘中风险监控 | riskwatch | 实时快照对照止盈止损线，触发即生成预警文件（唯一盘中任务，选14:00留足处理时间） |
| 16:00 | 模拟炒股工作流_2_收盘全流程 | eod | 收盘数据刷新→市场观察→候选池评分→决策→全部成果文档→交易复盘→工作日报→自动验收 |
| 周六10:00 | 模拟炒股工作流_3_周六周报 | weekly | 周报+组合分析+策略复盘 |

> 说明：曾按GPT规范第十六节配置过一天8次（08:00~18:00），实测后精简为2次/日——盘中观察/候选池/复盘/日报用的都是收盘数据，在16:00一次算完即可；14:00盘中风控是唯一必须盘中运行的任务。

卸载：双击 `uninstall_scheduler.bat`；流水线状态见 `data/pipeline_state.json`；盘中预警文件为 `outputs/盘中风险预警_日期.txt`（仅触发时生成）。

## 二、每日自动流程

1. **数据采集与完整性检查**：东财接口日K/快照/财务/板块（自动重试3次、失败回退本地缓存并标 DATA_STALE），每次调用写入 `data/fetch_log.jsonl` 审计
2. **市场观察**：指数、成交额、行业板块涨跌榜 → 自动生成"市场判断/投资机会/主要风险" → 《02_市场观察记录.xlsx》
3. **候选股票池**：基本面评分（F10：ROE/营收增速/净利增速/净利率）+ 技术面评分（均线/MACD/RSI/量能/支撑压力）综合排序取前8、保证ETF入池，**未入选证券记录淘汰原因** → 《03_候选股票池及初步分析表.xlsx》
4. **两层决策**：
   - 策略层产出拟交易（止盈+9%/止损-8%/MACD死叉技术分<45离场；评分≥60开仓；每两周至少1笔；每日最多新开3只；首建优先ETF底仓）
   - **规则引擎前置校验**（risk.py）：单只≤30%、现金≥20%、100股整数倍、卖出数量≤持仓，任一违规则 **BLOCK_TRADE** 并留审计记录（`data/risk_blocks.jsonl`）；风险状态 GREEN/YELLOW/RED
5. **账户账本**：`data/trades.csv` 为唯一事实来源，重放得出现金/持仓/已实现盈亏/费用
6. **成果文档自动刷新**（outputs/，对应任务书《项目最终成果清单》15个文件）
7. **自动验收**：`validator.py` 按18项检查输出完成度百分比，写入 `outputs/00_完成度自检报告.json`，达标才允许打包提交

## 三、人工判断的合规设计（PENDING/CONFIRMED）

任务书禁止"AI代替个人判断"。因此所有决策的人工判断状态初始为 **PENDING**，你在同花顺APP执行后运行 `python main.py confirm 决策ID`（或直接在 `data/confirmations.csv` 填ID）即变为 **CONFIRMED**；验收器会把"人工是否确认"计为检查项，未确认则 `ready_for_submission=false`。AI使用记录同样保留 PENDING 语义，不做任何伪造。

## 四、自动生成的成果清单

| 文件 | 对应任务书成果 |
|---|---|
| 01_小组信息登记表.xlsx | 小组信息登记表（手填） |
| 02_市场观察记录.xlsx | 市场观察记录 |
| 03_候选股票池及初步分析表.xlsx | 候选股票池 |
| 04_初始投资方案.docx | 初始投资方案 |
| 05_基本面分析_代码_名称.docx | 基本面分析（候选池前3个股） |
| 06_技术分析记录.xlsx | 技术分析记录 |
| 07_投资决策记录.xlsx（18列，含市场依据、规则校验、人工状态） | 投资决策/交易日志 |
| 08_委托指令单_日期.docx | 同花顺下单依据 |
| 09_交易日志与盈亏归因.xlsx | 完整交易记录 |
| 10_策略调整记录.docx | 策略调整记录 |
| 11_AI使用记录.xlsx | AI使用记录（Prompt→输出→核验→人工判断→采用） |
| 12_中期路演.pptx | 中期路演PPT |
| 13_投资总结报告.docx | 投资总结报告（含净值曲线） |
| 00_完成度自检报告.json | 自动验收结果 |

`python main.py package` 生成 `证券投资PBL_最终提交包.zip`：FINAL_SUBMISSION/ 下按 01~17 编号目录组织，含"数据来源与说明"。

## 五、与 GPT 部署规范的对照（本版本 = 原架构 + 采纳其6项机制）

| GPT规范提议 | 处理 | 理由 |
|---|---|---|
| 规则引擎前置 + BLOCK_TRADE + GREEN/YELLOW/RED | ✅ 采纳（src/risk.py） | 比内联约束更可审计 |
| 人工判断 PENDING/CONFIRMED | ✅ 采纳（confirmations.csv） | 不伪造人工判断，更合规 |
| project_validator 自动验收器 | ✅ 采纳（src/validator.py，18项） | 量化提交就绪度 |
| FINAL_SUBMISSION 打包 | ✅ 采纳（src/package.py） | 一键生成提交zip |
| DATA_UNAVAILABLE/DATA_STALE/采集时间戳/淘汰原因 | ✅ 采纳 | 数据完整性审计 |
| 交易记录增加市场依据/人工状态字段 | ✅ 采纳（决策记录18列） | 任务书字段全覆盖 |
| SQLite 数据中心 | ❌ 不迁移 | JSONL/CSV 账本可读可追溯，重放即状态，单机无并发需求 |
| GLM API 常驻编排层 | ❌ 不引入 | 指标必须程序计算（规范自身要求）；一天一次的确定性流水线不需要LLM在线编排，解释/复盘由会话内AI承担 |
| FastAPI/APScheduler/Docker/YAML | ❌ 不采用 | 单机Windows场景，Windows计划任务已覆盖；避免无谓依赖 |
| 一天8次调度 | ❌ 不采用 | 用户要求一天一次；收盘后15:35单次运行已覆盖全部环节 |

## 六、策略参数（config.json）

| 参数 | 默认 | 对应任务书要求 |
|---|---|---|
| initial_capital | 5,000,000 | 每人500万元虚拟资金 |
| max_single_position_pct | 0.30 | 单只≤30% |
| min_cash_pct | 0.20 | 风险控制 |
| take_profit_pct / stop_loss_pct | 0.09 / 0.08 | 止盈止损纪律 |
| min_trades_per_2weeks | 1 | 每两周至少1笔有效交易 |
| min_buy_score | 60 | 综合评分开仓门槛 |
| universe | 12只股票+ETF | 观察池（secid前缀：1=沪，0=深） |

## 七、目录结构

```
workflow/
├── main.py            # 入口：init / daily / weekly / report / validate / package / confirm
├── config.json        # 课程信息 + 策略参数 + 观察池
├── install_scheduler.bat / uninstall_scheduler.bat
├── src/
│   ├── em_client.py   # 东财接口（重试/缓存回退/采集审计）
│   ├── indicators.py  # 均线/MACD/RSI/支撑压力/量能 → 信号与评分
│   ├── fundamental.py # F10财务 → 基本面评分与判断
│   ├── screening.py   # 候选池筛选（含淘汰原因）
│   ├── risk.py        # 规则引擎（BLOCK_TRADE / GREEN/YELLOW/RED）
│   ├── decision.py    # 两层决策（策略层+规则校验层）
│   ├── portfolio.py   # 交易账本重放
│   ├── records.py     # 市场观察/决策/AI记录/净值/确认/拦截 存储
│   ├── validator.py   # 自动验收器（18项检查）
│   ├── package.py     # 最终提交包打包器
│   └── reports.py     # 成果文档生成（docx/xlsx/pptx/png）
├── data/              # 行情缓存 + 账本 + 过程记录 + fetch_log + pipeline_state
├── outputs/           # 自动生成的成果文档
└── FINAL_SUBMISSION/  # 打包用编号目录（生成 证券投资PBL_最终提交包.zip）
```
