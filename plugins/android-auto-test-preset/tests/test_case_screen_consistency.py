# -*- coding: utf-8 -*-
"""前置用例筛查技能卡一致性自测（2026-11-12）。

## 查什么

只查 `android-case-check/SKILL.md` 的**行为口径**，不查工具层代码：

1. 两层六类、固定短路顺序、只报第一项
2. 三类语义判定 + 隐含依赖（本机资源 / 联网解析服务）的静态识别
3. 不查设备 / 不查网络、网络层通不代表服务可用
4. 批量预筛只读不落盘
5. 不拦反例（防误拦）与旧方案废弃句已清掉
6. 既有关键词、派发目标、脱敏约束未被破坏

## 为什么单独一个文件

`test_skill_card_consistency.py` 管「老三维还在吗」，
`test_skill_matches_code.py` 管「有没有写进 App 专有内容」。
本文件管新增的筛查口径不被后续改卡时悄悄改掉 ——
尤其是「只静态识别、不查设备」这条，写歪了就会退化成现场瞎查。

从用户目录的 Junction 读卡，和另外两个脚本读的是同一份内容。
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

# Windows 控制台默认 GBK，打印 ✅ / → 这类字符会 UnicodeEncodeError。
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

SKILLS = Path.home() / ".agents" / "skills"
CHECK = SKILLS / "android-case-check" / "SKILL.md"

PASS: list[str] = []
FAIL: list[str] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    line = f"  [{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f"  -> {detail}"
    print(line)


def need(label: str, *words: str) -> None:
    for w in words:
        ck(f"含「{label}」：{w}", w in txt)


def forbid(label: str, *words: str) -> None:
    for w in words:
        ck(f"不含「{label}」：{w}", w not in txt)


print("=" * 74)
print("0) 技能卡存在吗")
print("=" * 74)
ck("android-case-check 存在", CHECK.is_file(), str(CHECK))
if not CHECK.is_file():
    print(f"\n  {len(PASS)} 通过 / {len(FAIL)} 失败")
    sys.exit(1)

txt = CHECK.read_text(encoding="utf-8")
front = txt.split("---", 2)[1] if txt.startswith("---") else ""

print()
print("=" * 74)
print("1) 两层六类与固定短路顺序")
print("=" * 74)
need("判定表", "准入判定：两层六类，固定顺序", "基础准入", "语义筛查",
     "按 1 → 6 短路", "六类全过才派发", "只报命中的那一类")
need("类别 2 表格口径", "涉及多项时列得出清单", "「所有 / 每一项」没列字段清单")
need("隐含依赖例外", "只提醒，照常派发", "只记一条提醒，不算命中", "不参与短路")
need("定位与分流", "以前 case 写得模糊", "让用例写到", "设备状态、服务可用性",
     "条件写「左右 / 按规定", "描述没写清 → 归类别 4")
need("类别小节", "类别 4 · 描述模糊", "类别 5 · 需拆分", "类别 6 · 不适合自动化")
need("只报一项", "只报前一项")
need("基础三维关键词", "有步骤吗", "有预期吗", "什么算 FAIL")
need("派发关键词", "android-executor", "PASS", "BLOCKED")

print()
print("=" * 74)
print("2) 三类语义判定与阈值")
print("=" * 74)
need("描述模糊", "读不懂才拦", "同一个词写在哪决定归哪类",
     "前提漏写 ≠ 描述模糊", "读不懂才归这里")
need("类别 5 与类别 2 分工", "列得出清单、但要分别走不同路径")
need("需拆分阈值", "两个互斥前置", "两个独立终态",
     "一条断言捆绑多个要分别取证的独立事实", "不替用户决定怎么拆")
need("拆分样例", "前提：无 X / 有 X", "拆成「无 X 验空态」「有 X 验选择」两条",
     "单独成条后再按类别 6 判")
need("不适合自动化", "写具体了仍做不到的，拦", "需人工或物理介入",
     "跨应用不可达入口", "依赖外部账号登录态")
need("类别 3/6 重叠", "与类别 3 重叠的按短路先报 3")
need("Oracle 释义", "Oracle = 判定依据")
need("六类能力边界", "本方案的能力边界：六类不适合自动化",
     "一条 ADB 通道同时负责观察和写记录", "证据不可稳定观测",
     "机械臂 / 治具 / 专业仪器", "第二台设备 / 外设 / 多端状态同步",
     "观察与记录通道被测试动作打断", "条件构造时间长",
     "observe → 决策 → act", "锚点与取证不可复现")
need("第 7 类归描述类", "第 7 类「用例质量与 Oracle 不明确」不归这里",
     "类别 1 / 2 / 4 / 5 的活", "别拿工具能力当借口")
need("六类边界", "明写要测上表这些对象", "隐含依赖，只提醒",
     "运行时才发生，不预判", "命令能模拟的状态 → 不拦")
need("无控件 ID 澄清", "「无控件 ID」不算",
     "定位走屏幕文字 / desc / 结构", "才算不可观测，归类别 3")
need("模糊时间归类别 4", "模糊时间与条件属于描述模糊", "不是「不适合自动化」",
     "前 X 天左右", "别拿类别 6 替描述模糊背锅",
     "描述没写清的一律先归类别 4", "改具体就能跑",
     "改具体仍要做工具层做不到的事")
need("预期没边界的集合", "预期是个没边界的集合", "「所有」「每一项」「一一对比」",
     "一会儿 PASS 一会儿 FAIL", "判据只有一条：能不能把要核对的项一条条列出来",
     "已列清单就不拦", "单一事实也不拦")
need("互斥前置与顺序两步", "互斥前置 ≠ 顺序两步",
     "一次执行能不能把两条路都走完", "顺序两步，不是互斥前置",
     "走得完就不拆")

print()
print("=" * 74)
print("3) 隐含依赖只提醒 / 不查设备")
print("=" * 74)
need("本机资源措辞", "固定素材 / 选择已有", "隐含本机资源")
need("外部服务措辞", "解析 / 导入 / 同步 / 云端 / 服务端返回", "隐含外部服务")
need("漏写前提信号", "同类用例写了「网络 / 数据可用」", "前提段写了别的前置")
need("隐含依赖不抢类别 4", "这类前提漏写不算类别 4", "用例仍读得懂，只是少写了依赖")
need("提醒口径", "这条可能依赖 X，执行时留意",
     "不说「设备上没有 X」「没有网络」", "也不要求用户确认")
need("只提醒不拦", "隐含依赖（只提醒，不拦）", "这两类是提醒，不是命中",
     "照常派发", "不停下等回复、不给三选项", "不给选项、不等回复",
     "⚠️ 提醒", "隐含依赖不走这一节", "只记一条提醒，不算命中",
     "不参与短路")
forbid("三选项已移除", "补前置说明", "由用户选", "类别 6 第一次就给")
need("不查设备网络", "不查设备、不查网络", "不 ping",
     "不查设备、不查网络、不替用户确认资源在不在")
need("网络检查不足", "网络层通不代表解析服务可用")
need("运行时才发生的排除", "崩溃 / ANR / 断连 / 记录通道异常", "服务当时恰好不可用")
need("不算隐含依赖", "不算隐含依赖（别误伤）",
     "把这份依赖抽掉，用例的预期还验不验得成", "另有可替代路径",
     "导入成功后新建", "解析流程正常完成",
     "步骤里的导入 / 解析只是到起点的路")
forbid("正向检查指令", "检查是否有网络", "检查设备是否有图", "检查网络是否可用")

print()
print("=" * 74)
print("4) 批量预筛只读不落盘")
print("=" * 74)
need("批量节", "## 批量预筛（只读，不落盘）", "同一套判定口径",
     "批量筛查 ≠ 批量派发")
need("只读与不落盘", "只读每份 md", "不写报告文件", "不给用例打标",
     "不建 session、不查运行库、不查设备、不查网络")
need("筛查依据", "`_steps.yaml` 不是筛查依据",
     "文件头自己写着「前置条件、造数据、判定都不在这里」")
need("清单形态", "可跑 N 条（其中附提醒 K 条）/ 待改 M 条",
     "编号 + 缺哪份依赖 + 提醒文案", "不计入待改、仍可跑",
     "编号 + 命中类别 + 原文证据 + 补齐建议")
need("来源衔接", "用户贴的优先级最高")

print()
print("=" * 74)
print("5) 拦截结果与重入闭环")
print("=" * 74)
need("拦截三要素", "命中哪一类", "引用用户原话的那一段", "补齐动作")
need("类别 2 补齐动作", "类别 2 / 4 / 5 / 6 的补齐动作怎么给",
     "2 预期没边界的集合", "不替用户列清单")
need("硬拦不做", "不派发（不给 android-executor", "不产生 run_history",
     '不说成"用例失败 / 功能有问题"')
need("重入", "重新过 1 → 6", "六类全过 → 立刻派发（提醒照旧带着）", "每条单独重判",
     "隐含依赖的提醒", "不需要用户回复", "隐含依赖不算命中")
need("派发原文", "用例原文（原样贴，一个字都不改）", "这是修订后版本")
forbid("旧提醒示例", "第3条预期含两件事，逐件核对")

print()
print("=" * 74)
print("6) 防误拦 / 旧约束未被破坏")
print("=" * 74)
need("不拦反例", "## 不拦反例", "同一终态下一条预期含两件事",
     "步骤数和预期数略有出入", "项能列出来", "那只是描述前置，不拦",
     "只记提醒、不拦", "条件写「左右 / 按规定", "才归类别 6 硬拦")
need("集合与糙用例的边界", "对照类别 2", "集合没边界，不是写得细",
     "预期只有一句话")
need("对照类别 4", "前提漏写、没写前提段、前提段写了别的前置",
     "按隐含依赖只提醒")
forbid("无条件放行句", "其余情况一律放行", "三条之外的理由一律不拦",
       "一条预期里含两件事（含就含")
forbid("旧方案废弃词", "langgraph-poc", "_cases.json", "_learn.json",
       "android_case(", "run_case", "android-case-runner", "android-worker", "landing")

print()
print("=" * 74)
print("7) frontmatter 触发描述")
print("=" * 74)
ck("覆盖单条派发前筛查", "要跑某条用例" in front)
ck("覆盖批量预筛", "筛查整批用例" in front)
ck("含派发目标", "android-executor" in front)
ck("声明隐含依赖只提醒", "隐含依赖" in front and "只提醒" in front)

print()
print("=" * 74)
print("8) 示例脱敏（与 test_skill_matches_code.py 同一套黑名单）")
print("=" * 74)
for pat, label in [
    (r"com\.zui\.", "com.zui.* 包名"),
    (r"com\.android\.settings\b", "具体系统包名"),
    (r"\biv_more\b", "控件 id iv_more"),
    (r"\bitem_title\b", "控件 id item_title"),
    (r"\bbtnImportFromGallery\b", "具体控件 id"),
    (r"\btimetable\w*", "timetable*"),
    (r"\balmanac\w*", "almanac*"),
    (r"联想日历|课程表|黄历|纵览", "App 业务名词"),
    (r"(?<!通知、)日历", "App 名「日历」"),
    (r"D:\\Project", "写死作者机器路径"),
    (r"D:\\dsh\\", "写死部署机路径"),
    (r"/home/xuyl", "写死部署机路径(2)"),
    (r"[Ll]eichao4|[Zz]hukl2", "真实用户名"),
    (r"(?<![0-9a-fA-F-])[0-9a-f]{8}(?![0-9a-fA-F-])", "疑似真设备 serial"),
]:
    hits = [f"L{i}: {ln.strip()[:50]}"
            for i, ln in enumerate(txt.splitlines(), 1) if re.search(pat, ln, re.I)]
    ck(f"没有「{label}」", not hits, str(hits[:2]))

print()
print("=" * 74)
print(f"  {len(PASS)} 通过 / {len(FAIL)} 失败")
print("=" * 74)
sys.exit(1 if FAIL else 0)
