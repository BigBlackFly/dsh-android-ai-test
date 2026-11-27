"""state：设备状态原语（权限/数据/应用生命周期），不动屏幕内容。

权限测试的第一选择：测"已授权/已拒绝"路径时用 grant/revoke 预置状态，
弹窗根本不出现；只有测弹窗交互本身（用户点允许/拒绝那一刻的行为）才需要
act --watch 的弹窗内点击。全部操作自动记入当前会话事件流。

用法：
  python state.py grant  --package com.app --perm android.permission.CAMERA
  python state.py revoke --package com.app --perm android.permission.CAMERA
  python state.py clear  --package com.app        # ⚠️ 清空应用全部数据
  python state.py start  --package com.app [--activity .MainActivity]
  python state.py stop   --package com.app
"""
from __future__ import annotations

import argparse
import sys

from common import (add_common_args_all_subcommands, apply_stdin_args, connect,
                    emit, fail, guard_terminated, log_auto)
from perm import PERMS, resolve_perm
from runtime import execution


# ★ UTF-8 兜底：Windows 控制台默认 GBK，print 一个 ⚠️/✅ 就崩。
#   必须在任何输出之前调用（含 argparse 的报错输出）。
try:
    from common import ensure_utf8_stdio
    ensure_utf8_stdio()
except Exception:  # noqa: BLE001  common 不可用时不要因此崩掉
    pass

# PERMS / resolve_perm 现在由 perm.py 提供（perm_tool.py 共用同一份）。
# 旧版这里只有 `PERMS.get(v, v)`：短名大小写不对就直接当全名用，
# 传 `CAMERA` 会拼出 `pm grant <pkg> CAMERA` 这种无效命令。


def _perm_granted(d, pkg: str, perm: str) -> bool:
    """回读 dumpsys 判断权限是否 granted（pm grant/revoke 成功不一定有输出，
    用空输出判成功会谎报——设备抖动时 output 可能为空）。

    dumpsys 全文取回本地解析：设备端 grep 在部分 ROM 上不存在或 -w 行为不一。
    """
    out = (d.shell(f"dumpsys package {pkg}").output or "")
    for line in out.splitlines():
        line = line.strip()
        if line.startswith(perm) and "granted=" in line:
            return "granted=true" in line
    return False


def _emit_result(ok: bool, payload: dict) -> None:
    """输出结果；**失败必须以非零退出码结束**。

    `emit()` 只往 stdout 写 `ok:false`，退出码仍是 0 —— 调用方（插件层、
    批量脚本）看到 0 就当成功，于是「撤销权限没生效」「清数据没成功」
    这类前置失败会被静默吞掉，后面的判定全建在没铺好的状态上。
    这里把失败接成 rc=1，业务细节原样留在 JSON 里。
    """
    emit(ok, payload)
    if not ok:
        sys.exit(1)


def main() -> None:
    p = argparse.ArgumentParser(description="state：设备状态原语")
    p.add_argument("--serial", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("grant", "revoke"):
        s = sub.add_parser(name)
        s.add_argument("--package", required=True)
        s.add_argument("--perm", required=True, help=f"短名或全名，短名: {', '.join(PERMS)}")

    s = sub.add_parser("clear")
    s.add_argument("--package", required=True)

    s = sub.add_parser("start")
    s.add_argument("--package", required=True)
    s.add_argument("--activity", default=None, help="可选，相对 activity 名")

    s = sub.add_parser("stop")
    s.add_argument("--package", required=True)

    # --serial 写在子命令前后都能解析（与其他工具一致）
    add_common_args_all_subcommands(p)
    apply_stdin_args()
    args = p.parse_args()
    guard_terminated(f"state {args.cmd}")
    try:
        d = connect(args.serial)
    except Exception as e:  # noqa: BLE001
        fail(f"设备连接失败: {e}")

    execution("may_have_executed")
    try:
        if args.cmd in ("grant", "revoke"):
            perm = resolve_perm(args.perm)
            out = (d.shell(f"pm {args.cmd} {args.package} {perm}").output or "").strip()
            granted = _perm_granted(d, args.package, perm)
            ok = granted if args.cmd == "grant" else not granted
            detail = f"pm {args.cmd} {args.package} {perm} → {'granted' if granted else 'not granted'}"
            execution("executed")
            log_auto("state", f"state {args.cmd}", detail,
                     {"perm": perm, "ok": ok, "verified": True})
            if ok:
                hint = "预置状态成功，触发该权限的操作不会再弹窗"
            elif "Exception" in out or "Error" in out:
                # 结构化返回而非 sys.exit：AI 拿到 JSON 自行决策（弹窗路径/换权限）
                hint = f"⚠️ pm {args.cmd} 被拒绝: {out[:200]}；改走弹窗路径（act --watch）"
            else:
                hint = ("⚠️ 回读校验未达预期（granted=" + str(granted) + "）——预置未生效，"
                        "该权限可能不是 runtime 权限或设备策略限制；"
                        "改走弹窗路径（act --watch），不要对未预置的状态做判定")
            _emit_result(ok, {"cmd": args.cmd, "perm": perm, "output": out,
                              "granted": granted, "verified": True, "hint": hint})
            return
        elif args.cmd == "clear":
            out = (d.shell(f"pm clear {args.package}").output or "").strip()
            ok = "Success" in out
            execution("executed")
            log_auto("state", "state clear", f"pm clear {args.package}", {"ok": ok})
            # hint 必须跟着 ok 走：原来不论成败都说「已回到首次使用态」，
            # 失败的 clear 会被当成前置已铺好，后面的判定全建在沙子上。
            hint = ("应用已回到首次使用态（含外部 Android/data）" if ok
                    else f"⚠️ pm clear 未成功（输出：{out[:200] or '(空)'}）——"
                         "应用数据可能没被清掉，不要假定已回到首次使用态")
            _emit_result(ok, {"output": out, "hint": hint})
        elif args.cmd == "start":
            comp = f"{args.package}/{args.activity}" if args.activity else args.package
            d.app_start(args.package, args.activity) if args.activity else d.app_start(args.package)
            execution("executed")
            log_auto("state", "state start", f"启动 {comp}", {"ok": True})
            emit(True, {"started": comp, "hint": "冷启动后旋转锁定可能被解锁，注意 rotation"})
        elif args.cmd == "stop":
            d.app_stop(args.package)
            execution("executed")
            log_auto("state", "state stop", f"force-stop {args.package}", {"ok": True})
            emit(True, {"stopped": args.package})
    except Exception as e:  # noqa: BLE001
        fail(f"state {args.cmd} 失败: {e}")


if __name__ == "__main__":
    main()
