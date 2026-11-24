import z from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'

/**
 * UI 布局辅助判定插件（独立插件）。
 *
 * ## 为什么独立、不进 android-tools / 测试主干
 *
 * 2026-11-23 用户拍板：重叠/遮挡/截断的**自动检测不做常驻链路**
 * （不在 capture/observe/act 里挂钩子），而是——
 *   **当 AI 需要判断这类 UI 问题时，按需调用本插件的工具**：
 *   本插件负责「数据收集 + 确定性分析」，主 AI 负责「语义判定」。
 *   分工边界：坐标事实（谁压谁/压多少/截断/树盲区）由代码算，异常与否由 AI 判。
 *
 * ## 分析引擎
 *
 * <target>\tools\layout_check.py（纯标准库，独立文件）：
 *   - 叶子节点两两配对；祖先豁免；全屏/近全屏叶子豁免（scrim/blur 误报源）
 *   - **按钮零重叠规则**：双可点控件交面积 >0 即报（业务规则：按钮间不能有一点重叠）
 *   - 目标包自动对准：未传 package 时优先读当前会话登记的被测包（db.sessions.package），
 *     回退 meta.json 的 focus 包——**不需要 AI 显式传包名**
 *   - pkg_found / tree_blind：目标包节点不在 dump 里（浮窗/分屏机型盲区）时显式告警
 *
 * 调用通道（runTool）与 android-tools 插件同款：stdin 传 JSON、venv python 执行。
 */

export const name = 'ui-layout-check'

export const inject = ['tools', 'shell']

export const Config = z.object({
  target: z
    .string()
    .description(
      'Android 测试工具层根目录（含 .venv / tools / storage），如 D:\\dsh\\android-ai-test',
    ),
  timeoutMs: z
    .number()
    .description('单次工具调用超时毫秒，默认 120000')
    .default(120_000),
})

function extractJson(text) {
  const trimmed = (text ?? '').trim()
  if (!trimmed) return null
  try {
    return JSON.parse(trimmed)
  } catch {
    // 不是整体 JSON，往下逐字符找起点
  }
  for (let i = 0; i < trimmed.length; i += 1) {
    if (trimmed[i] !== '{') continue
    try {
      return JSON.parse(trimmed.slice(i))
    } catch {
      // 继续试下一个 `{`
    }
  }
  return null
}

