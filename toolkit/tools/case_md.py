"""用例资产自检：`assets/cases/<应用>/` 下的 md 与 `_steps.yaml` 是否对得上。

## 这个脚本的历史包袱（改立场的原因）

它原来叫"把 YAML 渲染成 MD"，docstring 写着
「**YAML 是唯一执行真相，MD 是它的渲染产物**」「不要手改 MD」。
**这个立场已经反了**，而且是 2026-11-17 用户裁决后反的：

  · 真相 = 磁盘上人写的 `<stem>.md`（`tools/session.py` 里
    `find_case_file_by_ref` 直接读它；`start --case 176` 把正文整个存进
    `sessions.user_input` 供解析）
  · `<stem>_steps.yaml` 是**派生的跳转缓存**（`session.py export_steps`
    在 finish 时自动写），只存「锚点明确 + 落点可验 + 非前置」的 tap

原来的实现有三个真问题：

  1. `CASES_DIR.rglob("*.yaml")` 扫到的就是那 57 个 `_steps.yaml` ——
     它们的顶层是 `case/source_run/last_run/verdict/step_count/start_from/steps`，
     **没有 `meta`**。于是 `doc["meta"]["case_id"]` 恒为 None，
     `--check` 打出来一屏「case None」，什么也没校验。
  2. 真要渲染，`out = f.with_suffix(".md")` 会把 `x_steps.yaml` 变成
     `x_steps.md`（多一层 `_steps`），同时对不上人写的 `x.md`。
     若哪天有人把 yaml 命名成 `x.yaml`，**这一行会直接覆盖人手写的用例**。
  3. `render()` 依赖的整张 schema（`meta/confidence/playbook/preconditions/
     assertions/pitfalls`、`_LEVEL_BADGE` 的 verified/flaky/... 五档）
     在现实资产里**一个都没用上**。它是为一份从未落地的格式写的。

## 现在做什么

反过来：**用 md 当真值，去校验派生的 yaml 有没有过期/悬空/不可执行**。

    python tools/case_md.py --check     # 自检，有问题返回 1（CI/开工前可跑）
    python tools/case_md.py --report    # 出一张人看的表（用例 × 缓存状态 × 上次结论）

不写任何 `.md`，不覆盖任何源文件 —— 渲染立场没有了，就不该保留写入口。
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from datetime import datetime
from pathlib import Path

# ★ UTF-8 兜底：Windows 控制台默认 GBK，print 一个 ✅/⚠️ 就崩。
try:
    from common import ensure_utf8_stdio
    ensure_utf8_stdio()
except Exception:  # noqa: BLE001  common 不可用时不要因此崩掉
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

CASES_DIR = Path(__file__).resolve().parent.parent / "assets" / "cases"

# 裸坐标写进 `via` = 换个分辨率就废。与 `session.py._BAD_IN_BRACKET` 同口径，
# 但这里是查**已落盘的缓存**，所以连括号外的裸坐标也拦。
_BARE_COORD = re.compile(r"\d{2,5}\s*,\s*\d{2,5}")

# 「通用节点」：屏幕上有它不代表到了目标页面，不能当锚点也不能当落点。
# 与 `session.py._GENERIC_RIDS` 同源，这里只取最常误用的几个（保持清单短，
# 误报比漏报更烦人）。
_GENERIC_RIDS = {
    "button1", "button2", "button3", "content", "contentPanel",
    "parentPanel", "customPanel", "action_bar_root", "android:id/button1",
    "android:id/button2", "android:id/content",
}

# 缓存多久算"旧"：30 天。超过只是提示，不算错 —— 用例本身可能一直没跑。
_STALE_DAYS = 30


def _steps_files() -> list[Path]:
    return sorted(CASES_DIR.rglob("*_steps.yaml"))


def _case_mds() -> list[Path]:
    """用例正文（人写的真相）。排除派生的 `*_steps.md`（历史遗留命名）。"""
    return sorted(p for p in CASES_DIR.rglob("*.md") if not p.stem.endswith("_steps"))


def check_steps_yaml(f: Path, mds: set[str]) -> tuple[list[str], list[str]]:
    """校验一个 `_steps.yaml`。返回 (errors, warnings)。

    errors  = 这缓存**不能照着点**（悬空/自相矛盾/不可定位）
    warnings = 能点但有隐患（通用节点、裸坐标、过期）
    """
    errors: list[str] = []
    warnings: list[str] = []
    import yaml
    try:
        doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        return [f"解析失败：{e}"], []
    if not isinstance(doc, dict):
        return [f"顶层不是映射（是 {type(doc).__name__}）"], []

    stem = str(doc.get("case") or "").strip()
    if not stem:
        errors.append("缺 `case:` 字段，不知道对应哪条用例")
    elif stem not in mds:
        errors.append(f"`case: {stem}` 找不到对应的 `{stem}.md`（用例已删/改名 → 缓存悬空）")

    steps = doc.get("steps") or []
    if not isinstance(steps, list):
        errors.append(f"`steps:` 不是列表（是 {type(steps).__name__}）")
        steps = []

    # `step_count` 自报 vs 实际条数：不一致说明文件被手改过一半
    sc = doc.get("step_count")
    try:
        if sc is not None and int(sc) != len(steps):
            warnings.append(f"`step_count: {sc}` 与实际 {len(steps)} 步不一致")
    except (TypeError, ValueError):
        warnings.append(f"`step_count` 不是数字：{sc!r}")

    nums = []
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            errors.append(f"第 {i} 步不是映射")
            continue
        n = s.get("n")
        nums.append(n)
        if not s.get("via"):
            errors.append(f"第 {i} 步（n={n}）缺 `via`：不知道点什么")
        else:
            via = str(s["via"])
            if _BARE_COORD.search(via):
                warnings.append(f"第 {i} 步 via 含裸坐标 `{via}`（换分辨率就废）")
            for tok in re.findall(r"rid=([\w./:]+)", via):
                if tok in _GENERIC_RIDS:
                    warnings.append(
                        f"第 {i} 步用通用节点 `rid={tok}` 当锚点（几乎每屏都有，点了不知道点的是谁）")
        # 落点：跳页的步骤必须有 expect；in_page 的也必须能自证停在原地
        if not s.get("expect"):
            errors.append(f"第 {i} 步（n={n}）缺 `expect`：点完无从判断落在哪")
        if s.get("in_page") and not s.get("expect"):
            errors.append(f"第 {i} 步标了 in_page 却没有 expect（页内动作也必须有落点）")

    # n 必须从 1 递增 —— 断号说明导出时被截断
    want = list(range(1, len(steps) + 1))
    if nums and nums != want:
        warnings.append(f"步骤编号不连续：{nums}（期望 1..{len(steps)}）")

    lr = str(doc.get("last_run") or "")[:19]
    if lr:
        try:
            age = (datetime.now() - datetime.fromisoformat(lr)).days
            if age > _STALE_DAYS:
                warnings.append(f"上次实测是 {age} 天前（{lr}），路径可能已失效")
        except (ValueError, TypeError):
            warnings.append(f"`last_run` 不是合法时间：{lr}")
    return errors, warnings


def main() -> int:
    ap = argparse.ArgumentParser(
        description="用例资产自检：校验 _steps.yaml 与用例 md 是否对得上")
    ap.add_argument("--check", action="store_true",
                    help="只自检（有问题返回 1，适合 CI/开工前跑）")
    ap.add_argument("--report", action="store_true",
                    help="出一张人看的表：用例 × 有无缓存 × 上次结论")
    ap.add_argument("--case", default=None,
                    help="只处理某条用例（传 md 文件名里的编号或整段 stem）")
    args = ap.parse_args()

    if not CASES_DIR.is_dir():
        print(f"目录不存在：{CASES_DIR}", file=sys.stderr)
        return 2
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("需要 PyYAML：pip install pyyaml", file=sys.stderr)
        return 2

    mds = {p.stem: p for p in _case_mds()}
    stepfiles = _steps_files()
    if args.case:
        sel = str(args.case)
        stepfiles = [f for f in stepfiles
                     if sel in f.stem or f.stem == f"{sel}_steps"]
        mds = {k: v for k, v in mds.items() if sel in k}

    if args.report:
        print(f"{'用例':<48} {'缓存':<6} {'步数':<5} {'上次结论':<8} 上次实测")
        print("-" * 96)
        cached: dict[str, dict] = {}
        for f in stepfiles:
            try:
                d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001
                continue
            if isinstance(d, dict) and d.get("case"):
                cached[str(d["case"])] = d
        for stem in sorted(mds):
            d = cached.get(stem) or {}
            steps = d.get("steps") or []
            print(f"{stem[:47]:<48} {'有' if d else '—':<6} "
                  f"{len(steps) if steps else '—':<5} "
                  f"{str(d.get('verdict') or '—'):<8} {str(d.get('last_run') or '—')[:19]}")
        no_cache = sum(1 for s in mds if s not in cached)
        print(f"\n共 {len(mds)} 条用例，{len(cached)} 条有可复用跳转缓存，"
              f"{no_cache} 条没有（首次跑不会省找控件的时间，属正常）")
        return 0

    # 默认 / --check：完整自检
    rc = 0
    n_err = n_warn = 0
    for f in stepfiles:
        errs, warns = check_steps_yaml(f, set(mds))
        if errs:
            rc = 1
            n_err += len(errs)
            print(f"❌ {f.name}")
            for e in errs:
                print(f"     · {e}")
        if warns:
            n_warn += len(warns)
            if not errs:
                print(f"⚠️  {f.name}")
            for w in warns:
                print(f"     · {w}")
    # 悬空的 yaml 之外，还有"md 有但缓存没有"—— 那不算问题（首次跑必然没有）

    print(f"\n检查 {len(stepfiles)} 个 _steps.yaml："
          f"{n_err} 个错误 / {n_warn} 个警告")
    if not rc:
        print("✅ 全部缓存与用例 md 对得上，可照着点")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
