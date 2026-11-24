"""layout_check：视图树堆叠检测 —— 纯算法层，只产确定性事实，不做任何判定。

设计文档：toolkit/docs/视图树堆叠检测器设计.md
哲学对齐：「代码管纪律，AI 管语义」——坐标交集是确定性计算交给代码（零幻觉
零成本），AI 只回答「堆叠的是什么控件、是否破坏功能」。

为什么只取叶子节点：容器矩形与子节点重叠是布局常态（父子豁免另有规则），
容器-容器重叠几乎全是误报源。

为什么父子豁免在算法层而不在白名单：子矩形含于父矩形是树结构决定的必然
（except 弹窗跨层），不豁免的话每屏都会命中几十对——信号立刻失去信息量。

用法（AI 侧一般通过 android_layout_check 工具调用，不直接敲命令）：
  python layout_check.py --evidence storage/run_history/<uuid>/evidence/case30/step01
  python layout_check.py --xml storage/evidence/xxx/dump.xml --package com.zui.recorder
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from itertools import combinations
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK
except AttributeError:
    pass

DEFAULT_IOU = 0.3        # 两矩形交并比阈值（起步值，按误报样本回调）
DEFAULT_CONTAIN = 0.8    # 小矩形被大矩形吞掉面积比阈值
_MAX_OUT = 10            # 单次返回明细上限（防误报爆炸撑爆 payload）
# 屏幕外节点保留可见部分低于此比例的丢弃（动画滑出/回收中的节点）
_MIN_VISIBLE_RATIO = 0.05
# 全屏/近全屏节点豁免：毛玻璃(scrim/blur)、全屏遮罩与所有叶子相交是设计行为，
# 一屏能刷出上百对误报（实测 com.zui.launcher 一次 112 对）。宽高均 ≥95% 屏的叶子不配对。
_FULLSCREEN_RATIO = 0.95

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def scan(xml_text: str, fg_package: str | None = None,
         screen_size: list | tuple | None = None,
         iou: float = DEFAULT_IOU, contain: float = DEFAULT_CONTAIN,
         max_out: int = _MAX_OUT) -> dict:
    """扫描一份 uiautomator dump XML，返回控件堆叠信号。

    返回 dict（永不抛异常的契约由调用方 try 兜，这里尽量自兜）：
      {"parse_ok": True, "scanned": 叶子数, "count": 命中对数,
       "overlaps": [按严重度排序的明细（最多 max_out 条）]}
    解析失败时 {"parse_ok": False, "error": "..."} —— 调用方据此留痕。

    判定规则（两两配对，均为可见叶子）：
      - 祖先关系豁免（子含于父是布局常态，不算堆叠）
      - IoU > iou 阈值            → "overlap" 型（互相压住，同级控件最危险）
      - 交面积/较小矩形面积 > contain 阈值 → "contained" 型（一个压住另一个）
    产出只有坐标事实（谁、压谁、压多少），**没有也不该有"是否异常"字段**。
    """
    out: dict = {"parse_ok": True, "scanned": 0, "count": 0, "overlaps": []}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return {"parse_ok": False, "error": f"XML 解析失败: {e}"}

    # 目标包在树中的存在性（多窗口/浮窗场景的盲区信号）：
    # 部分机型 ROM 的 dump 抓不到 freeform/浮窗窗口节点（实测 2026-11-23，
    # com.zui.recorder 浮窗整窗缺失）——pkg_found=False 时树检不可信，须走视觉核对。
    tree_pkgs = {el.get("package") for el in root.iter("node") if el.get("package")}

    parents = {c: p for p in root.iter() for c in p}

    def ancestors(el) -> set:
        seen, p = set(), parents.get(el)
        while p is not None:
            seen.add(id(p))
            p = parents.get(p)
        return seen

    sw = sh = None
    if screen_size and len(screen_size) == 2:
        sw, sh = int(screen_size[0]), int(screen_size[1])

    leaves: list[dict] = []
    for el in root.iter("node"):
        if any(ch.tag == "node" for ch in el):
            continue                          # 只要叶子：容器重叠是常态
        a = el.attrib
        m = _BOUNDS_RE.search(a.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if x1 >= x2 or y1 >= y2:
            continue                          # 零尺寸
        pkg = a.get("package", "")
        if fg_package and pkg and pkg != fg_package:
            continue                          # 前台包过滤（同 summarize_xml 语义）
        # 屏幕外剔除：可见部分占比过低的（动画滑出/回收中）不算在场
        if sw and sh:
            vx2, vy2 = min(x2, sw), min(y2, sh)
            vx1, vy1 = max(x1, 0), max(y1, 0)
            area = (x2 - x1) * (y2 - y1)
            if vx1 >= vx2 or vy1 >= vy2:
                continue
            if ((vx2 - vx1) * (vy2 - vy1)) < _MIN_VISIBLE_RATIO * area:
                continue
            # 全屏/近全屏叶子豁免：遮罩层压全屏不是堆叠语义（见 _FULLSCREEN_RATIO 注释）
            if (x2 - x1) >= _FULLSCREEN_RATIO * sw and (y2 - y1) >= _FULLSCREEN_RATIO * sh:
                continue
        rid = (a.get("resource-id") or "").split("/")[-1]
        leaves.append({
            "class": (a.get("class") or "").split(".")[-1],
            "rid": rid,
            "text": (a.get("text") or "")[:20],
            "desc": (a.get("content-desc") or "")[:20],
            "clickable": a.get("clickable") == "true",
            "center": [(x1 + x2) // 2, (y1 + y2) // 2],
            "bounds": [x1, y1, x2, y2],
            "_el": el,
        })

    out["scanned"] = len(leaves)
    out["pkg_found"] = (fg_package in tree_pkgs) if fg_package else None
    hits: list[dict] = []
    for a, b in combinations(leaves, 2):
        ra, rb = a["bounds"], b["bounds"]
        iw = min(ra[2], rb[2]) - max(ra[0], rb[0])
        ih = min(ra[3], rb[3]) - max(ra[1], rb[1])
        if iw <= 0 or ih <= 0:
            continue
        inter = iw * ih
        area_a = (ra[2] - ra[0]) * (ra[3] - ra[1])
        area_b = (rb[2] - rb[0]) * (rb[3] - rb[1])
        smaller = min(area_a, area_b)
        # 祖先豁免：子含于父/父含于子都是树结构常态
        if id(a["_el"]) in ancestors(b["_el"]) or id(b["_el"]) in ancestors(a["_el"]):
            continue
        v_iou = inter / (area_a + area_b - inter)
        v_contain = inter / smaller if smaller else 0.0
        # 业务规则（2026-11-23 用户确认，两轮扩展）：
        #   **可点控件、文本内容，都不允许互相重叠**——
        #   语义内容节点（clickable 或带文本）两两之间交面积>0 即报，
        #   不看 IoU 阈值（1px 相交也是 BUG，不接受「设计性紧凑布局」辩护）。
        #   纯装饰叶子（无文本无可点的图标类）维持原阈值：
        #   角标压图标/毛玻璃相交是设计常态，IoU 闸门防误报爆炸。
        content_a = bool(a["clickable"] or a["text"])
        content_b = bool(b["clickable"] or b["text"])
        strict = content_a and content_b
        if (v_iou > iou) or (strict and inter > 0):
            kind = "overlap"
        elif v_contain > contain:
            kind = "contained"
        else:
            continue
        hits.append({
            "kind": kind,
            "iou": round(v_iou, 2),
            "contain_ratio": round(v_contain, 2),
            "strict": strict,
            "a": {k: a[k] for k in ("class", "rid", "text", "desc",
                                    "clickable", "center", "bounds")},
            "b": {k: b[k] for k in ("class", "rid", "text", "desc",
                                    "clickable", "center", "bounds")},
        })

    # 严重度排序：双可点互压 > 其他语义内容互压 > overlap > contained；同型按 IoU 降序
    def _sev(h: dict) -> tuple:
        both_click = (h["a"]["clickable"] and h["b"]["clickable"])
        strict = ((h["a"]["clickable"] or h["a"]["text"])
                  and (h["b"]["clickable"] or h["b"]["text"]))
        return (0 if (both_click and h["kind"] == "overlap") else
                1 if (strict and h["kind"] == "overlap") else
                2 if h["kind"] == "overlap" else 3, -h["iou"])
    hits.sort(key=_sev)
    out["count"] = len(hits)
    out["strict_count"] = sum(1 for h in hits if h.get("strict"))
    out["overlaps"] = hits[:max_out]
    if len(hits) > max_out:
        out["more"] = len(hits) - max_out
    return out


def _brief(h: dict) -> str:
    """一行人话摘要（给 payload 摘要用）。"""
    def who(n: dict) -> str:
        tag = n["rid"] or n["text"] or n["desc"] or n["class"]
        return f"{n['class']}[{tag}]" + ("(可点)" if n["clickable"] else "")
    return f"{who(h['a'])} × {who(h['b'])} ({h['kind']} iou={h['iou']})"


def main() -> None:
    from common import apply_stdin_args, emit, fail  # 延迟导入：scan 保持零依赖可测
    p = argparse.ArgumentParser(description="layout_check：视图树堆叠检测（确定性事实）")
    p.add_argument("--xml", help="dump.xml 路径")
    p.add_argument("--evidence", help="证据目录路径（内含 dump.xml，与 --xml 二选一）")
    p.add_argument("--package", help="按前台包名过滤（默认自动读同目录 meta.json 的 package）")
    p.add_argument("--iou", type=float, default=DEFAULT_IOU)
    p.add_argument("--contain", type=float, default=DEFAULT_CONTAIN)
    p.add_argument("--max", type=int, default=_MAX_OUT)
    apply_stdin_args()   # --stdin-json：参数走 stdin，避开 Windows 引号问题
    args = p.parse_args()

    xml_fp = None
    if args.xml:
        xml_fp = Path(args.xml)
    elif args.evidence:
        xml_fp = Path(args.evidence) / "dump.xml"
    if xml_fp is None:
        fail("需要 --xml <dump.xml 路径> 或 --evidence <证据目录>")
    if not xml_fp.is_file():
        fail(f"找不到 dump.xml: {xml_fp}")

    # 过滤包名（目标 App 对准），优先级：
    #   ① 显式 --package   ② 当前会话登记的被测包（db.sessions.package）
    #   ③ 同目录 meta.json 的 focus 包（capture 落盘，天然配对）
    # ②在前的语义（2026-11-23 用户拍板）：**检测对象 = 用例的目标 App**，
    # 而不是设备 focus 窗口——浮窗/分屏场景 focus 可能落在桌面，焦点不等于被测。
    fg = args.package
    meta_fp = xml_fp.parent / "meta.json"
    if not fg:
        try:
            from db import current_session, get_db
            _sid = current_session()
            if _sid:
                fg = get_db().get_session_package(_sid)
        except Exception:  # noqa: BLE001 —— 无会话/无 db 环境静默降级
            pass
    if not fg and meta_fp.is_file():
        try:
            fg = json.loads(meta_fp.read_text(encoding="utf-8")).get("package")
        except (OSError, ValueError):
            pass
    size = None
    if meta_fp.is_file():
        try:
            size = json.loads(meta_fp.read_text(encoding="utf-8")).get("size")
        except (OSError, ValueError):
            pass

    res = scan(xml_fp.read_text(encoding="utf-8"), fg_package=fg,
               screen_size=size, iou=args.iou, contain=args.contain,
               max_out=args.max)
    if not res.get("parse_ok"):
        fail(f"扫描失败: {res.get('error')}")
    payload = {
        "ok": True,
        "scanned": res["scanned"],
        "count": res["count"],
        "strict_count": res.get("strict_count", 0),
        "overlaps": res["overlaps"],
        "package": fg,
        "pkg_found": res.get("pkg_found"),
    }
    if res.get("more"):
        payload["more"] = res["more"]
    # 判定建议（业务规则下的规则性建议，非 AI 语义裁决）：
    #   语义内容（可点/文本）重叠 → verdict_hint=FAIL；
    #   tree_blind 时结果不可信，不给 verdict（避免误导）。
    tree_blind = bool(fg) and not res.get("pkg_found")
    strict_count = res.get("strict_count", 0)
    if tree_blind:
        verdict_hint = None
        verdict_reason = None
    elif strict_count > 0:
        verdict_hint = "FAIL"
        verdict_reason = (f"检出 {strict_count} 对可点/文本控件重叠"
                          "（业务规则：语义内容 1px 相交即 BUG）")
    else:
        verdict_hint = "PASS"
        verdict_reason = ("视图树层面无语义内容重叠"
                          "（自绘内容不在树内，必要时仍需视觉核对）")
    payload["verdict_hint"] = verdict_hint
    payload["verdict_reason"] = verdict_reason
    payload["hint"] = (
        "这是**确定性坐标事实**，不是异常判定：两源一致（你视觉也见堆叠）才可判"
        "布局异常。业务规则：**可点控件与文本内容之间不能有任何重叠**（双可点或"
        "带文本的语义内容 1px 相交即报，不接受「设计性紧凑布局」辩护）；"
        "仅非可点且无文本的装饰性叠加（角标压图标/悬浮球压内容/遮罩）可排除，"
        "但 PASS 时 finding 必须写明排除理由。verdict_hint=FAIL 时判 PASS "
        "会被 findings-batch 布局闸门拒绝，必须在 actual 写明排除理由。"
        "本轮排序：双可点互压排最前。")
    if tree_blind:
        payload["tree_blind"] = True
        payload["hint"] = (
            f"⚠️ 树盲区：目标包 {fg} 的节点不在本 dump 里（多窗口/浮窗场景部分机型"
            "抓不到非焦点窗口）。本结果对目标 App **不可信**——涉及浮窗/分屏内部"
            "布局的判定必须改走视觉核对（android_vision 看截图，重点查按钮重叠/"
            "遮挡/截断），不要以本扫描为准。")
    elif res["count"] == 0:
        payload["hint"] = ("未检出控件堆叠（目标包可见叶子两两无交集超阈值）。"
                           "注意：自绘内容（SurfaceView/Canvas）不在视图树里，"
                           "那类问题仍走视觉判定，且单源结论需注明。")
    # 结果快照落盘：证据与结论物理绑定，findings-batch 布局闸门据此校验
    try:
        from datetime import datetime
        snapshot = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "package": fg,
            "pkg_found": res.get("pkg_found"),
            "tree_blind": tree_blind,
            "scanned": res["scanned"],
            "count": res["count"],
            "strict_count": strict_count,
            "verdict_hint": verdict_hint,
            "verdict_reason": verdict_reason,
            "overlaps": res["overlaps"],
        }
        save_fp = xml_fp.parent / "layout_check.json"
        save_fp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1),
                           encoding="utf-8")
        payload["saved_to"] = str(save_fp)
    except OSError:
        pass  # 落盘失败不影响返回（闸门届时按「无检测记录」处理）
    emit(True, payload)


if __name__ == "__main__":
    main()