function toCommandLine(argv) {
  return argv
    .map((a) => {
      const s = String(a)
      if (s === '') return '""'
      if (!/[\s"&|<>^()]/.test(s)) return s
      return `"${s.replace(/"/g, '\\"')}"`
    })
    .join(' ')
}

function buildCommandLine(argv) {
  if (process.platform === 'win32') return toCommandLine(argv)
  return argv
    .map((a) => {
      const s = String(a)
      if (s === '') return "''"
      if (!/[\s"'&|<>()$`\\!*?;~#^]/.test(s)) return s
      return `'${s.replace(/'/g, `'\\''`)}'`
    })
    .join(' ')
}

function venvPython(target) {
  if (process.platform === 'win32') return `${target}\\.venv\\Scripts\\python.exe`
  return `${target}/.venv/bin/python`
}

export function apply(ctx, config = {}) {
  const target = config.target
  const timeoutMs = Number(config.timeoutMs ?? 120_000)

  const runTool = async (script, args, exec) => {
    if (!target) {
      return {
        ok: false,
        error: 'ui-layout-check: config.target 未配置（应指向 android-ai-test 目录）',
      }
    }
    const py = venvPython(target)
    const sep = process.platform === 'win32' ? '\\' : '/'
    const argv = [py, `${target}${sep}tools${sep}${script}`, '--stdin-json']
    const stdinPayload = JSON.stringify({ args })

    const request = {
      command: buildCommandLine(argv),
      workdir: target,
      timeoutMs,
      onExpiry: 'kill',
      env: {
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1',
      },
      stdin: stdinPayload,
      signal: exec?.signal,
      // 同 android-tools：测试工具要连 adb、读写 <target>\storage，声明不受限
      sandboxPolicy: {
        mode: 'danger-full-access',
        workspaceRoot: target,
      },
    }

    try {
      const spec = ctx.shell.resolve(request)
      const handle = await ctx.shell.execute(spec)
      const res = await handle.result()
      const out = res?.stdout?.text ?? ''
      const err = res?.stderr?.text ?? ''
      const parsed = extractJson(out)
      if (parsed) return parsed
      if ((res?.exitCode ?? 0) === 0 && out.trim()) {
        return { ok: true, output: out }
      }
      return {
        ok: false,
        error: (err || out || '(no output)').slice(0, 2000),
        exit_code: res?.exitCode ?? null,
      }
    } catch (e) {
      return { ok: false, error: `执行失败: ${e?.message ?? e}` }
    }
  }

  ctx.tools.register(
    defineTool({
      name: 'ui_layout_check',
      description:
        '【布局类判定的强制前置】UI 布局辅助判定（确定性坐标分析，非 AI 判定）。**触发规则（不满足即违规判定）**：只要你的观察结论或 finding 涉及「重叠 / 遮挡 / 截断 / 错位 / 挤压 / 布局 / 自适应」，或用例预期结果含「无重叠、无遮挡、无截断、布局完整、自适应」等字样——下结论前**必须先调用本工具**；判 FAIL 前调它核实坐标事实（两源一致才可判布局异常），判 PASS 前调它确认排除理由（PASS 的 finding 必须写明排除理由，无理由 PASS 无效）。业务规则：**可点控件与文本内容之间不能有任何重叠**（可点或带文本的语义内容 1px 相交即报）——不接受「设计性紧凑布局 / 间距小但功能正常」之类的排除辩护，仅非可点且无文本的装饰性叠加可排除。分析目标自动对准**当前会话登记的被测 App**（不用传包名；查其它窗口才用 package 覆盖）。产出：谁压谁/压多少，按严重度排序（双可点按钮互压最前，按钮间 1px 相交即报——业务规则：按钮不能有任何重叠）。返回含 verdict_hint（strict_count>0 → FAIL 建议）与 verdict_reason，且**结果自动落盘证据目录的 layout_check.json**——findings-batch / finding 提交时会据该文件校验：verdict_hint=FAIL 却判 PASS 且未写排除理由 → **拒绝提交**。⚠️ 返回 tree_blind=true 表示目标包节点不在视图树里（浮窗/分屏机型盲区），本结果不可信，必须改走视觉核对（android_vision）。',
      parameters: {
        evidence: {
          type: 'string',
          description:
            '证据目录路径（内含 dump.xml + meta.json），如 storage/evidence/case176/step03 或 run_history/<会话>/evidence/<case>/<step>。与 xml 二选一，推荐用目录',
        },
        xml: {
          type: 'string',
          description: 'dump.xml 路径（与 evidence 二选一）',
        },
        package: {
          type: 'string',
          description:
            '可选。覆盖过滤包名（默认自动：会话登记的被测包 > 证据 meta.json 的 focus 包）。浮窗场景想查别的窗口时才传',
        },
        iou: { type: 'number', description: '非按钮叶子的 IoU 阈值，默认 0.3（按钮对不受此限：1px 相交即报）' },
        contain: { type: 'number', description: '被吞面积比阈值，默认 0.8' },
        max: { type: 'number', description: '最多返回条数，默认 10（按严重度截断）' },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: true,
          properties: {
            parse_ok: { type: 'boolean' },
            scanned: { type: 'number' },
            count: { type: 'number' },
            overlaps: { type: 'array' },
            package: { type: 'string' },
            pkg_found: { type: 'boolean' },
            tree_blind: { type: 'boolean' },
          },
        },
        render: (_a, v) => [
          { type: 'text', text: typeof v === 'string' ? v : JSON.stringify(v) },
        ],
      },
      async execute(args, exec) {
        const rest = []
        if (args.evidence) rest.push('--evidence', String(args.evidence))
        if (args.xml) rest.push('--xml', String(args.xml))
        if (args.package) rest.push('--package', String(args.package))
        if (args.iou != null) rest.push('--iou', String(args.iou))
        if (args.contain != null) rest.push('--contain', String(args.contain))
        if (args.max != null) rest.push('--max', String(args.max))
        return runTool('layout_check.py', rest, exec)
      },
    }),
  )
}
