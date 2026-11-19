# -*- coding: utf-8 -*-
"""详情页复刻 —— FastAPI 子应用（挂载在父应用 /fk 路径下，2026-11-18）。

做什么：上传**别人家的详情页**（按屏截图或一整张长图）+ **我的产品图** →
AI 逐屏拆解版式/配色/字号层级 → 代码拼出逐屏复刻提示词 → 一键出整套同版式详情页。

四步向导：① 上传素材 → ② 拆解参考页 → ③ 复刻提示词 → ④ 出图

为什么这么设计（GitHub 调研结论，2026-11-18）：
  · 现成方案里最贴近需求的是 linkpix-skills 的 `detail-page-clone`（逐屏复刻配方）与
    QIYU `ecommerce-exact-replication`（style-transfer 提示词骨架）；
    两者共同结论 = **必须按屏拆、逐屏出、最后按原顺序拼回**，一次性出长图必然糊。
  · 所以本版块把「长图 → 自动切屏」做成服务器能力（PIL 等分切片），
    并把「拼回长图」做成下载项（/long），中间环节不用人去 PS 里手工切。
  · 拆解用视觉模型、拼装用代码：步骤 2/3 免费且可编辑，只有出图计费（3 积分/张）。

计费（父应用 accounts 中间件）：POST /fk/api/generate = 3 积分/张；
  拆解参考页、生成/编辑提示词、上传、停止、退分、ZIP、长图 **全部免费**。
记录隔离：fk/data/records/u<uid>/ + fk/data/output/u<uid>/，72h TTL。
"""
import io
import json
import os
import re
import shutil
import sys
import threading
import time
import zipfile
import base64

from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from PIL import Image

_HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(_HERE)                     # app/ 根
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import ai_sites                                   # noqa: E402
from pipeline import gen_image, load_key, chat_vision   # noqa: E402

try:
    from fk import prompts as P                   # noqa: E402
except Exception:                                 # noqa: BLE001 —— 独立运行时同目录
    import prompts as P                           # noqa: E402

try:
    from fk import fkstore                        # noqa: E402
except Exception:                                 # noqa: BLE001
    try:
        import fkstore                            # noqa: E402
    except Exception:                             # noqa: BLE001
        fkstore = None
        print("[fk] fkstore 不可用，COS 关闭（全走本地文件服务）")

DATA = os.path.join(_HERE, "data")
RECORDS = os.path.join(DATA, "records")
OUTPUT = os.path.join(DATA, "output")
FRONTEND = os.path.join(_HERE, "frontend")
for _d in (RECORDS, OUTPUT):
    os.makedirs(_d, exist_ok=True)

MAX_REFS = 8                                      # 参考详情页最多 8 屏
MAX_PRODS = 6                                     # 我的产品图最多 6 张
MAX_SCREENS = 12                                  # 单次出图上限
MAX_BYTES = 12 * 1024 * 1024
TTL_HOURS = float(os.environ.get("FK_TTL_HOURS", "72"))
CLEANUP_INTERVAL = int(os.environ.get("FK_CLEANUP_SEC", "900"))
SRC_SIDE = 1400                                   # 送 AI 的图长边上限（出图参考）
ANA_SIDE = 1024                                   # 拆解用图长边上限（省 token）
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

IMAGE_MODEL = os.environ.get("FK_IMAGE_MODEL", "gpt-image-2")
GEN_TIMEOUT = int(os.environ.get("FK_GEN_TIMEOUT", "600"))

app = FastAPI(title="详情页复刻", docs_url=None, redoc_url=None)


# ---------------- 身份 / 记录 ----------------
def _uid(req: Request):
    return int(req.scope.get("st_uid") or 0)


def _is_admin(uid):
    try:
        import accounts
        return accounts.is_admin(uid)
    except Exception:                             # noqa: BLE001
        return uid == 0


def _rec_path(tid, uid=None):
    if uid is None:
        if os.path.isdir(RECORDS):
            for d in os.listdir(RECORDS):
                p = os.path.join(RECORDS, d, tid + ".json")
                if os.path.isfile(p):
                    return p
        return None
    return os.path.join(RECORDS, "u{0}".format(uid), tid + ".json")


