# -*- coding: utf-8 -*-
"""Persona 闸门自测（2026-11-16）。

## 为什么有这个文件

11/16 用户在 DSH 里跑 64 条用例，**拦截一条没生效**。事后查出来不是 skill 的问题，
是 persona 和 skill 打架：

- `skills/android-case-check/SKILL.md` 已经换成两层六类（1 → 6）
- 但 `preset/cordis.patch.yml` 的派发节还写着「有步骤吗、有能判的预期吗」，
  并且明确「**不用读任何文件**」—— 等于授权主 Agent 跳过 skill、只凭两条判据心算

结果主 Agent 按 persona 走，1 → 6 根本没进上下文，27 条直接分组派发下去了。

所以这个测试盯的是**两份指令不再打架**：

1. persona 必须内联 1 → 6 的六类名称
2. persona 必须声明「先判定再派发」，并给出整批闸门（先出筛查清单）
3. persona 必须要求派发前加载 skill
4. persona 不许再出现旧的免判定措辞
5. persona 的六类名称与 SKILL.md 判定表对得上

## 边界

只读文本，不起 DSH、不校验 YAML 结构（那是 `verify_preset.py` 的活）。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

ROOT = Path(__file__).resolve().parent.parent
PERSONA = ROOT / "preset" / "cordis.patch.yml"
CHECK = Path.home() / ".agents" / "skills" / "android-case-check" / "SKILL.md"

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
print("0) persona 与 skill 都在吗")
print("=" * 74)
ck("preset/cordis.patch.yml 存在", PERSONA.is_file(), str(PERSONA))
ck("android-case-check 存在", CHECK.is_file(), str(CHECK))
if not PERSONA.is_file() or not CHECK.is_file():
    print(f"\n  {len(PASS)} 通过 / {len(FAIL)} 失败")
    sys.exit(1)

txt = PERSONA.read_text(encoding="utf-8")
skill = CHECK.read_text(encoding="utf-8")

print()
print("=" * 74)
print("1) 派发节必须内联 1 → 6")
print("=" * 74)
need("短路顺序", "1 → 6", "按顺序短路", "命中第一个就停", "只报那一类")
need("六类名称", "说不出步骤", "说不出什么算 FAIL", "超出工具层",
     "描述模糊", "需拆分", "不适合自动化")
need("拦截动作", "命中 1–6 → 不派", "原文证据", "补齐建议")
need("隐含依赖例外", "隐含依赖", "只提醒，照常派")

print()
print("=" * 74)
print("2) 先判定再派发 + 整批闸门")
print("=" * 74)
need("强制顺序", "先判定，再派发", "没判定就派 = 违规",
     "「用户要的是执行，不是筛查」不是免判定的理由")
need("整批闸门", "整批（一次 ≥ 2 条）多一道闸门",
     "可跑 N 条（附提醒 K 条）/ 待改 M 条",
     "用户确认后再分组派发", "不许跳过清单")
need("加载 skill", "派之前先加载它")

print()
print("=" * 74)
print("3) 旧的免判定措辞必须清掉")
print("=" * 74)
forbid("旧两条判据", "有步骤吗、有能判的预期吗")
forbid("免读文件授权", "不用读任何文件")
forbid("旧两步写法", "a. 用 **android-case-check** skill 心算判断")

print()
print("=" * 74)
print("4) persona 的六类与 SKILL.md 对得上吗")
print("=" * 74)
for w in ("描述模糊", "需拆分", "不适合自动化"):
    ck(f"skill 也讲「{w}」", w in skill)

if "描述模糊" in txt and "描述模糊" in skill:
    ck("六类名称两边一致（描述模糊 / 需拆分 / 不适合自动化）", True)
else:
    ck("六类名称两边一致（描述模糊 / 需拆分 / 不适合自动化）", False)

print()
print("=" * 74)
print(f"  {len(PASS)} 通过 / {len(FAIL)} 失败")
for f in FAIL:
    print(f"   ✗ {f}")
print("=" * 74)
sys.exit(0 if not FAIL else 1)
