# 云端自动运行 + 手机仪表盘 部署指南

目标：电脑关机也能每个交易日自动采集数据、记账、出报告；手机（鸿蒙6 自带浏览器即可）随时查看。

架构：
```
GitHub Actions 云端（免费，24小时在线）
  ├─ 每交易日 14:00  盘中风险监控（止盈止损预警）
  ├─ 每交易日 16:00  收盘全流程（观察→评分→决策→全部文档→复盘→日报→验收）
  ├─ 每周六 10:00    周报
  └─ 每次运行后自动 git 提交回传账本与成果（数据持久化）
手机浏览器 → dashboard/index.html → 读取仓库里的 data/dashboard/*.json 渲染
```

## 一、部署步骤（约10分钟，只做一次）

1. **注册/登录 GitHub**（github.com）。
2. **新建仓库**：右上角 + → New repository → 名称如 `pbl-workflow` → 选 **Public**（公开；数据里没有个人隐私，公开才能让手机免登录读取）→ Create。
3. **推送代码**：在本机命令行执行（把 `你的用户名/pbl-workflow` 换成你的）：
   ```
   cd C:\Users\Lenovo\Desktop\证券投资\workflow
   git remote add origin https://github.com/你的用户名/pbl-workflow.git
   git push -u origin main
   ```
   （文件夹里已建好本地 git 仓库和首个提交；若提示输入密码，用 GitHub 的 Personal Access Token 登录，不是账号密码。）
4. **启用 Actions**：仓库页 → Settings → Actions → General → Allow all actions（默认已允许）。定时任务在推送后自动生效。
5. **验证第一次运行**：仓库页 → Actions → 选 "PBL定时运行" → Run workflow → task 选 `eod` → Run。约2分钟后变绿 ✔，
   仓库里会出现 `data/dashboard/*.json`。若想直接验证盘中风控，task 选 `riskwatch`。
6. **手机打开仪表盘**（三选一，推荐A）：
   - **A. 本地文件**：把 `workflow/dashboard/index.html` 发到手机（微信文件传输/数据线均可），用浏览器打开，
     在"数据源设置"里填 `https://raw.githubusercontent.com/你的用户名/pbl-workflow/main/data/dashboard/` → 保存并刷新。
   - **B. GitHub Pages**：仓库 Settings → Pages → Branch 选 main → Save，得到 `https://你的用户名.github.io/pbl-workflow/dashboard/`，
     手机浏览器打开并同样配置数据源（用 jsDelivr 加速更稳：`https://cdn.jsdelivr.net/gh/你的用户名/pbl-workflow@main/data/dashboard/`）。
   - **C. 远程触发 / 手机回填成交价（可选）**：仪表盘"高级"面板可填 GitHub 令牌远程触发一次运行；
     "回填实际成交价"面板还能把成交价直接写进仓库账本（`data/trades.csv`），并可选同时确认决策。
     创建入口：GitHub → Settings → Developer settings → Fine-grained tokens → 仅授权该仓库，
     权限勾选 **Actions: Read and write**（触发运行）和 **Contents: Read and write**（回填成交价）。令牌只存在手机本地。

## 二、重要事项

- **账本唯一权威**：启用云端后请在本机双击 `uninstall_scheduler.bat` 停用本地计划任务，
  否则两边各自记账会分叉。云端仓库的数据就是正式账本；本机如需同步，`git pull` 即可。
- **定时精度**：GitHub Actions 定时任务可能有 5—30 分钟延迟，属正常。14:00 的盘中预警可能略晚，
  可在仪表盘"高级"里手动触发 `riskwatch` 补跑。
- **60天休眠保护**：仓库连续60天无提交 Actions 会被暂停；每次运行都会产生提交，正常使用不会触发。
  寒假长期不用时，记得开学后手动 Run 一次。
- **隐私**：公开仓库内容 = 模拟交易数据 + 课程文档（不含姓名学号）。若你往 01_小组信息登记表 填了真实姓名学号，
  请把仓库改为 Private（Settings → General → Danger Zone → Change visibility），并在仪表盘用带令牌的 raw 地址读取。
- **成本**：全部免费（公开仓库 Actions 每月2000分钟额度，本项目每天约3分钟，绰绰有余）。

## 二点五、外部准时触发器（可选，解决 GitHub cron 延迟）

GitHub 的定时任务可能延迟 5—30 分钟甚至漏投递。工作流里已经加了 14:25 / 16:25 / 10:25 冗余补跑，
如果想更准，可以再挂一个外部定时器（免费）：

### 方案A：cron-job.org（推荐，5 分钟搞定）