def _load_rec(tid, uid):
    """带属主校验；不存在或越权一律 None（对外 404）。admin 全量可见。"""
    p = _rec_path(tid, uid)
    if p and os.path.isfile(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:                         # noqa: BLE001
            return None
    if _is_admin(uid):
        p = _rec_path(tid)
        if p and os.path.isfile(p):
            try:
                return json.load(open(p, encoding="utf-8"))
            except Exception:                     # noqa: BLE001
                return None
    return None


def _save_rec(rec):
    p = _rec_path(rec["tid"], rec["uid"])
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(tmp, p)


def _pick(v, allowed, default):
    v = str(v or "").strip()
    return v if v in allowed else default


def _norm_opt(d):
    """前端参数收敛成程序要的 opt（非法值降级，不 400 —— 与 pt/dtl 同铁律）。"""
    d = d if isinstance(d, dict) else {}
    return {
        "strength": _pick(d.get("strength"), P.STRENGTH_IDS, "layout"),
        "ratio": _pick(d.get("ratio"), P.RATIOS, "3:4"),
        "res": _pick(d.get("res"), P.RESOLUTIONS, "2K"),
        "lang": _pick(d.get("lang"), P.LANGS, "中文"),
        "platform": _pick(d.get("platform"), P.PLATFORMS, "淘宝"),
        "product": str(d.get("product") or "").strip()[:120],
        "selling": str(d.get("selling") or "").strip()[:2000],
        "copy": str(d.get("copy") or "").strip()[:4000],
    }


# ---------------- 尺寸 / 图片 ----------------
def _ratio_px(ratio, res):
    """比例 + 清晰度 → 请求像素（长边 1K=1536 / 2K=3072，宽高取 16 倍数，总像素夹区间）。

    上游只保比例不保像素（pt/hd/dtl 都踩过），所以这里只保证「比例对 + 落在模型允许区间」，
    出图后再由 _fit_ratio 精确校正。
    """
    try:
        a, b = str(ratio).split(":")
        aw, ah = float(a), float(b)
    except Exception:                             # noqa: BLE001
        aw, ah = 3.0, 4.0
    long_edge = 1536 if str(res).upper() == "1K" else 3072
    if aw <= ah:
        w, h = int(round(long_edge * aw / ah)), long_edge
    else:
        w, h = long_edge, int(round(long_edge * ah / aw))
    w = max(256, (w // 16) * 16)
    h = max(256, (h // 16) * 16)
    tot = w * h
    if tot > 8294400:
        s = (8294400.0 / tot) ** 0.5
        w = max(256, (int(w * s) // 16) * 16)
        h = max(256, (int(h * s) // 16) * 16)
    elif tot < 655360:
        s = (655360.0 / tot) ** 0.5
        w = max(256, ((int(w * s) + 15) // 16) * 16)
        h = max(256, ((int(h * s) + 15) // 16) * 16)
    return "%dx%d" % (w, h)


def _fit_ratio(png_bytes, ratio, target=""):
    """出图后按比例精确校正：等比缩放 + 边缘色补边（绝不拉伸变形）。"""
    try:
        a, b = str(ratio).split(":")
        aw, ah = float(a), float(b)
        if aw <= 0 or ah <= 0:
            return png_bytes
    except Exception:                             # noqa: BLE001
        return png_bytes
    tw2 = th2 = 0
    t = str(target or "").strip().lower().replace("×", "x")
    if "x" in t:
        try:
            tw2, th2 = int(t.split("x")[0]), int(t.split("x")[1])
            if tw2 < 64 or th2 < 64:
                tw2 = th2 = 0
        except Exception:                         # noqa: BLE001
            tw2 = th2 = 0
    try:
        im = Image.open(io.BytesIO(png_bytes))
        im.load()
        im = im.convert("RGB")
        w, h = im.size
        want = aw / ah
        got = float(w) / float(h)
        cur = im
        if abs(got - want) >= 0.004:
            if got > want:
                tw, th = w, max(1, int(round(w / want)))
            else:
                tw, th = max(1, int(round(h * want))), h
            edge = im.getpixel((0, 0))
            cur = Image.new("RGB", (tw, th), edge)
            cur.paste(im, ((tw - w) // 2, (th - h) // 2))
        if tw2 and th2:
            cw, ch = cur.size
            if (cw, ch) != (tw2, th2):
                k = min(float(tw2) / cw, float(th2) / ch)
                nw, nh = max(1, int(round(cw * k))), max(1, int(round(ch * k)))
                rs = cur.resize((nw, nh), Image.LANCZOS) if (nw, nh) != (cw, ch) else cur
                edge = cur.getpixel((0, 0))
                out = Image.new("RGB", (tw2, th2), edge)
                out.paste(rs, ((tw2 - nw) // 2, (th2 - nh) // 2))
                cur = out
        if cur is im:
            return png_bytes
        buf = io.BytesIO()
        cur.save(buf, "PNG")
        return buf.getvalue()
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write("[fk] 比例校正失败（已回退原始尺寸）：%s\n" % e)
        return png_bytes


def _save_img(raw, uid, tid, name, side=SRC_SIDE):
    """落盘一张 JPEG（长边 ≤ side）→ 相对 DATA 的路径。"""
    im = Image.open(io.BytesIO(raw))
    im.load()
    im = im.convert("RGB")
    w, h = im.size
    m = max(w, h)
    if m > side:
        s = float(side) / float(m)
        im = im.resize((max(1, int(round(w * s))), max(1, int(round(h * s)))), Image.LANCZOS)
    od = os.path.join(OUTPUT, "u{0}".format(uid), tid)
    os.makedirs(od, exist_ok=True)
    p = os.path.join(od, name)
    im.save(p, "JPEG", quality=92)
    return os.path.relpath(p, DATA).replace("\\", "/")


def _read(rel):
    if not rel:
        return b""
    p = os.path.join(DATA, str(rel).replace("\\", "/").replace("/", os.sep))
    if not os.path.isfile(p):
        return b""
    try:
        return open(p, "rb").read()
    except Exception:                             # noqa: BLE001
        return b""


def _refs_bytes(rec, side=None):
    out = []
    for rel in (rec.get("refs") or []):
        b = _read(rel)
        if not b:
            continue
        if side:
            try:
                im = Image.open(io.BytesIO(b))
                im.load()
                im = im.convert("RGB")
                w, h = im.size
                if max(w, h) > side:
                    k = float(side) / max(w, h)
                    im = im.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=88)
                b = buf.getvalue()
            except Exception:                     # noqa: BLE001
                pass
        out.append(b)
    return out


def _prods_bytes(rec, side=None, limit=None):
    out = []
    for rel in (rec.get("prods") or []):
        b = _read(rel)
        if not b:
            continue
        if side:
            try:
                im = Image.open(io.BytesIO(b))
                im.load()
                im = im.convert("RGB")
                w, h = im.size
                if max(w, h) > side:
                    k = float(side) / max(w, h)
                    im = im.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=88)
                b = buf.getvalue()
            except Exception:                     # noqa: BLE001
                pass
        out.append(b)
    if limit:
        out = out[:limit]
    return out


# ---------------- COS ----------------
def _upload_cdn(uid, tid, rels):
    if fkstore is None:
        return {}
    try:
        return fkstore.upload_task(uid, tid, rels) or {}
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write("[fk] COS 回推失败（已回退本地）：%s\n" % e)
        return {}


def _cdn_of(rec, rel):
    try:
        return (rec.get("cdn") or {}).get(str(rel).replace("\\", "/").lstrip("/")) or ""
    except Exception:                             # noqa: BLE001
        return ""


def _img_resp(rec, rel, missing=None):
    cu = _cdn_of(rec, rel)
    if cu:
        return RedirectResponse(cu, status_code=302,
                                headers={"Cache-Control": "private,max-age=3600"})
    p = os.path.join(DATA, str(rel).replace("/", os.sep))
    if not os.path.isfile(p):
        return JSONResponse({"ok": False, "error": missing or "图片不存在（可能已过 72h 清理）"},
                            status_code=404)
    return FileResponse(p)


# ---------------- 并发闸门 ----------------
# FK_CONCURRENCY 同时是「单批内并行屏数」和「全局上游出图并发上限」：
#   _GEN_SEM 由每屏的生成区间各自占用，所以两批任务同时跑也不会超过这个数（防上游限流）。
FK_CONC = max(1, int(os.environ.get("FK_CONCURRENCY", "2")))
_GEN_SEM = threading.BoundedSemaphore(FK_CONC)
_STOP = {}
_STOP_LOCK = threading.Lock()

# 🔴 fk 记录的「读-改-写」必须串行（2026-11-19 并发化时加）：
#   _save_rec 是 tmp + os.replace —— 写入原子（不会出现半个 JSON），但**事务不原子**，
#   而且 tmp 是固定文件名 p+".tmp"，两个线程同写会互相截断。
#   批内改并发后，所有页状态更新一律走 _patch_page()，禁止在锁外拿着旧 rec 再 _save_rec。
_REC_LOCK = threading.Lock()


def _patch_page(tid, uid, i, upd, cdn=None):
    """在锁内「重读 → 改第 i 屏 → 写回」，避免并发下后写覆盖先写。

    upd: 要合并进 pages[i] 的字段；cdn: 要合并进 rec["cdn"] 的 {rel: url}。
    """
    with _REC_LOCK:
        rec = _load_rec(tid, uid)
        if not rec:
            return None
        pgs = rec.get("pages") or []
        if 0 <= i < len(pgs) and upd:
            pgs[i].update(upd)
        rec["pages"] = pgs
        if cdn:
            rec.setdefault("cdn", {}).update(cdn)
        _beat(rec)
        _save_rec(rec)
        return rec


def _b64_refs(items):
    """参考图 bytes → base64 字符串列表。

    🔴 必须转（2026-11-18 实测踩坑）：`pipeline._gen_once` 的 openai 协议分支
       （huahuaapi / lensapi）对 refs 做的是 `base64.b64decode(r)`，**它假定传进来的是 b64 串**。
       直接把原始 JPEG 字节丢进去 → 解出来是垃圾 → huahua 报「第 1 张参考图：参考图格式识别失败（107 字节…）」，
       多图线的兜底站点等于全废（apib 一没钱就整批 402 失败）。
       转成 b64 串后两条协议都对：apib 走 `_as_data_uri(str)`、openai 走 `b64decode(str)`。
    """
    out = []
    for b in (items or []):
        if not b:
            continue
        try:
            out.append(base64.b64encode(b).decode())
        except Exception:                         # noqa: BLE001
            continue
    return out


def _prod_only_prompt(prompt):
    """单图兜底提示词：把「版式参考图」整段换成「只有我的商品图」。

    🔴 为什么需要（2026-11-19 实况）：apib + huahuaapi 两家余额同时见底后，多图参考线
       没有任何可用站点，用户看到「生图失败（错误码 403）」。单图线（lensapi）还有钱且稳定。
    🔴 为什么不拼图：把「参考屏 + 产品图」拼成一张喂单图线，模型会把拼图原样当成品返回
       （实测成品就是那张拼图）—— 拼图兜底已实测否掉，别再试。
    ✅ 正解：丢掉参考图，版式改用**文字描述**（拆解结果里本来就有 Composition / 版式块 /
       Spacing / Typography），只把「我的产品图」作为 Image 1 送单图线。
    """
    out, skip = [], False
    for ln in str(prompt or "").split("\n"):
        if ln.startswith("Input images:"):
            skip = True
            out.append("Input images:")
            out.append("Image 1：**我的商品身份参考**（形状、材质、颜色、logo、包装以此为准，不得改变外观）")
            continue
        if skip:
            if ln.strip() == "":
                skip = False
                out.append(ln)
            continue
        out.append(ln)
    txt = "\n".join(out)
    return txt.replace(
        "Primary request: **严格复刻参考图的版式**",
        "Primary request: **严格按下方 Composition / 版式块 / Spacing 的文字描述排版**（本次没有版式参考图）")


def _want_stop(tid):
    with _STOP_LOCK:
        return bool(_STOP.get(tid))


# ---------------- 🔴 僵尸任务自愈（2026-11-19，与 dtl 同一套机制）----------------
# 现象：fk 出图失败/中断后**再也点不动「重出」**，提示「整批还在生成中，请等这一批结束」。
# 根因：出图跑 daemon 线程、状态写 JSON。进程重启（部署/OOM/崩溃）后线程没了，
#       但记录里仍是 status=running / pages[i].status=running，
#       而 generate 与 redo 都拿 running 做互斥 → **永久锁死，无任何自愈路径**。
#       实测 fk2611182244193a89cd：p1~p5 done、p6 永久 running、p7/p8 failed，
#       整批 status=running 把 redo 也一起拦死（fk 的 redo 比 dtl 多拦一层整批）。
# 修法与 dtl 一致（三重）：①活线程登记 ②心跳 ③启动全库复位 + sweep 兜底。
_LIVE_JOBS = {}                 # (tid, idx|0) → True，0 表示整批
_LIVE_LOCK = threading.Lock()
STALE_SEC = float(os.environ.get("FK_STALE_SEC", "900"))


def _job_begin(tid, idx=0):
    with _LIVE_LOCK:
        _LIVE_JOBS[(str(tid), int(idx))] = True


def _job_end(tid, idx=0):
    with _LIVE_LOCK:
        _LIVE_JOBS.pop((str(tid), int(idx)), None)


def _job_alive(tid, idx=0):
    with _LIVE_LOCK:
        return bool(_LIVE_JOBS.get((str(tid), int(idx))))


def _any_alive(tid):
    """该任务是否还有任何活线程（整批或任意单屏）。"""
    with _LIVE_LOCK:
        return any(k[0] == str(tid) for k in _LIVE_JOBS)


def _beat(rec):
    rec["beat_ts"] = time.time()
    return rec


def _is_stale(rec, idx=None):
    """running 但本进程无对应活线程 且（无心跳 或 心跳过期）→ 僵死。"""
    tid = rec.get("tid") or ""
    if idx is None:
        if rec.get("status") != "running":
            return False
        alive = _job_alive(tid, 0)
    else:
        pgs = rec.get("pages") or []
        if idx < 1 or idx > len(pgs) or pgs[idx - 1].get("status") != "running":
            return False
        alive = _job_alive(tid, idx) or _job_alive(tid, 0)
    if alive:
        return False
    beat = float(rec.get("beat_ts") or 0)
    if not beat:
        return True
    return (time.time() - beat) > STALE_SEC


def _reap_stale(rec, idx=None, why="服务重启导致生成中断"):
    """把僵死状态复位成 failed，让用户能重出。返回是否有改动。

    🔴 fk 特有：整批 status 必须一并复位，否则 redo 的「整批还在生成中」仍会拦死。
    """
    tid = rec.get("tid") or ""
    msg = "%s，请点「重出」重试。" % why
    pgs = rec.get("pages") or []
    changed = False
    if idx is not None and 1 <= idx <= len(pgs):
        if pgs[idx - 1].get("status") == "running":
            pgs[idx - 1]["status"] = "failed"
            pgs[idx - 1]["err"] = msg
            changed = True
    if idx is None:
        for pg in pgs:
            st = pg.get("status")
            if st == "running":
                pg["status"] = "failed"
                pg["err"] = msg
                changed = True
            elif st == "pending":
                pg["status"] = "skipped"
                changed = True
    # 整批：只要本进程已无该任务的任何活线程，就把 running 复位
    if rec.get("status") == "running" and not _any_alive(tid):
        rec["status"] = "failed"
        rec["err"] = msg
        changed = True
    if changed:
        rec["pages"] = pgs
        rec["n_done"] = len([x for x in pgs if x.get("status") == "done"])
        _beat(rec)
        _save_rec(rec)
        sys.stderr.write("[fk] 僵死任务已复位 tid=%s idx=%s（%s）\n" % (tid, idx, why))
    return changed


def _reap_all_on_boot():
    """启动时扫全库复位（进程刚起，任何 running 都不可能是活的）。"""
    n = 0
    try:
        if not os.path.isdir(RECORDS):
            return
        for d in os.listdir(RECORDS):
            dd = os.path.join(RECORDS, d)
            if not os.path.isdir(dd):
                continue
            for fn in os.listdir(dd):
                if not fn.endswith(".json"):
                    continue
                try:
                    rec = json.load(open(os.path.join(dd, fn), encoding="utf-8"))
                except Exception:                 # noqa: BLE001
                    continue
                if rec.get("status") != "running" and not any(
                        pg.get("status") == "running" for pg in (rec.get("pages") or [])):
                    continue
                if _reap_stale(rec, None, why="服务重启导致生成中断"):
                    n += 1
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write("[fk] 启动僵死清理异常: %s\n" % e)
    if n:
        sys.stderr.write("[fk] 启动时复位 %d 条僵死任务（可重出）\n" % n)


def _auto_refund(rec):
    """服务端兜底退分：未出图的屏按 3 积分/屏退回。

    🔴 为什么必须有这一层（2026-11-18 补）：原来只靠前端轮询结束后调 /refund，
       用户**关掉页面/切走菜单就不会调** → 上游余额不足那批（402）白扣分还没处说理。
       这里在 worker 收尾时自己算一遍；与 /refund 共用 rec["refunded"] 做幂等，
       谁先跑谁生效，不会重复退。
    """
    try:
        import accounts
        pgs = rec.get("pages") or []
        bad = len([p for p in pgs if p.get("status") != "done"])
        already = int(rec.get("refunded") or 0)
        need = max(0, bad - already)
        if need <= 0:
            return 0
        if accounts.is_admin(rec.get("uid") or 0):
            rec["refunded"] = bad
            return 0
        accounts.credit(rec["uid"], need * 3, "详情页复刻·未出图退回 x%d" % need, rec.get("tid") or "")
        rec["refunded"] = bad
        return need * 3
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write("[fk] 自动退分失败：%s\n" % e)
        return 0


# ---------------- 页面 / 元信息 ----------------
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(FRONTEND, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/ping")
def ping():
    return {"ok": True, "board": "fk", "cos": (fkstore.info() if fkstore else {})}


@app.get("/api/meta")
def meta():
    return {"ok": True, "strengths": P.STRENGTHS, "ratios": P.RATIOS,
            "resolutions": P.RESOLUTIONS, "langs": P.LANGS, "platforms": P.PLATFORMS,
            "maxRefs": MAX_REFS, "maxProds": MAX_PRODS, "maxScreens": MAX_SCREENS,
            "model": IMAGE_MODEL}


# ---------------- 步骤1：会话 + 上传 ----------------
def _new_tid():
    return "fk" + time.strftime("%y%m%d%H%M%S") + os.urandom(3).hex()


@app.post("/api/session")
async def new_session(req: Request,
                      refs: list[UploadFile] = File(default=[]),
                      prods: list[UploadFile] = File(default=[])):
    """建复刻会话，可同时带参考屏与产品图。不计费。"""
    uid = _uid(req)
    tid = _new_tid()
    rec = {"tid": tid, "uid": uid, "ts": time.time(), "status": "new",
           "refs": [], "prods": [], "opt": _norm_opt({}), "analysis": None,
           "screens": [], "pages": [], "cdn": {}, "err": ""}
    _save_rec(rec)
    n_ref = n_prod = 0
    for i, f in enumerate((refs or [])[:MAX_REFS]):
        b = await f.read()
        if not b or len(b) > MAX_BYTES:
            continue
        rel = _save_img(b, uid, tid, "ref%d.jpg" % (i + 1))
        rec["refs"].append(rel)
        n_ref += 1
    for i, f in enumerate((prods or [])[:MAX_PRODS]):
        b = await f.read()
        if not b or len(b) > MAX_BYTES:
            continue
        rel = _save_img(b, uid, tid, "prod%d.jpg" % (i + 1))
        rec["prods"].append(rel)
        n_prod += 1
    if rec["refs"]:
        rec["cdn"] = _upload_cdn(uid, tid, rec["refs"] + rec["prods"])
    _save_rec(rec)
    return {"ok": True, "tid": tid, "n_ref": n_ref, "n_prod": n_prod}


@app.post("/api/session/{tid}/upload")
async def add_images(tid: str, req: Request,
                     kind: str = Form(default="ref"),
                     files: list[UploadFile] = File(default=[])):
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    key = "prods" if kind == "prod" else "refs"
    cap = MAX_PRODS if key == "prods" else MAX_REFS
    added, new_rels = 0, []
    for f in (files or []):
        if len(rec[key]) >= cap:
            break
        b = await f.read()
        if not b or len(b) > MAX_BYTES:
            continue
        n = len(rec[key]) + 1
        prefix = "prod" if key == "prods" else "ref"
        rel = _save_img(b, uid, tid, "%s%d_%s.jpg" % (prefix, n, os.urandom(2).hex()))
        rec[key].append(rel)
        new_rels.append(rel)
        added += 1
    if new_rels:
        rec.setdefault("cdn", {}).update(_upload_cdn(uid, tid, new_rels))
        rec["analysis"] = None            # 素材变了 → 旧拆解作废，避免拿旧版式出新图
    _save_rec(rec)
    return {"ok": True, "added": added, "n_ref": len(rec["refs"]), "n_prod": len(rec["prods"])}


@app.post("/api/session/{tid}/remove")
async def remove_image(tid: str, req: Request):
    try:
        d = json.loads((await req.body()).decode("utf-8") or "{}") or {}
    except Exception:                             # noqa: BLE001
        d = {}
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    kind = "prods" if str(d.get("kind")) == "prod" else "refs"
    try:
        idx = int(d.get("idx") or 0)
    except Exception:                             # noqa: BLE001
        idx = 0
    arr = rec.get(kind) or []
    if 0 <= idx < len(arr):
        rel = arr.pop(idx)
        try:
            os.remove(os.path.join(DATA, rel.replace("/", os.sep)))
        except OSError:
            pass
        rec[kind] = arr
        rec["analysis"] = None
        _save_rec(rec)
    return {"ok": True, "n_ref": len(rec.get("refs") or []), "n_prod": len(rec.get("prods") or [])}


@app.post("/api/session/{tid}/slice")
async def slice_long(tid: str, req: Request):
    """把「第 1 张参考图」当整张长图，等分切成 N 屏（替换 refs）。不计费。

    为什么要这个：竞品详情页一般是一整张长图，直接整张送模型会糊成一团；
    逐屏复刻的前提就是先按屏拆。这里在服务端一次切好，用户不用去 PS 里手工切。
    """
    try:
        d = json.loads((await req.body()).decode("utf-8") or "{}") or {}
    except Exception:                             # noqa: BLE001
        d = {}
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    refs = rec.get("refs") or []
    if not refs:
        return JSONResponse({"ok": False, "error": "请先上传参考详情页"}, status_code=400)
    try:
        n = int(d.get("count") or 0)
    except Exception:                             # noqa: BLE001
        n = 0
    n = max(2, min(n or 4, MAX_REFS))
    try:
        raw = _read(refs[0])
        im = Image.open(io.BytesIO(raw))
        im.load()
        im = im.convert("RGB")
    except Exception as e:                        # noqa: BLE001
        return JSONResponse({"ok": False, "error": "参考图读取失败：%s" % str(e)[:80]},
                            status_code=400)
    w, h = im.size
    if h < n * 40:
        return JSONResponse({"ok": False, "error": "长图高度太小，切不出 %d 屏" % n}, status_code=400)
    step = h / float(n)
    new_rels = []
    for i in range(n):
        top = int(round(i * step))
        bot = int(round((i + 1) * step)) if i < n - 1 else h
        piece = im.crop((0, top, w, bot))
        buf = io.BytesIO()
        piece.save(buf, "JPEG", quality=92)
        rel = _save_img(buf.getvalue(), uid, tid, "ref%d_slice%s.jpg" % (i + 1, os.urandom(2).hex()))
        new_rels.append(rel)
    rec["refs"] = new_rels
    rec["analysis"] = None
    rec["slice"] = {"from": 1, "n": n, "src_w": w, "src_h": h}
    rec.setdefault("cdn", {}).update(_upload_cdn(uid, tid, new_rels))
    _save_rec(rec)
    return {"ok": True, "n_ref": len(new_rels), "w": w, "h": h}


@app.get("/api/session/{tid}/src/{kind}/{idx}")
def get_src(tid: str, kind: str, idx: int, req: Request):
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    key = "prods" if kind == "prod" else "refs"
    arr = rec.get(key) or []
    if idx < 0 or idx >= len(arr):
        return JSONResponse({"ok": False, "error": "没有这张图"}, status_code=404)
    return _img_resp(rec, arr[idx])


@app.get("/api/tasks/{tid}/refpng/{idx}")
def task_refpng(tid: str, idx: int, req: Request):
    """该屏「出图时实际用的那张参考图」——按 pages[i].ref_rel 取。

    对比滑块要用它而不是当前 refs[idx]，否则用户出图后重新切屏/增删参考图，
    历史成品的对比图就会指向一张模型从没见过的图。
    """
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    pgs = rec.get("pages") or []
    if idx < 1 or idx > len(pgs):
        return JSONResponse({"ok": False, "error": "没有这一屏"}, status_code=404)
    rel = pgs[idx - 1].get("ref_rel") or ""
    if not rel:
        return JSONResponse({"ok": False, "error": "这一屏没有记录参考图"}, status_code=404)
    return _img_resp(rec, rel)


def _pub_screens(rec):
    out = []
    for i, s in enumerate(rec.get("screens") or []):
        out.append({"idx": i + 1, "no": s.get("no") or (i + 1), "title": s.get("title") or "",
                    "goal": s.get("goal") or "", "enabled": s.get("enabled", True),
                    "prompt": s.get("prompt") or ""})
    return out


def _ref_idx(rec, no):
    """该屏该配哪张参考图 → 返回 refs 下标；没有参考图返回 None。

    🔴 屏号 ≠ 数组下标（跳屏时 pages[i].no 可能不等于 i+1），取图必须按屏号。
       越界回落最后一张 —— `_worker` / `_redo_worker` / 对比滑块三处必须同一套逻辑，
       否则「对比时看到的参考图」和「当初喂给模型的参考图」会不是同一张。
    """
    n = len(rec.get("refs") or [])
    if n <= 0:
        return None
    try:
        k = int(no or 0) - 1
    except Exception:                             # noqa: BLE001
        k = -1
    if k < 0 or k >= n:
        k = n - 1
    return k


def _pub_pages(rec):
    out = []
    tid = rec["tid"]
    for i, pg in enumerate(rec.get("pages") or []):
        no = int(pg.get("no") or (i + 1))
        # 优先用出图时记下的 ref_rel（免疫之后切屏/增删参考图导致的对比错位）
        if pg.get("ref_rel"):
            ref_url = "api/tasks/%s/refpng/%d" % (tid, i + 1)
            ref_no = int(pg.get("ref_no") or 0)
        else:
            k = _ref_idx(rec, no)
            ref_url = ("api/session/%s/src/ref/%d" % (tid, k)) if k is not None else ""
            ref_no = (k + 1) if k is not None else 0
        out.append({"idx": i + 1, "no": no, "status": pg.get("status") or "pending",
                    "err": pg.get("err") or "", "w": pg.get("w") or 0, "h": pg.get("h") or 0,
                    "url": ("api/tasks/%s/png/%d" % (tid, i + 1)) if pg.get("rel") else "",
                    "ref": ref_url if pg.get("rel") else "", "refNo": ref_no,
                    "title": pg.get("title") or "",
                    "fallback": bool(pg.get("fallback"))})
    return out


@app.get("/api/session/{tid}")
def get_session(tid: str, req: Request):
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    return {"ok": True, "tid": tid, "status": rec.get("status"),
            "refs": [("api/session/%s/src/ref/%d" % (tid, i)) for i in range(len(rec.get("refs") or []))],
            "prods": [("api/session/%s/src/prod/%d" % (tid, i)) for i in range(len(rec.get("prods") or []))],
            "opt": rec.get("opt") or {}, "analysis": rec.get("analysis"),
            "screens": _pub_screens(rec), "pages": _pub_pages(rec),
            "n_done": rec.get("n_done") or 0, "slice": rec.get("slice") or {},
            "err": rec.get("err") or ""}


# ---------------- 文案改写守卫（2026-11-19，雪碧反馈「照抄参考页文案」）----------------
# 现象：选「风格统一」出图后，画面文字跟参考页一字不差，没按自己的商品写。
# 根因：拆解模型把 my_* 直接填成了 copy 原句（实测 fk261119120122609e58：
#       my_headline「颜选定制材质」== 主标题 copy，8 屏里过半如此）。
#       而生成提示词只认 my_*，所以「改写」其实没发生 → 画面就照着参考图上的字抄。
# 修法：① 拆解提示词加硬规则（prompts.ANALYZE_SYSTEM 第 3 条）
#       ② 这里做程序检测：逐字雷同超阈值 → 触发**一次纯文本改写**（不重拆、不传图，便宜）
def _norm_txt(s):
    import re as _re                          # 本模块 re 在别处是局部导入，这里自带
    return _re.sub(r"[\s，,。.、·・:：;；！!？?\-—_~～/\\|()（）\[\]【】{}<>《》\"'“”‘’]+", "",
                   str(s or "")).lower()


def _copied_screens(ana):
    """返回 (逐字照抄的屏号列表, 有标题的屏数)。"""
    hit, total = [], 0
    for i, s in enumerate(ana.get("screens") or []):
        head = s.get("my_headline") or ""
        if not str(head).strip():
            continue
        total += 1
        cp = ""
        for b in (s.get("blocks") or []):
            if "主标题" in str(b.get("type") or ""):
                cp = b.get("copy") or ""
                break
        if not cp:
            for b in (s.get("blocks") or []):
                if b.get("copy"):
                    cp = b.get("copy")
                    break
        if cp and _norm_txt(head) == _norm_txt(cp):
            hit.append(i + 1)
    return hit, total


def _rewrite_copied(ana, opt):
    """逐字照抄 → 一次纯文本定向重写。返回 (ana, 改了屏数, 说明)。"""
    hit, total = _copied_screens(ana)
    scr = ana.get("screens") or []
    if not hit or total < 1 or len(hit) * 3 < total:
        return ana, 0, ""                     # 不到 1/3 屏雷同就不折腾（有些屏本来就该一样）
    items = []
    for s in scr:
        cps = [str(b.get("copy"))[:60] for b in (s.get("blocks") or []) if b.get("copy")]
        items.append({"no": int(s.get("no") or 0), "原标题": s.get("my_headline") or "",
                      "原标题下": s.get("my_sub") or "", "原标签": s.get("my_labels") or [],
                      "参考页原句": cps[:6]})
    prod = ana.get("product") or {}
    ctx = {"我的商品": prod.get("name") or opt.get("product") or "",
           "品类": prod.get("category") or "",
           "我的核心卖点": opt.get("selling") or "",
           "我提供的文案": str(opt.get("copy") or "")[:800]}
    sys_p = ("你是电商详情页文案改写员。给你「参考页原句」和「我的商品信息」，把它们改写成我自己的文案。\n"
             "硬规则：1) 输出句不得与任何「参考页原句」逐字相同，必须换措辞/换卖点角度/换句式；\n"
             "2) 字数与原句接近（±30%），保持原有节奏，不要写长；\n"
             "3) 不得出现参考页的品牌名、型号、价格、销量、认证标；\n"
             "4) 只写「我的商品图」里看得见的特征，不许编造看不见的参数；\n"
             "5) 只输出 JSON：{\"screens\":[{\"no\":1,\"my_headline\":\"\",\"my_sub\":\"\",\"my_labels\":[]}]}")
    user_p = json.dumps({"我的商品信息": ctx, "逐屏待改写": items}, ensure_ascii=False)
    txt = chat_vision(sys_p, user_p, images=None, json_mode=True, timeout=150)
    d = json.loads(txt)
    rows = d.get("screens") if isinstance(d, dict) else None
    if not isinstance(rows, list):
        return ana, 0, "改写返回格式不对，已保留原值"
    by_no = {}
    for x in rows:
        if isinstance(x, dict):
            try:
                by_no[int(x.get("no") or 0)] = x
            except Exception:                 # noqa: BLE001
                pass
    changed = 0
    for s in scr:
        x = by_no.get(int(s.get("no") or 0))
        if not x:
            continue
        h = str(x.get("my_headline") or "").strip()[:80]
        if h and _norm_txt(h) != _norm_txt(s.get("my_headline")):
            s["my_headline"] = h
            changed += 1
        sub = str(x.get("my_sub") or "").strip()[:120]
        if sub and _norm_txt(sub) != _norm_txt(s.get("my_sub")):
            s["my_sub"] = sub
        lb = x.get("my_labels")
        if isinstance(lb, list):
            lb = [str(t).strip()[:40] for t in lb if str(t).strip()][:6]
            if lb:
                s["my_labels"] = lb
    left, _ = _copied_screens(ana)
    note = "已按你的商品重写 %d 屏文案" % changed
    if left:
        note += "；仍有 %d 屏跟原文案一样，建议在步骤 2 手改" % len(left)
    return ana, changed, note


# ---------------- 步骤2：拆解参考页（免费） ----------------
@app.post("/api/analyze")
async def analyze(req: Request):
    try:
        d = json.loads((await req.body()).decode("utf-8") or "{}") or {}
    except Exception:                             # noqa: BLE001
        d = {}
    uid = _uid(req)
    tid = str(d.get("tid") or "")
    rec = _load_rec(tid, uid) if tid else None
    if not rec:
        return JSONResponse({"ok": False, "error": "会话不存在，请重新上传"}, status_code=404)
    opt = _norm_opt(d.get("opt") or rec.get("opt") or {})
    rec["opt"] = opt
    refs = _refs_bytes(rec, side=ANA_SIDE)
    prods = _prods_bytes(rec, side=ANA_SIDE, limit=3)
    if not refs:
        return JSONResponse({"ok": False, "error": "请先上传参考详情页（1~8 屏）"}, status_code=400)
    n_ref, n_prod = len(refs), len(prods)
    system, user = P.analyze_messages(opt, n_ref, n_prod)
    warn = ""
    try:
        txt = chat_vision(system, user, images=refs + prods, json_mode=True, timeout=300)
        data = json.loads(txt)
        if not isinstance(data, dict):
            raise ValueError("非对象")
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write("[fk] 拆解失败：%s\n" % str(e)[:300])
        data = {}
        warn = "AI 拆解没成功，已给出空白模板：你可以直接手填版式描述后继续，不影响出图。"
    ana = P.norm_analysis(data, n_ref, opt)
    # 🔴 文案照抄守卫：拆解常把 my_* 填成原句 → 检测到就纯文本重写一次（不重拆、不传图）
    copy_note = ""
    try:
        ana, _n_fix, copy_note = _rewrite_copied(ana, opt)
        sys.stderr.write("[fk] 文案守卫 tid=%s 重写=%d %s\n" % (tid, _n_fix, copy_note))
    except Exception as _e:                       # noqa: BLE001
        sys.stderr.write("[fk] 文案守卫异常（已跳过）：%s\n" % str(_e)[:200])
        copy_note = ""
    # 步骤3 的提示词在这里就一并拼好（确定性，免费），前端可逐屏改
    ana["screens"] = P.build_prompts(ana, opt, n_ref, n_prod)
    rec["analysis"] = ana
    rec["screens"] = ana["screens"]
    rec["n_ref"] = n_ref
    rec["n_prod"] = n_prod
    _save_rec(rec)
    out = {"ok": True, "tid": tid, "analysis": ana, "screens": _pub_screens(rec)}
    if warn:
        out["warn"] = warn
    if copy_note:
        out["copy_note"] = copy_note
    return out


@app.post("/api/analysis/save")
async def save_analysis(req: Request):
    """用户在步骤2 手改了版式/文案 → 存回并重拼提示词（免费）。"""
    try:
        d = json.loads((await req.body()).decode("utf-8") or "{}") or {}
    except Exception:                             # noqa: BLE001
        d = {}
    uid = _uid(req)
    tid = str(d.get("tid") or "")
    rec = _load_rec(tid, uid) if tid else None
    if not rec:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    ana = rec.get("analysis") or {}
    src = d.get("analysis")
    if isinstance(src, dict):
        # 只接受结构化字段，屏数按上传的参考屏数固定，防止前端塞乱七八糟的屏
        n_ref = int(rec.get("n_ref") or len(rec.get("refs") or []) or 1)
        old = {int(s.get("no") or 0): s for s in (ana.get("screens") or [])}
        merged = []
        for i in range(n_ref):
            base = dict(old.get(i + 1) or {})
            inc = (src.get("screens") or [])
            cur = inc[i] if i < len(inc) and isinstance(inc[i], dict) else {}
            for k in ("goal", "layout", "elements", "bg", "my_headline", "my_sub", "title"):
                if k in cur:
                    base[k] = str(cur.get(k) or "")[:1200]
            if isinstance(cur.get("my_labels"), list):
                base["my_labels"] = [str(x)[:60] for x in cur["my_labels"] if str(x).strip()][:6]
            if isinstance(cur.get("brand_marks"), list):
                base["brand_marks"] = [str(x)[:120] for x in cur["brand_marks"] if str(x).strip()][:10]
            base["no"] = i + 1
            base["enabled"] = bool(cur.get("enabled", base.get("enabled", True)))
            merged.append(base)
        if isinstance(src.get("style_dna"), dict):
            for k, v in src["style_dna"].items():
                if k == "palette" and isinstance(v, list):
                    ana.setdefault("style_dna", {})["palette"] = [str(x)[:20] for x in v if str(x).strip()][:5]
                elif k in ("font", "whitespace", "lighting", "graphic", "density"):
                    ana.setdefault("style_dna", {})[k] = str(v or "")[:200]
        if isinstance(src.get("product"), dict):
            for k in ("name", "category", "summary"):
                if k in src["product"]:
                    ana.setdefault("product", {})[k] = str(src["product"].get(k) or "")[:200]
        for k in ("remove_list", "keep_list"):
            if isinstance(src.get(k), list):
                ana[k] = [str(x)[:120] for x in src[k] if str(x).strip()][:20]
        ana["screens"] = merged
    opt = _norm_opt(d.get("opt") or rec.get("opt") or {})
    rec["opt"] = opt
    # 🔴 结构改了就**必须重拼**：否则手改版式/主标题后提示词还是旧的（自测踩过）。
    #    代价是这里会覆盖「逐屏手改过的提示词」，所以前端按钮文案写的是「保存并重拼」。
    for s in (ana.get("screens") or []):
        s["prompt"] = ""
    ana["screens"] = P.build_prompts(ana, opt, rec.get("n_ref") or len(rec.get("refs") or []) or 1,
                                     rec.get("n_prod") or len(rec.get("prods") or []))
    rec["analysis"] = ana
    rec["screens"] = ana["screens"]
    _save_rec(rec)
    return {"ok": True, "screens": _pub_screens(rec)}


@app.post("/api/prompt")
async def make_prompt(req: Request):
    """重拼提示词（改强度/比例/文案后点「重新生成提示词」，免费）。"""
    try:
        d = json.loads((await req.body()).decode("utf-8") or "{}") or {}
    except Exception:                             # noqa: BLE001
        d = {}
    uid = _uid(req)
    tid = str(d.get("tid") or "")
    rec = _load_rec(tid, uid) if tid else None
    if not rec:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    ana = rec.get("analysis") or {}
    if not (ana.get("screens") or []):
        return JSONResponse({"ok": False, "error": "请先拆解参考页"}, status_code=400)
    opt = _norm_opt(d.get("opt") or rec.get("opt") or {})
    rec["opt"] = opt
    force = bool(d.get("force"))
    if force:                                     # 强制按新参数重拼（覆盖手改）
        for s in ana["screens"]:
            s["prompt"] = ""
    ana["screens"] = P.build_prompts(ana, opt, rec.get("n_ref") or len(rec.get("refs") or []) or 1,
                                     rec.get("n_prod") or len(rec.get("prods") or []))
    rec["analysis"] = ana
    rec["screens"] = ana["screens"]
    _save_rec(rec)
    return {"ok": True, "screens": _pub_screens(rec)}


@app.post("/api/prompt/save")
async def save_prompt(req: Request):
    """逐屏手改提示词（不改「第几屏」的序号，序号由后端固定）。"""
    try:
        d = json.loads((await req.body()).decode("utf-8") or "{}") or {}
    except Exception:                             # noqa: BLE001
        d = {}
    uid = _uid(req)
    tid = str(d.get("tid") or "")
    rec = _load_rec(tid, uid) if tid else None
    if not rec:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    scr = rec.get("screens") or []
    inc = d.get("screens") or []
    if not isinstance(inc, list):
        inc = []
    for i, item in enumerate(inc):
        if i >= len(scr) or not isinstance(item, dict):
            continue
        if "prompt" in item:
            scr[i]["prompt"] = str(item.get("prompt") or "")[:8000]
        if "title" in item:
            scr[i]["title"] = str(item.get("title") or "")[:60]
        if "enabled" in item:
            scr[i]["enabled"] = bool(item.get("enabled"))
    rec["screens"] = scr
    if isinstance(rec.get("analysis"), dict):
        rec["analysis"]["screens"] = scr
    _save_rec(rec)
    n_on = len([s for s in scr if s.get("enabled", True) and str(s.get("prompt") or "").strip()])
    return {"ok": True, "n": n_on}


# ---------------- 步骤4：出图（3 积分/张） ----------------
@app.post("/api/generate")
async def generate(req: Request):
    try:
        d = json.loads((await req.body()).decode("utf-8") or "{}") or {}
    except Exception:                             # noqa: BLE001
        d = {}
    uid = _uid(req)
    tid = str(d.get("tid") or "")
    rec = _load_rec(tid, uid) if tid else None
    if not rec:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    if rec.get("status") == "running":
        # 🔴 僵死自愈：本进程无活线程 + 心跳过期 → 复位后放行，而不是永久拦死
        if _is_stale(rec):
            _reap_stale(rec, None)
        else:
            return JSONResponse({"ok": False, "error": "该会话正在生成中"}, status_code=400)
    scr = [s for s in (rec.get("screens") or [])
           if s.get("enabled", True) and str(s.get("prompt") or "").strip()]
    if not scr:
        return JSONResponse({"ok": False, "error": "没有可出的屏：请先拆解并确认提示词"}, status_code=400)
    # 🔴 张数以 body.n 为准并与计费口径严格对齐（父中间件就是按 body.n 扣分）：
    #    客户端只传小 n 就只出前 n 屏 —— 否则「传 n=1 出 12 张」= 白嫖，反过来说
    #    服务端自作主张按全部屏出图也会让扣费与出图不一致。
    try:
        want_n = int(d.get("n") or 0)
    except Exception:                             # noqa: BLE001
        want_n = 0
    if want_n > 0:
        scr = scr[:want_n]
    if len(scr) > MAX_SCREENS:
        scr = scr[:MAX_SCREENS]
    opt = _norm_opt(d.get("opt") or rec.get("opt") or {})
    rec["opt"] = opt
    rec["status"] = "running"
    rec["err"] = ""
    rec["pages"] = [{"status": "pending", "rel": "", "err": "", "w": 0, "h": 0,
                     "no": int(s.get("no") or (i + 1)),
                     "title": s.get("title") or ("第 %d 屏" % (i + 1))} for i, s in enumerate(scr)]
    with _STOP_LOCK:
        _STOP.pop(tid, None)
    _beat(rec)
    _save_rec(rec)
    _job_begin(tid, 0)
    threading.Thread(target=_worker, args=(tid, rec["uid"], scr, opt), daemon=True).start()
    return {"ok": True, "tid": tid, "n": len(scr)}


def _worker(tid, uid, screens, opt):
    """批内并发出图（FK_CONC 路）：每屏 refs = [该屏参考图, 我的产品图...]（参考图只给版式）。

    单屏失败只标记该屏，不中断整批；未出图的屏由前端调 /refund 退分。
    🔴 状态落盘一律走 _patch_page（加锁重读再改），绝不在线程里持旧 rec 直接 _save_rec。
    """
    rec = _load_rec(tid, uid) or {"tid": tid, "uid": uid}
    refs_all = _refs_bytes(rec)
    prods = _prods_bytes(rec, limit=4)
    od = os.path.join(OUTPUT, "u{0}".format(uid), tid)
    os.makedirs(od, exist_ok=True)
    size = _ratio_px(opt["ratio"], opt["res"])
    key = ""
    try:
        key = load_key()
    except Exception:                             # noqa: BLE001
        pass
    def _one(i, scr):
        """出一屏（跑在批内线程池里）。所有状态写入都走 _patch_page 串行落盘。"""
        if _want_stop(tid):
            return
        rec0 = _load_rec(tid, uid)
        if not rec0:
            return
        prompt = str(scr.get("prompt") or "")
        # 该屏对应的参考图：按屏号取（越界回落最后一张），与对比滑块同一套 _ref_idx
        no = int(scr.get("no") or (i + 1))
        k = _ref_idx(rec0, no)
        ref = [refs_all[k]] if (k is not None and k < len(refs_all)) else []
        # 记下这一屏实际用的参考图，供结果区「原图对比」精确取图（免疫后续 refs 变更）
        upd = {"status": "running", "err": ""}
        rl = rec0.get("refs") or []
        if k is not None and k < len(rl):
            upd["ref_rel"] = rl[k]
            upd["ref_no"] = k + 1
        _patch_page(tid, uid, i, upd)
        images = _b64_refs(ref + prods)       # 必须转 b64（见 _b64_refs 注释）
        fb = False
        try:
            # _GEN_SEM 在这里按「一次上游生成」占用 → 全局并发被 FK_CONC 卡住，
            # 多批任务同时跑也不会把上游打爆（原来的批次级闸门改成请求级）。
            with _GEN_SEM:
                try:
                    u = gen_image(key, prompt, size, refs=images or None)
                except Exception as e1:       # noqa: BLE001
                    # 多图线全挂（站点余额见底/协议不符）→ 改用「产品图+文字版式」走单图线
                    if not prods:
                        raise
                    sys.stderr.write("[fk] 第%d屏 多图失败，转「产品图+文字版式」单图兜底：%s\n"
                                     % (i + 1, str(e1)[:160]))
                    u = gen_image(key, _prod_only_prompt(prompt), size,
                                  refs=_b64_refs(prods[:1]))
                    fb = True
                raw = ai_sites.fetch_bytes(u)
            raw = _fit_ratio(raw, opt["ratio"], size)
            p = os.path.join(od, "p%d.png" % (i + 1))
            with open(p, "wb") as f:
                f.write(raw)
            im = Image.open(io.BytesIO(raw))
            rel = os.path.relpath(p, DATA).replace("\\", "/")
            cdn = _upload_cdn(uid, tid, [rel])
            _patch_page(tid, uid, i, {"status": "done", "rel": rel, "w": im.width,
                                      "h": im.height, "err": "", "fallback": fb},
                        cdn=cdn)
        except Exception as e:                # noqa: BLE001
            msg = ai_sites.sanitize(e)
            if _is_admin(uid) and ("403" in str(e) or "402" in str(e)):
                msg += "（管理员提示：上游站点额度不足或已停用，去系统设置充值/切换站点）"
            sys.stderr.write("[fk] 第%d屏失败：%s\n" % (i + 1, str(e)[:200]))
            _patch_page(tid, uid, i, {"status": "failed", "err": msg})

    try:
        with ThreadPoolExecutor(max_workers=FK_CONC) as ex:
            futs = [ex.submit(_one, i, scr) for i, scr in enumerate(screens)]
            for f in futs:
                try:
                    f.result()
                except Exception:             # noqa: BLE001
                    pass
        with _REC_LOCK:
            rec = _load_rec(tid, uid) or rec
            stopped = _want_stop(tid)
            rec["status"] = "stopped" if stopped else "done"
            rec["done_ts"] = time.time()
            pgs = rec.get("pages") or []
            for pg in pgs:
                if pg.get("status") in ("pending", "running"):
                    pg["status"] = "skipped"
            rec["pages"] = pgs
            # 🔴 并发后不能靠计数器累加（_patch_page 会重读记录，本地变量 done 必然对不上），
            #    一律从落盘状态反算，照 _redo_worker 的现成范式。
            rec["n_done"] = len([x for x in pgs if x.get("status") == "done"])
            rec["refunded_auto"] = _auto_refund(rec)      # 服务端兜底退分（不依赖前端）
            _save_rec(rec)
    except Exception as e:                        # noqa: BLE001
        with _REC_LOCK:
            rec = _load_rec(tid, uid) or rec
            rec.update({"status": "failed", "err": ai_sites.sanitize(e)})
            _save_rec(rec)
    finally:
        with _STOP_LOCK:
            _STOP.pop(tid, None)
        _job_end(tid, 0)


@app.post("/api/tasks/{tid}/stop")
def stop_task(tid: str, req: Request):
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    with _STOP_LOCK:
        _STOP[tid] = True
    return {"ok": True, "msg": "已停止出图，已出的屏保留，未出的屏积分不扣。"}


@app.post("/api/tasks/{tid}/refund")
def refund_unused(tid: str, req: Request):
    """未出图的屏退分（停止 / 单屏失败都退）。幂等：只补差额。"""
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    if rec.get("status") in ("running",):
        return JSONResponse({"ok": False, "error": "任务还在进行中"}, status_code=400)
    pgs = rec.get("pages") or []
    bad = len([p for p in pgs if p.get("status") != "done"])
    already = int(rec.get("refunded") or 0)
    need = max(0, bad - already)
    if need <= 0:
        return {"ok": True, "refunded": 0}
    try:
        import accounts
        if accounts.is_admin(rec["uid"]):
            rec["refunded"] = bad
            _save_rec(rec)
            return {"ok": True, "refunded": 0}
        accounts.credit(rec["uid"], need * 3, "详情页复刻·未出图退回 x%d" % need, tid)
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write("[fk] 退分失败：%s\n" % e)
        return JSONResponse({"ok": False, "error": "退分失败，请联系管理员"}, status_code=500)
    rec["refunded"] = bad
    _save_rec(rec)
    return {"ok": True, "refunded": need * 3}


@app.get("/api/tasks/{tid}")
def get_task(tid: str, req: Request):
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    return {"ok": True, "tid": tid, "status": rec.get("status"),
            "pages": _pub_pages(rec), "n_done": rec.get("n_done") or 0,
            "err": rec.get("err") or ""}


@app.get("/api/tasks/{tid}/png/{idx}")
def get_png(tid: str, idx: int, req: Request):
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    pgs = rec.get("pages") or []
    if idx < 1 or idx > len(pgs) or not pgs[idx - 1].get("rel"):
        return JSONResponse({"ok": False, "error": "这一屏还没出图"}, status_code=404)
    return _img_resp(rec, pgs[idx - 1]["rel"])


@app.post("/api/tasks/{tid}/redo")
async def redo_page(tid: str, req: Request):
    """重出：**支持单屏与批量**（2026-11-19 雪碧要求「失败的可以几个图同时重新生成」）。

    body: {"idx": 3}            → 单屏（老前端，行为不变）
          {"idxs": [6, 7, 8]}   → 批量，一次提交多屏，串行出图
    计费在父中间件 accounts.amount_for 的 fk_redo 分支：按 idxs 长度 × 3 积分。
    """
    try:
        d = json.loads((await req.body()).decode("utf-8") or "{}") or {}
    except Exception:                             # noqa: BLE001
        d = {}
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)

    # 🔴 整批 running 拦截：僵死则复位放行（否则失败屏永远重出不了 —— 本次事故）
    if rec.get("status") == "running":
        if _is_stale(rec):
            _reap_stale(rec, None)
            rec = _load_rec(tid, uid) or rec
        elif not _any_alive(tid):
            # 记录说 running 但本进程一个活线程都没有 → 同样视为僵死
            _reap_stale(rec, None, why="生成线程已结束但状态未复位")
            rec = _load_rec(tid, uid) or rec
        else:
            return JSONResponse({"ok": False, "error": "整批还在生成中，请等这一批结束"}, status_code=400)

    scr = rec.get("screens") or []
    pgs = rec.get("pages") or []
    # 解析屏号：idxs 优先，回落单个 idx
    raw = d.get("idxs")
    want = []
    if isinstance(raw, (list, tuple)) and raw:
        for x in raw:
            try:
                want.append(int(x))
            except Exception:                     # noqa: BLE001
                continue
    else:
        try:
            want = [int(d.get("idx") or 0)]
        except Exception:                         # noqa: BLE001
            want = [0]
    # 去重保序 + 范围校验
    seen, idxs = set(), []
    for i in want:
        if i in seen:
            continue
        seen.add(i)
        idxs.append(i)
    if not idxs or idxs == [0]:
        return JSONResponse({"ok": False, "error": "请指定要重出的屏号"}, status_code=400)
    bad = [i for i in idxs if i < 1 or i > len(pgs) or i > len(scr)]
    if bad:
        return JSONResponse({"ok": False, "error": "屏号不对：%s" % bad}, status_code=400)
    if len(idxs) > MAX_SCREENS:
        return JSONResponse({"ok": False,
                             "error": "一次最多重出 %d 屏" % MAX_SCREENS}, status_code=400)
    # 正在跑的屏不能重复提交（真在跑才拦，僵死的先复位）
    busy = []
    for i in idxs:
        if pgs[i - 1].get("status") == "running":
            if _is_stale(rec, i):
                _reap_stale(rec, i)
            else:
                busy.append(i)
    if busy:
        return JSONResponse({"ok": False,
                             "error": "第 %s 屏正在出图中，请等它结束" % busy}, status_code=400)
    rec = _load_rec(tid, uid) or rec
    scr = rec.get("screens") or []
    pgs = rec.get("pages") or []

    opt = _norm_opt(d.get("opt") or rec.get("opt") or {})
    jobs = []
    for i in idxs:
        prompt = str(scr[i - 1].get("prompt") or "")
        if not prompt.strip():
            return JSONResponse({"ok": False,
                                 "error": "第 %d 屏没有提示词，无法重出" % i}, status_code=400)
        jobs.append((i, prompt))
    for i, prompt in jobs:
        scr[i - 1]["prompt"] = prompt
        pgs[i - 1].update({"status": "running", "err": ""})
    rec["screens"] = scr
    rec["pages"] = pgs
    rec["status"] = "running"
    rec["opt"] = opt
    with _STOP_LOCK:
        _STOP.pop(tid, None)
    _beat(rec)
    _save_rec(rec)
    for i, _ in jobs:
        _job_begin(tid, i)
    # 批量走专用 worker（串行，逐屏更新状态）；单屏仍走原 _redo_worker 保持行为
    if len(jobs) == 1:
        i, prompt = jobs[0]
        threading.Thread(target=_redo_worker, args=(tid, uid, i, prompt, opt),
                         daemon=True).start()
    else:
        threading.Thread(target=_redo_many_worker, args=(tid, uid, jobs, opt),
                         daemon=True).start()
    return {"ok": True, "tid": tid, "idx": idxs[0], "idxs": idxs, "n": len(idxs)}


def _redo_many_worker(tid, uid, jobs, opt):
    """批量重出：串行跑多屏，单屏失败只标记该屏并退该屏的分，不中断其余。"""
    try:
        for i, prompt in jobs:
            if _want_stop(tid):
                rec = _load_rec(tid, uid)
                if rec:
                    pgs = rec.get("pages") or []
                    if i - 1 < len(pgs) and pgs[i - 1].get("status") == "running":
                        pgs[i - 1]["status"] = "skipped"
                        rec["pages"] = pgs
                        _beat(rec)
                        _save_rec(rec)
                _job_end(tid, i)
                _refund_one(uid, tid, i)
                continue
            # 复用单屏 worker 的全部逻辑（取参考图/出图/落盘/退分/状态）
            _redo_worker(tid, uid, i, prompt, opt, _release=False, _finalize=False)
            _job_end(tid, i)
    finally:
        for i, _ in jobs:
            _job_end(tid, i)
        # 收尾：整批状态按实际完成度复位，避免又留一个 running
        try:
            rec = _load_rec(tid, uid)
            if rec:
                pgs = rec.get("pages") or []
                if not any(pg.get("status") in ("running", "pending") for pg in pgs):
                    rec["status"] = "done"
                elif not _any_alive(tid):
                    rec["status"] = "failed"
                rec["n_done"] = len([x for x in pgs if x.get("status") == "done"])
                _beat(rec)
                _save_rec(rec)
        except Exception:                         # noqa: BLE001
            pass


def _refund_one(uid, tid, idx):
    """退单屏的 3 积分（管理员本来不扣，跳过）。"""
    try:
        import accounts
        if not accounts.is_admin(uid):
            accounts.credit(uid, 3, "详情页复刻·第%d屏重出未出图退回" % idx, tid)
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write("[fk] 批量重出退分失败 idx=%s：%s\n" % (idx, e))



def _redo_worker(tid, uid, idx, prompt, opt, _release=True, _finalize=True):
    """单屏重出。

    _release/_finalize：批量重出时由 _redo_many_worker 统一管理活线程登记与整批收尾，
    这里就不重复做（否则批量跑到一半就把整批状态改成 done，后续屏的状态会被误判）。
    """
    rec = _load_rec(tid, uid) or {}
    refs_all = _refs_bytes(rec)
    prods = _prods_bytes(rec, limit=4)
    od = os.path.join(OUTPUT, "u{0}".format(uid), tid)
    os.makedirs(od, exist_ok=True)
    size = _ratio_px(opt["ratio"], opt["res"])
    key = ""
    try:
        key = load_key()
    except Exception:                             # noqa: BLE001
        pass
    ref = []
    if refs_all:
        # 用 pages 里记下的屏号取参考图（跳过某些屏时 page 序号 ≠ 屏号，取错就会拿错版式）
        _pg = (rec.get("pages") or [])
        _no = int((_pg[idx - 1].get("no") if idx - 1 < len(_pg) else idx) or idx)
        k = _ref_idx(rec, _no)
        ref = [refs_all[k]] if (k is not None and k < len(refs_all)) else []
        # 重出的屏也要刷新「实际用的参考图」记录
        if k is not None and idx - 1 < len(_pg) and k < len(rec.get("refs") or []):
            _pg[idx - 1]["ref_rel"] = rec["refs"][k]
            _pg[idx - 1]["ref_no"] = k + 1
            rec["pages"] = _pg
            _save_rec(rec)
    fb = False
    try:
        try:
            u = gen_image(key, prompt, size, refs=_b64_refs(ref + prods) or None)
        except Exception as e1:                   # noqa: BLE001
            if not prods:
                raise
            sys.stderr.write("[fk] 重出第%d屏 多图失败，转「产品图+文字版式」单图兜底：%s\n"
                             % (idx, str(e1)[:160]))
            u = gen_image(key, _prod_only_prompt(prompt), size, refs=_b64_refs(prods[:1]))
            fb = True
        raw = ai_sites.fetch_bytes(u)
        raw = _fit_ratio(raw, opt["ratio"], size)
        p = os.path.join(od, "p%d.png" % idx)
        with open(p, "wb") as f:
            f.write(raw)
        im = Image.open(io.BytesIO(raw))
        rel = os.path.relpath(p, DATA).replace("\\", "/")
        rec = _load_rec(tid, uid) or rec
        pgs = rec.get("pages") or []
        if idx - 1 < len(pgs):
            pgs[idx - 1].update({"status": "done", "rel": rel, "w": im.width,
                                 "h": im.height, "err": "", "fallback": fb})
        rec["pages"] = pgs
        cdn = _upload_cdn(uid, tid, [rel])
        rec.setdefault("cdn", {}).update(cdn)
        n_done = len([x for x in pgs if x.get("status") == "done"])
        rec["n_done"] = n_done
        if _finalize:
            # 全部屏都完成 → done；否则若本进程已无活线程则复位，别留 running
            if n_done >= len(pgs):
                rec["status"] = "done"
            elif not _any_alive(tid) and not _job_alive(tid, idx):
                rec["status"] = "failed"
        _beat(rec)
        _save_rec(rec)
    except Exception as e:                        # noqa: BLE001
        rec = _load_rec(tid, uid) or rec
        pgs = rec.get("pages") or []
        if idx - 1 < len(pgs):
            pgs[idx - 1].update({"status": "failed", "err": ai_sites.sanitize(e)})
        rec["pages"] = pgs
        if _finalize:
            rec["status"] = "done"
        # 单屏重出收 3 积分，失败必须原路退回（不依赖前端是否还开着页面）
        try:
            import accounts
            if not accounts.is_admin(uid):
                accounts.credit(uid, 3, "详情页复刻·单屏重出失败退回", tid)
        except Exception as _e:                   # noqa: BLE001
            sys.stderr.write("[fk] 重出退分失败：%s\n" % _e)
        _beat(rec)
        _save_rec(rec)
    finally:
        # 🔴 必须释放活线程登记，否则该屏永远被判「在跑」，自愈不介入、用户又点不动
        if _release:
            _job_end(tid, idx)


def _long_image(rec, uid):
    """把已出的屏按顺序竖拼成一张长图（详情页交付形态）。"""
    pgs = [p for p in (rec.get("pages") or []) if p.get("status") == "done" and p.get("rel")]
    if not pgs:
        return None
    ims = []
    for p in pgs:
        try:
            im = Image.open(os.path.join(DATA, p["rel"].replace("/", os.sep)))
            im.load()
            ims.append(im.convert("RGB"))
        except Exception:                         # noqa: BLE001
            continue
    if not ims:
        return None
    W = max(i.width for i in ims)
    scaled = []
    for im in ims:
        if im.width != W:
            k = float(W) / im.width
            im = im.resize((W, max(1, int(round(im.height * k)))), Image.LANCZOS)
        scaled.append(im)
    H = sum(i.height for i in scaled)
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    y = 0
    for im in scaled:
        canvas.paste(im, (0, y))
        y += im.height
    od = os.path.join(OUTPUT, "u{0}".format(uid), rec["tid"])
    os.makedirs(od, exist_ok=True)
    p = os.path.join(od, "long.png")
    canvas.save(p, "PNG", optimize=True)
    return os.path.relpath(p, DATA).replace("\\", "/")


@app.get("/api/tasks/{tid}/long")
def long_image(tid: str, req: Request):
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    rel = _long_image(rec, uid)
    if not rel:
        return JSONResponse({"ok": False, "error": "还没有出图，无法拼长图"}, status_code=400)
    cu = _cdn_of(rec, rel)
    if cu:
        return RedirectResponse(cu, status_code=302)
    return FileResponse(os.path.join(DATA, rel.replace("/", os.sep)),
                        media_type="image/png", filename="详情页复刻_%s.png" % tid)


@app.get("/api/tasks/{tid}/zip")
def zip_task(tid: str, req: Request, idxs: str = ""):
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    want = None
    if idxs.strip():
        try:
            want = {int(x) for x in idxs.split(",") if x.strip()}
        except Exception:                         # noqa: BLE001
            want = None
    od = os.path.join(OUTPUT, "u{0}".format(uid), tid)
    zp = os.path.join(od, "fk_%s.zip" % tid)
    n = 0
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for i, pg in enumerate(rec.get("pages") or []):
            if pg.get("status") != "done" or not pg.get("rel"):
                continue
            if want is not None and (i + 1) not in want:
                continue
            fp = os.path.join(DATA, pg["rel"].replace("/", os.sep))
            if os.path.isfile(fp):
                z.write(fp, "第%02d屏_%s.png" % (i + 1, (pg.get("title") or "").replace("/", "_")[:20]))
                n += 1
        long_rel = _long_image(rec, uid)
        if long_rel:
            lp = os.path.join(DATA, long_rel.replace("/", os.sep))
            if os.path.isfile(lp):
                z.write(lp, "整套长图.png")
        # 2026-11-19 雪碧要求：包内不再带「提示词.txt」，只留成品图 + 整套长图
    if not n:
        try:
            os.remove(zp)
        except OSError:
            pass
        return JSONResponse({"ok": False, "error": "还没有出图，无法打包"}, status_code=400)
    return FileResponse(zp, media_type="application/zip", filename="详情页复刻_%s.zip" % tid)


@app.post("/api/tasks/delete")
async def delete_tasks(req: Request):
    try:
        d = json.loads((await req.body()).decode("utf-8") or "{}") or {}
    except Exception:                             # noqa: BLE001
        d = {}
    uid = _uid(req)
    tids = d.get("tids") or []
    if not isinstance(tids, list):
        tids = []
    n = 0
    for tid in tids[:50]:
        tid = str(tid)
        if not _ID_RE.match(tid):
            continue
        rec = _load_rec(tid, uid)
        if not rec:
            continue
        if rec.get("status") == "running":
            if _is_stale(rec):
                _reap_stale(rec, None)
            else:
                continue
        try:
            os.remove(_rec_path(tid, rec["uid"]))
        except OSError:
            pass
        shutil.rmtree(os.path.join(OUTPUT, "u{0}".format(rec["uid"]), tid), ignore_errors=True)
        if fkstore is not None:
            try:
                fkstore.delete_task(rec["uid"], tid)
            except Exception:                     # noqa: BLE001
                pass
        n += 1
    return {"ok": True, "deleted": n}


@app.get("/api/tasks")
def list_tasks(req: Request, limit: int = 20):
    uid = _uid(req)
    limit = max(1, min(int(limit or 20), 60))
    out = []
    base = os.path.join(RECORDS, "u{0}".format(uid))
    if not os.path.isdir(base):
        return {"ok": True, "tasks": []}
    files = sorted((os.path.join(base, f) for f in os.listdir(base) if f.endswith(".json")),
                   key=lambda p: os.path.getmtime(p), reverse=True)[:limit]
    for p in files:
        try:
            rec = json.load(open(p, encoding="utf-8"))
        except Exception:                         # noqa: BLE001
            continue
        pgs = rec.get("pages") or []
        thumb = ""
        for i, pg in enumerate(pgs):
            if pg.get("status") == "done" and pg.get("rel"):
                thumb = "api/tasks/%s/png/%d" % (rec["tid"], i + 1)
                break
        out.append({"tid": rec.get("tid"), "ts": rec.get("ts"), "status": rec.get("status"),
                    "n": len(pgs), "n_done": rec.get("n_done") or 0, "thumb": thumb,
                    "title": (rec.get("analysis") or {}).get("product", {}).get("name") or ""})
    return {"ok": True, "tasks": out}


def _sweep():
    while True:
        time.sleep(CLEANUP_INTERVAL)
        try:
            now = time.time()
            n = 0
            keep = set()
            if os.path.isdir(RECORDS):
                for d in os.listdir(RECORDS):
                    dd = os.path.join(RECORDS, d)
                    if not os.path.isdir(dd):
                        continue
                    for fn in os.listdir(dd):
                        if not fn.endswith(".json"):
                            continue
                        p = os.path.join(dd, fn)
                        try:
                            rec = json.load(open(p, encoding="utf-8"))
                        except Exception:         # noqa: BLE001
                            continue
                        _tid = rec.get("tid") or fn[:-5]
                        if rec.get("status") == "running":
                            # 🔴 僵死 running 先复位（否则永久免删 + 永久拦重出）
                            if _is_stale(rec):
                                _reap_stale(rec, None)
                            else:
                                keep.add(_tid)
                                continue
                        if now - (rec.get("ts") or 0) > TTL_HOURS * 3600:
                            shutil.rmtree(os.path.join(OUTPUT, d, _tid), ignore_errors=True)
                            try:
                                os.remove(p)
                            except OSError:
                                pass
                            n += 1
                            if fkstore is not None:
                                try:
                                    fkstore.delete_task(rec.get("uid") or d.lstrip("u"), _tid)
                                except Exception:  # noqa: BLE001
                                    pass
                        else:
                            keep.add(_tid)
            if fkstore is not None and keep:
                try:
                    fkstore.sweep(keep_tids=keep, ttl_hours=TTL_HOURS)
                except Exception as e:            # noqa: BLE001
                    sys.stderr.write("[fk] COS 孤儿清理异常: %s\n" % e)
            if n:
                sys.stderr.write("[fk] TTL 清理 %d 条过期会话\n" % n)
        except Exception as e:                    # noqa: BLE001
            sys.stderr.write("[fk] 清理线程异常: %s\n" % e)


# 🔴 启动即复位僵死任务：进程刚起，任何 running 都不可能是本进程的活线程。
_reap_all_on_boot()
threading.Thread(target=_sweep, daemon=True).start()
