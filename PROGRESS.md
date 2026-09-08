# 项目进度与交接说明（PROGRESS）

> 最后更新：2026-09-08　维护约定：每个重要节点更新本文件；实时运行状态看手机仪表盘或 `data/dashboard/summary.json`。

## 0. 一句话

模拟证券投资大赛自动化系统（云端 GitHub Actions + 手机仪表盘 + 本地工具箱）已完成第二轮系统性建设：
**云端全绿、账本准确、回测框架就位、数据源双活、决策证据链闭环（T+5/10/20 可验证）、报告素材自动汇总**。

## 1. 关键坐标

| 项目 | 位置 |
|---|---|
| 仓库 | https://github.com/Albertdev12/pbl-workflow （public，main） |
| 手机仪表盘 | https://albertdev12.github.io/pbl-workflow/dashboard/ |
| 本地目录 | `C:\Users\Lenovo\Desktop\证券投资\workflow` |
| 云端入口 | `cloud_run.py`（GitHub Actions 调用） |
| 本地入口 | `main.py` |
| 回测 | `backtest.py`（离线只读，结果写 `data/backtest_result.txt`） |
| 定时 | 14:00 riskwatch / 16:00 eod / 周六 10:00 weekly，另有 14:25 / 16:25 / 10:25 冗余补跑 |

## 2. 数据流

```
东财接口（主） ──失败──▶ 腾讯接口（备，只补缺口）──失败──▶ 本地CSV缓存(标 DATA_STALE)
        │
        ▼
行情/财务缓存(data/kline, data/fin) ─▶ 市场观察 ─▶ 候选池评分(基本面50%+技术面50%)
        │
        ▼
决策引擎（持仓风控 → 开仓评分 → 规则引擎前置校验 GREEN/YELLOW/RED）
        │
        ▼
账本 data/trades.csv（唯一事实来源，重放得现金/持仓/盈亏/费用）
        │
        ├─▶ 成果文档 outputs/01~15（docx/xlsx/pptx/png）
        ├─▶ 决策后验证 outputs/19（T+5/10/20 与沪深300对照）
        ├─▶ 报告素材包 outputs/16、周会材料包 outputs/18
        ├─▶ 自动验收 outputs/00（18项检查，写完成度）
        └─▶ 手机仪表盘 data/dashboard/*.json
```

## 3. 每日/每周流程

1. **14:00（riskwatch）**：盘中快照对照止损/移动止损线 + 市场急跌预警 → `data/riskwatch_log.jsonl`
2. **16:00（eod）**：刷新行情 → 市场观察 → 候选池 → 决策（幂等，同日只生成一次）→ 账本重放 → 全部文档 → 交易复盘 → 工作日报 → 决策后验证 → 报告素材包 → 自动验收
3. **周六 10:00（weekly）**：周报 + 周会材料包 + 中期路演PPT刷新

## 4. 当前状态（2026-09-08 收盘）

| 指标 | 数值 |
|---|---|
| 组合总资产 | 499.07 万元（-0.19%） |
| 同期沪深300 | +0.24%（同起点） |
| 现金 | 100.06 万元（20.0%） |
| 持仓 | 紫金矿业、沪深300ETF、中国平安、长江电力 |
| 累计交易 | 4 笔 |
| 自动验收 | **94%**（17/18） |
| 唯一缺项 | D2026090801 待人工确认（等 09-09 开盘执行后回填 + 确认即回 100%） |

## 5. 已完成的修复与升级

### 5.1 稳定性
- 修复 `industry_board_rank()` 无兜底导致的 exit 1（东财 502）→ 缓存回退 + 空列表降级
- `_refresh_if_stale()`：收盘发现行情落后于应有交易日 → 每 60 秒重试 3 次自愈
- **双数据源**：东财失败自动切腾讯（`qt.gtimg.cn` / `web.ifzq.gtimg.cn`），只补缺口不覆盖东财口径；指数成交额缺失时市场判断优雅降级
- RSI 在"14 天无下跌日"时得 NaN → `float(NaN)` 崩溃点修复
- Actions 升级到 `checkout@v7` / `setup-python@v7`（Node24）、超时 30 分钟、pip 重试、失败自动开 Issue
- 任务选择改用 `date -u +%H`（原来 14:00 的 riskwatch 从未被选中）

### 5.2 数据与账本
- 09-08 按实际成交价回填 3 笔买入（510300=4.641、601899=33.75、601318=56.12）
- 修复**技术面依据文本被逐字符拆开**的 bug（`"；".join(str)` → 直接取字符串），并还原历史 7 条决策记录（`tools/fix_tech_basis.py`）