1. 注册 https://cron-job.org → Create cronjob。
2. 填写：
   - **Title**：PBL 16:00 收盘全流程
   - **URL**：`https://api.github.com/repos/Albertdev12/pbl-workflow/actions/workflows/schedule.yml/dispatches`
   - **Schedule**：Every day 16:00（时区选 Asia/Shanghai；周末可只留周六 10:00 的 weekly 任务）
   - **Request method**：POST
   - **Headers**（逐个添加）：
     - `Authorization: Bearer <细粒度令牌>`
     - `Accept: application/vnd.github+json`
     - `Content-Type: application/json`
     - `User-Agent: pbl-cron`
   - **Request body**：`{"ref":"main","inputs":{"task":"eod"}}`
3. 保存后可点 "TEST RUN"，看到 HTTP 204 就是成功。
4. 同理再加两个：14:00 用 `{"task":"riskwatch"}`，周六 10:00 用 `{"task":"weekly"}`。

> 令牌：GitHub → Settings → Developer settings → Fine-grained tokens → 只授权本仓库 →
> 权限 `Actions: Read and write`。（触发用这个就够；手机回填成交价还需要 `Contents: Read and write`。）

### 方案B：Cloudflare Worker + Cron Triggers（进阶）

```js
export default {
  async scheduled(event, env, ctx) {
    await fetch("https://api.github.com/repos/Albertdev12/pbl-workflow/actions/workflows/schedule.yml/dispatches", {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + env.GH_TOKEN,
        "Accept": "application/vnd.github+json",
        "User-Agent": "pbl-worker",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs: { task: "eod" } }),
    });
  },
};
```

在 Worker 设置里加环境变量 `GH_TOKEN`，再配 Cron Trigger `0 8 * * 1-5`（UTC，等于北京时间 16:00）。

### 本地触发/观察（不需要浏览器）

```powershell
$env:GH_TOKEN = "<令牌>"
python tools/gh_run.py trigger eod   # 触发
python tools/gh_run.py wait          # 等待并打印每一步结论
python tools/gh_run.py run weekly    # 触发并等待
```

## 三、本地文件说明

| 文件 | 作用 |
|---|---|
| cloud_run.py | 云端入口：跑流水线 → 导出 data/dashboard/*.json → git 回传 |
| .github/workflows/schedule.yml | 定时+手动触发的 Actions 工作流 |
| requirements.txt | Python 依赖 |
| dashboard/index.html | 手机仪表盘（单文件，无外部依赖，鸿蒙浏览器直接用） |
| data/dashboard/ | 仪表盘数据：summary/nav/decisions/pool/market/alerts + **benchmark（沪深300净值）/todo（待办清单）/verify（决策后验证）** |
| src/verify.py | 决策后验证：T+5/T+10/T+20 表现与基准对照 |
| src/brief.py | 报告素材包（16）/周会材料包（18）生成 |
| src/benchmark.py | 沪深300净值序列，图表与仪表盘统一口径 |
| tools/fix_tech_basis.py | 一次性修复脚本（历史决策文本问题） |
| PROGRESS.md | 进度、待办与故障排查 |

## 三点五、仪表盘新增（2026-09-08）

- **今日待办**：自动汇总"待执行指令 / 待确认决策 / 待回填成交价 / 数据陈旧 / 验收缺项 / 风险预警"，
  每条都带可复制的本地命令，可勾选标记完成（状态存在手机本地）。
- **净值 vs 沪深300**：两条同起点归一曲线，直接看超额。
- **决策后验证**：每笔决策的 T+5/T+10/T+20 表现（到期后自动填充）。
- **回填实际成交价**：手机填编号+成交价即可写入仓库账本，可选同时确认决策；也可以只生成命令回家执行。

## 四、已完成的测试

### 2026-09-08（本轮收尾）

- ✅ 本地 eod 全流程：账本重放、文档生成、决策后验证、报告素材包、自动验收 94%（唯一缺项=待人工确认的新决策）
- ✅ 云端全绿：run #16（commit `a09f422`）用时 **3.0 分钟**；加入"东财接口按类别熔断"后，比上一轮 5.1 分钟缩短约 40%
- ✅ 云端产物核对：`data/dashboard/` 下 summary/nav/**benchmark**/todo/**verify**/decisions/pool/market/alerts 全部生成；
  `outputs/16_报告素材包`、`outputs/19_决策后验证.xlsx`、`outputs/charts/*.png` 随每次运行更新
- ✅ 手机仪表盘用 Node DOM 桩实测渲染：待办清单 / 组合与沪深300双曲线 / 决策后验证 / 持仓 / 决策 / 候选池均正常
- ✅ 回填成交价链路：`main.py fill` 在临时账本上验证（改价、改日期、改数量、重算金额、提示确认），正式账本未受影响
- ✅ 修复"技术面依据被逐字符拆开"的 bug（`"；".join(str)`），并用 `tools/fix_tech_basis.py` 还原历史 7 条决策记录
- ✅ CI 安装 fonts-noto-cjk，云端生成的图表中文不再显示为方框

### 2026-09-06（首轮）

