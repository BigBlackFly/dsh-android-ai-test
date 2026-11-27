"""测试会话 SQLite 持久化（v3：单库 + run_id 隔离）。

三张表：
  sessions  一次测试会话（用户口述用例 + 结论）
  events    统一事件流：observe/act/read 每次工具调用自动落一条（append-only）
  findings  断言/结论（从事件流独立出来，方便聚合统计）

零依赖（标准库 sqlite3）。

## 库位置：`storage/test.db` —— **所有运行共用一个库**

### 运行之间怎么隔离

  靠 `sessions.run_id` 字段隔离，不靠文件隔离：

    sessions 表  run_id  <- 这次运行的 uuid（带 `-` 的标准 UUID v4）
    events 表    通过 session_id 关联到 sessions，自然带上 run_id
    findings 表  同上

  跨运行查询就是一句 SQL：

    SELECT * FROM sessions WHERE run_id = '2b818755-fc18-454c-96c1-ea1ca0444902'
    SELECT s.run_id, s.status FROM sessions s WHERE s.title LIKE '%176%'

  单库之后 WebUI 刷新、用例详情、跨运行对比都只连一个库，不用逐库打开再合并。

### 运行目录

  `storage/run_history/<uuid>/` 只放证据和计时，不放 db：

    run_history/<uuid>/
        evidence/           <- 每次运行的截图
        timing/ calls/ recording_errors/

  「一次运行 = 一个目录」的直觉还在，删一次运行 = 删目录 + 删库里对应行。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from runtime import CALL_ID, STORAGE_DIR, current_record_dir, recording_issues

ROOT = Path(__file__).resolve().parent.parent

# ── run 目录是否还在（N-11）────────────────────────────────────────
# `sessions.run_id` 有两代格式：新的是 uuid（`new_record_dir()` 生成，目录一定在），
# 旧的是 12 位 hex（多库时代每个会话一个 `run_history/<hex12>/db.sqlite3`）。
# 单库化后旧值指向的目录**全都不存在**，而所有拼 `STORAGE_DIR/"run_history"/run_id`
# 的代码都还照着拼 —— 结果静默空转（回放空白、录屏收尾白等 timeout）。
#
# 所以「拼路径」之前统一先问这里：目录在不在。不在就别拼，直接标 stale。
def run_dir_path(run_id: str | None) -> Path | None:
    """`storage/run_history/<run_id>` 的路径；run_id 为空或不合法时返回 None。

    只做形状校验，**不判断存在性**（拼路径本身无害）。
    """
    if not run_id:
        return None
    name = str(run_id)
    if not re.fullmatch(r"[\w-]{1,80}", name):
        return None
    return STORAGE_DIR / "run_history" / name


def run_dir_missing(run_id: str | None) -> bool:
    """该 run_id 指向的运行目录是否**已不存在**。

    空 run_id / 形状不合法 → 也算 missing：调用方据此不要再拼路径。
    """
    path = run_dir_path(run_id)
    return path is None or not path.is_dir()

# 「预期1 …」「预期3：…」「步骤2 …」这类前缀，分组前要剥掉。与 common.py 里
# 同名正则同口径 —— 不 import common 是为了避免循环依赖（common 在函数内 import db）。
# 注意这里**只用于剥离**（把同一句文本的多种写法归一），不用于认编号；
# 认编号只在 common.match_expect_id 里做，且那里**不认「步骤」**（步骤编号 ≠ 预期编号）。
# ★ 2026-11-27（人裁决：预期才是判定基准）：PASS 判定的"时效自相矛盾"检测。
#
#   事故：另一台设备的 session 489 判 176 预期3（"进入空课程表后第一个小节显示加号"）
#   PASS，但它挂的证据是**点过格子之后**拍的帧 —— 实际"进入时没有、点一下才出现"，
#   与预期不符。本机同款：#619（session 152）、#1169（session 1000066）。
#
#   判据刻意做得**极窄**，只抓 actual 里自己写着"同一对象先无后有"的：
#       无 <东西> …（点击/长按/操作）后 … 出现/才出现 <同一个东西>
#   在真库 1548 条 PASS 上命中 2 条，**两条都是真问题，零误报**。
#   宽一点的判据（只按 expect 含"进入后"、或证据帧之前有 act）会命中 78~99 条，
#   绝大多数是正常的"默认值 + 再操作"描述 —— 那种不能用来报警。
_SUSPECT_THING = (r"(?:rid=)?[a-z][a-z0-9_]{4,}|加号|加号浮标|开关|按钮|入口|"
                   r"弹框|提示语|文案|图标")
_SUSPECT_NEG_RE = re.compile(
    r"(?:无|没有|不存在|未出现|不含|看不到)\s*(?:任何)?\s*(?P<t>" + _SUSPECT_THING + r")")
_SUSPECT_POS_RE = re.compile(
    r"(?:点击|点|长按|操作|滑动)[^。；\n]{0,25}?(?:后|之后)[^。；\n]{0,25}?"
    r"(?:出现|新出现|才出现|显示|浮出|变亮|变为)")

_EXPECT_PREFIX_RE = re.compile(
    r"^\s*(?:(?:预期|期望|断言|检查点)\s*\d{1,3}|(?:步骤|第)\s*\d{1,3})\s*(?:条)?\s*[:：.、,，]?\s*")


def _case_stems(package: str | None) -> list[str]:
    """某包下所有用例 md 的文件名（不含扩展名），按长度降序。

    用文件名当分组键的锚点：`assets/cases/<pkg>/` 里的文件名就是用例的唯一身份，
    比 `sessions.title` 稳定得多。按长度降序是为了让「重要的」先匹配，
    避免 `日历_11` 这种短串抢在 `com.zui.calendar_设置事件显示_11` 前面。
    """
    if not package:
        return []
    d = ROOT / "assets" / "cases" / package
    try:
        return sorted((p.stem for p in d.glob("*.md")), key=len, reverse=True)
    except OSError:
        return []


def _loose_case(s: str) -> str:
    """抹掉编号、标点、空白、全角括号里的备注，只留中文与字母用于模糊比对。"""
    s = re.sub(r"[（(][^)）]*[)）]", "", s or "")
    s = re.sub(r"[_\-]?\d{2,4}(?!\d)", "", s)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s.lower())


def _loose_parts(s: str | None) -> frozenset[str]:
    """把标题拆成可比较的片段，**编号单独成片**。

    `sessions.title` 的写法极不统一（`com.zui.calendar_176` 只有一个编号，
    `日历_176 新建课程表检查` 却是「模块 + 编号 + 描述」），整串比对命中率很低。
    拆成片段后，要求「标题片段」与「文件名片段」**完全相等**——
    不做包含、不做后缀，因为 `重要日倒数07` 做后缀匹配会撞上
    `…_重要日倒数_03`（03/04/05/07/08 同属「重要日倒数」系列，编号才是区分它们的唯一东西）。
    """
    parts: set[str] = set()
    for seg in re.split(r"[\s/·、,，:：]+", (s or "").strip()):
        seg = re.sub(r"[（(][^)）]*[)）]", "", seg)
        for tok in re.split(r"[_\-]+", seg):
            tok = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", tok.lower())
            if tok:
                parts.add(tok)
    return frozenset(parts)


def _case_key(title: str | None, package: str | None = None) -> str:
    """把同一用例的不同 title 写法归一到同一个键。

    同一份用例在 `sessions.title` 里有多种写法：
      `com.zui.calendar_新建课程表_176` / `com.zui.calendar_176`
      / `日历_176 新建课程表检查` / `新建课程表_176`
    直接拿 title 当分组键会把同一用例拆成多组，跨轮抖动永远凑不齐。

    **不能只按编号归并**：用例编号并不唯一（同一包内 `_03` 有两条、
    `_04`/`_05`/`_08` 都各有两条），而「重要日倒数」系列有 03/04/05/06/07/08/10
    七八条，编号是它们之间唯一的区别。只按编号归并会把不同用例并成一组，
    结论一 PASS 一 FAIL 就被报成「抖动」—— 纯假警。

    归一顺序（越靠前越可靠）：
      1. title 里原样含某个用例文件名 → 直接用那个文件名
      2. 去包名前缀后模糊相等（`新建课程表_176` → `_新建课程表_176`）
      3. 片段相等：标题片段集合 == 文件名片段集合
      4. 片段包含：文件名片段集合是标题片段集合的子集
      5. 都不中 → 退回去掉包名前缀的 title 原文

    第 2~4 步命中多个候选时，**只有长度最长的那一个唯一**才认领；并列退回 ⑤ ——
    宁可把同一用例拆成两组（漏报抖动），也不把两个用例错并成一组（制造假警）。
    """
    t = (title or "").strip()
    pk = (package or "").strip()
    stems = _case_stems(pk)
    if t and stems:
        for st in stems:
            if st in t:
                return st
    core = t[len(pk):].lstrip("_- ").strip() if pk and t.startswith(pk) else t
    loose = _loose_case(core)
    short = [st.replace(f"{pk}_", "") for st in stems] if pk else list(stems)

    def _pick(hits: list[str]) -> str | None:
        if not hits:
            return None
        best = max(hits, key=len)
        # 「唯一」按**内容集**算，不按条数：同一候选在拼接列表里可能出现两次
        # （包名前缀剥离与否的两种写法），那仍是同一个用例，不算歧义。
        return best if len(set(hits)) == 1 else None

    if loose and stems:
        cand = _pick([st for st, s in zip(stems, short) if _loose_case(s) == loose])
        if cand:
            return cand
    title_parts = _loose_parts(core)
    if title_parts and stems:
        stem_parts = {st: _loose_parts(s) for st, s in zip(stems, short)}
        cand = _pick([st for st, p in stem_parts.items() if p and p == title_parts])
        if cand:
            return cand
        cand = _pick([st for st, p in stem_parts.items() if p and p < title_parts])
        if cand:
            return cand
    return f"{pk}#{core}"

# 全局唯一数据库
DB_FILE = STORAGE_DIR / "test.db"


def _resolve_db_path() -> Path:
    """库放哪？固定是 `storage/test.db`，与运行上下文无关。

    库永远只有一个，任何进程（工具、WebUI、导出脚本、统计）都能直接 get_db()。
    run_id 只用于**写入时归属**（session start 时确定），不参与定位库。
    """
    return DB_FILE


def _current_file() -> Path:
    """'当前会话号' 记在哪。

    单库后不再跟着 record 目录走 —— 固定放 storage/ 下。
    多个运行同时跑是不允许的（设备只有一台），所以一个全局指针够用。
    """
    return STORAGE_DIR / "current_session"


# ⚠️ 不能在模块级解析路径 —— `import db` 那一刻还没有 record 上下文，
# 会直接抛 RuntimeError，连报错信息都看不到。用到时才解析
# （get_db() / SessionDB 内部调 _resolve_db_path）。
# 这个名字只为兼容旧引用，值是 None 表示"还没解析"。
DB_PATH: Path | None = None
CURRENT_FILE = _current_file()   # 仅为兼容旧引用；内部一律用 _current_file()

# 结论只有四种：PASS / FAIL / BLOCKED / ERROR。
#
# 不用含糊的中间态 —— 实际用起来会变成"不敢判 FAIL 就写 XXX"。
# 用例预期是有明确对照的：符合就 PASS，不符合就 FAIL，
# 环境/前置问题就 BLOCKED，工具异常就 ERROR。
STATUSES = ("PASS", "FAIL", "BLOCKED", "ERROR")
# 会话中间态：不是结论，不计入通过率统计；必须以 finish 收尾。
INTERIM = ("running",)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  title TEXT,
  user_input TEXT,
  package TEXT,
  related TEXT,
  device TEXT,
  case_id INTEGER,
  status TEXT DEFAULT 'running',
  kind TEXT DEFAULT 'test',
  terminated INTEGER DEFAULT 0,
  terminate_reason TEXT,
  perm_intent_action TEXT,
  perm_intent_perm TEXT,
  perm_intent_at TEXT,
  perm_denied_keys TEXT,
  knowledge_shown INTEGER DEFAULT 0,
  knowledge_hint_keys TEXT,
  summary TEXT,
  report_path TEXT,
  started_at TEXT,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  seq INTEGER,
  kind TEXT,
  tool TEXT,
  detail TEXT,
  data_json TEXT,
  evidence TEXT,
  duration_ms INTEGER,
  started_at TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS findings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  event_id INTEGER,
  status TEXT,
  expect TEXT,
  expect_id INTEGER,
  expect_label TEXT,
  actual TEXT,
  note TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS finding_events(
  finding_id INTEGER NOT NULL REFERENCES findings(id),
  event_id INTEGER NOT NULL REFERENCES events(id),
  PRIMARY KEY(finding_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_findings_session ON findings(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_kind ON sessions(kind);
"""