### 5.3 策略
- 回测发现：原策略 +70.6% 是**前视偏差**（打乱基本面分数后崩到 +3.5%）
- 无偏口径下：固定止盈 -2.75% / 跌破20日线 +7.40% / **移动止损(高点回撤12%) +20.76%**
- 已上线：`trailing_stop_pct=0.12` 替代固定止盈、`etf_target_weight=0.30`、`max_batch_build=2`

## 6. 回测结论（无偏对照，2025-04-21 ~ 2026-09-07，338 个交易日）

| 离场规则 | 总收益 | 最大回撤 | 夏普 | 交易笔数 | 卖出胜率 |
|---|---|---|---|---|---|
| 固定止盈+9%/止损-8% | -2.75% | -16.93% | -0.11 | 110 | 29% |
| 跌破20日均线 | +7.40% | -14.63% | 0.40 | 359 | 32% |
| **移动止损（高点回撤12%）** | **+20.76%** | **-12.54%** | **0.94** | **64** | 22% |
| 对照：等权持有12只 | +18.48% | -7.21% | 1.10 | 12 | — |

> 交易越频繁收益越低（12笔 > 64笔 > 110笔 > 359笔），费用与择时损耗是主要拖累。

## 7. 本批次新增能力

| 能力 | 入口 | 产物 |
|---|---|---|
| 决策后验证（T+5/10/20 + 基准对照） | `python main.py verify` | `outputs/19_决策后验证.xlsx`、`data/verify_log.jsonl`、仪表盘 `verify.json` |
| 报告素材包 | `python main.py brief` | `outputs/16_报告素材包_日期.md`（四段手写素材） |
| 周会材料包 | `python main.py weekly` | `outputs/18_周会材料包_周次.md` |
| 回填实际成交价 | `python main.py fill ID 价格 [日期] [--confirm]` | 更新 `data/trades.csv` |
| 图表增强 | eod 自动 | 净值vs沪深300、收益对比、持仓结构、候选池评分（PPT/报告内嵌） |
| 手机待办清单 | 仪表盘 | `todo.json`（含可复制命令） |
| 手机回填成交价 | 仪表盘"回填"面板 | 直接写仓库 `data/trades.csv` / `confirmations.csv` |

## 8. 待办

### 需要人工完成（唯一挡在 100% 前的）
- [ ] 09-09 开盘在同花顺买入长江电力 13,500 股 → 回填实际成交价 → 确认决策：
      `python main.py fill D2026090801 <实际成交价> 2026-09-09 --confirm`（或手机仪表盘"回填"面板一键提交）

### 后续增强（非阻塞）
- [ ] 外部准时触发器（cron-job.org / Cloudflare Worker 调 `workflow_dispatch`），解决 GitHub cron 延迟
- [ ] 观察池 12 → 20 只：在 `config.json` 的 `universe` 追加 `{code,name,kind,industry,secid}`，
      然后 `python main.py data`（抓缓存）→ `python backtest.py`（重新回测确认结论未变）→ `python main.py eod`
- [ ] 决策后验证积累到 20+ 样本后，做一次"评分门槛 60 分是否合适"的复盘

## 9. 常用命令

```bash
cd C:\Users\Lenovo\Desktop\证券投资\workflow
git pull                                  # 本地同步（工具操作前必做）
python main.py validate                   # 自动验收
python main.py eod                        # 本地跑一遍收盘全流程
python main.py fill D2026090801 27.90 2026-09-09 --confirm   # 回填实际成交价并确认
python main.py verify                     # 决策后验证
python main.py brief                      # 报告素材包
python main.py report / package           # 重生成文档 / 打包
python backtest.py                        # 回测
```

云端：仓库 → Actions → PBL定时运行 → Run workflow（task: eod / riskwatch / weekly / report / validate / verify）

## 10. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| Actions 红叉 exit 1 | 数据源波动 / 依赖安装失败 | 看日志；**不要对旧运行点 Re-run**（锁定旧 commit），用 Run workflow 新触发 |
| 手机显示旧数据 | CDN/缓存 | 下拉刷新 / "恢复默认地址" / 网址加 `?t=123` |
| `DATA_STALE` 红字 | 行情未更新到最新交易日 | 等自愈重试；或手动触发 eod |
| `DATA_FALLBACK` 提示 | 东财接口 502/超时 | 正常，已自动切腾讯源，无需处理 |
| 定时任务没跑 | GitHub cron 延迟/漏投递 | 手动 Run workflow；冗余 cron 已加 25 分补跑 |
| 60 天无提交 | Actions 暂停 | 手动 Run 一次即可恢复 |

## 11. 注意事项

- **账本唯一权威**：`data/trades.csv`；本地计划任务请保持停用（`uninstall_scheduler.bat`），避免与云端分叉
- **不要 Re-run 旧失败运行**：会沿用旧 commit，修复无效
- 仓库公开，01_小组信息登记表请勿填真实姓名学号（或改私有仓库）
- 模拟盘数据，不构成任何投资建议
