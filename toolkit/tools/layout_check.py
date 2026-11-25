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
        # 业务规则（2026-11-25 收敛，防文字堆叠误判假 PASS/FAIL）：
        #   **只有双可点控件堆叠才构成 FAIL 强信号**（strict）——
        #   1px 相交即报，不接受「设计性紧凑布局」辩护。
        #   文字/文本堆叠暂不作为 FAIL 标准：仍按普通 IoU 阈值呈现为坐标事实
        #   （供 AI 结合截图参考），但不计 strict、不触发硬判定。
        #   纯装饰叶子（无文本无可点）同样维持 IoU 阈值，防误报爆炸。
        strict = bool(a["clickable"] and b["clickable"])
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

    # 严重度排序：双可点互压(strict) > overlap > contained；同型按 IoU 降序
    def _sev(h: dict) -> tuple:
        strict = (h["a"]["clickable"] and h["b"]["clickable"])
        return (0 if (strict and h["kind"] == "overlap") else
                1 if h["kind"] == "overlap" else 2, -h["iou"])
    hits.sort(key=_sev)
    out["count"] = len(hits)
    out["strict_count"] = sum(1 for h in hits if h.get("strict"))
    out["overlaps"] = hits[:max_out]
    if len(hits) > max_out:
        out["more"] = len(hits) - max_out
    return out


# 检测结果状态文件（可观测性记录，与证据目录同处；不参与任何判定）。
# 每次调用 append 一条 run 记录，保留最新一条在 latest 字段，便于人直接查看。
_STATE_NAME = "layout_check.json"
_STATE_MAX_RUNS = 20


def _append_state(directory: Path, record: dict) -> Path | None:
    """把一次检测结果写入 <证据目录>/layout_check.json（latest + runs 历史）。
    失败静默——记录是辅助能力，不许影响检测返回。"""
    fp = directory / _STATE_NAME
    try:
        existing: dict = {}
        if fp.is_file():
            loaded = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        runs = existing.get("runs") if isinstance(existing.get("runs"), list) else []
        runs.append(record)
        state = {
            "latest": record,
            "runs": runs[-_STATE_MAX_RUNS:],
        }
        fp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        return fp
    except (OSError, ValueError, TypeError):  # noqa: BLE001
        return None


def _mark_boxes(step_dir: Path, overlaps: list[dict]) -> None:
    """在问题现场截图上把命中区域画红框 + 编号，写 <step>/layout_boxes.png。

    坐标系：dump bounds 与 shot.png 同为设备像素（含横屏 3840x2560，实测一致），
    直接用 bounds 画框。框只标坐标事实，不标"异常/正常"——语义判定仍归 AI。
    失败一律无痕跳过（截图缺失/Pillow 不可用），不影响检测主返回。
    """
    png = step_dir / "shot.png"
    if not png.is_file():
        return
    try:
        from PIL import Image, ImageDraw
        img = Image.open(png).convert("RGB")
        draw = ImageDraw.Draw(img)
        iw, ih = img.size
        for n, hit in enumerate(overlaps, 1):
            for key in ("a", "b"):
                x1, y1, x2, y2 = hit[key]["bounds"]
                color = (220, 38, 38) if key == "a" else (255, 140, 0)
                draw.rectangle([x1, y1, x2, y2], outline=color, width=6)
            ax1, ay1, ax2, ay2 = hit["a"]["bounds"]
            bx1, by1, bx2, by2 = hit["b"]["bounds"]
            ix1, iy1, ix2, iy2 = max(ax1, bx1), max(ay1, by1), min(ax2, bx2), min(ay2, by2)
            if ix1 < ix2 and iy1 < iy2:
                draw.rectangle([ix1, iy1, ix2, iy2], outline=(220, 38, 38), width=10)
                draw.text((min(ix1 + 4, iw - 30), min(max(iy1 - 2, 0), ih - 20)),
                          str(n), fill=(220, 38, 38))
        img.save(step_dir / "layout_boxes.png")
    except Exception:  # noqa: BLE001 —— 可视化失败不影响主链路
        pass


def main() -> None:
    from common import (CALL_ID, apply_stdin_args, emit, fail,
                        log_auto, rel)  # 延迟导入：scan 保持零依赖可测
    p = argparse.ArgumentParser(description="layout_check：视图树堆叠检测（确定性事实）")
    p.add_argument("--xml", help="dump.xml 路径")
    p.add_argument("--evidence", help="证据目录路径（内含 dump.xml，与 --xml 二选一）")
    p.add_argument("--package", help="按前台包名过滤（默认自动读当前会话登记的被测包，再退 meta.json）")
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
    # 只产坐标事实，不做异常判定——语义裁决归 AI（结合截图/视觉）。
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
    # tree_blind 是覆盖度事实（不是判定）：目标包节点不在 dump 里，
    # 浮窗/分屏机型 ROM 抓不到非焦点窗口时本结果对目标 App 不可信。
    tree_blind = bool(fg) and not res.get("pkg_found")
    if tree_blind:
        payload["tree_blind"] = True
        payload["hint"] = (
            f"树盲区：目标包 {fg} 的节点不在本 dump 里（多窗口/浮窗场景部分机型"
            "抓不到非焦点窗口），本结果对目标 App 不可信，改走 android_vision 视觉核对。")
    elif res["count"] == 0:
        payload["hint"] = ("视图树层面未检出控件堆叠。自绘内容（SurfaceView/Canvas）"
                           "不在视图树里，那类问题仍需视觉判定。")

    # 可追溯性：① 事件入 events 表（与 act/observe/vision 同款，供 flaky_expects
    # 跨轮比对「判定时检出了什么」）；② 状态回填证据目录供人直接查看。
    # 两者都只记录事实，不参与判定、不触发任何强制逻辑。
    try:
        from datetime import datetime
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "call_id": CALL_ID,
            "evidence": str(xml_fp.parent),
            "dump": str(xml_fp),
            "package": fg,
            "pkg_found": res.get("pkg_found"),
            "tree_blind": tree_blind,
            "scanned": res["scanned"],
            "count": res["count"],
            "strict_count": res.get("strict_count", 0),
            "overlaps": res["overlaps"],
            "params": {"iou": args.iou, "contain": args.contain, "max": args.max},
        }
        state_fp = _append_state(xml_fp.parent, record)
        if state_fp:
            payload["state_file"] = str(state_fp)
        # 问题现场：命中时在对应截图上把异常区域画红框（编号与 overlaps 对齐），
        # 供人工回溯 + AI 用 android_vision 看图核对。纯可视化，不参与判定。
        # 无截图/无命中/Pillow 缺失一律无痕跳过。
        if res["overlaps"]:
            _mark_boxes(xml_fp.parent, res["overlaps"])
            boxes_fp = xml_fp.parent / "layout_boxes.png"
            if boxes_fp.is_file():
                payload["boxes_image"] = str(boxes_fp)
        # 事件入表：detail 是人读摘要，data 存完整结果（flaky 分析可比对每轮检出）
        eid = log_auto(
            "layout_check", "layout_check",
            f"layout check: count={res['count']} strict={record['strict_count']}"
            f" pkg={fg or '-'} blind={'Y' if tree_blind else 'N'}",
            record, rel(xml_fp), None, None)
        if eid:
            payload["event_id"] = eid
    except Exception:  # noqa: BLE001 —— 记录失败不影响主返回
        pass
    emit(True, payload)


if __name__ == "__main__":
    main()