# 会话类型：
#   test = 正式测试（进「测试记录」，计入仪表盘成功率）
#   diag = 验证/调试（工具自检、探针；默认不显示，不计入）
#   mock = 占位/构造数据（冒烟样例、手工造的演示数据；**永不入库**）
SESSION_KINDS = ("test", "diag", "mock")
# 只有这些类型算正式记录（进列表与统计）
FORMAL_KINDS = ("test",)


class SessionDB:
    """Thread-local connections; trust-critical writes raise on failure."""

    def __init__(self, path: Path | None = None):
        # ★ 必须**调用时**才解析路径，不能用 `path: Path = DB_PATH`。
        #
        #   默认参数在**函数定义时**求值 —— 也就是模块 import 那一刻。
        #   而 `session.py start` 的流程是先 import、再设环境变量、最后才
        #   get_db()。如果路径在 import 时就定死，库会建到当时那个位置去，
        #   而环境变量指定的位置反而是空的。
        #
        #   所以：None 默认值 + 调用时解析，环境变量才能生效。
        self.path = str(path if path is not None else _resolve_db_path())
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        if getattr(self._local, "conn", None) is None:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            # 幂等迁移：早期建的表补列
            for stmt in ("ALTER TABLE events ADD COLUMN duration_ms INTEGER",
                         "ALTER TABLE events ADD COLUMN started_at TEXT",
                         "ALTER TABLE sessions ADD COLUMN related TEXT",
                         "ALTER TABLE sessions ADD COLUMN case_id INTEGER",
                         "ALTER TABLE sessions ADD COLUMN kind TEXT DEFAULT 'test'",
                         "ALTER TABLE sessions ADD COLUMN terminated INTEGER DEFAULT 0",
                         "ALTER TABLE sessions ADD COLUMN terminate_reason TEXT",
                         "ALTER TABLE sessions ADD COLUMN perm_intent_action TEXT",
                         "ALTER TABLE sessions ADD COLUMN perm_intent_perm TEXT",
                         "ALTER TABLE sessions ADD COLUMN perm_intent_at TEXT",
                         # ★ 2026-11-27：媒体类权限「先拒后允」两阶段，记这条会话已经拒过哪几类（camera/photo/video）
                         "ALTER TABLE sessions ADD COLUMN perm_denied_keys TEXT",
                         "ALTER TABLE sessions ADD COLUMN knowledge_shown INTEGER DEFAULT 0",
                         # ★ N-05：知识卡提示去重从"会话级一次性"改成"按卡键"
                         "ALTER TABLE sessions ADD COLUMN knowledge_hint_keys TEXT",
                         "ALTER TABLE events ADD COLUMN call_id TEXT",
                         # 结构化预期编号：#4 起 findings 能对齐"第几条预期"，
                         # 不再靠凑中文。expect_id 是按位置排的 1..N（机器比对用），
                         # expect_label 存用例原文写的编号（3-4 / 1~2 原样），仅供人读。
                         "ALTER TABLE findings ADD COLUMN expect_id INTEGER",
                         "ALTER TABLE findings ADD COLUMN expect_label TEXT",
                         # 单库化的关键列：这次运行属于哪个 uuid
                         "ALTER TABLE sessions ADD COLUMN run_id TEXT"):
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass
            conn.commit()
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_call ON events(call_id)")
            self._ensure_seq_unique(conn)
            # run_id 是单库的主查询维度（前端按 run 筛、按 run 删）
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_run ON sessions(run_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at)")
            conn.commit()
            self._local.conn = conn
        return self._local.conn

    @staticmethod
    def _now() -> str:
        # 毫秒精度：相邻事件差 = AI 决策耗时（时间轴展示依赖它）
        return datetime.now().isoformat(timespec="milliseconds")

    # ── 写 ──────────────────────────────────────────────────────
    def start_session(self, title: str, user_input: str, device: str | None = None,
                      case_id: int | None = None, kind: str = "test",
                      run_id: str | None = None) -> int:
        """开一个会话。run_id 不传时自动取当前运行目录名。

        run_id 是单库的主隔离维度：同一个 uuid 下的会话（正常只有一个，
        但重复 start 会产生多个）都能靠它聚起来。
        """
        if kind not in SESSION_KINDS:
            kind = "test"
        if run_id is None:
            rec = current_record_dir()
            run_id = rec.name if rec is not None else None
        cur = self._conn().execute(
            "INSERT INTO sessions (run_id, title, user_input, device, case_id,"
            " kind, started_at) VALUES (?,?,?,?,?,?,?)",
            (run_id, title, user_input, device, case_id, kind, self._now()))
        self._conn().commit()
        return cur.lastrowid

    def log_event(self, session_id: int, kind: str, tool: str, detail: str = "",
                  data: dict | None = None, evidence: str | None = None,
                  duration_ms: int | None = None,
                  started_at: str | None = None) -> int | None:
        """记一条事件。`seq` 在会话内必须严格递增且唯一。

        ⚠️ **seq 是"先读后写"，必须在一个写事务里做完**（真事故 2026-11-25）：
          旧写法 `SELECT MAX(seq)+1` 与 `INSERT` 之间没有互斥，
          两个进程同时进 —— Python 的 sqlite3 是 legacy 事务模式，
          `with conn:` 只在**第一个写语句**前才开事务，SELECT 属于读，
          于是两边都读到同一个 MAX，**都插入成功**。
          实测两进程各写 60 条：120 条写成功，只有 119 个唯一 seq
          （seq=42 重复），时间轴据此排序会错位，`created_at` 还会倒退。
          真库痕迹：session 53 的 seq=229 有两行，相隔 4ms。

          修法两层：
            ① `BEGIN IMMEDIATE` 在**读之前**就拿写锁，别的进程排队等
               （timeout=10 秒，正常写一毫秒内就让出）；
            ② `UNIQUE(session_id, seq)` 兜底 —— 就算哪天锁又被绕过，
               数据库直接拒绝，而不是静默写进去两行。见 `_conn()` 里的迁移。
        """
        conn = self._conn()
        # isolation_level=None：**关掉 sqlite3 的隐式事务**，让我们自己发
        # BEGIN IMMEDIATE。否则 driver 会在第一条非 DML 语句前自作主张地
        # 开一个 DEFERRED 事务，等读到 INSERT 才升级写锁 —— 竞态窗口还在。
        with self._immediate(conn):
            if not conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                raise ValueError(f"Session {session_id} does not exist")
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE session_id=?",
                (session_id,)).fetchone()[0]
            cur = conn.execute(
                "INSERT INTO events (session_id, seq, kind, tool, detail, data_json,"
                " evidence, duration_ms, started_at, created_at, call_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, seq, kind, tool, detail,
                 json.dumps(data, ensure_ascii=False) if data else None,
                 evidence, duration_ms, started_at, self._now(), CALL_ID))
            return cur.lastrowid

    @staticmethod
    def _ensure_seq_unique(conn: sqlite3.Connection) -> None:
        """尽力给 events 加 `UNIQUE(session_id, seq)`，作为 seq 去重的兜底。

        ⚠️ 为什么要「尽力」而不是直接建：真库已有历史重复
           （session 53 的 seq=229 有两行）。直接建唯一索引会抛
           `UNIQUE constraint failed`，于是**每次开库都失败** —— 比重复 seq 更糟。
           所以：能建就建；建不上就只提示一次，正确性仍由
           `log_event` 里的 `BEGIN IMMEDIATE` 保证（那是主防线）。
           历史行**原样保留**：它们是既成事实，删任何一条都是篡改记录。
        """
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_events_session_seq "
                "ON events(session_id, seq)")
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            if getattr(SessionDB, "_seq_notice_done", False):
                return
            SessionDB._seq_notice_done = True
            print("[db] 注意：events 表存在历史重复 (session_id, seq)，"
                  "唯一索引未建立；新写入由 BEGIN IMMEDIATE 保证不重复。"
                  "历史行原样保留（不作篡改）。", file=sys.stderr)

    @staticmethod
    @contextmanager
    def _immediate(conn: sqlite3.Connection):
        """`BEGIN IMMEDIATE ... COMMIT`，异常回滚。

        不用 `with conn:`：那个只在第一条写语句前开 DEFERRED 事务，
        对「先 SELECT 再 INSERT」的读改写模式保护不住（见 log_event 说明）。
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            conn.rollback()
            raise
        conn.commit()

    def add_finding(self, session_id: int, status: str, expect: str = "",
                    actual: str = "", note: str = "", event_id: int | None = None,
                    event_ids: list[int] | None = None,
                    expect_id: int | None = None,
                    expect_label: str | None = None) -> int:
        """记一条断言。

        `expect_id` 是**结构化预期编号**（1..N，按用例「预期结果」段的位置序），
        `expect_label` 是用例原文写的编号（`3-4`/`1~2` 原样，仅供人读）。

        ⚠️ 为什么加这两个字段：`expect` 是自由中文，同一句预期实测有 4 种写法，
        机器没法跨轮比对 —— 同一用例跑 7 轮得 6 个 FAIL 1 个 ERROR，到底是产品
        不稳还是判据漂了，看库里两段不同的中文谁也说不清。有了 expect_id，
        「预期3 这轮 PASS 下轮 FAIL」可以一句 SQL 查出来（见 `flaky_expects`）。
        文本仍必填（人要看），编号是**附加**的对齐键，不替代文本。
        """
        if status not in STATUSES:
            raise ValueError(f"Invalid finding status: {status}")
        ids = list(dict.fromkeys(([event_id] if event_id is not None else []) + (event_ids or [])))
        if not actual.strip():
            raise ValueError("Finding actual observation/reason is required")
        if status in ("PASS", "FAIL") and (not ids or not expect.strip()):
            raise ValueError("Verified findings require an expectation and explicit evidence events")
        if expect_id is not None and (not isinstance(expect_id, int) or expect_id < 1):
            raise ValueError(f"expect_id must be a positive integer, got {expect_id!r}")
        conn = self._conn()
        with conn:
            session = conn.execute("SELECT status FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session or session["status"] not in INTERIM:
                raise ValueError("Findings require an existing unfinished session")
            for eid in ids:
                self._validate_evidence(conn, session_id, eid)
            # ★ N-13：一张截图（一个 event）挂多条 finding 时**必须能看出来**。
            #   真实库里最极端的一个 event 挂了 9 条 finding（124 组这样的组合）。
            #   这本身合法 —— 同一次 actions 可以同时验证多条预期；但事后复盘时
            #   「9 条结论都只凭这一张图」必须一眼可见，不能埋在关系表里。
            #   所以：同一 event 已有结论时，把这件事追加进 note（不改人写的原文）。
            shared = sorted({
                int(r[0]) for r in conn.execute(
                    "SELECT f.id FROM finding_events fe"
                    " JOIN findings f ON f.id = fe.finding_id"
                    " WHERE fe.event_id IN (%s) ORDER BY f.id" %
                    ",".join("?" for _ in ids), ids).fetchall()
            }) if ids else []
            if shared:
                marker = (f"【同一证据复用】该 finding 与 #{', #'.join(map(str, shared))} "
                          f"共用同一批证据事件（event {', '.join(map(str, ids))}）——"
                          f"多条预期由同一次操作观察得出")
                note = f"{note}；{marker}" if note.strip() else marker
            # ★ 2026-11-27：PASS 的时效自相矛盾（见 suspect_timeliness 说明）。
            #   只标 note，不改 status —— 文本启发式判错了会把真 PASS 冤枉成可疑。
            if status == "PASS":
                _suspect = self.suspect_timeliness(expect, actual)
                if _suspect:
                    note = f"{note}；{_suspect}" if note.strip() else _suspect
            cur = self._conn().execute(
                "INSERT INTO findings (session_id, event_id, status, expect, expect_id,"
                " expect_label, actual, note, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (session_id, ids[0] if ids else None, status, expect, expect_id,
                 (expect_label or None), actual, note, self._now()))
            fid = cur.lastrowid
            conn.executemany("INSERT INTO finding_events VALUES (?,?)", [(fid, eid) for eid in ids])
            return fid

    @staticmethod
    def suspect_timeliness(expect: str, actual: str) -> str:
        """判断一条 PASS 是不是「时效自相矛盾」：actual 自己写着"进入时没有、操作后才有"。

        返回空串 = 没发现问题；否则返回一句给人看的说明（写进 finding 的 note）。

        为什么只在 note 里标、不改 status：这判据是文本启发式，宁可漏不可错 ——
        判错了会把真 PASS 标成可疑，比漏报一条更糟。所以它只做「提醒复核」，
        人的结论不动。
        """
        text = actual or ""
        tokens = {m.group("t") for m in _SUSPECT_NEG_RE.finditer(text)}
        if not tokens:
            return ""
        if not _SUSPECT_POS_RE.search(text):
            return ""
        repeated = [tok for tok in tokens
                    if len(re.findall(re.escape(tok), text)) >= 2]
        if not repeated:
            return ""
        return ("【时效待复核】actual 自述「" + "、".join(sorted(repeated)) +
                "」进入时不存在、操作后才出现；若预期写的是「进入即应显示」，"
                "本条应为 FAIL —— 判定基准是预期，不是步骤写法")

    @staticmethod
    def _validate_evidence(conn: sqlite3.Connection, session_id: int, event_id: int) -> None:
        event = conn.execute(
            "SELECT session_id, evidence FROM events WHERE id=?", (event_id,)).fetchone()
        if not event or event["session_id"] != session_id or not event["evidence"]:
            raise ValueError(f"Event {event_id} must contain evidence from session {session_id}")
        path = Path(event["evidence"])
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file():
            raise ValueError(f"Event {event_id} evidence is missing: {path}")

    # 统计数字由工具算、工具写，**不再校验 AI 手写的数字**。
    #
    # 历史（2026-10-29 → 2026-11-13）：曾经用正则 grep summary 里的
    # 「(\d+)\s*(PASS|FAIL|BLOCKED)」，把每个数字当计数和 findings 比对。
    # 起因是 176 那轮 summary 写「8 PASS / 4 FAIL」而库里是 9/3。
    #
    # ⚠️ 为什么废掉：正则在**自由文本**上分不清「计数」和「编号」。
    #   实测（2026-11-13 会话 #160）：summary 写「预期1 PASS，预期2 PASS，预期3 FAIL」，
    #   断言实际是 2 PASS / 1 FAIL，正则抓出 1/2/3 三个数全当计数 → 连续 3 次
    #   提交被拒，执行侧只能把结论改成纯文字描述才收得了尾。
    #   加特例只治当下的写法：排除「预期N」之后，「第 2 条 PASS」「①PASS」
    #   照样漏 —— 这是跟自然语言打地鼠，永远补不完。
    #
    # ✅ 现在：findings 表是**确定且唯一**的真值，数字就该由工具算。
    #   finish 时把权威统计追加到 summary 末尾（_STATS_LINE 前缀），
    #   报告/回看读这一行；AI 正文怎么写都不影响。
    # ERROR 也算一条断言（进「断言 N 条」的分母），但它不是用例结论，
    # 只在真出现时才出现在统计行里，免得每行都挂着「ERROR 0」。
    _COUNT_STATUSES = ("PASS", "FAIL", "BLOCKED", "ERROR")
    _STATS_PREFIX = "[统计] "

    def _stats_line(self, findings: list) -> str:
        """权威统计行：断言总数 + 各结论条数（由 findings 表算出）。"""
        counts = {word: 0 for word in self._COUNT_STATUSES}
        for finding in findings:
            st = finding["status"]
            if st in counts:
                counts[st] += 1
        words = [w for w in self._COUNT_STATUSES
                 if w != "ERROR" or counts.get("ERROR")]
        parts = " / ".join(f"{word} {counts[word]}" for word in words)
        return f"{self._STATS_PREFIX}断言 {len(findings)} 条：{parts}"

    @classmethod
    def _strip_stats_line(cls, summary: str) -> str:
        """去掉上一次追加的统计行，避免重复 append / resume 后叠加。"""
        return "\n".join(
            line for line in summary.split("\n")
            if not line.startswith(cls._STATS_PREFIX))

    def finish_session(self, session_id: int, status: str, summary: str = "",
                       package: str | None = None) -> bool:
        if status not in STATUSES:
            raise ValueError(f"Invalid session status: {status}")
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not row or row["status"] not in INTERIM:
                raise ValueError("Finish requires an existing unfinished session")
            findings = conn.execute("SELECT * FROM findings WHERE session_id=?", (session_id,)).fetchall()
            statuses = {finding["status"] for finding in findings}
            if row["terminated"] and status != "BLOCKED":
                raise ValueError("Terminated sessions must finish BLOCKED")
            if status == "PASS":
                # PASS 只允许 PASS findings —— 一条 FAIL 都不能有
                if not findings or not statuses <= {"PASS"}:
                    raise ValueError(
                        "PASS requires all findings PASS (no FAIL, no BLOCKED)")
            if status == "FAIL" and "FAIL" not in statuses:
                raise ValueError("FAIL requires a FAIL finding")
            if status in ("BLOCKED", "ERROR") and not summary.strip():
                raise ValueError("BLOCKED/ERROR requires a reason in summary")
            # 统计行由工具追加（唯一权威来源）；先剥掉旧的再追加，防 resume 后叠加
            _body = self._strip_stats_line(summary).rstrip()
            _stats = self._stats_line(findings)
            summary = f"{_body}\n{_stats}" if _body else _stats
            if status not in ("ERROR", "BLOCKED") and recording_issues(Path(self.path).parent, session_id):
                raise ValueError("Recording/ledger is incomplete; finish ERROR/BLOCKED with a reason")
            for finding in findings if status in ("PASS", "FAIL") else []:
                if finding["status"] not in ("PASS", "FAIL"):
                    continue
                ids = [r[0] for r in conn.execute(
                    "SELECT event_id FROM finding_events WHERE finding_id=?", (finding["id"],))]
                if finding["event_id"] is not None and finding["event_id"] not in ids:
                    ids.append(finding["event_id"])
                if not ids or not finding["expect"].strip() or not finding["actual"].strip():
                    raise ValueError(f"Finding {finding['id']} is missing evidence or concrete observations")
                for eid in ids:
                    self._validate_evidence(conn, session_id, eid)
            conn.execute(
                "UPDATE sessions SET status=?, summary=?, finished_at=?,"
                " package=COALESCE(?, package) WHERE id=?",
                (status, summary, self._now(), package, session_id))
            conn.commit()
            return True
        except (sqlite3.Error, ValueError, OSError):
            conn.rollback()
            raise

    def set_package(self, session_id: int, package: str) -> None:
        """首个 observe 回填被测包名（已有值不覆盖）。"""
        try:
            self._conn().execute(
                "UPDATE sessions SET package=? WHERE id=? AND (package IS NULL OR package='')",
                (package, session_id))
            self._conn().commit()
        except sqlite3.Error:
            pass

    def get_session_package(self, session_id: int) -> str | None:
        """会话的被测包名（崩溃分级的 target 依据）。

        用会话声明而非"当前前台包"：被测 App 崩溃后前台回落到桌面，
        拿前台包分类会把被测包的崩溃误判成 other 而放过。
        """
        try:
            row = self._conn().execute(
                "SELECT package FROM sessions WHERE id=?", (session_id,)).fetchone()
            return (row["package"] or None) if row else None
        except (sqlite3.Error, TypeError):
            return None

    def terminate_session(self, session_id: int, reason: str) -> None:
        """标记会话为**终态**：被测/关联包崩溃后，后续动作一律拒绝执行。

        这是工具层硬约束（不只靠 AI 自觉读 `terminate` 信号）：
        崩溃一旦发生，继续观测/点击只会产生不可信的数据和多余动作。
        """
        try:
            self._conn().execute(
                "UPDATE sessions SET terminated=1, terminate_reason=? WHERE id=?",
                (reason, session_id))
            self._conn().commit()
        except sqlite3.Error:
            pass

    def get_termination(self, session_id: int) -> str | None:
        """会话是否已终止；返回终止原因（None=未终止，可继续执行）。"""
        row = self._conn().execute(
            "SELECT terminated, terminate_reason FROM sessions WHERE id=?",
            (session_id,)).fetchone()
        if row is None:
            raise ValueError(f"Session {session_id} does not exist")
        if row["terminated"]:
            return row["terminate_reason"] or "会话已终止"
        return None

    def clear_termination(self, session_id: int) -> bool:
        """清除终态（resume 时用：人工确认后允许继续）。"""
        try:
            self._conn().execute(
                "UPDATE sessions SET terminated=0, terminate_reason=NULL WHERE id=?",
                (session_id,))
            self._conn().commit()
            return True
        except sqlite3.Error:
            return False

    # ── 权限测试意图（声明式，跨命令持久）──────────────────────────
    # 设计参考 AiAgentTest 的 set_permission_intent：AI 在分支开头声明
    # "接下来遇到权限弹窗该点同意还是拒绝"，之后每次 act/observe 自动按此响应。
    # 关键：决策**提前做完**，动作时只执行——弹窗约 6s 自动消失，来不及现场想。
    #
    # ⚠️ TTL 曾经是 600 秒且**过期即静默降级成 grant**（真事故 2026-11-25）：
    #    AI 声明 `--action deny` 准备验"拒绝路径"，10 分钟后再遇到弹窗，
    #    get_perm_intent() 返回 None → perm.DEFAULT_ACTION="grant" →
    #    **弹窗被点成「允许」**。用例测的拒绝路径根本没走到，但日志里只有一行
    #    「（默认）」，看不出声明过 deny。这不是过期问题，是**语义反转**。
    #
    #    现在：TTL 只用来判「该不该再信」，**过期不进 grant**，而是
    #    返回 `expired=True` 让调用方在日志里显式记一行，并把处置权
    #    （点不点）交给人 —— 见 `perm.handle(action=None)` 的缺省分支。
    #    TTL 也放宽到 30 分钟：一条 GUI 用例从声明到真正遇到弹窗，
    #    10 分钟经常不够（实测 176 那轮光造数据就 12 分钟）。
    PERM_INTENT_TTL_S = 1800.0  # 30 分钟

    def set_perm_intent(self, session_id: int, action: str,
                        permission: str = "") -> bool:
        """action ∈ {"grant","deny"}；permission 仅作标记（如 media_images）。"""
        if action not in ("grant", "deny"):
            return False
        try:
            self._conn().execute(
                "UPDATE sessions SET perm_intent_action=?, perm_intent_perm=?,"
                " perm_intent_at=? WHERE id=?",
                (action, (permission or "").strip(), self._now(), session_id))
            self._conn().commit()
            return True
        except sqlite3.Error:
            return False

    def clear_perm_intent(self, session_id: int) -> bool:
        try:
            self._conn().execute(
                "UPDATE sessions SET perm_intent_action=NULL, perm_intent_perm=NULL,"
                " perm_intent_at=NULL WHERE id=?", (session_id,))
            self._conn().commit()
            return True
        except sqlite3.Error:
            return False

    def get_perm_intent(self, session_id: int) -> dict | None:
        """返回声明过的权限意图；无声明返回 None。

        返回 dict 的字段：
          action       "grant" / "deny"
          permission   声明时带的标记（如 media_images），可空
          expired      True = 声明已超过 TTL，**action 仍原样带出**
          age_s        声明距今秒数
          ttl_s        TTL

        ⚠️ 过期**不返回 None**（旧行为，已修）：调用方拿 None 会走
           `perm.DEFAULT_ACTION="grant"`，把声明的 deny **静默反转成同意**。
           现在把 `expired=True` 一起交出去，由调用方决定是「按声明执行并记警示」
           还是「不点、留给人工」——`act.py` 选的是前者（保证用例能往下走），
           但会在动作日志里写一条明确的 ⚠️，不再有静默。
        """
        try:
            row = self._conn().execute(
                "SELECT perm_intent_action, perm_intent_perm, perm_intent_at"
                " FROM sessions WHERE id=?", (session_id,)).fetchone()
        except sqlite3.Error:
            return None
        if not row or not row["perm_intent_action"]:
            return None
        at = row["perm_intent_at"] or ""
        try:
            age_s = (datetime.now() - datetime.fromisoformat(at)).total_seconds()
        except (ValueError, TypeError):
            # 时间戳坏了：声明本身还在，按「已过期但内容可信」处理，
            # 不因为一个脏时间戳把人声明的 deny 丢掉
            age_s = self.PERM_INTENT_TTL_S + 1
        return {"action": row["perm_intent_action"],
                "permission": row["perm_intent_perm"] or "",
                "expired": age_s > self.PERM_INTENT_TTL_S,
                "age_s": round(age_s, 1),
                "ttl_s": self.PERM_INTENT_TTL_S}

    # ── 媒体类权限「两阶段」：先拒后允（2026-11-27 人明确裁决）────────
    # 原始需求原话：「第一次点拒绝。第二次点同意」「第一次如果是主动点击了
    # 拒绝，第二次不应该再点拒绝，应该点允许。并且第一次点拒绝权限是不会
    # 发生变化的。」
    #
    # 为什么不能靠 TTL / 一次性的「意图过期」去实现：那是副作用，不是规则。
    # 旧版正是靠「AI 手动 perm-intent --clear」把 deny 弄掉，第二次弹框才
    # 落回默认 grant；一旦没人清（现在 act --permAction 是内联写入），deny
    # 会一直粘着，第二次框只剩「拒绝并不再询问」（被 perm.py 主动排除，因为
    # 点了这个权限就再也不会弹框、同意分支就没了）→ 匹配不到按钮 →
    # unmatched，框留在屏上，AI 只能反复重触发（实测会话 #1000232 连栽 4 次）。
    #
    # 所以把「已经拒过哪几类权限」显式记在会话上，由工具据此推进第二阶段。
    def perm_denied_keys(self, session_id: int) -> set[str]:
        """本会话已经把哪些权限类（camera/media/mic）「拒绝」过一次。"""
        try:
            row = self._conn().execute(
                "SELECT perm_denied_keys FROM sessions WHERE id=?",
                (session_id,)).fetchone()
        except sqlite3.Error:
            return set()
        raw = row["perm_denied_keys"] if row else None
        if not raw:
            return set()
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return set()
        if not isinstance(data, list):
            return set()
        return {str(x) for x in data if str(x).strip()}

    def mark_perm_denied(self, session_id: int, key: str) -> None:
        """记一次「这类权限已经拒绝过了」，返回给下一阶段判断用。"""
        key = (key or "").strip()
        if not key:
            return
        keys = self.perm_denied_keys(session_id)
        if key in keys:
            return
        keys.add(key)
        try:
            self._conn().execute(
                "UPDATE sessions SET perm_denied_keys=? WHERE id=?",
                (json.dumps(sorted(keys), ensure_ascii=False), session_id))
            self._conn().commit()
        except sqlite3.Error:
            pass

    def clear_perm_denied(self, session_id: int) -> None:
        """清掉两阶段状态（换分支 / 重新测拒绝路径时用）。"""
        try:
            self._conn().execute(
                "UPDATE sessions SET perm_denied_keys=NULL WHERE id=?", (session_id,))
            self._conn().commit()
        except sqlite3.Error:
            pass

    def mark_knowledge_shown(self, session_id: int) -> None:
        """记录"知识卡提示已给过"——避免每步重复带索引浪费 token。"""
        try:
            self._conn().execute(
                "UPDATE sessions SET knowledge_shown=1 WHERE id=?", (session_id,))
            self._conn().commit()
        except sqlite3.Error:
            pass

    def knowledge_shown(self, session_id: int) -> bool:
        """兼容旧口径：本会话是否提示过**任何**知识卡。"""
        try:
            row = self._conn().execute(
                "SELECT knowledge_shown FROM sessions WHERE id=?",
                (session_id,)).fetchone()
            return bool(row and row["knowledge_shown"])
        except (sqlite3.Error, TypeError, IndexError):
            return False

    def knowledge_hint_keys(self, session_id: int) -> set[str]:
        """本会话已经提示过的知识卡键集合（键的算法见 common.knowledge_hint_key）。

        ★ N-05：原来是 `sessions.knowledge_shown` 一个**会话级布尔** —— 一张卡提过
        之后，切到别家应用的界面（哪怕那家也有专属卡）再也不提示。实测真库
        418/427 会话都是「提示过一次就永久静默」，而 `_system.md` 写的是
        「按前台包读卡」，从没说过只提示一次。

        现在换成键集合：每个包各提示一次，无卡包的系统卡提示整个会话只提一次。
        """
        try:
            row = self._conn().execute(
                "SELECT knowledge_hint_keys FROM sessions WHERE id=?",
                (session_id,)).fetchone()
        except (sqlite3.Error, TypeError, IndexError):
            return set()
        raw = row["knowledge_hint_keys"] if row else None
        if not raw:
            return set()
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return set()
        return {str(x) for x in data} if isinstance(data, list) else set()

    def mark_knowledge_hint(self, session_id: int, key: str) -> None:
        """记录某个知识卡键已经提示过。

        同时把旧列 `knowledge_shown` 置 1：那是"有没有提示过任何卡"的粗粒度
        标记，旧的读法（webui / 老会话）还在用，不能让它失真。
        """
        key = str(key or "").strip()
        if not key:
            return
        keys = self.knowledge_hint_keys(session_id)
        if key in keys:
            return
        keys.add(key)
        try:
            self._conn().execute(
                "UPDATE sessions SET knowledge_hint_keys=?, knowledge_shown=1 WHERE id=?",
                (json.dumps(sorted(keys), ensure_ascii=False), session_id))
            self._conn().commit()
        except sqlite3.Error:
            pass

    def set_related(self, session_id: int, related: list[str]) -> None:
        try:
            self._conn().execute("UPDATE sessions SET related=? WHERE id=?",
                                 (",".join(related), session_id))
            self._conn().commit()
        except sqlite3.Error:
            pass

    def get_related(self, session_id: int) -> list[str]:
        """关联包列表（崩溃分级的"关联"判定域）。"""
        try:
            row = self._conn().execute(
                "SELECT related FROM sessions WHERE id=?", (session_id,)).fetchone()
            return [x.strip() for x in (row["related"] or "").split(",") if x.strip()]
        except (sqlite3.Error, TypeError):
            return []

    def set_session_status(self, session_id: int, status: str) -> bool:
        """中间态切换（现在只剩 running）。结论态必须走 finish_session。"""
        if status not in INTERIM:
            return False
        try:
            self._conn().execute("UPDATE sessions SET status=? WHERE id=?",
                                 (status, session_id))
            self._conn().commit()
            return True
        except sqlite3.Error:
            return False

    def flaky_expects(self, min_runs: int = 2, limit: int = 50) -> list[dict]:
        """找出**同一句预期、跨轮结论反了**的断言（抖动）。

        这是 #4（结构化 expect_id）真正的落地价值 —— 没有 expect_id 时这个
        查询写不出来：只能按 `expect` 中文精确分组，而同一句预期实测有 4 种写法
        「预期1 打开课程表页面」「预期1：…」「打开课程表页面」「步骤1 打开课程表界面」，
        分组后每组剩 1 条，看起来每次都"稳定"。

        ## 两级分组（第二级不能省）

        实测（2026-11-18，回填 1446 条存量断言）：
          · 只按 `(用例, expect_id)` 分组 → 26 组「抖动」
          · 再按 `(用例, expect_id, 归一化原文)` 分组 → **21 组**

        差出来的 5 组是**合法拆分**，不是抖动：同一个「预期1」被 AI 拆成
        `预期1（拆分1-a）`/`1-b`/`1-c` 三个断言分别判，文本不同、结论当然可以不同
        （日历_15 新建-弹框检查就是这种）。把这些算进抖动会让报表天天报假警。

        所以：
          · `flaky`  —— 同 expect_id + **同一句文本** 既 PASS 又 FAIL → 真抖动，要人看
          · `variants` —— 同 expect_id 下出现了多种文本 → 判据漂了，是"为什么抖"的线索
        两者分开返回，不混在一起。

        ## 分组键里的「用例」怎么来

        `sessions.title` 同一用例有 4 种写法（`com.zui.calendar_新建课程表_176` /
        `com.zui.calendar_176` / `日历_176 …`），直接当键会把同一用例拆成几组，
        抖动就永远凑不齐。这里按**编号**归一（`_case_key`），重号只在同包内
        （03 有两条）且还要求 expect 文本一致，可接受。
        """
        sql = """
        SELECT s.title AS title,
               s.package AS package,
               f.expect_id AS eid,
               f.expect AS expect,
               f.expect_label AS label,
               f.status AS status,
               s.id AS sid
          FROM findings f JOIN sessions s ON s.id = f.session_id
         WHERE f.status IN ('PASS','FAIL')
           AND f.expect_id IS NOT NULL
        """
        try:
            rows = self._conn().execute(sql).fetchall()
        except sqlite3.Error:
            return []

        def _norm(s: str) -> str:
            """抹平排版差异（编号前缀/空白/全角标点），**不动文字**。"""
            s = _EXPECT_PREFIX_RE.sub("", (s or "").strip())
            s = s.replace("\u3000", "")
            s = re.sub(r"\s+", "", s)
            for a, b in (("\u201c", '"'), ("\u201d", '"'), ("\u2018", "'"),
                         ("\u2019", "'"), ("\uff1b", ";"), ("\uff0c", ","),
                         ("\uff1a", ":"), ("\u3002", ".")):
                s = s.replace(a, b)
            return s

        # (case_key, eid) → 该预期条目的总览
        broad: dict[tuple, dict] = {}
        # (case_key, eid, norm_text) → 同一句预期的结论
        fine: dict[tuple, dict] = {}
        for r in rows:
            ck = _case_key(r["title"], r["package"])
            eid = r["eid"]
            b = broad.setdefault((ck, eid), {
                "case": r["title"], "expect_id": eid, "sessions": set(),
                "pass": 0, "fail": 0, "labels": [], "texts": []})
            b["sessions"].add(r["sid"])
            b["pass" if r["status"] == "PASS" else "fail"] += 1
            if r["label"] and r["label"] not in b["labels"]:
                b["labels"].append(r["label"])
            txt = (r["expect"] or "").strip()
            if txt and txt not in b["texts"] and len(b["texts"]) < 4:
                b["texts"].append(txt)

            nt = _norm(r["expect"])
            f = fine.setdefault((ck, eid, nt), {
                "case": r["title"], "expect_id": eid, "text": txt,
                "label": r["label"], "sessions": set(), "pass": 0, "fail": 0})
            f["sessions"].add(r["sid"])
            f["pass" if r["status"] == "PASS" else "fail"] += 1

        flaky: list[dict] = []
        for f in fine.values():
            if f["pass"] and f["fail"] and len(f["sessions"]) >= min_runs:
                flaky.append({"case": f["case"], "expect_id": f["expect_id"],
                              "text": f["text"], "label": f["label"],
                              "sessions": len(f["sessions"]),
                              "pass": f["pass"], "fail": f["fail"]})
        variants = [{"case": b["case"], "expect_id": b["expect_id"],
                     "sessions": len(b["sessions"]), "pass": b["pass"],
                     "fail": b["fail"], "labels": b["labels"],
                     "texts": b["texts"]}
                    for b in broad.values() if len(b["texts"]) > 1]

        flaky.sort(key=lambda x: -(x["pass"] + x["fail"]))
        variants.sort(key=lambda x: -(x["pass"] + x["fail"]))
        return {"flaky": flaky[:limit], "variants": variants[:limit]}

    def finding_stats(self, session_id: int) -> dict:
        """各结论的断言计数（finish 自检用：有 FAIL 时结论不得 PASS）。"""
        try:
            rows = self._conn().execute(
                "SELECT status, COUNT(*) FROM findings WHERE session_id=?"
                " GROUP BY status", (session_id,)).fetchall()
            return {r[0]: r[1] for r in rows}
        except sqlite3.Error:
            return {}

    def count_crash_events(self, session_id: int) -> int:
        try:
            return self._conn().execute(
                "SELECT COUNT(*) FROM events WHERE session_id=?"
                " AND data_json LIKE '%crash_detected%'",
                (session_id,)).fetchone()[0]
        except sqlite3.Error:
            return 0

    def list_events(self, session_id: int) -> list[dict]:
        """某会话的全部事件（按 seq 升序）。给导出台账/步骤序列/复盘用。

        N-24：以前只给 `seq/kind/tool/detail/data_json`，**拿不到事件主键、
        证据路径和耗时** —— 于是任何"按事件回查证据"的调用方（导出台账、
        复盘、定位某一步的截图）都得绕道再查一遍库，或者干脆查不出来。
        现在把 id / evidence / duration_ms / started_at / created_at /
        call_id 一起带出：这些列都很窄，多读一行的成本远低于二次查询。
        """
        try:
            rows = self._conn().execute(
                "SELECT id, seq, kind, tool, detail, data_json, evidence,"
                " duration_ms, started_at, created_at, call_id"
                " FROM events WHERE session_id=? ORDER BY seq",
                (session_id,)).fetchall()
        except sqlite3.Error:
            return []
        keys = ("id", "seq", "kind", "tool", "detail", "data_json", "evidence",
                "duration_ms", "started_at", "created_at", "call_id")
        return [dict(zip(keys, r)) for r in rows]

    def efficiency_stats(self, session_id: int) -> dict:
        """操作效率自查（finish 时提示，防止"跑得很脏"却无人察觉）。

        判据来自 SKILL.md「执行节奏 → 操作循环」：
          · **observe 次数应明显少于 act 次数**（0 也正常）——
            第一反应是读上一步的 `next`，不是再 observe 一次。
          · **swipe 用于拨滚轮是反模式** —— 有惯性会过冲，
            重复手势应用 `act tap --repeat N`。

        这里只**如实统计并提示**，不阻断 finish（效率差不等于用例结论错）。

        ## N-26：为什么必须解析 JSON，不能 grep 字符串

        旧写法靠 `'"type": "swipe"' in data_json` 判滑动。这有两个坑：
          ① 只能命中 `json.dumps` 的默认分隔符风格（`", "` / `": "`）；
             经 `separators=(",", ":")` 或手写压缩过的 JSON 一律漏计
             —— 实测真库 `json.loads` 数出 436 次 swipe，字符串匹配得 0；
          ② 子串匹配会命中**任何**层级里的 `type`（比如某个嵌套 dict
             也叫 `type`），把非滑动动作算成滑动，反之亦然。
        现在统一 `json.loads` 后按字段取，并保留旧版漏掉的 `multi_stage_swipe`
        （多段连续滑动同样是滑动，且更该被提示"能不能用 repeat 代替"）。
        """
        out = {"act": 0, "observe": 0, "swipe": 0, "cp_miss": 0, "ratio": None}
        try:
            rows = self._conn().execute(
                "SELECT kind, data_json FROM events WHERE session_id=?",
                (session_id,)).fetchall()
        except sqlite3.Error:
            return out
        _SWIPE_KINDS = {"swipe", "multi_stage_swipe", "drag"}
        for kind, dj in rows:
            if kind == "observe":
                out["observe"] += 1
            elif kind == "act":
                out["act"] += 1
                if not dj:
                    continue
                try:
                    data = json.loads(dj)
                except (ValueError, TypeError):
                    continue
                if not isinstance(data, dict):
                    continue
                act = data.get("action")
                act_type = str(act.get("type") or "") if isinstance(act, dict) else ""
                if act_type in _SWIPE_KINDS:
                    out["swipe"] += 1
                ck = data.get("checkpoint")
                if isinstance(ck, dict) and ck.get("ok") is False:
                    out["cp_miss"] += 1
        if out["act"]:
            out["ratio"] = round(out["observe"] / out["act"], 2)
        return out

    def delete_session(self, session_id: int, remove_evidence: bool = True) -> dict:
        """删除一次测试记录：数据库行 + **连带磁盘证据**（截图/日志）。

        返回 {"ok","files","dirs"}。证据定位走三条线索（缺一会漏删）：
          ① events.evidence 字段记的相对路径；
          ② storage/{evidence,logs}/<会话id>/ 自动命名目录；
          ③ ①中每个文件**所在的步骤目录**（shot.png 与 dump.xml/meta.json 同目录，
             只删 events 里那一个文件会把 dump/meta 留成孤儿）。

        ## ⚠️ 性能：**先删数据库行立刻返回，文件删除放后台**（2026-11-03）

        真实事故：WebUI 删一个 40 证据的会话耗时 **6.8 秒**（120 文件/40 目录）。
        期间 HTTP 连接挂着，浏览器/系统回收空闲连接后，响应写不回去 ——
        ConnectionAbortedError [WinError 10053]，前端 fetch 也失败，
        用户看到「没删掉」，实际库已删 → 界面与事实不一致。

        顺序反过来就好：**数据库行毫秒级删掉 → 响应立刻回 → 文件后台慢慢删**。
        证据文件只是"录像"，晚几秒删掉不影响数据一致性；
        即使 webui 进程退出没删完，也只是残留几个孤儿文件，无害。

        后台线程用 daemon=True：webui 关停时不阻塞（没删完就当残留）。
        """
        import shutil
        import threading

        conn = self._conn()

        # ① 先收集证据路径（删行之前，还能查到）
        ev_paths = [r["evidence"] for r in conn.execute(
            "SELECT evidence FROM events WHERE session_id=?",
            (session_id,)).fetchall() if r["evidence"]]

        # ② 删数据库行（毫秒级）
        try:
            cur = conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            conn.execute("DELETE FROM events WHERE session_id=?", (session_id,))
            conn.execute(
                "DELETE FROM finding_events WHERE finding_id IN "
                "(SELECT id FROM findings WHERE session_id=?)", (session_id,))
            conn.execute("DELETE FROM findings WHERE session_id=?", (session_id,))
            conn.commit()
        except sqlite3.Error:
            return {"ok": False, "files": 0, "dirs": 0}
        ok = cur.rowcount > 0
        if not ok or not remove_evidence:
            return {"ok": ok, "files": 0, "dirs": 0}

        # ③ 文件删除放后台 daemon 线程（秒级，不阻塞 HTTP 响应）
        db_path = self.path
        storage_root = Path(db_path).parent.resolve()
        evidence_root = (ROOT / "evidence").resolve()

        def _allowed(fp: Path) -> bool:
            """证据路径白名单：`storage/` 之下，或项目根的 `evidence/` 之下。

            evidence 是 common.rel() 出来的**项目根相对**路径；没有 run 目录时
            落在 ROOT/evidence/...（`evidence_paths` 的无 record_dir 分支）。
            只认 storage_root 会把这类整片跳过 —— 历史缺陷：它们永远删不掉，
            而返回的 files/dirs 又是写死的 0，调用方看不出没删。
            """
            try:
                return fp.is_relative_to(storage_root) or fp.is_relative_to(evidence_root)
            except OSError:
                return False

        # 待删清单在**同步段**算好：只做目录枚举（毫秒级），unlink/rmtree 放后台。
        # 这样 files/dirs 是真实数字，而不是写死的 0（旧实现让调用方以为删干净了）。
        targets: list[Path] = []
        step_dirs: set[Path] = set()
        for rel_p in ev_paths:
            try:
                fp = (ROOT / rel_p).resolve()
            except (OSError, ValueError):
                continue
            if not _allowed(fp):
                continue
            if fp.is_file():
                targets.append(fp)
            parent = fp.parent
            if (parent != storage_root and parent != evidence_root
                    and parent.is_dir() and _allowed(parent)):
                step_dirs.add(parent)
        auto_dirs: list[Path] = []
        for base in ("evidence", "logs"):
            d = Path(db_path).parent / base / str(session_id)
            if d.is_dir():
                auto_dirs.append(d)
        candidate_dirs = sorted(step_dirs) + auto_dirs
        counted: set[Path] = set(targets)
        for d in candidate_dirs:
            for f in d.rglob("*"):
                if f.is_file():
                    counted.add(f.resolve())
        files_n = len(counted)
        dirs_n = len(candidate_dirs)

        def _bg_cleanup() -> None:
            try:
                for d in candidate_dirs:
                    shutil.rmtree(d, ignore_errors=True)
                for f in targets:
                    try:
                        if f.is_file():
                            f.unlink()
                    except OSError:
                        pass
                # 清掉遗留的空目录（只清空的，不动有内容的）
                ev_root = Path(db_path).parent / "evidence"
                if ev_root.is_dir():
                    for sub in sorted(ev_root.rglob("*"), reverse=True):
                        if sub.is_dir() and not any(sub.iterdir()):
                            try:
                                sub.rmdir()
                            except OSError:
                                pass
            except Exception:  # noqa: BLE001
                pass      # 后台清理失败无害：顶多残留孤儿文件

        threading.Thread(target=_bg_cleanup, daemon=True,
                         name=f"evidence-cleanup-{session_id}").start()
        # files/dirs 是**本次待删的真实数量**（同步枚举出来的）；实际 unlink 在后台，
        # 极端情况下可能残留几个孤儿文件 —— 数字表示"计划删这么多"，不是"已删掉这么多"。
        return {"ok": True, "files": files_n, "dirs": dirs_n, "deferred": True}

    def set_session_kind(self, session_id: int, kind: str) -> bool:
        """切换会话类型（test=进测试记录 / diag=验证调试，默认不展示）。"""
        if kind not in SESSION_KINDS:
            return False
        try:
            self._conn().execute("UPDATE sessions SET kind=? WHERE id=?",
                                 (kind, session_id))
            self._conn().commit()
            return True
        except sqlite3.Error:
            return False

    def run_stats(self, days: int = 3) -> list[dict]:
        """最近 N 天每天的执行情况（仪表盘柱状图：成功率/失败率）。

        只统计正式测试（kind='test'），验证/调试会话不计入，
        否则自测噪音会污染成功率曲线。
        """
        try:
            rows = self._conn().execute(
                "SELECT substr(started_at,1,10) AS day, COUNT(*) AS total,"
                " SUM(CASE WHEN status='PASS' THEN 1 ELSE 0 END) AS pass,"
                " SUM(CASE WHEN status='FAIL' THEN 1 ELSE 0 END) AS fail,"
                " SUM(CASE WHEN status='BLOCKED' THEN 1 ELSE 0 END) AS blocked,"
                " SUM(CASE WHEN status NOT IN ('PASS','FAIL','BLOCKED')"
                "     THEN 1 ELSE 0 END) AS other"
                " FROM sessions WHERE kind='test'"
                # date('now') 是 UTC，而 started_at 是本地时间——
                # 用 'localtime' 对齐，否则跨天边界（本地 00:00~08:00）会算错一天。
                "   AND substr(started_at,1,10) >= date('now','localtime', ?)"
                " GROUP BY day ORDER BY day DESC", (f"-{max(0, days - 1)} day",)).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error:
            return []

    # ── 读（Web UI 用）───────────────────────────────────────────
    def list_sessions(self, limit: int = 100, kind: str | None = "test",
                      include: tuple[str, ...] | None = None) -> list[dict]:
        """会话列表。

        kind='test'（默认）= 只看正式测试记录；
        include=None 且 kind=None → 全部（含 diag/mock，仅调试用）；
        include=("test","diag") → 正式 + 验证调试（mock 仍排除）。
        mock（占位/构造数据）**永不进正式列表**。
        """
        # ⚠️ 时间基准坑（踩过两次，务必看清）：
        #   · julianday('now') 是 **UTC**，而 started_at/finished_at 由
        #     datetime.now() 写成**本地时间**——直接相减差一个时区（UTC+8 得 -28800s）。
        #   · 但 'localtime' 修饰符只能加在 **'now'** 上，**不能加在 finished_at 上**：
        #     julianday(finished_at,'localtime') 的语义是"把 finished_at 当 UTC 再换算"，
        #     而它本来就是本地串 → 对已收尾会话二次偏移（150s 变成 28950s）。
        #   正解：对 'now' 就地取本地时间；finished_at 已是本地串，原样使用。
        sql = ("SELECT s.*, MAX(0, ROUND((julianday(COALESCE(s.finished_at,"
               " datetime('now','localtime'))) - julianday(s.started_at)) * 86400, 1))"
               " AS duration_seconds,"
               " (SELECT COUNT(*) FROM events e WHERE e.session_id=s.id) AS event_count,"
               " (SELECT COUNT(*) FROM findings f WHERE f.session_id=s.id"
               "   AND f.status='FAIL') AS fail_count,"
               " (SELECT COUNT(*) FROM findings f WHERE f.session_id=s.id) AS finding_count"
               " FROM sessions s")
        params: tuple = ()
        if include:
            ph = ",".join("?" for _ in include)
            sql += f" WHERE COALESCE(s.kind,'test') IN ({ph})"
            params = tuple(include)
        elif kind:
            sql += " WHERE COALESCE(s.kind,'test')=?"
            params = (kind,)
        sql += " ORDER BY s.id DESC LIMIT ?"
        rows = self._conn().execute(sql, params + (limit,)).fetchall()
        out = [dict(r) for r in rows]
        # 「回放」入口是否可用：该会话至少有一帧的截图还在磁盘上。
        # ⚠️ 证据可能被手工清理或误删 —— 那时按钮要置灰，而不是点进去才发现播不了。
        # 放在这里统一算（列表页要按行显示），避免前端为每条记录各发一次请求。
        for r in out:
            evs = self._conn().execute(
                "SELECT evidence FROM events WHERE session_id=? AND evidence IS NOT NULL"
                " AND evidence != '' LIMIT 50", (r["id"],)).fetchall()
            r["has_evidence"] = any((ROOT / e["evidence"]).is_file() for e in evs)
            # 运行目录已不存在的旧会话：前端据此置灰「回放」，后端据此不拼证据路径。
            r["stale_run_id"] = run_dir_missing(r.get("run_id"))
        return out

    def session_kind_counts(self) -> dict:
        """各类型会话数（测试记录页「显示验证记录」开关用）。

        mock 不计入（它对用户不可见，不该出现在任何计数里）。
        """
        try:
            rows = self._conn().execute(
                "SELECT COALESCE(kind,'test') AS k, COUNT(*) AS n"
                " FROM sessions WHERE COALESCE(kind,'test') != 'mock'"
                " GROUP BY k").fetchall()
            return {r["k"]: r["n"] for r in rows}
        except sqlite3.Error:
            return {}

    def stale_sessions(self, max_idle_min: float = 60.0) -> list[dict]:
        """列出**声称还在跑、但早没人动**的会话（僵尸会话）。

        ## 为什么需要这个

        `session.py start` 发现还有 `status='running'` 的会话就拒绝新建。
        这条防线本身是对的（防同名重复开），但它**没有出口**：
        子 Agent 被中途停止、或 AI 进程被杀，会话就永远停在 running，
        设备执行通道被一直占住 —— 实测（2026-11-17）连续撞了三次：

          #1000216 / #1000217 / #1000218 都是「子 Agent 被停止的残留会话」，
          只能靠人工 `abandon --id N --reason "..."` 一个个收掉

        ## 阈值为什么是 60 分钟

        实测 `storage/test.db` 全部 10547 个相邻事件间隔（2026-11-18）：

          P50 0.20 分 / P90 0.40 分 / P99 1.37 分 / **P99.5 2.43 分**
          >30 分钟的只有 4 个，>60 分钟的只有 3 个（共 10961 个事件）

        也就是说正常跑测试时，**两次工具调用之间极少超过 3 分钟**
        （模型思考 + 一次冷启动）。60 分钟是 P99.5 的 25 倍，误杀概率极低；
        反过来，真僵尸（几小时不动）一定会被收掉。

        ⚠️ 判据是**最后一次事件距今多久**，不是会话总跨度。
        所以「连续跑了 13 小时、一直在动」的会话（历史上 #83 就是）不会被误判 ——
        它有事件在刷新 last_activity。

        ## 不改库

        这里只**读**。真要回收得由调用方显式 `finish_session`，
        因为作废要留 summary 说明，不能静默改状态。
        """
        try:
            rows = self._conn().execute("""
                SELECT s.id AS id, s.title AS title, s.kind AS kind,
                       s.started_at AS started_at, s.terminated AS terminated,
                       (SELECT COUNT(*) FROM events e
                         WHERE e.session_id = s.id) AS nev,
                       (SELECT MAX(e.created_at) FROM events e
                         WHERE e.session_id = s.id) AS last_ev
                  FROM sessions s
                 WHERE s.status = 'running'
                 ORDER BY s.id
            """).fetchall()
        except sqlite3.Error:
            return []
        now = datetime.now()
        out = []
        for r in rows:
            ref = r["last_ev"] or r["started_at"]
            try:
                idle = (now - datetime.fromisoformat(str(ref)[:19])).total_seconds() / 60.0
            except (ValueError, TypeError):
                continue          # 时间串坏了就别猜，跳过
            if idle >= max_idle_min:
                out.append({"id": r["id"], "title": r["title"] or "",
                            "kind": r["kind"] or "test",
                            "started_at": r["started_at"],
                            "last_activity": ref,
                            "idle_min": round(idle, 1),
                            "events": r["nev"],
                            "terminated": bool(r["terminated"])})
        out.sort(key=lambda x: -x["idle_min"])
        return out

    def findings_of(self, session_id: int) -> list[dict]:
        """某会话的全部 findings（按 id 升序），每条带 `event_ids`。

        比 `get_session()` 轻：不拉事件流、不拼 evidence 引用。
        给 checklist 对账（"哪些预期已经勾过"）和"两次勾选之间必须有新事件"
        的判据用 —— 那两处都是热路径，不值得为两个字段把整会话装一遍。
        """
        conn = self._conn()
        out: list[dict] = []
        for row in conn.execute(
                "SELECT * FROM findings WHERE session_id=? ORDER BY id",
                (session_id,)).fetchall():
            finding = dict(row)
            ids = [r[0] for r in conn.execute(
                "SELECT event_id FROM finding_events WHERE finding_id=?",
                (finding["id"],))]
            if finding["event_id"] is not None and finding["event_id"] not in ids:
                ids.insert(0, finding["event_id"])
            finding["event_ids"] = ids
            out.append(finding)
        return out

    def get_session(self, session_id: int) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            return None
        s = dict(row)
        s["events"] = [dict(r) for r in self._conn().execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY seq", (session_id,))]
        s["findings"] = [dict(r) for r in self._conn().execute(
            "SELECT * FROM findings WHERE session_id=? ORDER BY id", (session_id,))]
        # 事件入参 -> 展示用引用。前端要显示会话内序号（seq）而不是数据库主键
        # （id 全库累计、动辄上万，跟时间线上的 #1 #2 对不上，看着像跳错了）；
        # 还要知道证据文件在不在 —— 历史数据里有一批截图被清理过，
        # 那种引用只能显示「已清理」，不能给链接（点过去什么都没有）。
        by_id = {e["id"]: e for e in s["events"]}
        for finding in s["findings"]:
            ids = [r[0] for r in self._conn().execute(
                "SELECT fe.event_id FROM finding_events fe"
                " LEFT JOIN events e ON e.id=fe.event_id"
                " WHERE fe.finding_id=?"
                " ORDER BY COALESCE(e.seq, 999999), fe.event_id",
                (finding["id"],))]
            if finding["event_id"] is not None and finding["event_id"] not in ids:
                ids.insert(0, finding["event_id"])
            finding["event_ids"] = ids
            refs = []
            for eid in ids:
                ev = by_id.get(eid)
                if not ev:
                    continue
                ev_rel = ev.get("evidence") or ""
                refs.append({
                    "id": eid,
                    "seq": ev.get("seq"),
                    "kind": ev.get("kind"),
                    "has_file": bool(ev_rel) and (ROOT / ev_rel).is_file(),
                })
            finding["event_refs"] = refs
        return s

    # ── 单库化后新增：按 run_id 查询 ─────────────────────────────
    def sessions_of_run(self, run_id: str) -> list[dict]:
        """某次运行（uuid）下的全部会话。

        正常只有一个；重复 start 会产生多个（2026-10-31 跑 46 就撞见过）。

        ⚠️ 这里**不按目录存在性过滤**：会话记录本身是真的，目录没了仍然要能
        按 run_id 查出来（否则历史记录会"消失"）。调用方要判回放可用性，
        用 `run_dir_missing(run_id)`。
        """
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM sessions WHERE run_id=? ORDER BY id", (run_id,))]

    def list_runs(self, limit: int = 200) -> list[dict]:
        """运行列表（按 run_id 聚合），新的在前。

        单库化让这件事变成一句 GROUP BY —— 以前要开 138 个库才能算出来。

        ## 代表状态怎么取（N-25 修正）

        旧写法按 `PASS, FAIL, BLOCKED, ERROR` 从前到后找第一个命中 ——
        于是「同一次运行里一条 PASS、一条 FAIL」会显示成 **PASS**，
        把失败盖掉了；`running` 更是没进优先级，只有全部落空时才靠
        `sts[0]` 兜底。
        现在的口径是「这次运行到底跑完没有 + 最坏结论」：
          ① 还有 `running` → 整次没跑完，显示 `running`
          ② 否则按 `ERROR > BLOCKED > FAIL > PASS` 取最坏的那个
        （同一运行多次会话里出现的最坏结论，才是这次运行该被记住的结论）。

        ## 标题为什么不再 split(",")

        `GROUP_CONCAT(DISTINCT title)` 用逗号连接，而**用例标题里本来就有逗号**
        （如「日历_176 新建课程表，检查…」）→ `split(",")[0]` 会把标题截断成
        前半句。SQLite 的 `group_concat(DISTINCT x, y)` 不支持自定义分隔符，
        所以改成单独再查一遍每个 run 的首个 title（limit ≤ 200，成本可忽略）。
        """
        rows = self._conn().execute(
            "SELECT run_id,"
            " COUNT(*) AS session_count,"
            " MIN(started_at) AS started_at,"
            " MAX(COALESCE(finished_at, started_at)) AS ended_at,"
            " GROUP_CONCAT(DISTINCT status) AS statuses"
            " FROM sessions WHERE run_id IS NOT NULL AND run_id != ''"
            " GROUP BY run_id ORDER BY MIN(started_at) DESC LIMIT ?",
            (limit,)).fetchall()
        out = [dict(r) for r in rows]
        _ORDER = ("running", "ERROR", "BLOCKED", "FAIL", "PASS")
        for r in out:
            sts = [s for s in (r.pop("statuses") or "").split(",") if s]
            r["statuses"] = sorted(set(sts))
            r["status"] = "running" if "running" in sts else (
                next((s for s in _ORDER if s in sts), "running"))
            # 旧格式 run_id（hex12）指向的目录已不存在 —— 回放/录屏都不可能有。
            r["stale_run_id"] = run_dir_missing(r.get("run_id"))
        # 单独取每个 run 的首个 title（避开 GROUP_CONCAT 的逗号歧义）
        if out:
            ids = [r["run_id"] for r in out]
            marks = ",".join("?" for _ in ids)
            first: dict[str, str] = {}
            for run_id, title in self._conn().execute(
                    f"SELECT run_id, title FROM sessions"
                    f" WHERE run_id IN ({marks})"
                    f" AND title IS NOT NULL AND title != ''"
                    f" ORDER BY id", ids).fetchall():
                first.setdefault(run_id, title)
            for r in out:
                r["title"] = first.get(r["run_id"]) or ""
        return out

    def stats_by_run(self) -> dict:
        """按 run 维度的总量（供仪表盘）。"""
        row = self._conn().execute(
            "SELECT COUNT(DISTINCT run_id) AS runs, COUNT(*) AS sessions"
            " FROM sessions WHERE run_id IS NOT NULL AND run_id != ''").fetchone()
        return {"runs": row[0], "sessions": row[1]}

    def session_stats(self) -> dict:
        """正式测试记录的结论统计（kind='test'，验证/调试会话不计入）。"""
        row = self._conn().execute(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN status='PASS' THEN 1 ELSE 0 END) AS pass_n,"
            " SUM(CASE WHEN status='FAIL' THEN 1 ELSE 0 END) AS fail_n,"
            " SUM(CASE WHEN status='BLOCKED' THEN 1 ELSE 0 END) AS blocked_n,"
            " SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running_n,"
            " SUM(CASE WHEN status='ERROR' THEN 1 ELSE 0 END) AS error_n"
            " FROM sessions WHERE COALESCE(kind,'test')='test'").fetchone()
        return dict(row) if row else {}


_DB: SessionDB | None = None


def get_db() -> SessionDB:
    global _DB
    if _DB is None:
        _DB = SessionDB()
    return _DB


# ── 当前会话（工具层自动挂接）────────────────────────────────────
def set_current(session_id: int) -> None:
    # 用 _current_file() 而不是模块级 CURRENT_FILE —— 它要跟着 record 目录走
    cf = _current_file()
    cf.parent.mkdir(parents=True, exist_ok=True)
    cf.write_text(str(session_id), encoding="utf-8")


def clear_current(expected_id: int | None = None) -> None:
    if expected_id is not None:
        try:
            if int(_current_file().read_text(encoding="utf-8").strip()) != expected_id:
                return
        except FileNotFoundError:
            return
    _current_file().unlink(missing_ok=True)


def current_session() -> int | None:
    explicit = os.environ.get("ANDROID_TEST_SESSION_ID")
    if explicit is not None:
        return int(explicit)
    try:
        return int(_current_file().read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return None
