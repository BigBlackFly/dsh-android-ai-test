"""vision：可选的视觉模型通道（OpenAI 兼容多模态 API）。

配置在测试台「视觉模型」页维护（storage/vision.json）。
v2 默认由对话 AI 自身判图；本工具用于批量回放等无对话场景的工具级视觉断言。

用法：
  python vision.py ask --image storage/evidence/c1/step02/shot.png \
      --prompt "按钮是置灰的吗？回答 置灰/可用 + 一句依据"
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

from common import apply_stdin_args, emit, fail, log_auto, rel
from runtime import STORAGE_DIR

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK
except AttributeError:
    pass


def _layout_signal(img: Path) -> tuple[str, int]:
    """与截图配对的 dump.xml 跑视图树堆叠检测，返回 (注入文本, 命中对数)。

    背景（2026-11-23 实测）：AI 调 android_vision 时只贴自己的问题，
    capture() 检出的 layout_overlap 信号根本不在场 —— 视觉模型不知道
    该重点看哪。本函数把信号**自动**注入 prompt，调用方无感。
    未命中/无 dump/解析失败一律返回 ("", 0)（无痕，不改变调用行为）。
    """
    try:
        d = img.parent / "dump.xml"
        if not d.exists():
            return "", 0
        fg = size = None
        mp = img.parent / "meta.json"
        if mp.exists():
            m = json.loads(mp.read_text(encoding="utf-8"))
            fg, size = m.get("package"), m.get("size")
        from layout_check import scan
        r = scan(d.read_text(encoding="utf-8"),
                 fg_package=fg, screen_size=size)
        if not r.get("parse_ok") or not r.get("overlaps"):
            return "", 0
        def _nm(x: dict) -> str:
            return x.get("rid") or x.get("desc") or x.get("text") or x["class"]
        lines = ["【视图树检测信号 —— 确定性坐标事实，请重点核对】",
                 f"本屏视图树检出 {r['count']} 对控件堆叠，最严重几对："]
        for o in r["overlaps"][:3]:
            strict = bool((o["a"].get("clickable") or o["a"].get("text"))
                          and (o["b"].get("clickable") or o["b"].get("text")))
            tag = ("【语义内容重叠（可点/文本）——按业务规则直接定性 BUG，不豁免】"
                   if strict else "")
            lines.append(
                f"- {o['kind']} iou={o['iou']}: "
                f"{_nm(o['a'])}{o['a']['bounds']} × {_nm(o['b'])}{o['b']['bounds']}"
                + (f" {tag}" if strict else ""))
        lines.append("请在图上逐一核对："
                     "可点控件与文本内容之间的重叠——哪怕只有一点——"
                     "按业务规则直接定性为 BUG，不存在「设计性紧凑布局」的排除理由；"
                     "仅非可点且无文本的装饰性叠加（角标压图标/悬浮球压内容/遮罩）"
                     "可排除，但要说明。")
        return "\n".join(lines), r["count"]
    except Exception:  # noqa: BLE001
        return "", 0


def main() -> None:
    p = argparse.ArgumentParser(description="vision：视觉模型问图（OpenAI 兼容）")
    p.add_argument("--image", required=True, help="图片路径（png/jpg）")
    p.add_argument("--prompt", required=True, help="问题/断言指令")
    # 默认 2000：这类模型的 reasoning 开销很大，max_tokens 给小了会出现
    # completion_tokens 全被 reasoning 吃光、正文是空字符串的情况 ——
    # 看起来"调用成功"，实际什么都没答，比报错更坑。
    p.add_argument("--max-tokens", type=int, default=2000)
    apply_stdin_args()   # 支持 --stdin-json（插件统一走 stdin 传参）
    args = p.parse_args()

    conf_fp = STORAGE_DIR / "vision.json"
    try:
        conf = json.loads(conf_fp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        fail("未配置视觉模型：先在测试台「视觉模型」页填写并保存")
    base = (conf.get("base_url") or "").rstrip("/")
    model = conf.get("model") or ""
    key = conf.get("api_key") or ""
    if not (base and model and key):
        fail("配置不完整（base_url / model / api_key），先在测试台「视觉模型」页补全")

    img = Path(args.image)
    if not img.exists():
        fail(f"图片不存在: {img}")
    mime = "image/jpeg" if img.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    b64 = base64.b64encode(img.read_bytes()).decode()

    # 视图树信号自动融合：同目录 dump.xml 检出堆叠 → 注入 prompt 让视觉模型重点核对
    sig, sig_count = _layout_signal(img)
    prompt_text = args.prompt + ("\n\n" + sig if sig else "")

    body = json.dumps({
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        "max_tokens": args.max_tokens,
    }).encode()

    req = urllib.request.Request(
        base + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=120))
    except Exception as e:  # noqa: BLE001
        fail(f"视觉模型调用失败: {e}")

    try:
        text = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        fail(f"响应格式异常: {json.dumps(resp, ensure_ascii=False)[:300]}")

    # ★ 空回答必须当成失败，不能报 ok:true。
    #
    #   实测（2026-10-28）：max_tokens 太小时 reasoning 吃光预算，
    #   返回 text="" 且 ok:true。AI 看到 ok 就以为"模型说没有"，
    #   把"模型没答"误当成"画面里没有"—— 这是会直接导致误判的。
    usage = resp.get("usage") or {}
    if not (text or "").strip():
        reasoning = (usage.get("completion_tokens_details") or {}).get(
            "reasoning_tokens")
        fail(
            "视觉模型返回了空回答"
            + (f"（{reasoning} token 全花在 reasoning 上）" if reasoning else "")
            + "。加大 --max-tokens 重试（如 3000），"
              "或把问题问得更短更具体。**不要把空回答当成「画面里没有」**")

    # ★ 必须落一条事件，否则这条视觉断言**绑不到证据**。
    #
    #   实测（2026-11-13）：findings-batch 要求 PASS/FAIL 至少带一个 event_id，
    #   而 vision 原来一个事件都不记 → 拿 vision 做完断言只能引用别的一步
    #   （图上到底问了什么、答了什么，事后查不到），或者干脆判 BLOCKED。
    #
    #   证据就用传进来的那张图（不重拍）：视觉断言的对象本来就是那张图。
    #
    # ⚠️ log_auto 在**没有活动会话**时返回 None（静默不记），所以下面要判空：
    #    无条件把 event_id 塞进 payload 的话，调用方看到的是 `event_id: null`
    #    —— 分不清"记了但没 id"还是"根本没记"，绑证据时就会拿个空值上去。
    _eid = log_auto(
        "vision", "vision ask",
        f"问图：{args.prompt[:40]}" + (f"（含视图树信号 {sig_count} 对）" if sig_count else ""),
        {"image": str(img), "prompt": args.prompt,
         "layout_signal": sig_count,
         "answer": text, "usage": usage,
         "model": model, "max_tokens": args.max_tokens},
        rel(img), None,
        datetime.now().isoformat(timespec="milliseconds"))

    payload = {"text": text, "usage": usage,
               "hint": "视觉模型输出是辅助判据，PASS/FAIL 结论仍需结合 dump/OCR 证据"}
    if sig_count:
        payload["layout_signal_injected"] = sig_count
        payload["layout_signal_note"] = (
            f"已自动把视图树检出的 {sig_count} 对堆叠注入本次问图 prompt"
            "（最严重 3 对），视觉模型被要求重点核对。")
    if _eid:
        payload["event_id"] = _eid
        payload["evidence"] = rel(img)
    else:
        # 没会话就没账 —— 必须说出来，不能让调用方以为已经绑好了证据
        payload["recorded"] = False
        payload["record_hint"] = ("本次调用**没有活动会话**，事件未落库；"
                                  "结论要能复核，请先 session.py start 再问图")
    emit(True, payload)


if __name__ == "__main__":
    main()
