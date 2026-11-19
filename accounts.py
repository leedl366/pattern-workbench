#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""速图AIGC · 用户 / 积分 / 充值卡密体系（单文件模块）

- SQLite 持久化：users / sessions / ledger（积分流水）/ codes（充值卡密）/ psd_paid（PSD 首下记录）
- 密码 PBKDF2-HMAC-SHA256（6 万次迭代，随机盐），会话 token 存库（30 天有效）
- 定价（2026-10-12 雪碧拍板）：
    1 积分 = 0.1 元；注册送 10 积分
    抠图（图案提取建任务）3 积分/张；多图编辑 AI 生图 3 积分/张（按 n 计）；
    下载 PSD 3 积分/张（每个任务每人只收首次）；批量去水印 <300 张 3 积分/张，>=300 张 1 积分/张
- admin 组：跳过登录扣费（永不扣积分），可进管理后台
"""
import hashlib, hmac, json, os, re, secrets, sqlite3, threading, time

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data", "accounts.db")

REGISTER_BONUS = 10                      # 注册送体验积分
SESSION_DAYS = 30
COOKIE_NAME = "st_session"

# ---------------------------------------------------------------- 邀约返利（2026-11-15 雪碧拍板）
# 规则：老用户分享链接 → 新人注册时绑定邀请关系（写死不可改）
#        ① 邀请人立即得 20 积分
#        ② 被邀请人之后每次「真实充值」→ 邀请人得 充值积分 × 30%（向下取整）
# 🔴 铁律：30% 只对**真实充值积分**算。赠送积分 / 邀请奖励 / 人工补偿 / 退款回冲
#    一律不返 —— 否则「A 邀请 B，B 拿奖励再充值」能滚雪球无限套利。
# 🔴 返利只有一层：返给邀请人的积分不再触发下一级（不做多级分销）。
INVITE_REGISTER_REWARD = 20      # 每成功邀请 1 人，邀请人得 20 积分
INVITE_RECHARGE_RATE = 0.30      # 被邀请人充值积分 × 30% 返给邀请人
INVITE_IP_LIMIT = 10             # 同一 IP 24 小时内最多 10 个「有效邀请」
                                 # （第 11 个起：注册照常成功，但不绑邀请关系、不发奖）
INVITE_MONTH_CAP = 5000          # 单人滚动 30 天内返利上限（积分），防大号刷量
INVITE_CODE_LEN = 6

# 充值套餐（金额元 -> 实得积分）
PACKAGES = [
    {"price": 5, "points": 50, "note": "体验包"},
    {"price": 10, "points": 110, "note": "送 10 积分"},
    {"price": 30, "points": 340, "note": "送 40 积分"},
    {"price": 50, "points": 580, "note": "送 80 积分"},
    {"price": 100, "points": 1200, "note": "送 200 积分"},
]

# 消耗价目（前端展示 + 服务端计费共用这一份）
PRICE_LIST = [
    {"key": "extract", "name": "图案提取（抠图）", "desc": "3 积分 / 张"},
    {"key": "duotu", "name": "多图编辑（AI 生图）", "desc": "3 积分 / 张"},
    {"key": "psd", "name": "下载分层 PSD", "desc": "3 积分 / 张（同一任务重复下载不重复扣）"},
    {"key": "wm_small", "name": "批量去水印（300 张以下）", "desc": "3 积分 / 张"},
    {"key": "wm_bulk", "name": "批量去水印（300 张及以上）", "desc": "1 积分 / 张"},
    {"key": "dtl", "name": "详情页生成（整套电商图）", "desc": "3 积分 / 张（AI 分析与提示词生成免费；未出图的张数自动退回）"},
    {"key": "aigc", "name": "AI 生图（单张融合 / 批量出图）", "desc": "3 积分 / 张（未出图的张数自动退回）"},
    {"key": "fk", "name": "详情页复刻（照别人版式出整套）", "desc": "3 积分 / 屏（拆解与提示词免费；未出图的屏自动退回）"},
    {"key": "swap", "name": "商品替换（图1场景换图2物品）", "desc": "3 积分 / 张（未出图的张数自动退回）"},
]

_lock = threading.Lock()
_conf_cache = {"t": 0.0, "n": 2}
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # 去掉易混淆 0O1IL


# ---------------------------------------------------------------- DB 基础
def _db():
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with _lock, _db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT UNIQUE NOT NULL,
          pass_hash TEXT NOT NULL,
          salt TEXT NOT NULL,
          points INTEGER NOT NULL DEFAULT 0,
          grp TEXT NOT NULL DEFAULT 'user',
          created_ts REAL,
          last_login_ts REAL);
        CREATE TABLE IF NOT EXISTS sessions(
          token TEXT PRIMARY KEY,
          uid INTEGER NOT NULL,
          created_ts REAL,
          expires_ts REAL);
        CREATE TABLE IF NOT EXISTS ledger(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          uid INTEGER NOT NULL,
          delta INTEGER NOT NULL,
          balance INTEGER NOT NULL,
          reason TEXT,
          ref TEXT,
          ts REAL);
        CREATE TABLE IF NOT EXISTS codes(
          code TEXT PRIMARY KEY,
          points INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'new',
          batch TEXT,
          created_ts REAL,
          used_by INTEGER,
          used_ts REAL);
        CREATE TABLE IF NOT EXISTS psd_paid(
          tid TEXT NOT NULL,
          uid INTEGER NOT NULL,
          ts REAL,
          PRIMARY KEY(tid, uid));
        """)
        # ---- 增量迁移（幂等）：新增字段一律走这里，别改上面的建表语句 ----
        # 老库已存在 users 表，ALTER 才能补列；重复执行会先被 PRAGMA 判断挡住。
        _cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        for _col, _ddl in (("invite_code", "TEXT"),      # 自己的邀请码（6 位，去掉易混字符）
                           ("invited_by", "INTEGER"),    # 谁邀请的我（注册时写死，永不改）
                           ("reg_ip", "TEXT")):          # 注册 IP（同 IP 邀请数限制用）
            if _col not in _cols:
                c.execute("ALTER TABLE users ADD COLUMN %s %s" % (_col, _ddl))
        # 邀请码唯一（部分索引：允许 NULL，老数据不会被卡）
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_users_invite_code "
                  "ON users(invite_code) WHERE invite_code IS NOT NULL")
        c.execute("CREATE INDEX IF NOT EXISTS ix_users_invited_by ON users(invited_by)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_users_reg_ip ON users(reg_ip)")
        # 返利明细表：每笔奖励落一条，防重复发放 + 可审计 + 月上限统计
        c.execute("""CREATE TABLE IF NOT EXISTS invite_rewards(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          inviter_uid INTEGER NOT NULL,
          invitee_uid INTEGER NOT NULL,
          kind TEXT NOT NULL,
          base_points INTEGER NOT NULL DEFAULT 0,
          reward INTEGER NOT NULL DEFAULT 0,
          ref TEXT,
          ts REAL)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_invite_rewards_inviter "
                  "ON invite_rewards(inviter_uid, ts)")
    _seed_admin()
    _backfill_invite_codes()


def _hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("utf-8"), 60000).hex()


def _seed_admin():
    """首启种子管理员：环境变量 PATTERN_ADMIN_USER / PATTERN_ADMIN_PASS 可覆盖默认值。"""
    name = os.environ.get("PATTERN_ADMIN_USER") or "admin"
    pw = os.environ.get("PATTERN_ADMIN_PASS") or "admin123456"
    with _lock, _db() as c:
        row = c.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()
        if row:
            return
        salt = secrets.token_hex(8)
        c.execute(
            "INSERT INTO users(username,pass_hash,salt,points,grp,created_ts) VALUES(?,?,?,0,'admin',?)",
            (name, _hash_pw(pw, salt), salt, time.time()))
        print("[accounts] 已创建管理员账号 %s（默认密码请尽快在后台修改）" % name)


# ---------------------------------------------------------------- 会话
def _new_session(uid):
    tok = secrets.token_urlsafe(32)
    now = time.time()
    with _lock, _db() as c:
        c.execute("INSERT INTO sessions(token,uid,created_ts,expires_ts) VALUES(?,?,?,?)",
                  (tok, uid, now, now + SESSION_DAYS * 86400))
        c.execute("DELETE FROM sessions WHERE expires_ts<?", (now,))
    return tok


def uid_from_request(request):
    tok = request.cookies.get(COOKIE_NAME) or ""
    if not tok:
        return None
    with _db() as c:
        row = c.execute("SELECT uid,expires_ts FROM sessions WHERE token=?", (tok,)).fetchone()
    if not row or row["expires_ts"] < time.time():
        return None
    return int(row["uid"])


def get_user(uid):
    with _db() as c:
        row = c.execute("SELECT id,username,points,grp,created_ts,last_login_ts FROM users WHERE id=?",
                        (uid,)).fetchone()
    return dict(row) if row else None


def is_admin(uid):
    u = get_user(uid)
    return bool(u and u.get("grp") == "admin")


# ---------------------------------------------------------------- 积分
def charge(uid, amount, reason, ref=""):
    """扣积分。管理员永不扣。返回 (ok, 余额)。"""
    amount = int(amount or 0)
    if amount <= 0:
        return True, None
    with _lock, _db() as c:
        u = c.execute("SELECT grp FROM users WHERE id=?", (uid,)).fetchone()
        if u and u["grp"] == "admin":
            return True, None
        cur = c.execute("UPDATE users SET points=points-? WHERE id=? AND points>=?",
                        (amount, uid, amount))
        if cur.rowcount == 0:
            return False, None
        bal = c.execute("SELECT points FROM users WHERE id=?", (uid,)).fetchone()["points"]
        c.execute("INSERT INTO ledger(uid,delta,balance,reason,ref,ts) VALUES(?,?,?,?,?,?)",
                  (uid, -amount, bal, reason, ref, time.time()))
        return True, bal


def refund(uid, amount, reason, ref=""):
    amount = int(amount or 0)
    if amount <= 0:
        return
    with _lock, _db() as c:
        u = c.execute("SELECT grp FROM users WHERE id=?", (uid,)).fetchone()
        if u and u["grp"] == "admin":
            return
        c.execute("UPDATE users SET points=points+? WHERE id=?", (amount, uid))
        bal = c.execute("SELECT points FROM users WHERE id=?", (uid,)).fetchone()["points"]
        c.execute("INSERT INTO ledger(uid,delta,balance,reason,ref,ts) VALUES(?,?,?,?,?,?)",
                  (uid, amount, bal, reason, ref, time.time()))


def credit(uid, amount, reason, ref=""):
    """加积分（充值/人工调整），管理员也照记流水。"""
    amount = int(amount or 0)
    if amount == 0:
        return
    with _lock, _db() as c:
        c.execute("UPDATE users SET points=points+? WHERE id=?", (amount, uid))
        bal = c.execute("SELECT points FROM users WHERE id=?", (uid,)).fetchone()["points"]
        c.execute("INSERT INTO ledger(uid,delta,balance,reason,ref,ts) VALUES(?,?,?,?,?,?)",
                  (uid, amount, bal, reason, ref, time.time()))


# ---------------------------------------------------------------- 邀约返利
def _new_invite_code():
    """6 位邀请码，字符集已去掉 0O1IL 等易混字符（30^6 ≈ 7.3 亿组合）。"""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(INVITE_CODE_LEN))


def _backfill_invite_codes():
    """给存量用户补邀请码。幂等：已有码的跳过，可反复调用。"""
    try:
        with _lock, _db() as c:
            rows = c.execute("SELECT id FROM users "
                             "WHERE invite_code IS NULL OR invite_code=''").fetchall()
            for r in rows:
                for _try in range(20):
                    try:
                        c.execute("UPDATE users SET invite_code=? WHERE id=?",
                                  (_new_invite_code(), r["id"]))
                        break
                    except sqlite3.IntegrityError:      # 撞码 → 换一个重试
                        continue
    except Exception as e:                              # noqa: BLE001
        # 补码失败绝不能阻断启动（登录/计费与本功能无关）
        print("[accounts] 邀请码补发跳过：%s" % str(e)[:120])


def ensure_invite_code(uid):
    """取某用户的邀请码；没有就现场生成（双保险，防止迁移漏掉）。"""
    with _db() as c:
        row = c.execute("SELECT invite_code FROM users WHERE id=?", (uid,)).fetchone()
    if row and row["invite_code"]:
        return row["invite_code"]
    for _try in range(20):
        code = _new_invite_code()
        try:
            with _lock, _db() as c:
                c.execute("UPDATE users SET invite_code=? WHERE id=?", (code, uid))
            return code
        except sqlite3.IntegrityError:
            continue
    return ""


def _client_ip(request):
    """取真实客户端 IP。

    Caddy 反代会把真实 IP **追加到 X-Forwarded-For 末尾**（客户端自带的 XFF 会排在前面），
    所以取最后一段才可信；直连场景退化为 request.client.host。
    """
    xff = request.headers.get("x-forwarded-for") or ""
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    try:
        return request.client.host or ""
    except Exception:                                   # noqa: BLE001
        return ""


def _ip_invite_count(c, ip, hours=24):
    """该 IP 最近 N 小时内「成功绑定邀请关系」的注册数（= 有效邀请数）。"""
    if not ip:
        return 0
    row = c.execute(
        "SELECT COUNT(*) AS n FROM users "
        "WHERE reg_ip=? AND invited_by IS NOT NULL AND created_ts>=?",
        (ip, time.time() - hours * 3600)).fetchone()
    return int(row["n"] or 0)


def _bind_inviter(uid, ref_code, ip):
    """注册成功后绑定邀请人并发放注册奖励。返回 (ok, inviter_uid, msg)。

    🔴 所有失败都**静默不绑**（只记 msg），绝不让邀请码问题把注册带崩。
    """
    code = (ref_code or "").strip().upper().replace(" ", "")
    if not code:
        return False, None, "无邀请码"
    with _lock, _db() as c:
        inv = c.execute("SELECT id FROM users WHERE invite_code=?", (code,)).fetchone()
        if not inv:
            return False, None, "邀请码不存在"
        inviter = int(inv["id"])
        if inviter == int(uid):
            return False, None, "不能邀请自己"
        me = c.execute("SELECT invited_by FROM users WHERE id=?", (uid,)).fetchone()
        if me and me["invited_by"]:
            return False, None, "该账号已绑定过邀请人"
        if _ip_invite_count(c, ip) >= INVITE_IP_LIMIT:
            return False, None, "该 IP 24 小时内有效邀请已达上限（%d 个）" % INVITE_IP_LIMIT
        c.execute("UPDATE users SET invited_by=?, reg_ip=? WHERE id=?",
                  (inviter, ip or "", uid))
    _grant_invite(inviter, uid, "register", 0, INVITE_REGISTER_REWARD, code)
    return True, inviter, "ok"


def _grant_invite(inviter_uid, invitee_uid, kind, base_points, reward, ref=""):
    """真正发钱的地方：写 invite_rewards 明细 + 给邀请人加积分。

    受 INVITE_MONTH_CAP（滚动 30 天）约束 —— 超了就少发或不发。
    返回实际发放的积分（0 = 没发）。
    """
    reward = int(reward or 0)
    if reward <= 0 or not inviter_uid:
        return 0
    now = time.time()
    with _lock, _db() as c:
        used = c.execute("SELECT COALESCE(SUM(reward),0) AS s FROM invite_rewards "
                         "WHERE inviter_uid=? AND ts>=?",
                         (inviter_uid, now - 30 * 86400)).fetchone()["s"]
        left = int(INVITE_MONTH_CAP) - int(used or 0)
        if left <= 0:
            return 0
        reward = min(reward, left)
        c.execute("INSERT INTO invite_rewards(inviter_uid,invitee_uid,kind,base_points,"
                  "reward,ref,ts) VALUES(?,?,?,?,?,?,?)",
                  (inviter_uid, invitee_uid, kind, int(base_points or 0), reward, ref or "", now))
    reason = "邀请注册奖励" if kind == "register" else "邀请充值返利"
    credit(inviter_uid, reward, reason, "被邀请人uid:%d%s" % (invitee_uid, (" " + ref) if ref else ""))
    return reward


def pay_recharge_reward(invitee_uid, points, ref=""):
    """被邀请人充值后给邀请人返 30%。points = 本次**真实到账**的积分。

    🔴 只允许在这三处调用：卡密兑换、管理员勾了「记为充值」的人工加分。
       赠送 / 邀请奖励 / 退款回冲 一律不许调，否则能套利。
    """
    pts = int(points or 0)
    if pts <= 0:
        return 0
    with _db() as c:
        row = c.execute("SELECT invited_by FROM users WHERE id=?", (invitee_uid,)).fetchone()
    inviter = int(row["invited_by"]) if row and row["invited_by"] else 0
    if not inviter:
        return 0
    reward = int(pts * INVITE_RECHARGE_RATE)
    if reward <= 0:
        return 0
    return _grant_invite(inviter, invitee_uid, "recharge", pts, reward, ref or "")


def invite_stats(uid, for_admin=False):
    """「我的邀请」页要的全部数据：码、人数、返利、明细。

    🔴 for_admin=False（客户页面）**不返回** month_cap / ip_limit —— 防刷阈值是内部风控参数，
       不该出现在客户能看到的接口里（2026-11-15 雪碧要求：客户页看不到，管理员能看见）。
    """
    code = ensure_invite_code(uid)
    now = time.time()
    with _db() as c:
        n_invited = c.execute("SELECT COUNT(*) AS n FROM users WHERE invited_by=?",
                              (uid,)).fetchone()["n"]
        tot = c.execute("SELECT COALESCE(SUM(reward),0) AS s FROM invite_rewards "
                        "WHERE inviter_uid=?", (uid,)).fetchone()["s"]
        mon = c.execute("SELECT COALESCE(SUM(reward),0) AS s FROM invite_rewards "
                        "WHERE inviter_uid=? AND ts>=?",
                        (uid, now - 30 * 86400)).fetchone()["s"]
        rows = c.execute(
            "SELECT r.kind,r.base_points,r.reward,r.ref,r.ts,u.username AS invitee "
            "FROM invite_rewards r LEFT JOIN users u ON u.id=r.invitee_uid "
            "WHERE r.inviter_uid=? ORDER BY r.id DESC LIMIT 100", (uid,)).fetchall()
    out = {"code": code, "invited_count": int(n_invited or 0),
           "total_reward": int(tot or 0), "month_reward": int(mon or 0),
           "register_reward": INVITE_REGISTER_REWARD,
           "recharge_rate": int(round(INVITE_RECHARGE_RATE * 100)),
           "items": [dict(r) for r in rows]}
    if for_admin:
        out["month_cap"] = INVITE_MONTH_CAP
        out["ip_limit"] = INVITE_IP_LIMIT
    return out


def psd_already_paid(tid, uid):
    with _db() as c:
        row = c.execute("SELECT 1 FROM psd_paid WHERE tid=? AND uid=?", (tid, uid)).fetchone()
    return bool(row)


def mark_psd_paid(tid, uid):
    with _lock, _db() as c:
        c.execute("INSERT OR IGNORE INTO psd_paid(tid,uid,ts) VALUES(?,?,?)",
                  (tid, uid, time.time()))


# ---------------------------------------------------------------- 计费规则
_RE_TASK_CREATE = ("POST", re.compile(r"^/api/tasks$"))
_RE_BATCH_CREATE = ("POST", re.compile(r"^/api/tasks/batch$"))
_RE_PSD_FILE = re.compile(r"^/api/tasks/([^/]+)/file/(.+\.psd)$", re.I)
_RE_WM_COMMIT = ("POST", re.compile(r"^/wm/api/commit$"))
_RE_AI_GEN = ("POST", re.compile(r"^/duotu/api/ai_gen$"))
_RE_HD_FIX = ("POST", re.compile(r"^/hd/api/fix$"))
_RE_HD_PSD = re.compile(r"^/hd/api/tasks/([^/]+)/psd$")
# 详情页生成（2026-11-18）：只有「出图」和「单页重生成」收费，
# 分析产品 / 生成提示词 / 上传 / 停止 / 退分 / ZIP 全部不收费（雪碧拍板 D1）。
_RE_DTL_GEN = ("POST", re.compile(r"^/dtl/api/generate$"))
_RE_DTL_REDO = re.compile(r"^/dtl/api/tasks/([^/]+)/redo$")
# AI 生图（2026-11-18 第二轮）：同样只有「出图」和「单张重生成」收费，
# 上传 / 切模式 / 删图 / 停止 / 退分 / ZIP 全部不收费。
_RE_AIGC_GEN = ("POST", re.compile(r"^/aigc/api/generate$"))
_RE_AIGC_REDO = re.compile(r"^/aigc/api/tasks/([^/]+)/redo$")
# 详情页复刻（2026-11-18 第八个版块）：同样只有「出图」和「单屏重出」收费，
# 拆解参考页 / 拼装提示词 / 上传 / 长图切屏 / 停止 / 退分 / ZIP / 长图 全部不收费。
_RE_FK_GEN = ("POST", re.compile(r"^/fk/api/generate$"))
_RE_FK_REDO = re.compile(r"^/fk/api/tasks/([^/]+)/redo$")
# 商品替换（2026-11-19）：只有「出图」和「单张重出」收费，
# 上传 / 轮询 / 停止 / 退分 / ZIP 全部不收费。
_RE_SWAP_GEN = ("POST", re.compile(r"^/swap/api/generate$"))
_RE_SWAP_REDO = re.compile(r"^/swap/api/tasks/([^/]+)/redo$")

# 批量任务创建是 multipart/form-data（N 张图同传），不是 JSON：
# 前端在 FormData 里先塞一个纯文本字段 n=<张数>，这里直接扫原始 body 取，
# 比解析 multipart 便宜得多；取不到再退化为数 name="images" 出现次数。
_RE_FORM_N = re.compile(rb'name="n"\r?\n\r?\n(\d+)')


def _batch_n(body):
    try:
        m = _RE_FORM_N.search(body or b"")
        if m:
            n = int(m.group(1))
            if n > 0:
                return n
    except Exception:                            # noqa: BLE001
        pass
    try:
        return int((body or b"").count(b'name="images"'))
    except Exception:                            # noqa: BLE001
        return 0


# 2026-10-13 v2：批量改为「每个图案一个独立配置」→ 计费口径跟着改成 **按任务数**（3 分/任务）。
# 任务数从 multipart 里的 items JSON 数出（前端会同时传 n=任务数，但以 items 为准更准）；
# 老协议（只传若干 images、每图一任务）下 n / images 段数恰好也等于任务数 → 口径天然一致。
_RE_FORM_ITEMS = re.compile(rb'name="items"\r?\n\r?\n(.*?)\r?\n--', re.S)


def _batch_tasks(body):
    b = body or b""
    try:
        m = _RE_FORM_ITEMS.search(b)
        if m:
            arr = json.loads(m.group(1).decode("utf-8", "ignore") or "[]")
            if isinstance(arr, list) and arr:
                return len(arr)
    except Exception:                            # noqa: BLE001
        pass
    return _batch_n(b)


def match_charge(method, path):
    """命中计费规则则返回规则描述（不含金额），否则 None。need_body=需要从请求体算量。"""
    if method == "POST" and _RE_BATCH_CREATE[1].match(path):
        return {"kind": "extract_batch", "need_body": True}
    if method == "POST" and _RE_TASK_CREATE[1].match(path):
        return {"kind": "extract", "need_body": False}
    if method == "GET":
        m = _RE_PSD_FILE.match(path)
        if m:
            return {"kind": "psd", "need_body": False, "tid": m.group(1)}
    if method == "POST" and _RE_WM_COMMIT[1].match(path):
        return {"kind": "wm", "need_body": True}
    if method == "POST" and _RE_AI_GEN[1].match(path):
        return {"kind": "duotu", "need_body": True}
    if method == "POST" and _RE_HD_FIX[1].match(path):
        return {"kind": "hd_fix", "need_body": True}
    if method == "GET":
        m = _RE_HD_PSD.match(path)
        if m:
            return {"kind": "hd_psd", "need_body": False, "tid": m.group(1)}
    if method == "POST" and _RE_DTL_GEN[1].match(path):
        return {"kind": "dtl_gen", "need_body": True}
    if method == "POST":
        m = _RE_DTL_REDO.match(path)
        if m:
            return {"kind": "dtl_redo", "need_body": False, "tid": m.group(1)}
    if method == "POST" and _RE_AIGC_GEN[1].match(path):
        return {"kind": "aigc_gen", "need_body": True}
    if method == "POST":
        m = _RE_AIGC_REDO.match(path)
        if m:
            return {"kind": "aigc_redo", "need_body": False, "tid": m.group(1)}
    if method == "POST" and _RE_FK_GEN[1].match(path):
        return {"kind": "fk_gen", "need_body": True}
    if method == "POST":
        m = _RE_FK_REDO.match(path)
        if m:
            return {"kind": "fk_redo", "need_body": True, "tid": m.group(1)}
    if method == "POST" and _RE_SWAP_GEN[1].match(path):
        return {"kind": "swap_gen", "need_body": True}
    if method == "POST":
        m = _RE_SWAP_REDO.match(path)
        if m:
            return {"kind": "swap_redo", "need_body": False, "tid": m.group(1)}
    return None


def _duotu_conf_n():
    """读 duotu 的 ai_config.json 拿默认 n（60 秒缓存）。"""
    now = time.time()
    if now - _conf_cache["t"] < 60:
        return _conf_cache["n"]
    n = 2
    try:
        p = os.path.join(BASE, "duotu", "data", "ai_config.json")
        d = json.load(open(p, encoding="utf-8"))
        n = int(d.get("n") or 2)
    except Exception:                            # noqa: BLE001
        pass
    _conf_cache.update({"t": now, "n": n})
    return n


def amount_for(rule, body, uid):
    """由规则+请求体算 (amount, reason, ref)。amount=0 表示本次不收（如 PSD 已付过/管理员）。"""
    kind = rule["kind"]
    if is_admin(uid):
        return 0, "", ""
    if kind == "extract":
        return 3, "图案提取·抠图", ""
    if kind == "extract_batch":
        n = _batch_tasks(body)                   # n = 任务（图案）数，非图片数
        if n <= 0:
            return 0, "", ""
        return 3 * n, "图案提取·批量抓取 x%d 个图案" % n, ""
    if kind == "psd":
        tid = rule["tid"]
        if psd_already_paid(tid, uid):
            return 0, "", ""
        return 3, "下载分层PSD", tid
    if kind == "wm":
        try:
            items = (json.loads(body.decode("utf-8") or "{}") or {}).get("items") or []
        except Exception:                        # noqa: BLE001
            items = []
        n = len(items)
        if n <= 0:
            return 0, "", ""
        unit = 1 if n >= 300 else 3
        return n * unit, "批量去水印%d张" % n, ""
    if kind == "duotu":
        try:
            d = json.loads(body.decode("utf-8") or "{}") or {}
        except Exception:                        # noqa: BLE001
            d = {}
        try:
            n = int(d.get("n") or _duotu_conf_n())
        except Exception:                        # noqa: BLE001
            n = _duotu_conf_n()
        n = max(1, min(n, 8))
        return 3 * n, "多图编辑AI生图x%d" % n, ""
    if kind == "hd_fix":
        n = _batch_n(body)                       # multipart 纯文本 n=<张数>，与批量模式同协议
        if n <= 0:
            return 0, "", ""
        return 3 * n, "高清修复x%d张" % n, ""
    if kind == "hd_psd":
        tid = rule["tid"]
        if psd_already_paid(tid, uid):
            return 0, "", ""
        return 3, "高清修复·下载分层PSD", tid
    if kind == "dtl_gen":
        # 张数由前端按「提示词里第 N 页命中数」算好放在 body.n；
        # 取不到就不扣（宁可少扣也不能瞎扣），上限 15 与原站生成数量一致。
        try:
            d = json.loads(body.decode("utf-8") or "{}") or {}
        except Exception:                        # noqa: BLE001
            d = {}
        try:
            n = int(d.get("n") or 0)
        except Exception:                        # noqa: BLE001
            n = 0
        n = max(0, min(n, 15))
        if n <= 0:
            return 0, "", ""
        return 3 * n, "详情页生成x%d张" % n, str(d.get("tid") or "")
    if kind == "dtl_redo":
        return 3, "详情页生成·单页重生成", rule["tid"]
    if kind == "aigc_gen":
        # 张数由前端按「单张=1 / 批量=参考图数」算好放 body.n。
        # 取不到就不扣（宁可少扣也不能瞎扣），上限 10 与原站批量上传上限一致。
        try:
            d = json.loads(body.decode("utf-8") or "{}") or {}
        except Exception:                        # noqa: BLE001
            d = {}
        try:
            n = int(d.get("n") or 0)
        except Exception:                        # noqa: BLE001
            n = 0
        n = max(0, min(n, 10))
        if n <= 0:
            return 0, "", ""
        return 3 * n, "AI 生图x%d张" % n, str(d.get("tid") or "")
    if kind == "aigc_redo":
        return 3, "AI 生图·单张重新生成", rule["tid"]
    if kind == "fk_gen":
        # 屏数由前端按「已开启且提示词非空」算好放 body.n；取不到就不扣（宁可少扣不能瞎扣），
        # 上限 12 与 fk.MAX_SCREENS 一致。
        try:
            d = json.loads(body.decode("utf-8") or "{}") or {}
        except Exception:                        # noqa: BLE001
            d = {}
        try:
            n = int(d.get("n") or 0)
        except Exception:                        # noqa: BLE001
            n = 0
        n = max(0, min(n, 12))
        if n <= 0:
            return 0, "", ""
        return 3 * n, "详情页复刻x%d屏" % n, str(d.get("tid") or "")
    if kind == "fk_redo":
        # 2026-11-19 支持**批量重出**：body 里给 idxs=[1,3,7] 就按张数扣；
        # 只给 idx（老前端）仍按 1 张扣，行为不变。上限与 MAX_SCREENS 一致。
        try:
            d = json.loads(body.decode("utf-8") or "{}") or {}
        except Exception:                        # noqa: BLE001
            d = {}
        idxs = d.get("idxs")
        if isinstance(idxs, (list, tuple)) and idxs:
            n = len({int(x) for x in idxs if str(x).strip().isdigit() or isinstance(x, int)})
        else:
            n = 1 if d.get("idx") else 0
        n = max(0, min(n, 12))
        if n <= 0:
            return 0, "", ""
        if n == 1:
            return 3, "详情页复刻·单屏重出", rule["tid"]
        return 3 * n, "详情页复刻·重出%d屏" % n, rule["tid"]
    if kind == "swap_gen":
        # 张数由前端按「生成数量」放 body.n（1–4，上限与 swap.MAX_N 一致）。
        # 取不到就不扣（宁可少扣也不能瞎扣）
        try:
            d = json.loads(body.decode("utf-8") or "{}") or {}
        except Exception:                        # noqa: BLE001
            d = {}
        try:
            n = int(d.get("n") or 0)
        except Exception:                        # noqa: BLE001
            n = 0
        n = max(0, min(n, 4))
        if n <= 0:
            return 0, "", ""
        return 3 * n, "商品替换x%d张" % n, ""
    if kind == "swap_redo":
        return 3, "商品替换·单张重出", rule["tid"]
    return 0, "", ""


# ---------------------------------------------------------------- HTTP API
router = APIRouter(prefix="/accounts/api")

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\-一-龥]{2,24}$")


def _set_session_cookie(resp, token):
    resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_DAYS * 86400,
                    httponly=True, samesite="lax", path="/")


@router.post("/register")
async def register(request: Request):
    d = await request.json()
    username = (d.get("username") or "").strip()
    password = d.get("password") or ""
    ref = (d.get("ref") or "").strip()          # 邀请码（选填，来自分享链接 ?ref=）
    if not _USERNAME_RE.match(username):
        return JSONResponse({"ok": False, "error": "用户名需 2-24 位（中英文/数字/下划线）"}, 400)
    if len(password) < 6:
        return JSONResponse({"ok": False, "error": "密码至少 6 位"}, 400)
    salt = secrets.token_hex(8)
    now = time.time()
    ip = _client_ip(request)
    with _lock, _db() as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            return JSONResponse({"ok": False, "error": "用户名已被注册"}, 409)
        # 注册即分配邀请码（锁内查重，避免撞唯一索引后整条注册失败）
        self_code = ""
        for _try in range(20):
            _c = _new_invite_code()
            if not c.execute("SELECT 1 FROM users WHERE invite_code=?", (_c,)).fetchone():
                self_code = _c
                break
        cur = c.execute(
            "INSERT INTO users(username,pass_hash,salt,points,grp,created_ts,reg_ip,invite_code) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (username, _hash_pw(password, salt), salt, REGISTER_BONUS, "user", now,
             ip or "", self_code or None))
        uid = cur.lastrowid
        c.execute("INSERT INTO ledger(uid,delta,balance,reason,ref,ts) VALUES(?,?,?,?,?,?)",
                  (uid, REGISTER_BONUS, REGISTER_BONUS, "新用户注册赠送", "", now))
    # 邀约绑定：失败不影响注册结果（比如邀请码过期/超 IP 上限 → 只是没绑定）
    inv_ok, inviter, inv_msg = _bind_inviter(uid, ref, ip)
    tok = _new_session(uid)
    resp = JSONResponse({"ok": True,
                         "user": {"username": username, "points": REGISTER_BONUS, "grp": "user"},
                         "invited": bool(inv_ok),
                         "invite_msg": inv_msg})
    _set_session_cookie(resp, tok)
    return resp


@router.post("/login")
async def login(request: Request):
    d = await request.json()
    username = (d.get("username") or "").strip()
    password = d.get("password") or ""
    with _db() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row or not hmac.compare_digest(row["pass_hash"], _hash_pw(password, row["salt"])):
        return JSONResponse({"ok": False, "error": "用户名或密码错误"}, 401)
    uid = int(row["id"])
    with _lock, _db() as c:
        c.execute("UPDATE users SET last_login_ts=? WHERE id=?", (time.time(), uid))
    tok = _new_session(uid)
    resp = JSONResponse({"ok": True, "user": {"username": username, "points": row["points"], "grp": row["grp"]}})
    _set_session_cookie(resp, tok)
    return resp


@router.post("/logout")
async def logout(request: Request):
    tok = request.cookies.get(COOKIE_NAME) or ""
    if tok:
        with _lock, _db() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (tok,))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@router.get("/me")
async def me(request: Request):
    uid = uid_from_request(request)
    if not uid:
        return JSONResponse({"ok": False, "error": "未登录"}, 401)
    u = get_user(uid)
    if not u:
        return JSONResponse({"ok": False, "error": "账号不存在"}, 401)
    return {"ok": True, "user": {"username": u["username"], "points": u["points"],
                                 "grp": u["grp"], "invite_code": ensure_invite_code(uid)}}


@router.get("/packages")
async def packages():
    return {"ok": True, "packages": PACKAGES, "prices": PRICE_LIST,
            "tip": "1 积分 = 0.1 元，5 元起充"}


@router.post("/redeem")
async def redeem(request: Request):
    uid = uid_from_request(request)
    if not uid:
        return JSONResponse({"ok": False, "error": "未登录"}, 401)
    d = await request.json()
    code = (d.get("code") or "").strip().upper().replace(" ", "")
    if not code:
        return JSONResponse({"ok": False, "error": "请输入充值码"}, 400)
    now = time.time()
    with _lock, _db() as c:
        row = c.execute("SELECT * FROM codes WHERE code=?", (code,)).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "充值码不存在"}, 404)
        if row["status"] != "new":
            return JSONResponse({"ok": False, "error": "充值码已被使用"}, 409)
        cur = c.execute("UPDATE codes SET status='used',used_by=?,used_ts=? WHERE code=? AND status='new'",
                        (uid, now, code))
        if cur.rowcount == 0:
            return JSONResponse({"ok": False, "error": "充值码已被使用"}, 409)
        pts = int(row["points"])
        c.execute("UPDATE users SET points=points+? WHERE id=?", (pts, uid))
        bal = c.execute("SELECT points FROM users WHERE id=?", (uid,)).fetchone()["points"]
        c.execute("INSERT INTO ledger(uid,delta,balance,reason,ref,ts) VALUES(?,?,?,?,?,?)",
                  (uid, pts, bal, "卡密充值", code, now))
    # 邀约返利：卡密是用户真金白银买的 → 算真实充值，给邀请人返 30%
    inv_reward = pay_recharge_reward(uid, pts, code)
    return {"ok": True, "points": pts, "balance": bal, "invite_reward": inv_reward}


@router.post("/change_password")
async def change_password(request: Request):
    uid = uid_from_request(request)
    if not uid:
        return JSONResponse({"ok": False, "error": "未登录"}, 401)
    d = await request.json()
    old, new = d.get("old") or "", d.get("new") or ""
    if len(new) < 6:
        return JSONResponse({"ok": False, "error": "新密码至少 6 位"}, 400)
    with _lock, _db() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row or not hmac.compare_digest(row["pass_hash"], _hash_pw(old, row["salt"])):
            return JSONResponse({"ok": False, "error": "原密码错误"}, 401)
        salt = secrets.token_hex(8)
        c.execute("UPDATE users SET pass_hash=?,salt=? WHERE id=?",
                  (_hash_pw(new, salt), salt, uid))
        c.execute("DELETE FROM sessions WHERE uid=?", (uid,))
    return {"ok": True}


# ---------------------------------------------------------------- 管理后台 API
def _require_admin(request):
    uid = uid_from_request(request)
    if not uid or not is_admin(uid):
        return None
    return uid


@router.get("/admin/users")
async def admin_users(request: Request):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "无权限"}, 403)
    since = time.time() - 30 * 86400
    with _db() as c:
        rows = c.execute(
            "SELECT u.id,u.username,u.points,u.grp,u.created_ts,u.last_login_ts,"
            "u.invite_code,u.invited_by,inv.username AS inviter_name,"
            "(SELECT COUNT(*) FROM users x WHERE x.invited_by=u.id) AS invited_count,"
            "COALESCE((SELECT SUM(reward) FROM invite_rewards WHERE inviter_uid=u.id),0) AS invite_reward,"
            "COALESCE((SELECT SUM(reward) FROM invite_rewards "
            "          WHERE inviter_uid=u.id AND ts>=?),0) AS invite_reward_month "
            "FROM users u LEFT JOIN users inv ON inv.id=u.invited_by "
            "ORDER BY u.id DESC LIMIT 500", (since,)
        ).fetchall()
    return {"ok": True, "users": [dict(r) for r in rows],
            "invite_rule": {"register_reward": INVITE_REGISTER_REWARD,
                            "recharge_rate": int(round(INVITE_RECHARGE_RATE * 100)),
                            "ip_limit": INVITE_IP_LIMIT,
                            "month_cap": INVITE_MONTH_CAP}}


@router.post("/admin/adjust")
async def admin_adjust(request: Request):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "无权限"}, 403)
    d = await request.json()
    username = (d.get("username") or "").strip()
    delta = int(d.get("delta") or 0)
    reason = (d.get("reason") or "人工调整").strip()
    # 🔴 is_recharge：管理员勾了「记为充值」才给邀请人返 30%。
    # 因为「人工调整积分」既可能是用户微信转账了，也可能是补偿出图失败 ——
    # 一刀切返会白送钱，必须人工确认。
    is_recharge = bool(d.get("is_recharge"))
    if not username or delta == 0:
        return JSONResponse({"ok": False, "error": "参数不完整"}, 400)
    with _lock, _db() as c:
        row = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "用户不存在"}, 404)
        uid = int(row["id"])
    credit(uid, delta, "后台调整:" + reason + ("（记为充值）" if is_recharge else ""))
    inv_reward = 0
    if is_recharge and delta > 0:              # 只有「加」分才返，扣分不返
        inv_reward = pay_recharge_reward(uid, delta, "admin:" + reason)
    return {"ok": True, "balance": get_user(uid)["points"], "invite_reward": inv_reward}


@router.post("/admin/gen_codes")
async def admin_gen_codes(request: Request):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "无权限"}, 403)
    d = await request.json()
    points = int(d.get("points") or 0)
    count = max(1, min(int(d.get("count") or 1), 200))
    batch = (d.get("batch") or "").strip() or time.strftime("%Y%m%d")
    if points <= 0:
        return JSONResponse({"ok": False, "error": "积分必须大于 0"}, 400)
    made = []
    with _lock, _db() as c:
        for _ in range(count):
            for _try in range(20):
                code = "ST-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4)) + \
                       "-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))
                try:
                    c.execute("INSERT INTO codes(code,points,batch,created_ts) VALUES(?,?,?,?)",
                              (code, points, batch, time.time()))
                    made.append(code)
                    break
                except sqlite3.IntegrityError:
                    continue
    return {"ok": True, "codes": made, "points": points, "batch": batch}


@router.get("/admin/codes")
async def admin_codes(request: Request, status: str = "", limit: int = 200):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "无权限"}, 403)
    q = "SELECT code,points,status,batch,created_ts,used_ts FROM codes"
    args = []
    if status in ("new", "used"):
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY created_ts DESC LIMIT ?"
    args.append(min(limit, 1000))
    with _db() as c:
        rows = c.execute(q, args).fetchall()
    return {"ok": True, "codes": [dict(r) for r in rows]}


@router.get("/admin/ledger")
async def admin_ledger(request: Request, username: str = "", limit: int = 200):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "无权限"}, 403)
    q = ("SELECT l.uid,u.username,l.delta,l.balance,l.reason,l.ref,l.ts "
         "FROM ledger l LEFT JOIN users u ON u.id=l.uid")
    args = []
    if username:
        q += " WHERE u.username=?"
        args.append(username)
    q += " ORDER BY l.id DESC LIMIT ?"
    args.append(min(limit, 1000))
    with _db() as c:
        rows = c.execute(q, args).fetchall()
    return {"ok": True, "ledger": [dict(r) for r in rows]}


# ---------------------------------------------------------------- 我的积分明细（客户自助）
# 2026-11-15：此前只有 /admin/ledger 给管理员看，客户完全看不到自己的消费/充值记录
# （invite.html 还写着「可在充值页的流水中查到」，而充值页压根没有流水 → 假承诺）。
# 这里补一个**只能看自己**的接口：uid 一律取自会话，绝不接受前端传 uid。
_LEDGER_KINDS = ("all", "recharge", "consume", "refund", "invite", "gift", "adjust", "other")


def _ledger_kind(reason, delta):
    """把流水 reason 归类，供客户页做筛选标签。顺序敏感：「邀请充值返利」必须先判邀请。"""
    r = str(reason or "")
    if r.startswith("失败退回"):
        return "refund"
    if r.startswith("邀请"):
        return "invite"
    if r.startswith("后台调整"):
        return "adjust"
    if delta > 0:
        if ("卡密" in r) or ("充值" in r):
            return "recharge"
        if ("注册" in r) or ("赠送" in r):
            return "gift"
        return "other"
    return "consume"


def _ledger_text(reason):
    """客户可见的措辞：把内部前缀翻成人话，去掉内部标记。"""
    r = str(reason or "").strip() or "积分变动"
    for sep in (":", "："):
        if r.startswith("失败退回" + sep):
            return "生成失败退回 · " + r[len("失败退回" + sep):]
        if r.startswith("后台调整" + sep):
            return "管理员调整 · " + r[len("后台调整" + sep):].replace("（记为充值）", "")
    return r


@router.get("/ledger")
async def my_ledger(request: Request, kind: str = "all", limit: str = "100"):
    """我的积分明细。只返回本人记录；kind 用于前端筛选（all/recharge/consume/refund/invite/gift/adjust/other）。

    ⚠️ limit 收 str 再自己解析：收 int 的话前端传个非法值 FastAPI 直接 422，
       客户页会白屏。这里一律降级为默认值（100），钳位 1~500。
    """
    uid = uid_from_request(request)
    if not uid:
        return JSONResponse({"ok": False, "error": "未登录"}, 401)
    kind = (kind or "all").strip().lower()
    if kind not in _LEDGER_KINDS:
        kind = "all"
    try:
        limit = max(1, min(int(str(limit or "100").strip()), 500))
    except Exception:                            # noqa: BLE001
        limit = 100
    with _db() as c:
        rows = c.execute("SELECT id,delta,balance,reason,ref,ts FROM ledger "
                         "WHERE uid=? ORDER BY id DESC LIMIT 2000", (uid,)).fetchall()
        agg = c.execute(
            "SELECT COALESCE(SUM(CASE WHEN delta>0 THEN delta ELSE 0 END),0) AS tin,"
            "COALESCE(SUM(CASE WHEN delta<0 THEN -delta ELSE 0 END),0) AS tout,"
            "COUNT(*) AS n FROM ledger WHERE uid=?", (uid,)).fetchone()
    items = []
    for r in rows:
        k = _ledger_kind(r["reason"], r["delta"])
        if kind != "all" and k != kind:
            continue
        items.append({"id": r["id"], "delta": r["delta"], "balance": r["balance"],
                      "reason": _ledger_text(r["reason"]), "kind": k, "ts": r["ts"]})
    return {"ok": True, "items": items[:limit], "has_more": len(items) > limit,
            "total": int(agg["n"] or 0),
            "total_in": int(agg["tin"] or 0), "total_out": int(agg["tout"] or 0)}


# ---------------------------------------------------------------- 邀约相关接口
@router.get("/invite/info")
async def invite_info(request: Request):
    """「我的邀请」页数据：我的码 / 已邀请人数 / 累计返利 / 明细。"""
    uid = uid_from_request(request)
    if not uid:
        return JSONResponse({"ok": False, "error": "未登录"}, 401)
    st = invite_stats(uid)
    return {"ok": True, "invite": st}


@router.get("/invite/check")
async def invite_check(ref: str = ""):
    """注册页校验邀请码（公开接口：注册前还没登录）。

    只回脱敏后的邀请人用户名，不泄露任何账号信息。
    """
    code = (ref or "").strip().upper().replace(" ", "")
    if not code:
        return {"ok": False, "error": "缺少邀请码"}
    with _db() as c:
        row = c.execute("SELECT username FROM users WHERE invite_code=?", (code,)).fetchone()
    if not row:
        return {"ok": False, "error": "邀请码不存在"}
    name = row["username"] or ""
    if len(name) >= 3:
        masked = name[0] + "*" * (len(name) - 2) + name[-1]
    elif len(name) == 2:
        masked = name[0] + "*"
    else:
        masked = name
    return {"ok": True, "inviter": masked, "code": code}


@router.get("/admin/invites")
async def admin_invites(request: Request, username: str = "", limit: int = 200):
    """管理后台：全部返利明细 + 谁拉了多少人。"""
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "无权限"}, 403)
    q = ("SELECT r.id,r.inviter_uid,ui.username AS inviter,r.invitee_uid,"
         "ue.username AS invitee,r.kind,r.base_points,r.reward,r.ref,r.ts "
         "FROM invite_rewards r "
         "LEFT JOIN users ui ON ui.id=r.inviter_uid "
         "LEFT JOIN users ue ON ue.id=r.invitee_uid")
    args = []
    if username:
        q += " WHERE ui.username=?"
        args.append(username)
    q += " ORDER BY r.id DESC LIMIT ?"
    args.append(min(limit, 1000))
    with _db() as c:
        rows = c.execute(q, args).fetchall()
        # 邀请人排行：join 回 users 拿「邀请人自己」的名字（不是被邀请人的）
        tops = c.execute(
            "SELECT inv.id AS id, inv.username AS username, COUNT(*) AS invited, "
            "COALESCE((SELECT SUM(reward) FROM invite_rewards WHERE inviter_uid=inv.id),0) AS reward "
            "FROM users u JOIN users inv ON inv.id=u.invited_by "
            "WHERE u.invited_by IS NOT NULL "
            "GROUP BY u.invited_by ORDER BY invited DESC LIMIT 50").fetchall()
    return {"ok": True, "rewards": [dict(r) for r in rows], "top": [dict(r) for r in tops]}
