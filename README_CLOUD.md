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
   - **C. 远程触发（可选）**：仪表盘"高级"面板可填 GitHub 令牌远程触发一次运行。
     创建入口：GitHub → Settings → Developer settings → Fine-grained tokens → 仅授权该仓库、仅 `Actions: Read and write` 权限。令牌只存在手机本地。

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

## 三、本地文件说明

| 文件 | 作用 |
|---|---|
| cloud_run.py | 云端入口：跑流水线 → 导出 data/dashboard/*.json → git 回传 |
| .github/workflows/schedule.yml | 定时+手动触发的 Actions 工作流 |
| requirements.txt | Python 依赖 |
| dashboard/index.html | 手机仪表盘（单文件，无外部依赖，鸿蒙浏览器直接用） |
| data/dashboard/ | 仪表盘数据（summary/nav/decisions/pool/market/alerts.json） |

## 四、已完成的测试（2026-09-06）

- ✅ eod 全流程真实数据运行：抓行情/财务 → 评分 → 决策 → 文档 → 验收，总资产/持仓/现金与账本逐项核对一致
- ✅ riskwatch 运行：alerts.json 与 summary.recent_alerts 正常导出
- ✅ 幂等性：同日重复 eod，trades.csv 行数不变（无重复记账）
- ✅ git 回传链路：本地裸仓库验证 commit + push 成功（并修复了 push 未显式指定分支的bug）
- ✅ 仪表盘手机视口(390×844)浏览器实测：KPI/指令卡/持仓表/净值图/决策/候选池/市场观察/验收全部正常渲染，
  数据与账本核对一致；单点净值、空数据、加载失败均有兜底显示
- ⏳ Actions 端到端需你的 GitHub 仓库，推送后按"步骤5"验证首次运行即可
