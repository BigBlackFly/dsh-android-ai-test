"""session：测试会话生命周期。开会话后，observe/act/read 自动把每次调用
作为事件写入当前会话；AI 只需在断言时记 finding、结束时 finish。

用法：
  python session.py start --title "日历新建课程表" --input "用户口述的用例原文..."
  python session.py finding --status PASS --event-id <ID> --expect "出现新建成功toast" --actual "OCR读到'创建成功'"
  python session.py finish --status PASS --summary "3/4 PASS，1 BLOCKED(无图库权限)"
  python session.py list
  python session.py current
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from common import (case_expectations, emit, find_case_file_by_ref,
                    knowledge_hint, knowledge_hint_key, match_expect_id, resolve_case_stem,
                    steps_hint)
import sqlite3
from db import (  # noqa: I001
    INTERIM, STATUSES, SessionDB, clear_current, current_session, get_db,
    run_dir_missing, run_dir_path, set_current,
)
from runtime import (
    CASES_DIR, STORAGE_DIR, current_record_dir,
    detect_shell, last_record_dir, new_record_dir, pin_record,
    require_writable_storage, unpin_record,
)
from video_recording import start_video_recording, stop_video_recording

# 只有这几条不写盘。其余一律进门先实测 storage 可写性 ——
# 否则报错点会漂到后面某个不相干的调用上，根本看不出是 storage 的问题。
_READ_ONLY_CMDS = frozenset({"list", "current", "checklist"})

# Windows 控制台默认 GBK，中文/特殊符号会炸 → 全部 CLI 输出强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def maybe_file(v: str) -> str:
    """`@路径` → 读 UTF-8 文件内容作为值。

    中文长文本直接放命令行参数，在部分终端/管道里会被 GBK 双重解码
    （旧 skill 的 GBK 事故同族）；用例口述这种长文本一律建议走 @文件。
    """
    if v and v.startswith("@"):
        return Path(v[1:]).read_text(encoding="utf-8")
    return v


# 「弹窗标题」类 rid：出现即说明某个弹层打开了，是最可靠的页内落点。
#
# 2026-10-28 实测为什么需要它：
#   开「上课时间」弹窗后，sig 新增里有 content/parentPanel/customPanel/...
#   还有 button1（确定）。按字母序取第一个 rid 会拿到 `rid=button1` ——
#   而**几乎每个弹窗都有 button1**。下次执行时只要屏幕上还有任意 button1，
#   校验就误判成"弹窗开了"，然后点下一步全错。
#
#   同一轮里三个弹窗的独特标题 rid 分别是不 npTitle / dialog_title / alertTitle，
#   所以按优先级挑，挑到哪个都能唯一指认"哪个弹窗开了"。
_MODAL_TITLE_RIDS = (
    "npTitle", "dialog_title", "alertTitle", "title_template",
    "curriculum_guide_title",
)

# 「通用节点」rid：几个弹窗/页面都有的容器和按钮。
#
# 它们**不能当锚点**（点了不知道点的是哪个控件），
# 也**不能当落点**（屏幕上有它不代表到了目标页面）。
#
# 2026-10-29 实测教训：这轮导出的 17 步里有
#   via: rid=android:id/button1   （确定）
#   via: rid=android:id/button2   （取消）
#   expect: rid=contentPanel / rid=action_bar_root
# 全是通用节点 —— 下次执行时第一步就会误判。
_GENERIC_RIDS = {
    "content", "parentPanel", "customPanel", "custom", "divider",
    "buttonPanel", "button_divider", "button1", "button2", "button3",
    "empty", "topPanel", "action_bar_root", "contentPanel", "custom",
    "alertTitle", "title_template", "android:id/button1",
    "android:id/button2", "android:id/button3", "android:id/content",
}


_OPEN_CLOSE = {"(": ")", "[": "]", "{": "}", "（": "）", "【": "】"}
# 括号里出现这些 = 坐标/位置描述，整条不可执行。
# `\d,\d` 是裸坐标：AI 常直接写 `text=关闭(193,718)` / `rid=xx (952,732)`，
# 前面不带 x=/y= —— 这种换个分辨率就废，必须拒。
_BAD_IN_BRACKET = re.compile(
    r"[xXyY]\s*=\s*-?\d|第\s*\d+\s*[节格行列]|[×x]\s*\d+|\d\s*,\s*\d")


def _strip_brackets(via: str) -> str | None:
    """去掉 via 里的括号注释；括号里是坐标/位置描述则返回 None（整条不可执行）。

    ★ 为什么是「先看括号里是什么」而不是「有括号就拒」（2026-11-17 改）：

    AI 很爱在锚点后面加一句人类注释 —— `rid=themeLayout(text=主题)`、
    `text=设置(hotseat)`。括号里是**正常的文字补充**，锚点本身完全可执行。
    原来一刀切全拒，实测全库 5940 次 tap 里有 1591 次（26%）因此进不了 yaml，
    是所有过滤器里最大的一块。后果很具体：`长按图标_117` 的 start_from 从
    「桌面」退到了「主题预览页」——「桌面 → 设置 → 主题」三步全被丢掉，
    剩下的两步都是页内动作，下次照它走第一步就得自己先摸到主题预览页。

    真正该拒的只有括号里写坐标/位置的那种：
        `弹框外空白（x=300,y=1700）`、`日列选中行上一格 (-1)×22`
    这类换个机型就废，且多半要 AI 现场判断。
    """
    out: list[str] = []
    i = 0
    while i < len(via):
        close = _OPEN_CLOSE.get(via[i])
        if close is None:
            out.append(via[i])
            i += 1
            continue
        j = via.find(close, i + 1)
        if j < 0:
            return None                  # 括号不配对，说不清是什么
        if _BAD_IN_BRACKET.search(via[i + 1:j]):
            return None
        i = j + 1
    return "".join(out)


def _anchors_of(via: str) -> list[str] | None:
    """把 via 拆成可执行的锚点列表；不可执行返回 None。

    可执行 = 纯 `key=value`，多个用逗号/空格分隔，每个值都非空。

    ⚠️ 三类必须拒掉（都是实测踩过的）：
      ① **坐标/位置混入**：`rid=cv_empty_content 第1节 周一格`
         —— rid 有 14 个同名的格子，光凭 rid 不知道点哪个。
         括号里的坐标（`弹框外空白（x=300,y=1700）`）同样拒，见 `_strip_brackets`。
      ② **通用 rid**：`rid=android:id/button1`
         —— 每个弹窗都有，点它等于瞎点。
      ③ **空值 / 非 kv**：`rid=`、`返回`
    """
    if not via:
        return None
    via = _strip_brackets(via)
    if not via:
        return None
    parts = [p for p in re.split(r"[,\s]+", via.strip()) if p]
    if not parts:
        return None
    for p in parts:
        if "=" not in p:
            return None                      # 有人话
        key, _, val = p.partition("=")
        key = key.strip().lower()
        val = val.strip()
        if not key or not val:
            return None                      # `x=` 空值
        if key in ("t", "c"):
            return None                      # 文本/坐标锚点，不稳
        # 值里还有非 ASCII 且不是 rid/desc 常见字符 → 多半混了中文描述
        if key in ("rid", "desc", "id") and any(
                "\u4e00" <= ch <= "\u9fff" for ch in val):
            return None
        # ③ 通用 rid
        if key == "rid" and val in _GENERIC_RIDS:
            return None
        if key == "rid" and val.split("/")[-1] in _GENERIC_RIDS:
            return None
    return parts


def _pick_landing(new_nodes: set[str]) -> str | None:
    """从「点击后新增的节点」里挑一个最可靠的当落点。

    优先级：
      ① 弹窗标题类 rid（npTitle / dialog_title / alertTitle ...）—— 唯一指认某个弹层
      ② 普通 rid（排除 _GENERIC_RIDS 那批通用容器）
      ③ desc
      （text 一律不选：改个文案就废）

    返回形如 `rid=npTitle`，挑不出就 None。
    """
    rids = [str(n) for n in new_nodes if str(n).startswith("rid=")]
    # ① 弹窗标题
    for want in _MODAL_TITLE_RIDS:
        for r in rids:
            if r[4:] == want:
                return r
    # ② 普通 rid：排除"几乎每个页面都有"的通用容器
    specific = sorted(
        r for r in rids
        if r[4:] not in _GENERIC_RIDS
        and r[4:].split("/")[-1] not in _GENERIC_RIDS)
    if specific:
        return specific[0]
    # ③ desc
    descs = sorted(str(n) for n in new_nodes if str(n).startswith("desc="))
    return descs[0] if descs else None


def _case_stem_of(row: dict) -> str | None:
    """一条会话记录对应的用例 md 文件名；对不上返回 None。"""
    return resolve_case_stem(row.get("package") or "", row.get("title"),
                             row.get("user_input"))


def _resolve_expect_id(db, session_id: int, expect_text: str,
                        explicit: int | None = None,
                        explicit_label: str | None = None) -> tuple[int | None, str | None]:
    """把一条断言的 `expect` 对到用例原文的**第几条预期**（expect_id, expect_label）。

    ## 为什么要自动反推

    `expect_id` 如果是纯可选参数，实际没人传 —— AI 记断言时想的是"把看到
    的写下来"，不会先去数第几条。结果字段永远 NULL，跨轮比对仍然做不了。
    所以这里**从会话原文反推**：`sessions.user_input` 存着用例 md 全文
    （`start --case 176` 时写入），解析出预期清单，再拿这条 expect 去对。

    对不上就返回 `(None, None)`，字段留空 —— 不猜、不硬塞一个编号
    （塞错会让"预期3 抖动"这种查询出现假阳性，比没有更糟）。

    显式传了 `explicit` 就优先用它（AI 自己想写时仍可覆盖）。
    """
    if explicit is not None:
        return explicit, (explicit_label or str(explicit))
    try:
        row = db.get_session(session_id) or {}
    except Exception:  # noqa: BLE001
        return None, None
    exps = case_expectations(row.get("user_input"))
    hit = match_expect_id(exps, expect_text)
    if not hit:
        return None, None
    return hit["id"], hit["label"]


def _checklist(db, session_id: int) -> list[dict]:
    """用例的预期清单 × 本会话已记的结论 —— 这就是 checklist。

    ★ 不另建表：清单就是 `sessions.user_input` 里那段「预期结果」，
    用 `common.case_expectations` 拆条（复合预期按 `；` / `。` 拆开）；
    「勾没勾」就等于「findings 里有没有 expect_id 指向它」。清单随 case 走，
    不随会话走 —— 所以不需要额外的存储，也不需要迁移。

    返回 `[{id, label, text, status, finding_id, actual}]`，按用例原文顺序。
    原文里解析不出预期段时返回 `[]`（调用方按「无可对账清单」处理）。
    """
    row = db.get_session(session_id) or {}
    exps = case_expectations(row.get("user_input"))
    if not exps:
        return []
    hits = {f.get("expect_id"): f for f in db.findings_of(session_id)
            if f.get("expect_id")}
    out: list[dict] = []
    for exp in exps:
        finding = hits.get(exp["id"]) or {}
        out.append({"id": exp["id"], "label": exp["label"], "text": exp["text"],
                    "status": finding.get("status"),
                    "finding_id": finding.get("id"),
                    "actual": finding.get("actual") or ""})
    return out


def _pending(items: list[dict]) -> list[dict]:
    """还没记结论的预期（没有对应 finding）—— 就是「没勾的项」。"""
    return [it for it in items if not it.get("status")]


def _check_gate(db, session_id: int) -> tuple[dict | None, str]:
    """勾选前的两道硬校验，返回 `(当前帧事件, 拒绝原因)`。

    ★ 这里的两个判断都是结构性的，不看语义：

    ① **两次勾选之间必须产生新的看/动事件** —— 否则就是会话末尾连着打勾。
       判据只比 seq：上一条 finding 的证据 seq 必须严格小于本次除 check 之外
       的最大 seq。不需要新列（上一条 finding 的证据事件本来就在库里）。
    ② **证据只能是当下这一帧** —— 取最后一条带证据的事件，参数里**不提供**
       `--event`，所以从设计上就没法回头挑一个「更有利」的旧帧。
    """
    events = db.list_events(session_id)
    if not events:
        return None, ("本会话还没有任何事件 —— 先 observe 或 act 一步，"
                      "勾选必须绑在当下这一帧上")
    by_id = {e.get("id"): e for e in events}
    # check 事件不算「看/动」：把它算进去，"连着打勾"的判据就永远成立不了了。
    cur_max = max((e.get("seq") or 0) for e in events
                  if e.get("kind") != "check")
    last_seq = 0
    for finding in db.findings_of(session_id):
        for eid in finding.get("event_ids") or []:
            ev = by_id.get(eid)
            if ev:
                last_seq = max(last_seq, ev.get("seq") or 0)
    if last_seq and cur_max <= last_seq:
        return None, (
            "自上一次勾选之后没有产生新的看/动事件 —— 不能连着打勾。"
            "先做一步（observe 或 act）看到新状态，再勾下一条。"
            "为什么卡这个：会话末尾连着打勾时，人只能凭记忆回填，"
            "证据必然从前面挑，判出来的结论和当帧对不上。")
    frame = None
    for ev in reversed(events):
        if ev.get("evidence"):
            frame = ev
            break
    if frame is None:
        return None, ("本会话还没有任何带证据的事件（截图/视图树）—— "
                      "先 observe 或 act 一步；只读文本的调用留不下可判的证据")
    return frame, ""


def export_steps(db, session_id: int) -> tuple[str | None, str]:
    """把本轮「能复用的跳转步骤」导成 `<md文件名>_steps.yaml`。

    ★ N-06：返回 `(path, skip_reason)` 而不是裸 `str | None`。
    原来三处 `return None` 是**静默**的 —— 会话跑完在 finish 的输出里
    完全看不出「这次没导出步骤」，下次跑同一条用例才发现没有 `_steps.yaml`，
    却不知道是哪一轮、为什么没生成。现在每一处都给出原因，由 finish
    写进 `self_check`，人和 AI 都能看见。

    返回：`(路径, "")` 成功；`(None, 原因)` 没导出。

    ## 存什么

    只存**用例步骤**里那些「锚点明确 + 落点可验」的动作：

        steps:
          - n: 1
            via: rid=btnCreateManually
            why: 用例步骤2：点击手动创建课程表
            expect: activity=.timetable.management.EditTimetableActivity

    ## 落点怎么来

    优先用 AI 写的 `expect`；**没有就退而用点击后的前台 activity**。

    实测（2026-10-28）：AI 大多数点击都不写 `--expect`，
    176 那轮 25 个 act 里 expect 全空。但每个 act 事件都带 `activity`
    （点击后的前台页面）—— 用它当落点，可用率从 0% 涨到 60%+。

    **只收「activity 变了」的**：没变说明是本页内动作（开/关弹窗），
    不是跳转，自动点的意义不大。

    ## 不存什么

    1. **前置准备** —— `why` 里含「前置 / 准备」字样的。
       同一份用例每次前置可能完全不同（这轮要删课表、下轮不用）。
    2. **带坐标的 via** —— 换机型就废，且这种多半要 AI 现场判断。
    3. **非 tap**（input / swipe 需要额外参数）。
    4. **没有可用落点的**（既没 expect 也没 activity 变化）。

    ## 为什么只存「跳转」不存判定

    跳转是纯机械的（点 A 到 B），代码能做；判定要理解用例，必须 AI 做。
    """
    row = db.get_session(session_id) or {}
    pkg = (row.get("package") or "").strip()
    if not pkg:
        return None, "会话没有记录被测包名，不知道该写进哪个用例目录"

    rows = db.list_events(session_id) or []
    steps: list[dict] = []

    # 起始页面：取第一条带 activity 的事件（通常是开头那次 observe）。
    # 不用 None 起手 —— 否则第一个 act 的 after 和 None 一比必定"变了"，
    # 会把「本页内动作」误判成跳转（实测 rid=iv_more 就这么被误收过）。
    prev_activity: str | None = None
    prev_sig: set[str] = set()
    for ev in rows:
        try:
            d0 = json.loads(ev.get("data_json") or "{}")
        except ValueError:
            continue
        a0 = str(d0.get("activity") or "").strip()
        if a0:
            prev_activity = a0
            prev_sig = set(d0.get("sig") or [])
            break

    for ev in rows:
        if ev.get("kind") != "act":
            # 非 act（observe/read）也要更新基线，否则差分跨了中间那步会失真
            try:
                dn = json.loads(ev.get("data_json") or "{}")
            except ValueError:
                continue
            an = str(dn.get("activity") or "").strip()
            if an:
                prev_activity = an
            sn = set(dn.get("sig") or [])
            if sn:
                prev_sig = sn
            continue
        try:
            data = json.loads(ev.get("data_json") or "{}")
        except ValueError:
            continue
        act = data.get("action") or {}
        via = str(act.get("via") or "").strip()
        expect = str(act.get("expect") or "").strip()
        why = str(act.get("why") or "").strip()
        after = str(data.get("activity") or "").strip()
        after_sig = set(data.get("sig") or [])

        # ① 前置不要（用例原文里没写、AI 自己补的准备工作）
        if any(k in why for k in ("前置", "准备", "prep")):
            prev_activity = after or prev_activity
            prev_sig = after_sig or prev_sig
            continue
        # ② 只收 tap
        if act.get("type") != "tap":
            prev_activity = after or prev_activity
            prev_sig = after_sig or prev_sig
            continue
        # ③ 锚点必须可执行（纯 key=value、无坐标/人话、非通用 rid）
        anchor_parts = _anchors_of(via)
        if not anchor_parts:
            prev_activity = after or prev_activity
            prev_sig = after_sig or prev_sig
            continue
        clean_via = ",".join(anchor_parts)

        # ⑤ 落点，三级优先：
        #    ① AI 写的 expect
        #    ② activity 变了 → activity=xxx（真跳转）
        #    ③ activity 没变但 sig 有新增 → 用新增节点当落点（页内动作）
        #
        #    第 ③ 条是 2026-10-28 补的。实测缺失它会导致漏掉必需的一步：
        #      rid=iv_more（打开「更多」菜单）不跳页，activity 不变，
        #      于是被当成"页内无效动作"丢掉 —— 而下一步「选课程表」
        #      恰恰依赖菜单已经打开。丢了它，下次执行第一步就点不到。
        #
        #    sig 是「点击后屏幕上可见的节点集合」（已去重）。
        #    和上一事件的 sig 做差，就知道这一步让什么新东西出现了。
        landing = expect
        in_page = False
        if not landing:
            if after and after != prev_activity:
                landing = f"activity={after}"
            else:
                new_nodes = after_sig - prev_sig
                landing = _pick_landing(new_nodes)
                if landing:
                    in_page = True

        # ★ from_activity 必须是「动作之前」的页面，所以在更新 prev_* 之前取。
        #
        #   2026-10-29 实测踩到：更新语句写在 item 构造之前，
        #   于是 from_activity 记成了「点完之后在哪」。导出结果自相矛盾：
        #
        #     start_from: activity=.timetable.display.TimetableActivity
        #     - n: 1  via: text=课程表
        #       expect: activity=.timetable.display.TimetableActivity
        #     （从课程表页出发，点「课程表」，又到课程表页）
        #
        #   之前没暴露是因为第一步往往是不跳页的页内动作（如 iv_more
        #   开菜单），前后 activity 相同，取错也看不出来。
        from_activity = prev_activity
        prev_activity = after or prev_activity
        prev_sig = after_sig or prev_sig

        if not landing:
            continue

        item = {"n": 0, "why": why[:60], "via": clean_via, "expect": landing,
                "from_activity": from_activity}
        if in_page:
            item["in_page"] = True
        steps.append(item)

    if not steps:
        return None, ("本轮没有「锚点明确 + 落点可验」的跳转步骤"
                      "（AI 没写 --expect，且点击前后 activity 没变）"
                      "—— 不是出错，是这次没什么可复用的")

    # ★ 去重：同一对 (via, expect) 只留**最后**一次。
    #
    #   为什么会有重复：AI 可能为了做前置先走一遍、正式执行再走一遍。
    #   实测（2026-10-28 20:44 那轮）：
    #     seq3  why=步骤1：菜单里选择课程表        ← 其实是为了删课表
    #     seq12 why=步骤1：选择课程表，预期1 ...   ← 这才是正式的
    #   两步的 (via, expect) 完全一样。
    #
    #   保留最后一次：后面的那次是"正式执行"，前面的多半是踩点/前置。
    seen: dict[tuple[str, str], int] = {}
    for i, s in enumerate(steps):
        seen[(s["via"], s["expect"])] = i
    steps = [s for i, s in enumerate(steps)
             if seen[(s["via"], s["expect"])] == i]
    for i, s in enumerate(steps, 1):
        s["n"] = i

    # 起点校验：第一步点击前在哪。
    #
    # ⚠️ 为什么必须记：steps 第 1 步（如 rid=iv_more）只在**日历首页**存在。
    #   如果下次起点在别的页面，这一步要么找不到（好，会停），
    #   要么**别的页面恰好也有同名 rid** → 点错地方 → 后 8 步全错。
    #   所以记下起点 activity，执行前先验。
    start_from = None
    for s in steps:
        fa = (s.get("from_activity") or "").strip()
        if fa:
            start_from = fa
            break

    # 导出文件名 = 用例 md 的文件名 + `_steps.yaml`（与 md 同名，天然唯一）。
    title = str(row.get("title") or "")
    stem = resolve_case_stem(pkg, title, row.get("user_input"))
    if not stem:
        # 解析不出对应用例 md —— 不硬造一个孤儿产物（WebUI 里只会是空白条目）。
        # 但要**说清是哪一步没成**，否则下次发现没 yaml 只能靠猜。
        return None, (f"解析不出对应的用例 md 文件名（title={title!r}）—— "
                      f"{pkg} 的用例目录里没有能对上的 md；"
                      f"标题里带个用例编号或完整用例名就能对上")
    out_dir = CASES_DIR / pkg
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{stem}_steps.yaml"

    # 步数变少时**照样覆盖**，只在返回里告警。
    #
    #   「旧步数多就不覆盖」看着稳妥，实际分不清「旧的坏」和「旧的好」，
    #   一旦写坏就永久锁死：过滤 bug 时期导出过 17 步的脏文件（混了坐标、
    #   通用 rid、验证绕路），后来正确只剩 5 步，5 < 17 被拒，坏文件一直留着，
    #   每轮 start 都把它当上次路径喂给 AI（含改设备数据的步骤）。
    #
    #   坏文件的害处（AI 照错路径走、改坏设备数据）远大于一次覆盖错；
    #   而且文件里带 source_run，随时能追回是哪一轮写的。
    drop_note = ""
    if out.is_file():
        try:
            old = out.read_text(encoding="utf-8")
            om = re.search(r"^step_count:\s*(\d+)", old, re.M)
            old_n = int(om.group(1)) if om else 0
        except (OSError, ValueError):
            old_n = 0
        if old_n > len(steps):
            drop_note = (f"（已覆盖：步数 {old_n} → {len(steps)}，比上次少 "
                         f"{old_n - len(steps)} 步。如果不是路径变短，"
                         f"回看这轮是否没走全）")

    lines = [
        f"# 自动导出：{stem} 的可复用跳转步骤",
        "#",
        "# 只含「锚点明确 + 落点可验 + 非前置」的 tap。",
        "# 前置条件、造数据、判定都不在这里 —— 那些每次都不一样。",
        "#",
        "# 用法：照着点即可。每步点之前验 expect，不命中就停、交回 AI。",
        "# 注意：这是**上一次**的实测路径，不是唯一正确路径。",
        f"case: {stem}",
        # ★ 2026-11-26：这里原来是 `Path(str(db.path)).parent.name`，
        #   db.path 是 `storage/test.db` → 恒等于 "storage"。57/57 个
        #   _steps.yaml 全是 `source_run: storage`，等于这个字段没信息量。
        #   正确来源是这一轮的运行目录名（run_id），回放时才找得到证据。
        f"source_run: {row.get('run_id') or 'unknown'}",
        f"last_run: {row.get('started_at') or ''}",
        f"verdict: {row.get('status') or ''}",
        f"step_count: {len(steps)}",
    ]
    lines.append("steps:")
    # 起点：第 1 步之前得在哪个页面。执行前先验它，不然可能点错地方。
    if start_from:
        lines[-1:-1] = [f"start_from: activity={start_from}"]
    for s in steps:
        lines += [
            f"  - n: {s['n']}",
            f"    why: {s['why']}",
            f"    via: {s['via']}",
            f"    expect: {s['expect']}",
        ]
        if s.get("in_page"):
            # 不跳页的动作（开/关弹层、切 tab）。expect 验「新节点出现了没」，
            # 不用等 activity 变化。
            lines.append("    in_page: true")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"assets/cases/{pkg}/{out.name}{drop_note}", ""




def main() -> None:
    p = argparse.ArgumentParser(description="测试会话生命周期")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start")
    s.add_argument("--title", default=None)
    s.add_argument("--input", default=None, help="用户口述的用例原文（追溯锚点）")
    s.add_argument("--case", type=str, default=None, dest="case_id",
                   help="引用 assets/cases 下的用例文件，自动带出标题与正文。"
                        "传文件名末尾的编号（如 176）；该编号在本应用内重号时"
                        "传 模块_编号（如 图库导入入口_168）。不要传文件名或路径")
    s.add_argument("--device", default=None)
    s.add_argument("--force", action="store_true",
                   help="即使已有运行中的会话也强行新建（默认拒绝，防孤儿会话）")
    s.add_argument("--diag", action="store_true",
                   help="标记为验证/调试会话：不进「测试记录」库、不计入仪表盘成功率。"
                        "用于工具自检、探针验证等非正式测试的跑动")
    s.add_argument("--package", default=None,
                   help="被测包名（强烈建议显式声明）：崩溃分级的 target 依据。"
                        "不声明则靠首个 observe 回填前台包——但被测 App 崩溃后"
                        "前台会回落到桌面，会把被测包崩溃误判成 other")
    s.add_argument("--related", default="",
                   help="关联包（逗号分隔），其崩溃同样按 BLOCKED 处理；"
                        "权限框宿主默认已含")
    s.add_argument("--no-restart", action="store_true", dest="no_restart",
                   help="跳过冷启动复位。**默认会 force-stop 并冷启动被测包**，"
                        "把起点钉死在 App 首页——这样用例 YAML 的步骤顺序可直接执行，"
                        "不必迁就「上次停在哪」。仅在用例前提要求「App 已处于某状态」"
                        "（如接着上一步继续）时才跳过。注意：只关进程、不清数据。")

    s = sub.add_parser("finding")
    s.add_argument("--status", required=True, choices=STATUSES)
    s.add_argument("--expect", default="")
    s.add_argument("--actual", default="")
    s.add_argument("--note", default="")
    s.add_argument("--event-id", action="append", type=int, default=[],
                   help="关联本会话证据事件；可重复传入多个事件 ID")
    s.add_argument("--expect-id", type=int, default=None, dest="expect_id",
                   help="这条断言对应用例原文的第几条预期（1 起）。**通常不用传**："
                        "不给就会拿 expect 文本自动去对用例原文；"
                        "只有自动对不上、而你确定是第几条时才手填")
    s.add_argument("--id", type=int, default=None,
                   help="记录到指定会话（不给就记到当前会话）")

    # ★ 勾 checklist：一个验证点勾一次，证据自动绑当下这一帧。
    #
    # 与 `finding` 的区别只有两点，但正是这两点堵住了假 PASS：
    #   ① 证据不用传（也传不了）—— 自动取最后一条带证据的事件；
    #   ② 每勾一次之间必须有过新的看/动事件，禁止会话末尾集中补写。
    #
    # 为什么需要它：全库 397 个多断言会话里 354 个（89.2%）的 finding 是
    # 会话结束那一刻一次性写进去的 —— 写的时候手上没有"当帧"，只能从历史里
    # 挑看起来最像的那一帧。实测就出了 #619/#1169 这种拿"点击后"的帧去证
    # "进入即应显示"的假 PASS。
    s = sub.add_parser("check", help="勾一条预期：证据自动绑当下这一帧")
    s.add_argument("--status", required=True, choices=STATUSES)
    s.add_argument("--expect-id", type=int, default=None, dest="expect_id",
                   help="清单里第几条预期（1 起，看 checklist 的输出）")
    s.add_argument("--expect", default="",
                   help="不想数编号就给预期文本，会自动对到清单里的某一条")
    s.add_argument("--actual", default="")
    s.add_argument("--note", default="")
    s.add_argument("--id", type=int, default=None, help="指定会话（默认当前）")

    s = sub.add_parser("checklist", help="看这条用例的预期清单勾到哪了")
    s.add_argument("--id", type=int, default=None, help="指定会话（默认当前）")

    # ★ 批量记断言：一次调用写多条
    #
    # 2026-10-28 实测：176 那轮记了 9 次 finding，每次间隔约 6 秒
    # （AI 思考 + 进程冷启动），共 53 秒。合并成一次就没这个开销。
    #
    # 用法：给一个 JSON 文件，内容是数组：
    #   [
    #     {"status": "PASS", "expect": "预期1 ...", "actual": "...",
    #      "event_id": [13]},
    #     {"status": "FAIL", "expect": "预期2 ...", "actual": "...",
    #      "event_id": [15, 16]}
    #   ]
    s = sub.add_parser("findings-batch",
                       help="一次提交多条断言（给 JSON 文件）")
    s.add_argument("--file", required=True,
                   help="JSON 文件：数组，每项 {status, expect, actual, "
                        "event_id:[..], note}")
    s.add_argument("--id", type=int, default=None, help="指定会话（默认当前）")

    s = sub.add_parser("finish")
    s.add_argument("--status", required=True, choices=STATUSES)
    s.add_argument("--summary", default="")
    s.add_argument("--package", default=None)
    # ⚠️ --device 收下但**不参与逻辑**（2026-11-02）。
    #
    #    真实踩过：跑 168 时 AI 按 start 的参数习惯写了
    #    `finish --status PASS --device HA2DMRBK`，argparse 直接报
    #    「unrecognized arguments: --device」，AI 只好去掉重试 ——
    #    多花一次往返。finish 本来就不需要设备（连库都不连设备），
    #    但为了"同一个会话的 start/finish 参数风格一致"，收下忽略更省事。
    s.add_argument("--device", default=None,
                   help="（兼容用，收下忽略）设备 serial；finish 不连设备")
    s.add_argument("--id", type=int, default=None,
                   help="收尾指定会话（不给就收尾当前会话）")

    s = sub.add_parser("resume", help="崩溃终止后人工确认继续（清除终态）")
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--note", default="", help="恢复原因，随事件入库")

    s = sub.add_parser("perm-intent",
                       help="声明/查看/清除权限响应意图（act/observe 遇到权限弹窗时按此自动响应）")
    s.add_argument("--action", default=None, choices=["grant", "deny"],
                   help="遇到权限弹窗点【同意】(grant) 还是【拒绝】(deny)")
    s.add_argument("--perm", default="",
                   help="权限类型标记（如 media_images/camera/location），仅用于记录")
    s.add_argument("--clear", action="store_true", help="清除意图（分支测完就清）")
    s.add_argument("--show", action="store_true", help="只查看当前意图")
    s.add_argument("--id", type=int, default=None, help="指定会话（默认当前）")

    sub.add_parser("list")
    sub.add_parser("current")
    # abandon 以前只解绑指针，**不动 sessions.status** —— 于是被放弃的会话
    # 永远停在 'running'：列表里看着还在跑、仪表盘 running_n 一直不归零
    # （2026-11-09 本机踩过：自测误开的 #51 就是这么变成僵尸的）。
    # 现在它写一条终态收尾。为什么用 ERROR 不用 BLOCKED：BLOCKED 在产品口径里
    # 是「用例的前置不成立、没机会验证」，算缺陷线索；而作废会话是「这条根本没跑」，
    # 进 ERROR 桶（db.session_stats 单列 error_n，日报趋势里落 other），
    # 不会污染 PASS/FAIL/BLOCKED 三条统计。summary 前缀「作废：」让记录自解释。
    a = sub.add_parser("abandon",
                       help="作废一个会话：写终态收尾 + 解绑指针（不产出用例结论）")
    a.add_argument("--id", type=int, default=None,
                   help="会话 id；缺省 = 当前指针指向的会话")
    a.add_argument("--reason", default="",
                   help='作废原因（必填，写进 summary，如 --reason "title 写错重开"）')
    a.add_argument("--device", default=None, help="（兼容用，收下忽略）")

    # reap：批量回收"僵尸会话"（status 还是 running，但早就没人动了）。
    #
    # 为什么必须有：start 的防重复检查只有拦截、没有出口。
    # 实测 2026-11-17 连续三次被残留会话堵住（#1000216/#1000217/#1000218，
    # 都是子 Agent 被中途停止留下的），每次都要人工 abandon 才能继续。
    # 根因不在工具，而在"没有自动收尸机制"。
    r = sub.add_parser("reap",
                       help="回收僵尸会话：把长时间无新事件的 running 会话"
                            "按 ERROR 收尾（不算用例结论）")
    r.add_argument("--idle-min", type=float, default=60.0, dest="idle_min",
                   help="多久没有新事件算僵尸（分钟，默认 60）。"
                        "实测正常跑测试时事件间隔 P99.5 只有 2.4 分钟，60 分钟极安全")
    r.add_argument("--id", type=int, default=None,
                   help="只处理指定会话（仍受 --idle-min 约束）")
    r.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="只列出会被回收的会话，不动库")

    args = p.parse_args()

    if args.cmd not in _READ_ONLY_CMDS:
        require_writable_storage(args.cmd)

    # ══════════════════════════════════════════════════════════════
    # ★ 先做参数校验，**再**建运行目录
    #
    #   顺序反过来会：`session.py start --title X`（漏了 --input）先
    #   建出一个空的 run_history/<uuid>/ 目录、再报错退出，工作目录里
    #   堆一堆空 uuid 目录，看起来像"跑过了"。
    #
    #   现在：不合法就直接退，一个字节都不写。
    # ══════════════════════════════════════════════════════════════
    db = get_db()

    if args.cmd == "start":
        # ⚠️ 防重复 start（2026-11-02 新增）。
        #
        #    真实事故（10-31 跑 46）：同一目录下建了两个会话 ——
        #      session 1  跑 4 个事件后就被抛弃 → 永远停在 running
        #      session 2  235 个事件 / 32 分钟
        #    旧 session 成了孤儿，既没结论也没人管，污染统计。
        #
        #    根因：`start` 只在「没有 record 目录」时才新建目录，
        #    但**每次都无条件新建 session** 并覆盖 current 指针。
        #
        #    处置：发现还有 running 会话就停下，并给两条明确出路 ——
        #    不要静默新建，也不要硬拦（AI 可能确实需要重开）。
        if not args.force:
            _cur = current_session()
            if _cur:
                try:
                    _s = db.get_session(_cur)
                except Exception:  # noqa: BLE001
                    _s = None
                if _s and str(_s.get("status")) == "running":
                    _nev = len(_s.get("events") or [])
                    _age = str(_s.get("started_at") or "")[:19]
                    _last = (_s.get("events") or [{}])[-1].get("created_at") or _age
                    _idle = ""
                    try:
                        from datetime import datetime as _dt
                        _m = (_dt.now() - _dt.fromisoformat(str(_last)[:19])).total_seconds() / 60
                        if _m >= 10:
                            _idle = f"\n  最后活动 {str(_last)[:19]}（已有 {_m:.0f} 分钟没有新事件）"
                    except (ValueError, TypeError):
                        pass
                    raise SystemExit(
                        f"已有运行中的会话 #{_cur}「{_s.get('title') or ''}」\n"
                        f"  开始 {_age}    已记 {_nev} 个事件{_idle}\n"
                        f"\n"
                        f"  继续跑 → 直接用现有会话，不要再 start\n"
                        f"\n"
                        f"  这条其实已经没人管了（子 Agent 被停止 / 进程被杀会这样）：\n"
                        f"    session.py reap --dry-run\n"
                        f"        （列出所有 running 但长时间无新事件的会话，不动库）\n"
                        f"    session.py reap\n"
                        f"        （按 ERROR 收尾回收，阈值默认 60 分钟；不算用例结论）\n"
                        f"\n"
                        f"  确实要重来（title 写错等），挑一条收尾再 start：\n"
                        f"    session.py abandon --id {_cur} --reason \"重开\"\n"
                        f"        （作废：进 ERROR 桶，不算用例结论 —— 跑错了用这条）\n"
                        f"    session.py finish --status BLOCKED --summary \"重开\"\n"
                        f"        （算一条「没机会验证」的结论 —— 用例真跑不动用这条）\n"
                        f"\n"
                        f"  （确定不要这条检查时加 --force）")

        # ── 定位这条用例是哪份 md 文件 ──────────────────────────────
        #
        # ★ 用例的**唯一真相是磁盘上的 md 文件**（2026-11-17 用户裁决：
        #   「case 表整个删掉」）。数据库里不再有用例表，所以这里只做一件事：
        #   把 `--case` 给的标识解成一份 md，读它的标题行和正文。
        #
        # 标识接受两种写法：
        #   · 纯编号      `--case 176`
        #   · 模块_编号   `--case 图库导入入口_168`
        # 编号在本应用内**已有重号**（启动应用_03 vs 重要日倒数_03），
        # 所以重号时必须用后者；一个编号只命中一条时写编号就行。
        _case_file = find_case_file_by_ref(args.case_id, args.package)
        if _case_file is not None:
            # 文件头是 `# com.zui.calendar_176` 这种标题行，正文才是用例
            _raw = _case_file.read_text(encoding="utf-8", errors="replace")
            title = _raw.splitlines()[0].lstrip("# ").strip() if _raw else None
            user_input = _raw
        else:
            title = maybe_file(args.title) if args.title else None
            user_input = maybe_file(args.input) if args.input else None
        if not title or not user_input:
            raise SystemExit("需要 --case <编号>"
                             "（编号在本应用内重复时写 模块_编号）"
                             "或 --title/--input（新用例，中文长文本用 @文件）")

        # 只有通过重复会话检查和参数校验后才创建并 pin 新运行目录。
        # 否则一次被拒绝的 start 会把仍在运行会话的记录指针切走。
        _rec = new_record_dir()
        pin_record(_rec)
        print(json.dumps({"record_dir": str(_rec)}, ensure_ascii=False))

        sid = db.start_session(title, user_input, args.device,
                               kind="diag" if args.diag else "test")
        set_current(sid)
        if args.package:
            # 显式声明优先：首个 observe 的 backfill 不会覆盖已有值
            db.set_package(sid, args.package)
        related = [x.strip() for x in args.related.split(",") if x.strip()]
        if related:
            db.set_related(sid, related)
        # 扩 logcat 缓冲区防冲（-G 5M）+ 清 crash 缓冲区归因；设备不在也不影响建会话
        from common import try_connect
        d = try_connect(args.device)
        if d is not None:
            notes = []
            for cmd, okmsg in (("logcat -G 5M", "logcat 缓冲区已扩到 5M"),
                               ("logcat -b crash -c", "crash 缓冲区已清空")):
                try:
                    d.shell(cmd)
                    notes.append(okmsg)
                except Exception:
                    notes.append(f"{cmd} 失败（可手动 logcat.py setup / clear）")

            # ── 屏幕常亮：测试过程中不许灭屏 ────────────────────────────
            # 灭屏的代价不是「白等」那么简单：屏幕一黑，UI 树和截图都拿不到，
            # AI 会判成「控件找不到」，然后按 2 次上限记 BLOCKED —— 一条用例
            # 就这么废了，而真实原因只是设备省电。
            #
            # `svc power stayon true` 对应「充电时不锁屏」，插着 USB 就一直亮。
            # **开了不还原**（用户 2026-11-06 的裁决：「只开不管，一直保持常亮」）：
            # 还原反而会在收尾和下一轮开工之间留一个灭屏窗口。
            # 失败不阻断开工 —— 屏幕设置不成功也该把用例跑完，只在 note 里写明。
            try:
                d.shell("svc power stayon true")
                notes.append("屏幕常亮已开启（stayon true）")
            except Exception as e:  # noqa: BLE001
                notes.append(f"屏幕常亮设置失败：{e}（跑的过程中可能灭屏）")

            # ── u2 输入法：换成 AdbKeyboard 并设为默认 ──────────────────
            # 为什么在开场做（2026-11-05）：换新设备、做过 OTA、或用户手动切回
            # 搜狗之后，`send_keys(clear=True)` 会直接抛
            #   AdbBroadcastError: ADB_KEYBOARD_CLEAR_TEXT failed: Attempt to read
            #   from field 'ExtractedText.text' on a null object reference
            # 这个报错出现在 act 的输出里，AI 很容易误判成「控件坏了」，
            # 然后按「同一目标最多试 2 次」记 BLOCKED —— 实际是环境没准备。
            #
            # 用 u2 自带能力（_input.py:49 set_input_ime）：没装就先装
            # （内置 assets/app-uiautomator.apk），装了就 ime enable/set +
            # settings put secure default_input_method。已就绪时只花 ~145ms。
            # 不自己在 connect() 里做 —— 那是每次工具调用都付的代价。
            try:
                d.set_input_ime(True)
                notes.append(f"输入法已就绪（{d.current_ime()}）")
            except Exception as e:  # noqa: BLE001
                notes.append(f"输入法设置失败：{e}（会打不了字，先手动确认设备输入法）")
            device_note = "；".join(notes)
        else:
            device_note = "设备未连接（连上后手动跑 logcat.py setup + logcat.py clear）"

        # ── 冷启动复位：把起点钉死在「App 冷启动后的首页」────────────────
        # 为什么默认做（2026-10-15 定）：起点如果取决于「上一次停在哪个界面」，
        # 那么用例 YAML 的步骤顺序就没法直接用 —— 复跑时设备可能停在链路中间，
        # 判据对不上，只能靠人去「迁就设备现在在哪」（这正是「融合」要解决的问题）。
        # 冷启动把这个问题从根上消掉：**不管上次停在哪，冷启动后一定在首页**，
        # 于是「从首页按固定路径走到 s1」成为可能，YAML 顺序即可直接执行。
        #
        # ⚠️ 只 force-stop（关进程），**不清数据**。
        # 「清数据」（pm clear / pm revoke）是**用例明确要求**时才做的事
        # （如「首次使用」「清除日历的数据」「测首次授权」）——见 SKILL「清理的范围跟着用例走」。
        # 拿 pm clear 当默认复位＝拿大锤砸一切：会清掉本用例要用的前置数据。
        restart_note = None
        pkg = args.package
        if d is not None and pkg and not args.no_restart:
            try:
                d.app_stop(pkg)
                d.app_start(pkg)
                restart_note = f"已冷启动复位 {pkg}（起点固定为 App 首页）"
            except Exception as e:  # noqa: BLE001
                restart_note = f"冷启动复位失败：{e}（起点未固定，需自行确认当前界面）"
        elif args.no_restart:
            restart_note = "已跳过冷启动复位（--no-restart），**起点不固定**，需自行确认当前界面"
        if restart_note:
            device_note = f"{device_note}；{restart_note}"

        # 录屏在 App 冷启动后开始，落入本次唯一 run_history 目录。
        # 录屏是辅助证据；环境不支持时只提示，不阻断测试用例。
        video_note = "录屏未启动（设备未连接）"
        if d is not None:
            try:
                _serial = args.device or getattr(d, "serial", None)
                _size = d.window_size()
                _video = start_video_recording(_serial, current_record_dir(), _size)
                video_note = ("录屏已启动"
                              if _video.get("started")
                              else f"录屏未启动：{_video.get('reason') or '未知原因'}")
            except Exception as _video_error:
                video_note = f"录屏未启动：{_video_error}"
        device_note = f"{device_note}；{video_note}"

        (STORAGE_DIR / f".crash_seen_{sid}").unlink(missing_ok=True)
        out = {"ok": True, "session_id": sid, "related": related,
               "note": f"后续 observe/act/read 自动记入本会话；{device_note}"}

        # ★ 环境探测：告诉 AI 这台机器上该用什么 shell 语法。
        #
        #   用户要求：「skills 应该能够根据当前用户的设备情况。
        #             自动调整命令的格式和写法。而不是我们强制限定死。」
        #
        #   所以不是让 AI 自己猜、也不是在 SKILL 里写死"Windows 用 PS"，
        #   而是在开场把探测结果直接给它。
        out["env"] = detect_shell()
        # 知识卡浮现：有卡就提示路径 + 小节索引（按卡键去重，见 common.knowledge_hint_key）
        #
        # ★ N-05：去重键从「会话级布尔」改成「按卡」。这里 args.package 可能为空
        #   （start 时还没看到设备），那就 key="" 不提示、不落标记，
        #   留给第一个 observe 补 —— 与原来的行为一致。
        _kkey = knowledge_hint_key(args.package)
        hint = (knowledge_hint(args.package, shown_keys=db.knowledge_hint_keys(sid))
                if _kkey else None)
        if hint:
            out["knowledge_hint"] = hint
            # 只要发出了 hint 就落标记（无卡包也有 system_card 提示，同样别重复发）
            db.mark_knowledge_hint(sid, _kkey)
        # 上次走过的跳转步骤：纯参考，不自动执行。
        # 每次跑同一条用例，AI 都要重新摸「更多按钮在哪、菜单里哪个是课程表」——
        # 这些是机械跳转，finish 时已经导出到 steps.yaml，开场给它看。
        try:
            steps = steps_hint(args.package, title, user_input)
            if steps:
                out["last_steps"] = steps
        except Exception as e:  # noqa: BLE001
            out.setdefault("warnings", []).append(f"步骤提示读取失败：{e}")
        print(json.dumps(out, ensure_ascii=False))
    elif args.cmd == "finding":
        sid = args.id if getattr(args, "id", None) else current_session()
        if not sid:
            raise SystemExit("没有进行中的会话，先 session.py start（或用 --id 指定）")
        _exp_text = maybe_file(args.expect)
        _eid, _elab = _resolve_expect_id(db, sid, _exp_text,
                                          getattr(args, "expect_id", None))
        fid = db.add_finding(sid, args.status, _exp_text,
                             maybe_file(args.actual), maybe_file(args.note),
                             event_ids=args.event_id,
                             expect_id=_eid, expect_label=_elab)
        emit(True, {"finding_id": fid, "evidence_event_ids": args.event_id,
                    "expect_id": _eid, "expect_label": _elab})
    elif args.cmd == "check":
        sid = args.id if getattr(args, "id", None) else current_session()
        if not sid:
            raise SystemExit("没有进行中的会话，先 session.py start（或用 --id 指定）")
        items = _checklist(db, sid)
        if not items:
            raise SystemExit(
                "这条用例的原文里解析不出「预期结果」段，没有可勾的清单。\n"
                "  常见原因：start 时没传 --case，或 user_input 不是用例原文。\n"
                "  这种情况用 session.py finding --status ... --expect ... --event-id ... 记")
        target = None
        if args.expect_id is not None:
            for item in items:
                if item["id"] == args.expect_id:
                    target = item
                    break
            if target is None:
                raise SystemExit(f"清单里没有第 {args.expect_id} 条预期"
                                 f"（共 {len(items)} 条，先跑 checklist 看一遍）")
        else:
            _text = maybe_file(args.expect)
            if not _text.strip():
                raise SystemExit("要指定勾哪一条：--expect-id N（或 --expect \"预期文本\"）")
            _eid, _ = _resolve_expect_id(db, sid, _text, None)
            if _eid is None:
                raise SystemExit(
                    "这句 expect 对不上用例原文的任何一条预期。\n"
                    "  先跑 checklist 看清单原文，再用 --expect-id N 指定")
            for item in items:
                if item["id"] == _eid:
                    target = item
                    break
        if target["status"]:
            raise SystemExit(
                f"第 {target['id']} 条已经勾过了（{target['status']}，"
                f"finding #{target['finding_id']}）。\n"
                "  判错了就在 summary 里写「更正 finding#N」，不要重复占位。")
        frame, why = _check_gate(db, sid)
        if frame is None:
            raise SystemExit(why)
        _expect_text = f"预期{target['label']} {target['text']}".strip()
        fid = db.add_finding(sid, args.status, _expect_text,
                             maybe_file(args.actual), maybe_file(args.note),
                             event_ids=[frame["id"]],
                             expect_id=target["id"], expect_label=target["label"])
        # 勾选本身留一条轻量事件（不带证据），事后能看出"什么时候勾的"。
        # ⚠️ 它 kind=check，不参与 _check_gate 的「新事件」判据。
        db.log_event(sid, "check", "session check",
                     f"勾第{target['id']}条 → {args.status}"
                     f"（证据 event {frame['id']} / seq {frame.get('seq')}）",
                     data={"expect_id": target["id"], "status": args.status,
                           "event_id": frame["id"], "finding_id": fid})
        left = _pending(_checklist(db, sid))
        out = {"ok": True, "finding_id": fid, "expect_id": target["id"],
               "expect_label": target["label"], "status": args.status,
               "evidence_event_id": frame["id"],
               "evidence_seq": frame.get("seq"),
               "remaining": len(left),
               "remaining_ids": [it["id"] for it in left]}
        if left:
            out["hint"] = ("还剩 %d 条没勾：%s —— 一条一条来，每条都先看到新状态"
                           % (len(left), "、".join(f"预期{it['label']}" for it in left)))
        else:
            out["hint"] = "清单已全部勾完，可以 finish 了"
        emit(True, out)
    elif args.cmd == "checklist":
        sid = args.id if getattr(args, "id", None) else current_session()
        if not sid:
            raise SystemExit("没有进行中的会话（或用 --id 指定）")
        items = _checklist(db, sid)
        if not items:
            raise SystemExit("这条用例的原文里解析不出「预期结果」段，没有清单")
        left = _pending(items)
        emit(True, {
            "session_id": sid, "total": len(items), "done": len(items) - len(left),
            "pending": len(left),
            "items": [{"id": it["id"], "label": it["label"],
                       "text": it["text"][:120],
                       "status": it["status"] or "未勾",
                       "finding_id": it["finding_id"]} for it in items],
        })
    elif args.cmd == "findings-batch":
        # 一次写多条断言。任何一条不合法就整批不写（避免写一半）。
        sid = args.id if getattr(args, "id", None) else current_session()
        if not sid:
            raise SystemExit("没有进行中的会话，先 session.py start（或用 --id 指定）")
        try:
            # ★ utf-8-sig：PowerShell 的 `Set-Content -Encoding utf8` 会写
            #   BOM，直接 json.loads 会报 "Unexpected UTF-8 BOM"。
            #   实测（2026-10-28）：AI 写断言文件必然用 Set-Content，必踩。
            raw = Path(maybe_file(args.file)).read_text(encoding="utf-8-sig")
            items = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"读不了断言文件：{exc}")
        if not isinstance(items, list) or not items:
            raise SystemExit("文件内容要是非空数组")
        # 先校验
        for i, it in enumerate(items, 1):
            if not isinstance(it, dict):
                raise SystemExit(f"第 {i} 项不是对象")
            st = it.get("status")
            if st not in STATUSES:
                raise SystemExit(
                    f"第 {i} 项 status 不合法：{st!r}（可用 {'/'.join(STATUSES)}）")
            if not it.get("expect"):
                raise SystemExit(f"第 {i} 项缺 expect")
        ids = []
        _matched = []
        for it in items:
            ev = it.get("event_id") or it.get("event_ids") or []
            if isinstance(ev, int):
                ev = [ev]
            ids.append([int(x) for x in ev])
            _et = it.get("expect", "")
            _eid, _elab = _resolve_expect_id(db, sid, _et, it.get("expect_id"))
            fid = db.add_finding(sid, it["status"], _et,
                                 it.get("actual", ""), it.get("note", ""),
                                 event_ids=ids[-1],
                                 expect_id=_eid, expect_label=_elab)
            _matched.append({"finding_id": fid, "expect_id": _eid,
                             "expect_label": _elab})
            ids[-1] = fid
        _unmatched = sum(1 for m in _matched if m["expect_id"] is None)
        emit(True, {"count": len(items), "finding_ids": ids,
                    "expects": _matched,
                    "note": ("批量写入完成" if not _unmatched else
                             f"批量写入完成；其中 {_unmatched} 条对不上用例原文的预期"
                             "（expect_id 留空，不影响结论，只是跨轮比对少了这一条）")})
    elif args.cmd == "resume":
        # 用途只有一个：**崩溃终止后人工确认继续**（清除终态，否则工具仍拒绝执行）。
        #
        # ⚠️ 顺序要紧：先校验理由，**通过后才清终态**。
        # 反过来会导致"无理由被拒"的那次调用已把终态抹掉，
        # 第二次不带理由的 resume 反而放行（真实 bug）。
        was_terminated = db.get_termination(args.id)
        note = maybe_file(args.note) if args.note else ""
        if was_terminated and not note:
            raise SystemExit(
                f"会话 #{args.id} 因崩溃被终止，继续执行必须给出理由："
                f"resume --id {args.id} --note \"为什么可以继续（如已确认偶发、已恢复前置）\"")
        db.clear_termination(args.id)
        db.set_session_status(args.id, "running")
        set_current(args.id)
        if note:
            db.log_event(args.id, "note", "session resume", note)
        out = {"ok": True, "session_id": args.id, "status": "running"}
        if was_terminated:
            out["note"] = "已清除崩溃终态，会话恢复运行"
            out["was_terminated"] = True
            out["terminate_reason"] = was_terminated
        print(json.dumps(out, ensure_ascii=False))
    elif args.cmd == "finish":
        sid = args.id if getattr(args, "id", None) else current_session()
        if not sid:
            raise SystemExit("没有进行中的会话（要收尾别的会话请加 --id）")
        summary = maybe_file(args.summary)
        _session_row = db.get_session(sid) or {}
        _run_id = _session_row.get("run_id")
        # ★ N-11：旧会话的 run_id 是 hex12，单库化后指向的目录**早就不存在**。
        #   照着拼只会在后面白等录屏 timeout、并给 unpin_record 一个假路径。
        #   目录不在就不拼（_video_dir=None），并把这件事写进返回，让人看得见。
        _stale_run = bool(_run_id) and run_dir_missing(_run_id)
        _video_dir = None if _stale_run else run_dir_path(_run_id)
        # ★ checklist 对账：判 PASS 时清单必须全勾完。
        #   漏勾的预期 = "这条根本没验证" —— 按四态语义那是 BLOCKED，不是 PASS。
        #   放在 finish_session 之前拦：那一步已经会因「PASS 却含非 PASS finding」
        #   报错，但报的是内部口径，不如这里直接点名哪几条没勾。
        _items = _checklist(db, sid)
        _pend = _pending(_items)
        if _items and _pend and args.status == "PASS":
            raise SystemExit(
                "还有 %d 条预期没勾，不能判 PASS：\n  %s\n"
                "  每条预期都要有自己的一条结论；漏勾说明这条没验证到。\n"
                "  真没机会验的（前置不成立 / 进不去 / 崩溃），结论用 BLOCKED "
                "并在 summary 里写明原因。"
                % (len(_pend),
                   "\n  ".join(f"预期{it['label']} {it['text'][:60]}"
                               for it in _pend)))
        ok = db.finish_session(sid, args.status, summary, args.package)
        # ★ 录屏收尾必须在结论落库**之后**：停止要等 scrcpy 把 MP4 尾写好，
        #   正常一两秒，scrcpy 卡住时要等到 timeout。放在前面，
        #   这份等待就白压在最关键的"把结论存下来"上 —— 关窗、超时、崩溃，
        #   丢的是用例结论，而不是一段视频。
        _video_result = (stop_video_recording(_video_dir, timeout=15)
                         if _video_dir else {"stopped": False,
                                             "reason": ("运行目录已不存在（stale run_id）"
                                                        if _stale_run
                                                        else "当前会话没有 run_id")})
        cleanup_warning = None
        try:
            clear_current(sid)
        except (ValueError, OSError) as error:
            cleanup_warning = f"Conclusion committed, but current pointer cleanup failed: {error}"
        crash_n = db.count_crash_events(sid)
        stats = db.finding_stats(sid)
        self_check = []
        if _items and _pend:
            self_check.append(
                "ℹ️ 还有 %d 条预期没勾（%s）—— 判 PASS 时会被拦下"
                % (len(_pend), "、".join(f"预期{it['label']}" for it in _pend)))
        if cleanup_warning:
            self_check.append(cleanup_warning)
        # ★ 解绑"当前 record"指针 —— 只有指针确实属于本会话才解。
        #   不清的后果：下一次不属于任何会话的 observe/act 会误接到上一次的目录里。
        #   带 owner 的后果（N-02）：**不带 owner 会误删别人还在跑的指针**，
        #   被删的那个会话后续证据会落到 _orphan/，而且没有任何报错。
        unpin_record(_video_dir)
        # 操作效率自查（SKILL.md「执行节奏」）：
        # 只提示不阻断 —— 效率差不等于用例结论错，但**必须让人看见**，
        # 否则"跑得很脏"会悄悄过去（实测有的会话 44% 的 act 是无效试探）。
        eff = db.efficiency_stats(sid)
        if eff["act"]:
            if eff["observe"] >= eff["act"]:
                self_check.append(
                    f"⚠️ observe({eff['observe']}) 不少于 act({eff['act']}) —— "
                    f"SKILL 要求 observe 明显少于 act（第一反应该是读上一步的 next）")
            if eff["swipe"]:
                self_check.append(
                    f"⚠️ 用了 {eff['swipe']} 次 swipe —— 拨滚轮/重复手势请改用 "
                    f"act tap --repeat N（swipe 有惯性会过冲）")
            if eff["cp_miss"]:
                self_check.append(
                    f"⚠️ 有 {eff['cp_miss']} 次检查点未命中 —— 确认是否为无效试探步")
        # 采集路径边（不再导出步骤序列）。
        # 为什么换掉：步骤序列是最易腐的知识（换个状态/版本就废），
        # 导出「可复用的跳转步骤」（只含锚点明确+落点可验+非前置的 tap）。
        # 下次跑同一条用例时可以直接照着点这几步，不用 AI 每步重新找控件。
        #
        # ⚠️ diag（验证/调试）会话不导出。
        #   自检会话的标题常常也带用例编号（如"联想日历_176 步骤提示验证"），
        #   导出会覆盖掉真实运行的步骤文件 —— 实测踩过一次。
        steps_note = None
        try:
            _kind = str((db.get_session(sid) or {}).get("kind") or "test")
            if _kind == "diag":
                steps_note = "（diag 会话，跳过步骤导出）"
            else:
                steps_note, _skip = export_steps(db, sid)
                # ★ N-06：没导出也要说为什么，别静默。
                #   原来 export_steps 返回 None 就什么都不写，跑完一轮
                #   在 finish 输出里完全看不出「这次没生成 _steps.yaml」。
                if not steps_note and _skip:
                    self_check.append(f"ℹ️ 步骤未导出：{_skip}")
        except Exception as e:  # noqa: BLE001
            self_check.append(f"⚠️ 步骤导出失败：{e}")

        out = {"ok": ok, "session_id": sid, "crash_events": crash_n,
               "findings": stats, "self_check": self_check,
               "hint": (f"本会话有 {crash_n} 个崩溃事件——若涉及被测/关联包，"
                        "结论应为 BLOCKED 且报告需附 crash.log 证据路径；"
                        "无关包崩溃仅需在报告中说明")
               if crash_n else "无崩溃事件"}
        out["video_recording"] = _video_result
        # 旧会话（hex12 run_id）的运行目录早已不存在：显式报出来，
        # 免得人以为"录屏没启动"或"证据丢了"，实际是那代记录本来就没有。
        if _stale_run:
            out["stale_run_id"] = True
            out["stale_note"] = ("该会话的 run_id 属于旧记录格式，"
                                 "对应的 run_history 目录已不存在 —— "
                                 "没有可收尾的录屏，证据回放也不可用。"
                                 "结论与 finding 不受影响。")
        if steps_note:
            out["steps_exported"] = steps_note
            # ★ 只有**真的导出成功**才说导出。旧写法只看 steps_note 非空，
            #   而 diag 会话的 steps_note 是「（diag 会话，跳过步骤导出）」
            #   —— 于是同一份输出里既说「跳过导出」又说「已把步骤导出」，
            #   自相矛盾（2026-11-27 真机验证时实测到）。
            if steps_note.startswith("assets/"):
                out["steps_note"] = (
                    "已把本轮「锚点明确+落点可验」的跳转步骤导出。"
                    "下次跑同一条用例可以直接照着点这几步，"
                    "前置条件/造数据/判定不在里面（那些每次不一样）。")
        emit(True, out)
        # ★ 会话收尾 -> 清掉"当前 record"指针。
        #
        #   不清的后果：下一次不属于任何会话的 observe/act
        #   会误接到上一次的目录里，把这次的证据写进历史运行。
        #   （解绑动作在函数开头就做了，见 unpin_record(_video_dir) 那处）
    elif args.cmd == "abandon":
        sid = args.id if getattr(args, "id", None) else current_session()
        if not sid:
            raise SystemExit(
                "没有进行中的会话可放弃。\n"
                "  想作废历史记录里某条：session.py abandon --id <会话id> --reason \"...\"\n"
                "  看有哪些：session.py list")
        cur = current_session()
        row = db.get_session(sid) or {}
        if not row:
            raise SystemExit(f"会话 #{sid} 不存在")
        status = str(row.get("status"))
        if status not in INTERIM:
            # 已经有结论了：绝不覆盖人写过的判定，只把指针解绑。
            if cur == sid:
                clear_current(sid)
            unpin_record(None if run_dir_missing(row.get("run_id"))
                         else run_dir_path(row.get("run_id")))
            emit(True, {"session_id": sid, "status": status,
                        "stale_run_id": run_dir_missing(row.get("run_id")),
                        "note": f"会话 #{sid} 已是结论态（{status}），"
                                f"abandon 不覆盖已写的结论；本次只解绑了指针"})
            raise SystemExit(0)
        reason = (maybe_file(args.reason) or "").strip()
        if not reason:
            raise SystemExit(
                f'abandon 需要原因：session.py abandon --id {sid} --reason "为什么作废"\n'
                "  原因会写进 summary —— 不然半年后没人知道这条为什么没结论。")
        # 崩溃终止过的会话只能落 BLOCKED（db.finish_session 里写死的规矩），
        # 这里跟着让步，别让人卡在一个想不通的 ValueError 上。
        final = "BLOCKED" if row.get("terminated") else "ERROR"
        _run_id = row.get("run_id")
        _stale_run = bool(_run_id) and run_dir_missing(_run_id)
        _video_dir = None if _stale_run else run_dir_path(_run_id)
        _video_result = stop_video_recording(_video_dir) if _video_dir else {
            "stopped": False,
            "reason": ("运行目录已不存在（stale run_id）" if _stale_run
                       else "当前会话没有 run_id")}
        db.finish_session(sid, final, f"作废：{reason}")
        if cur == sid:
            clear_current(sid)
        unpin_record(_video_dir)
        emit(True, {"session_id": sid, "status": final, "reason": reason,
                    "stale_run_id": _stale_run,
                    "video_recording": _video_result,
                    "note": ("已作废收尾，不计入用例通过率；"
                             + ("该会话曾被崩溃终止，按规矩落 BLOCKED；"
                                if final == "BLOCKED" else "进 ERROR 桶；")
                             + "证据目录保留不动")})
    elif args.cmd == "reap":
        # 回收僵尸会话：running 但长时间无新事件。
        #
        # ⚠️ 落 ERROR 而不是 BLOCKED：BLOCKED 在产品口径里是
        #    「用例的前置不成立、没机会验证」，是缺陷线索；僵尸会话是
        #    「这条根本没跑完，而且没人管了」，与 abandon 同桶。
        #    summary 前缀「自动回收：」让记录自解释 —— 半年后能分清
        #    这是人作废的还是机器收的。
        stale = db.stale_sessions(max_idle_min=args.idle_min)
        if getattr(args, "id", None):
            stale = [s for s in stale if s["id"] == args.id]
        if not stale:
            emit(True, {"reaped": [], "note":
                        f"没有僵尸会话（阈值 {args.idle_min:g} 分钟内无新事件的 running 会话）"})
            raise SystemExit(0)
        if getattr(args, "dry_run", False):
            emit(True, {"dry_run": True, "would_reap": stale,
                        "note": f"{len(stale)} 个会话会被回收（--dry-run，未动库）"})
            raise SystemExit(0)
        done, failed = [], []
        cur = current_session()
        for s in stale:
            reason = (f"自动回收：{s['idle_min']:g} 分钟无新事件"
                      f"（阈值 {args.idle_min:g} 分钟），最后活动 {s['last_activity']}，"
                      f"已记 {s['events']} 个事件")
            try:
                # 崩溃终止过的只能落 BLOCKED（finish_session 的规矩）
                final = "BLOCKED" if s["terminated"] else "ERROR"
                _run_id = (db.get_session(s["id"]) or {}).get("run_id")
                db.finish_session(s["id"], final, reason)
                # 录屏收尾放结论之后，且用短超时：reap 是设备卡死时的救命命令，
                # 批量回收 N 个会话不能每个都等满默认 timeout。视频是辅助证据，
                # 收不干净也不该拖住回收本身。
                _video_dir = (None if run_dir_missing(_run_id)
                              else run_dir_path(_run_id))
                _video_result = (stop_video_recording(_video_dir, timeout=5)
                                 if _video_dir else {"stopped": False,
                                                     "reason": ("运行目录已不存在（stale run_id）"
                                                                if _run_id else "run_id 不存在")})
            except (ValueError, sqlite3.Error, OSError) as e:  # noqa: PERF203
                failed.append({"id": s["id"], "error": str(e)})
                continue
            if cur == s["id"]:
                clear_current(s["id"])
                unpin_record(_video_dir)
            done.append({"id": s["id"], "status": final,
                         "stale_run_id": bool(_run_id) and run_dir_missing(_run_id),
                         "video_recording": _video_result,
                         "idle_min": s["idle_min"], "events": s["events"]})
        emit(not failed, {"reaped": done, "failed": failed,
                          "note": (f"回收 {len(done)} 个僵尸会话，进 ERROR 桶，"
                                   f"不计入用例通过率；证据目录保留不动")
                                  + (f"；{len(failed)} 个失败" if failed else "")})
    elif args.cmd == "perm-intent":
        sid = args.id if getattr(args, "id", None) else current_session()
        if not sid:
            raise SystemExit("没有进行中的会话（或用 --id 指定）")
        # ⚠️ --id 指向不存在的会话时必须报错，不能 ok:true（旧版会）。
        #    被人拿来确认"意图已声明"时，ok:true 会造成完全错误的印象。
        if not db.get_session(sid):
            emit(False, {"session_id": sid, "error": "会话不存在",
                         "hint": "用 session.py list 查看有效会话号"})
            raise SystemExit(1)
        if args.clear:
            db.clear_perm_intent(sid)
            # ★ 2026-11-27：两阶段的「这一类已经拒过」也要一起清 ——
            #   换分支（测完拒绝再测同意）或从头重测时，残留的 denied 键会让
            #   下一个同类权限框直接被翻成「允许」，拒绝分支就再也测不到了。
            db.clear_perm_denied(sid)
            db.log_event(sid, "perm", "perm-intent",
                         "权限意图已清除，并重置两阶段状态（已拒过的权限类清零）")
            print(json.dumps({"ok": True, "session_id": sid, "intent": None,
                              "denied_keys": [],
                              "note": "权限意图与两阶段状态均已清除：此后遇到权限弹窗"
                                      "回到【默认同意】，且同类权限会重新从「拒绝」开始"},
                              ensure_ascii=False))
        elif args.show or not args.action:
            cur = db.get_perm_intent(sid)
            # ★ 两阶段状态也要看得见：只报意图不报 denied 键，运维会以为
            #   「声明了 deny 就一定会点拒绝」，而第二次同类框其实点的是允许。
            _denied = sorted(db.perm_denied_keys(sid))
            if cur:
                # 过期也照样报出来，并说清"这步会怎么执行"
                out = {"ok": True, "session_id": sid, "intent": cur,
                       "denied_keys": _denied,
                       "hint": ("声明仍有效：后续 act 遇到权限弹窗自动点"
                                f"【{'同意' if cur['action'] == 'grant' else '拒绝'}】"
                                + ("；⚠️ 已过期，但仍按此声明执行，动作日志会标 ⚠️"
                                   if cur["expired"] else ""))}
            else:
                # 旧提示写的是"只会记录、不会点击"——与实际相反（默认会点同意）。
                out = {"ok": True, "session_id": sid, "intent": None,
                       "denied_keys": _denied,
                       "hint": "当前未声明意图：遇到权限弹窗会按【默认同意】自动点击；"
                               "同类媒体权限第一次仍会先点「拒绝」（两阶段固定规则），"
                               "第二次才点「允许」。要强制走拒绝分支，"
                               "先 perm-intent --action deny 声明。",
                       "default_action": "grant"}
            print(json.dumps(out, ensure_ascii=False))
        else:
            ok = db.set_perm_intent(sid, args.action, args.perm)
            db.log_event(sid, "perm", "perm-intent",
                         f"权限意图: {args.action}"
                         + (f"（{args.perm}）" if args.perm else ""))
            print(json.dumps({"ok": ok, "session_id": sid,
                              "intent": {"action": args.action, "permission": args.perm,
                                         "expires_in_s": db.PERM_INTENT_TTL_S},
                              "hint": (f"后续 act/observe 遇到权限弹窗将自动点"
                                       f"【{'同意' if args.action == 'grant' else '拒绝'}】"
                                       f"；有效期 {db.PERM_INTENT_TTL_S:.0f} 秒，"
                                       f"超过后仍按此声明执行但动作日志会标 ⚠️；"
                                       f"声明 deny 时同类权限只拒一次 —— "
                                       f"第二次出现会自动改点「允许」（两阶段固定规则）；"
                                       f"分支测完请 perm-intent --clear")},
                             ensure_ascii=False))
    elif args.cmd == "list":
        print(json.dumps(db.list_sessions(20), ensure_ascii=False, indent=2))
    elif args.cmd == "current":
        print(json.dumps({"session_id": current_session()}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (sqlite3.Error, ValueError, OSError) as error:
        emit(False, {"error": str(error), "stage": "session",
                     "rejected": isinstance(error, ValueError),
                     "hint": "Correct the input or storage issue; rejected finish does not commit."})
        raise SystemExit(1)
