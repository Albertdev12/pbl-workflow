# -*- coding: utf-8 -*-
"""最终提交包打包器（采纳自GPT规范第十九节）：按编号目录组织成果并生成zip。"""
import json
import os
import shutil
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "outputs")
SUBMIT = os.path.join(BASE, "FINAL_SUBMISSION")

# outputs文件名前缀 -> 提交包编号目录
MAP = {
    "01_": ("01_小组信息", None),
    "02_": ("02_市场观察", None),
    "03_": ("03_股票候选池", None),
    "04_": ("04_初始投资计划", None),
    "05_": ("05_基本面分析", None),
    "06_": ("06_技术分析", None),
    "07_": ("07_交易决策记录", None),
    "08_": ("08_委托指令单", None),
    "09_": ("09_交易记录与盈亏归因", None),
    "10_": ("10_策略调整", None),
    "11_": ("11_AI使用记录", None),
    "12_": ("12_中期路演PPT", None),
    "13_": ("13_投资总结报告", None),
    "14_": ("14_交易复盘", None),
    "15_": ("15_工作日报", None),
    "16_": ("16_报告素材包", None),
    "18_": ("18_周会材料包", None),
    "19_": ("19_决策后验证", None),
    "00_": ("00_自检报告", None),
}


def build():
    if os.path.exists(SUBMIT):
        shutil.rmtree(SUBMIT)
    os.makedirs(SUBMIT)
    copied = 0
    for fname in sorted(os.listdir(OUT)):
        for prefix, (folder, _) in MAP.items():
            if fname.startswith(prefix):
                d = os.path.join(SUBMIT, folder)
                os.makedirs(d, exist_ok=True)
                shutil.copy2(os.path.join(OUT, fname), os.path.join(d, fname))
                copied += 1
                break
    # 图表与数据来源
    cfg = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8"))
    src = {
        "数据来源": "主源：东方财富公开行情接口（push2his/push2/datacenter.eastmoney.com）；"
                    "备用源：腾讯行情接口（web.ifzq.gtimg.cn / qt.gtimg.cn），主源失败时自动切换",
        "数据口径": "日线行情(前复权)、实时快照(PE/PB/市值)、F10主要财务指标、行业板块涨跌榜；"
                    "备用源只补主源缺失的日期，不覆盖已有口径，避免两套复权方式混用",
        "指标计算": "均线MA5/10/20/60、MACD(12,26,9)、RSI(14)、60日高低点支撑压力、量比，全部由Python计算",
        "核验方式": "可与同花顺APP个股页面交叉核对",
        "采集日志": "data/fetch_log.jsonl（每次接口调用时间戳）；data/verify_log.jsonl（决策后验证记录）",
        "课程规则": cfg["course"],
    }
    d = os.path.join(SUBMIT, "17_数据来源与说明")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "数据来源说明.json"), "w", encoding="utf-8") as f:
        json.dump(src, f, ensure_ascii=False, indent=2)
    copied += 1
    zip_path = os.path.join(BASE, "证券投资PBL_最终提交包.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(SUBMIT):
            for f in files:
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, os.path.dirname(SUBMIT)))
    return zip_path, copied


if __name__ == "__main__":
    zp, n = build()
    print(zp, n)