- ✅ eod 全流程真实数据运行：抓行情/财务 → 评分 → 决策 → 文档 → 验收，总资产/持仓/现金与账本逐项核对一致
- ✅ riskwatch 运行：alerts.json 与 summary.recent_alerts 正常导出
- ✅ 幂等性：同日重复 eod，trades.csv 行数不变（无重复记账）
- ✅ git 回传链路：本地裸仓库验证 commit + push 成功（并修复了 push 未显式指定分支的bug）
- ✅ 仪表盘手机视口(390×844)浏览器实测：KPI/指令卡/持仓表/净值图/决策/候选池/市场观察/验收全部正常渲染，
  数据与账本核对一致；单点净值、空数据、加载失败均有兜底显示
- ⏳ Actions 端到端需你的 GitHub 仓库，推送后按"步骤5"验证首次运行即可

---

## 五、AI 智能推荐（DeepSeek Flash）

### 5.1 启用（只做一次，约 1 分钟）

1. 去 platform.deepseek.com 注册 → API Keys → 创建一个 key（`sk-` 开头）。
2. 打开你的 GitHub 仓库 → **Settings → Secrets and variables → Actions → New repository secret**
   - Name 填 **`DEEPSEEK_API_KEY`**（必须一模一样）
   - Secret 填刚创建的 key → Add secret
3. 完成。下一个定时点（或手动 Run workflow 选 `ai`）就会自动产出推荐。

> 没配密钥时系统**照常运行**，只是跳过 AI 部分并在日志里提示，不会报错。

### 5.2 每日运行时刻表（北京时间，周一至周五）

| 时间 | 任务 | AI 做什么 |
|---|---|---|
| 08:50 | `ai` | **盘前计划**：基于上一交易日收盘，给出今日可执行的买入清单 |
| 14:00 | `riskwatch` | **盘中分析**：用盘中实时快照更新推荐，保留收盘前下单窗口 |
| 16:00 | `eod` | **收盘复盘**：收盘数据定稿，给出下一交易日计划 |

周六 10:00 照常出周报（不含 AI）。三个时点各一次，全天共 3 次 AI 分析。

### 5.3 数据来源与准确性保障（三层）

1. **权威口径取价**：估值/推荐价一律用东方财富快照的**未复权真实成交价**
   （前复权序列在 ETF、除息日会有千分之一级偏差，已实测确认并规避）。
2. **双源交叉校验**：每只候选的收盘价由「东方财富快照」与「腾讯前复权日K」逐只比对，
   偏差 > 0.5% 的直接剔除，**不进模型**；模型也拿不到不可信的数字。
3. **防幻觉清洗**：模型返回的代码必须存在于当次候选表（幻觉代码直接丢弃），
   买入区间/止损/目标价全部锚定真实收盘价并按 ATR 约束在合理区间内。

数据源：东方财富公开行情接口（`push2`/`push2delay`/`push2his`，主源）+ 腾讯行情（备用源）。
主源主机首次调用自动探测并缓存，云端与本机都能用。

### 5.4 密钥安全

- 密钥**只**从环境变量 `DEEPSEEK_API_KEY` 读取，且只注入到工作流的"运行流水线"这一步
  （不放在 job 级 env，缩小暴露面）。
- 不写入任何文件、不进 git、不进 `config.json`。
- 所有日志与异常文本统一经 `_redact()` 过滤；落盘前再扫一遍，命中密钥直接中止写入。
- 仓库是 Public，但密钥存在 Secrets 里，公开仓库也读不到。

### 5.5 成本

单次约 8.1k 输入 tokens + 1k 输出 tokens，按 `deepseek-v4-flash` 定价（$0.14 / $0.28 每百万 tokens）
计算约 **$0.0014/次**。按 3 次/天、每周 5 天算，到 2026-12-18 累计约 **0.3 美元**。

### 5.6 输出位置

| 位置 | 内容 |
|---|---|
| 手机仪表盘「AI 智能推荐」面板 | 市场解读、风险等级、买入清单（区间/止损/目标/盈亏比/仓位）、回避方向 |
| `data/dashboard/ai.json` | 最新一次结果（原始 JSON，供二次开发） |
| `data/ai_advice.jsonl` | 历史全量（每次一行，最多保留 400 次，自动截断防膨胀） |

模型参数在 `config.json` 的 `ai` 段：改 `model` 可切 `deepseek-v4-pro`，改 `temperature` / `max_tokens` / `candidate_pool` 调整行为。

### 5.7 运行截止与延长

- **截止日**：`config.json` → `ai.run_until`（当前 `2026-12-18`）。到期后 AI 分析自动停止，
  其余课程流程不受影响；把日期改大即可延长。
- **临时停用 AI**：`config.json` → `ai.enabled` 改 `false`。
- **手动跑一次**：仓库 Actions → Run workflow → task 选 `ai`（按当前时间自动判定盘前/盘中/盘后）。
