# -*- coding: utf-8 -*-
"""详情页生成工作台 —— FastAPI 子应用（挂载在父应用 /dtl 路径下）。

复刻自 dst.yemanai.cn「电商详情页 → image2详情页」版块（2026-11-18）。
四步向导：上传信息 → 分析产品 → 提示词 → 生成套图。

复刻边界（重要）：
  · 界面 / 全部提示词 / 四步状态机 / 参数语义 = 1:1（提示词从原站 bundle 提原文，见 prompts.py）
  · 原站服务端不可见 → AI 调用改接工作台站点池（pipeline.gen_image / chat_vision），
    计费改接 accounts.py，图床改接 COS（dtlstore）。

计费（父应用 accounts 中间件）：POST /dtl/api/generate = 3 积分/张；
  分析产品与生成提示词**不收费**（2026-11-18 雪碧拍板 D1）。
记录隔离：records/u<uid>/ + output/u<uid>/，72h TTL。
"""
import base64
import base64
import io
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
import zipfile

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from PIL import Image

_HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(_HERE)                     # app/ 根
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import ai_sites                                   # noqa: E402
from pipeline import gen_image, load_key, chat_vision, download   # noqa: E402

try:
    from dtl import prompts as P                  # noqa: E402
    from dtl import arch as A                     # noqa: E402
except Exception:                                 # noqa: BLE001 —— 独立运行时同目录
    import prompts as P                           # noqa: E402
    import arch as A                              # noqa: E402

try:
    from dtl import dtlstore                      # noqa: E402
except Exception:                                 # noqa: BLE001
    try:
        import dtlstore                           # noqa: E402
    except Exception:                             # noqa: BLE001
        dtlstore = None
        print("[dtl] dtlstore 不可用，COS 关闭（全走本地文件服务）")

DATA = os.path.join(_HERE, "data")
RECORDS = os.path.join(DATA, "records")
OUTPUT = os.path.join(DATA, "output")
FRONTEND = os.path.join(_HERE, "frontend")
for _d in (RECORDS, OUTPUT):
    os.makedirs(_d, exist_ok=True)

MAX_IMAGES = 6                                    # 原站 image2 上传上限（页面显示 0/6）
MAX_BYTES = 12 * 1024 * 1024
MAX_PAGES = 15                                    # 原站生成数量 1..15
TTL_HOURS = float(os.environ.get("DTL_TTL_HOURS", "72"))
CLEANUP_INTERVAL = int(os.environ.get("DTL_CLEANUP_SEC", "900"))
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SRC_SIDE = 1400                                   # 送 AI 的产品图长边上限

# 🔴 原站硬约束：image2 模式的生图模型被 useEffect 锁死为 gpt-image-2，UI 不给选。
#    这里照抄（D3 决策：先锁死保证与原站效果一致）。
IMAGE_MODEL = os.environ.get("DTL_IMAGE_MODEL", "gpt-image-2")

PLATFORMS = ["淘宝", "京东", "拼多多", "亚马逊", "tiktok", "Temu", "ozon", "速卖通"]
LANGS = ["无文字", "中文", "繁体中文", "英文", "日文", "泰文", "俄文", "西班牙语"]
REGIONS = ["中国", "美国", "泰国", "马来西亚", "墨西哥", "巴西", "俄罗斯"]
RATIOS = ["1:1", "3:4", "4:3", "9:16", "16:9"]     # 原站 ecommerce 无「不选择」
RESOLUTIONS = ["1K", "2K"]

app = FastAPI(title="详情页生成", docs_url=None, redoc_url=None)


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


# ---------------- 参数归一化（非法值降级，不 400 —— 与 pt 版块同铁律） ----------------
def _pick(v, allowed, default):
    v = str(v or "").strip()
    return v if v in allowed else default


def _norm_opt(d):
    """把前端传来的参数收敛成 prompts 需要的 opt 字典。"""
    try:
        cnt = int(d.get("count") or 1)
    except Exception:                             # noqa: BLE001
        cnt = 1
    cnt = max(1, min(MAX_PAGES, cnt))
    ids = d.get("arch_ids") or []
    if not isinstance(ids, list):
        ids = []
    # 不在这里静默截断：超上限由 /api/prompt 明确报错（前端也有一键对齐与上限提示）。
    # 静默 [:MAX_PAGES] 会让用户「勾了 20 个却只出 15 张」而不知原因。
    ids = [str(x) for x in ids if x]
    extra = d.get("arch_extra") or []
    if not isinstance(extra, list):
        extra = []
    extra = [{"id": str(x.get("id") or ""), "title": str(x.get("title") or ""),
              "subtitle": str(x.get("subtitle") or "")}
             for x in extra if isinstance(x, dict) and x.get("id")][:MAX_PAGES]
    nm = d.get("needs_model")
    if isinstance(nm, str):
        nm = nm.strip().lower() in ("true", "1", "yes", "是", "需要")
    return {
        "platform": _pick(d.get("platform"), PLATFORMS, "淘宝"),
        "lang": _pick(d.get("lang"), LANGS, "中文"),
        "region": _pick(d.get("region"), REGIONS, "中国"),
        "ratio": _pick(d.get("ratio"), RATIOS, "3:4"),
        "res": _pick(d.get("res"), RESOLUTIONS, "1K"),
        "kind": _pick(d.get("kind"), A.TYPES, "电商详情页"),
        "count": cnt,
        "selling": str(d.get("selling") or "").strip()[:4000],
        "product": str(d.get("product") or "").strip()[:200],
        "audience": str(d.get("audience") or "").strip()[:1000],
        "style": str(d.get("style") or "").strip()[:2000],
        "c1": str(d.get("c1") or "").strip()[:20],
        "c2": str(d.get("c2") or "").strip()[:20],
        "c3": str(d.get("c3") or "").strip()[:20],
        "c4": str(d.get("c4") or "").strip()[:20],
        "needs_model": bool(nm),
        "model_prompt": str(d.get("model_prompt") or "").strip()[:1000],
        "arch_ids": ids,
        "arch_extra": extra,
    }


def _ratio_px(ratio, res):
    """比例 + 清晰度 → 请求像素。

    🔴 上游（apib/lensapi）会把尺寸就近吸附，只保比例不保像素 —— 这是 pt/hd 早就踩过的坑。
    所以这里给的像素只求「比例正确 + 落在模型允许区间」，出图后再由 _fit_ratio 精确校正。
    gpt-image-2 要求宽高为 16 的倍数、总像素 655,360~8,294,400。

    2026-11-18 对齐能影3C：档位按**长边**算（1K=1536 / 2K=3072），
    原来按短边 1024 算出 1024×1365，比能影的 1152×1536 少 21% 像素，小字发虚。
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
    # 取 16 的倍数（向下，避免超上限）
    w = max(256, (w // 16) * 16)
    h = max(256, (h // 16) * 16)
    # 总像素夹到允许区间
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
    """出图后按比例精确校正：等比缩放 + 边缘色补边（绝不拉伸变形）。

    与 hd._fit_to_target 同思路。上游只保比例不保像素，故必须做这一步。
    target = 请求像素串（如 "1152x1536"）：给了就把成品精确落到该像素
    （等比 contain 缩放 + 边缘色补边），对齐能影3C 的同档输出尺寸。
    """
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
        if abs(got - want) >= 0.004:              # 比例不对 → 先补边
            if got > want:                        # 太宽 → 补上下
                tw, th = w, max(1, int(round(w / want)))
            else:                                 # 太高 → 补左右
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
        if cur is im:                             # 比例已对且无目标像素 → 不重编码
            return png_bytes
        buf = io.BytesIO()
        cur.save(buf, "PNG")
        return buf.getvalue()
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write("[dtl] 比例校正失败（已回退原始尺寸）：%s\n" % e)
        return png_bytes


def _save_src(raw, uid, tid, idx):
    """产品图落盘（长边 ≤1400 JPEG）→ 相对路径。送 AI 与前端预览共用。"""
    im = Image.open(io.BytesIO(raw))
    im.load()
    im = im.convert("RGB")
    w, h = im.size
    m = max(w, h)
    if m > SRC_SIDE:
        s = float(SRC_SIDE) / float(m)
        im = im.resize((max(1, int(round(w * s))), max(1, int(round(h * s)))), Image.LANCZOS)
    od = os.path.join(OUTPUT, "u{0}".format(uid), tid)
    os.makedirs(od, exist_ok=True)
    p = os.path.join(od, "src%d.jpg" % idx)
    im.save(p, "JPEG", quality=90)
    return os.path.relpath(p, DATA).replace("\\", "/")


def _b64_refs(items):
    """参考图 bytes → base64 字符串列表。

    🔴 必须转（2026-11-18 实测）：`pipeline._gen_once` 的 openai 协议分支（huahuaapi/lensapi）
       对 refs 做 `base64.b64decode(r)`，假定传进来是 b64 串；直接丢原始 JPEG 字节会解出垃圾，
       站点报「参考图格式识别失败（107 字节…）」→ 多图线兜底全废（apib 没钱就整批失败）。
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


def _src_bytes(rec):
    """读回该任务的产品图 bytes（按上传顺序）。缺失的跳过。

    ⚠️ 只给 **chat_vision** 用（它接受 bytes / data URI / base64）。
    生图必须用 `_src_b64()` —— 见下方注释。
    """
    out = []
    for rel in (rec.get("src_rels") or []):
        p = os.path.join(DATA, rel)
        if os.path.isfile(p):
            try:
                out.append(open(p, "rb").read())
            except Exception:                     # noqa: BLE001
                pass
    return out


def _src_b64(rec):
    """读回产品图并转 **base64 字符串**，供 pipeline.gen_image 用。

    🔴🔴 2026-11-18 事故根因（402 表象 / 400 真因）：
      `gen_image(refs=...)` 内部走 `_gen_once` → `base64.b64decode(r)`，
      **期望 base64 字符串**；而本模块原先直接把 `_src_bytes()` 的原始字节喂进去，
      于是 b64decode 把 JPEG 字节当 base64 解，产出乱码二进制 →
      lensapi 回 400「图片格式无效或尺寸超过限制」、huahuaapi 回 500，
      链尾 apib 恰好欠费回 402 → 用户只看到「生图失败（错误码 402）」，
      误以为是余额问题。实测同图同尺寸：bytes 全链 11 跳失败 / base64 首跳 24.3s 成功。

    🔴🔴 2026-11-19 二次事故（**双重编码**）：本函数与调用点的 `_b64_refs()`
      **同时存在**，等于 base64 编了两遍 → `_gen_once` 只解一遍 → 拿到的是
      「base64 文本的字节」而非 JPEG → PIL 无法识别 → 上游又报图片格式无效。
      实证：原图 64954B → 一次 b64 86608 字符（PIL 可解码 1400×1400）
            → 二次 b64 115480 字符（PIL cannot identify image file）。
      **修法：编码只在一个地方做。** 本函数保留（语义清晰、给 worker 用），
      调用点**不再套 `_b64_refs`**，直接 `refs=imgs`。

    对照 `server.py` 的正确调用点：一律 `P.img_to_b64(path)` 后再传。
    `chat_vision` 接受 bytes，所以「分析产品 / 生成提示词」一直正常 ——
    这也是问题只在最后一步生图暴露的原因。
    """
    return [base64.b64encode(b).decode("ascii") for b in _src_bytes(rec)]


# ---------------- COS ----------------
def _upload_cdn(uid, tid, rels):
    if dtlstore is None:
        return {}
    try:
        return dtlstore.upload_task(uid, tid, rels) or {}
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write("[dtl] COS 回推失败（已回退本地）：%s\n" % e)
        return {}


def _cdn_of(rec, rel):
    try:
        return (rec.get("cdn") or {}).get(str(rel).replace("\\", "/").lstrip("/")) or ""
    except Exception:                             # noqa: BLE001
        return ""


def _img_resp(rec, rel, media_type=None, missing=None):
    """有 COS 直链 → 302；否则本机 FileResponse。"""
    cu = _cdn_of(rec, rel)
    if cu:
        return RedirectResponse(cu, status_code=302,
                                headers={"Cache-Control": "private,max-age=3600"})
    p = os.path.join(DATA, rel)
    if not os.path.isfile(p):
        return JSONResponse({"ok": False, "error": missing or "图片不存在（可能已过 72h 清理）"},
                            status_code=404)
    return FileResponse(p, media_type=media_type) if media_type else FileResponse(p)


# ---------------- 并发闸门 ----------------
_GEN_SEM = threading.BoundedSemaphore(int(os.environ.get("DTL_CONCURRENCY", "2")))
_STOP = {}                                        # tid → True 表示请求停止
_STOP_LOCK = threading.Lock()


def _want_stop(tid):
    with _STOP_LOCK:
        return bool(_STOP.get(tid))


# ---------------- 🔴 僵尸任务自愈（2026-11-19）----------------
# 现象：页面生成失败后**再也点不动「重新生成」**，提示「该页正在生成中」/「该会话正在生成中」。
# 根因：出图跑在 daemon 线程里，状态写在 JSON。进程一旦重启/被杀（部署、OOM、崩溃），
#       线程没了但记录里仍是 status=running / pages[i].status=running，
#       而 generate / redo 都用 running 做互斥拦截 → **永久锁死，没有任何自愈路径**。
#       实测 b912b3f4c280 就卡在这个状态（p1 running，其余 pending，服务已重启多次）。
# 修法（三重）：
#   ① 进程内活线程登记 _LIVE_JOBS：只有真在跑的才算 running；
#   ② 启动时扫全库，把「running 但本进程没有对应活线程」的记录判为中断 → 标 failed 并给出可操作文案；
#   ③ 兜底心跳：running 记录带 beat_ts，超过 STALE_SEC 没更新即视为僵死（防进程内线程异常退出未复位）。
_LIVE_JOBS = {}                 # (tid, idx|0) → True，0 表示整批
_LIVE_LOCK = threading.Lock()
STALE_SEC = float(os.environ.get("DTL_STALE_SEC", "900"))   # 15 分钟无心跳即判僵死


def _job_begin(tid, idx=0):
    with _LIVE_LOCK:
        _LIVE_JOBS[(str(tid), int(idx))] = True


def _job_end(tid, idx=0):
    with _LIVE_LOCK:
        _LIVE_JOBS.pop((str(tid), int(idx)), None)


def _job_alive(tid, idx=0):
    with _LIVE_LOCK:
        return bool(_LIVE_JOBS.get((str(tid), int(idx))))


def _beat(rec):
    """给记录打心跳时间戳（保存前调用）。"""
    rec["beat_ts"] = time.time()
    return rec


def _is_stale(rec, idx=None):
    """该任务/该页是否已僵死：本进程无活线程 且（无心跳 或 心跳过期）。

    idx=None 看整批状态；给 idx 看单页。
    """
    tid = rec.get("tid") or ""
    if idx is None:
        if rec.get("status") != "running":
            return False
        alive = _job_alive(tid, 0)
    else:
        pgs = rec.get("pages") or []
        if idx < 1 or idx > len(pgs) or pgs[idx - 1].get("status") != "running":
            return False
        # 单页既可能由整批 worker 跑，也可能由 redo 跑
        alive = _job_alive(tid, idx) or _job_alive(tid, 0)
    if alive:
        return False
    beat = float(rec.get("beat_ts") or 0)
    if not beat:
        return True                      # 老记录没心跳字段 → 本进程又没活线程 → 判僵死
    return (time.time() - beat) > STALE_SEC


def _reap_stale(rec, idx=None, why="服务重启导致生成中断"):
    """把僵死状态复位成 failed，让用户能重新生成。返回是否有改动。"""
    tid = rec.get("tid") or ""
    changed = False
    msg = "%s，请点「重新生成」重试。" % why
    pgs = rec.get("pages") or []
    if idx is None:
        if rec.get("status") == "running":
            rec["status"] = "failed"
            rec["err"] = msg
            changed = True
        for pg in pgs:
            if pg.get("status") in ("running", "pending"):
                pg["status"] = "failed" if pg.get("status") == "running" else "skipped"
                if pg.get("status") == "failed":
                    pg["err"] = msg
                changed = True
    else:
        if 1 <= idx <= len(pgs) and pgs[idx - 1].get("status") == "running":
            pgs[idx - 1]["status"] = "failed"
            pgs[idx - 1]["err"] = msg
            changed = True
        # 整批状态若也挂着 running 一并复位，否则 generate 仍被拦
        if rec.get("status") == "running" and not _job_alive(tid, 0):
            rec["status"] = "failed"
            rec["err"] = msg
            changed = True
    if changed:
        rec["pages"] = pgs
        _beat(rec)
        _save_rec(rec)
        sys.stderr.write("[dtl] 僵死任务已复位 tid=%s idx=%s（%s）\n" % (tid, idx, why))
    return changed


def _reap_all_on_boot():
    """启动时扫全库复位僵死记录（本进程刚起，任何 running 都不可能是活的）。"""
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
                p = os.path.join(dd, fn)
                try:
                    rec = json.load(open(p, encoding="utf-8"))
                except Exception:                 # noqa: BLE001
                    continue
                if rec.get("status") != "running" and not any(
                        (pg.get("status") == "running") for pg in (rec.get("pages") or [])):
                    continue
                if _reap_stale(rec, None, why="服务重启导致生成中断"):
                    n += 1
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write("[dtl] 启动僵死清理异常: %s\n" % e)
    if n:
        sys.stderr.write("[dtl] 启动时复位 %d 条僵死任务（可重新生成）\n" % n)


# ---------------- 页面 / 元信息 ----------------
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(FRONTEND, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/ping")
def ping():
    return {"ok": True, "board": "dtl"}


@app.get("/api/meta")
def meta():
    """前端初始化用的全部选项 + 三套架构（避免前端硬编码，改后端即生效）。"""
    return {"ok": True, "platforms": PLATFORMS, "langs": LANGS, "regions": REGIONS,
            "ratios": RATIOS, "resolutions": RESOLUTIONS, "types": A.TYPES,
            "maxImages": MAX_IMAGES, "maxPages": MAX_PAGES,
            "model": IMAGE_MODEL,
            "arch": {k: A.public(k) for k in A.TYPES},
            "headings": {k: A.heading(k) for k in A.TYPES},
            "defaults": {k: A.defaults(k) for k in A.TYPES}}


# ---------------- 步骤1：建会话 + 上传产品图 ----------------
@app.post("/api/session")
async def new_session(req: Request, images: list[UploadFile] = File(default=[])):
    """建一个详情页会话（tid），可同时带产品图。不计费。

    允许 0 张图（原站允许「只输卖点」也能分析）。
    """
    uid = _uid(req)
    imgs = [f for f in (images or []) if f.filename]
    if len(imgs) > MAX_IMAGES:
        return JSONResponse({"ok": False, "error": "最多上传 %d 张产品图" % MAX_IMAGES},
                            status_code=400)
    tid = uuid.uuid4().hex[:12]
    rels = []
    for i, f in enumerate(imgs):
        raw = await f.read()
        if len(raw) > MAX_BYTES:
            return JSONResponse({"ok": False, "error": "%s 超过单张 12MB 上限" % f.filename},
                                status_code=400)
        try:
            Image.open(io.BytesIO(raw)).load()
        except Exception:                         # noqa: BLE001
            return JSONResponse({"ok": False, "error": "%s 不是有效图片" % f.filename},
                                status_code=400)
        rels.append(_save_src(raw, uid, tid, i + 1))
    rec = {"tid": tid, "uid": uid, "ts": time.time(), "status": "draft",
           "src_rels": rels, "opt": {}, "analysis": {}, "prompt": "",
           "pages": [], "cdn": {}}
    cdn = _upload_cdn(uid, tid, rels)
    rec["cdn"] = cdn
    _save_rec(rec)
    return {"ok": True, "tid": tid, "n": len(rels),
            "images": [{"idx": i + 1, "url": "api/session/%s/src/%d" % (tid, i + 1)}
                       for i in range(len(rels))]}


@app.post("/api/session/{tid}/upload")
async def add_images(tid: str, req: Request, images: list[UploadFile] = File(...)):
    """追加产品图（总数不超 6）。"""
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    rels = list(rec.get("src_rels") or [])
    imgs = [f for f in images if f.filename]
    if len(rels) + len(imgs) > MAX_IMAGES:
        return JSONResponse({"ok": False,
                             "error": "最多 %d 张，当前已有 %d 张" % (MAX_IMAGES, len(rels))},
                            status_code=400)
    for f in imgs:
        raw = await f.read()
        if len(raw) > MAX_BYTES:
            return JSONResponse({"ok": False, "error": "%s 超过 12MB" % f.filename},
                                status_code=400)
        try:
            Image.open(io.BytesIO(raw)).load()
        except Exception:                         # noqa: BLE001
            return JSONResponse({"ok": False, "error": "%s 不是有效图片" % f.filename},
                                status_code=400)
        rels.append(_save_src(raw, rec["uid"], tid, len(rels) + 1))
    rec["src_rels"] = rels
    rec["cdn"].update(_upload_cdn(rec["uid"], tid, rels))
    _save_rec(rec)
    return {"ok": True, "n": len(rels)}


@app.post("/api/session/{tid}/remove")
async def remove_image(tid: str, req: Request):
    """删一张产品图（body: {idx: 1-based}）。删后重排序号，保持 URL 连续。"""
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    try:
        body = json.loads((await req.body()).decode("utf-8") or "{}") or {}
    except Exception:                             # noqa: BLE001
        body = {}
    try:
        idx = int(body.get("idx") or 0)
    except Exception:                             # noqa: BLE001
        idx = 0
    rels = list(rec.get("src_rels") or [])
    if idx < 1 or idx > len(rels):
        return JSONResponse({"ok": False, "error": "序号无效"}, status_code=400)
    old = rels.pop(idx - 1)
    try:
        os.remove(os.path.join(DATA, old))
    except OSError:
        pass
    # 重命名后续文件，保证 src1..srcN 连续
    od = os.path.join(OUTPUT, "u{0}".format(rec["uid"]), tid)
    new_rels = []
    for i, r in enumerate(rels):
        want = os.path.join(od, "src%d.jpg" % (i + 1))
        cur = os.path.join(DATA, r)
        if os.path.abspath(cur) != os.path.abspath(want) and os.path.isfile(cur):
            try:
                os.replace(cur, want)
            except OSError:
                pass
        new_rels.append(os.path.relpath(want, DATA).replace("\\", "/"))
    rec["src_rels"] = new_rels
    rec["cdn"] = _upload_cdn(rec["uid"], tid, new_rels)   # 序号变了 → 整体重推
    _save_rec(rec)
    return {"ok": True, "n": len(new_rels)}


@app.get("/api/session/{tid}/src/{idx}")
def get_src(tid: str, idx: int, req: Request):
    if not _ID_RE.match(tid):
        return JSONResponse({"ok": False}, status_code=404)
    rec = _load_rec(tid, _uid(req))
    rels = (rec or {}).get("src_rels") or []
    if idx < 1 or idx > len(rels):
        return JSONResponse({"ok": False}, status_code=404)
    return _img_resp(rec, rels[idx - 1], media_type="image/jpeg")


@app.get("/api/session/{tid}")
def get_session(tid: str, req: Request):
    """回读会话（刷新页面/回退步骤时恢复状态）。"""
    if not _ID_RE.match(tid):
        return JSONResponse({"ok": False}, status_code=404)
    rec = _load_rec(tid, _uid(req))
    if not rec:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    return {"ok": True, "tid": tid, "opt": rec.get("opt") or {},
            "analysis": rec.get("analysis") or {}, "prompt": rec.get("prompt") or "",
            "n_images": len(rec.get("src_rels") or []),
            "status": rec.get("status"), "pages": _pub_pages(rec)}


# ---------------- 步骤2：分析产品（不计费） ----------------
@app.post("/api/analyze")
async def analyze(req: Request):
    """看产品图 + 卖点 + 参数 → 严格 JSON（产品名/卖点/人群/四色/3套风格/模特/推荐架构）。

    🔴 原站容错必须照抄：JSON 解析失败或接口失败 → **仍然放行到步骤2**，
       架构回落到该类型两个默认项，只给前端一个 warning。否则用户会卡在步骤1。
    """
    try:
        d = json.loads((await req.body()).decode("utf-8") or "{}") or {}
    except Exception:                             # noqa: BLE001
        d = {}
    uid = _uid(req)
    tid = str(d.get("tid") or "")
    rec = _load_rec(tid, uid) if tid else None
    if not rec:
        return JSONResponse({"ok": False, "error": "会话不存在，请重新上传图片"}, status_code=404)
    opt = _norm_opt(d)
    imgs = _src_bytes(rec)
    if not imgs and not opt["selling"]:
        return JSONResponse({"ok": False, "error": "请先上传产品图或输入核心卖点。"},
                            status_code=400)
    rec["opt"] = opt
    _save_rec(rec)

    fallback = {"productName": opt["product"], "coreSellingPoints": opt["selling"],
                "targetAudience": "", "designStyle": "", "primaryColor": "",
                "secondaryColor": "", "accentColor": "", "bgColor": "",
                "designStyleOptions": [], "needsModel": False, "modelPrompt": "",
                "selectedArchitectures": [{"id": i} for i in A.defaults(opt["kind"])]}
    system, user = P.analyze_messages(opt, P.arch_list_text(opt["kind"]))
    warn = ""
    try:
        txt = chat_vision(system, user, images=imgs, json_mode=True, timeout=240)
        data = json.loads(txt)
        if not isinstance(data, dict):
            raise ValueError("非对象")
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write("[dtl] 分析失败（已回落默认架构）：%s\n" % str(e)[:200])
        data = fallback
        warn = "AI 分析未成功，已用默认内容架构，你可以手动调整后继续。"

    ana = _norm_analysis(data, opt)
    rec["analysis"] = ana
    _save_rec(rec)
    out = {"ok": True, "tid": tid, "analysis": ana,
           "arch_ids": ana["arch_ids"], "arch_extra": ana["arch_extra"]}
    if warn:
        out["warn"] = warn
    return out


def _norm_analysis(d, opt):
    """把 AI 的 JSON 收敛成前端可直接用的结构（含原站的内置/自创架构分流）。"""
    def s(k, lim=2000):
        return str(d.get(k) or "").strip()[:lim]

    styles = []
    for i, x in enumerate((d.get("designStyleOptions") or [])[:3]):
        if not isinstance(x, dict):
            continue
        styles.append({
            "id": str(x.get("id") or ("style_%d" % (i + 1))),
            "name": str(x.get("name") or ("设计风格 %d" % (i + 1))),
            "description": str(x.get("description") or ""),
            "primaryColor": str(x.get("primaryColor") or d.get("primaryColor") or "#000000"),
            "secondaryColor": str(x.get("secondaryColor") or d.get("secondaryColor") or "#FFFFFF"),
            "accentColor": str(x.get("accentColor") or d.get("accentColor") or "#FF0000"),
            "bgColor": str(x.get("bgColor") or d.get("bgColor") or "#F7F7F7"),
        })
    nm = d.get("needsModel")
    if isinstance(nm, str):
        nm = nm.strip().lower() in ("true", "是", "需要", "yes")

    # 原站分流：命中内置表 → 只收 id；未命中但有 title → 进「AI 自创架构」
    builtin = {a["id"] for a in A.table(opt["kind"])}
    ids, extra = [], []
    for x in (d.get("selectedArchitectures") or []):
        if isinstance(x, str):
            aid, title, sub = x, x, "自定义架构"
        elif isinstance(x, dict):
            aid = str(x.get("id") or "")
            title = str(x.get("title") or "")
            sub = str(x.get("subtitle") or "")
        else:
            continue
        if not aid:
            continue
        ids.append(aid)
        if aid not in builtin and title:
            extra.append({"id": aid, "title": title, "subtitle": sub})
    if not ids:
        ids = A.defaults(opt["kind"])

    first = styles[0] if styles else {}
    return {
        "product": s("productName", 200) or opt["product"],
        "selling": s("coreSellingPoints", 4000) or opt["selling"],
        "audience": s("targetAudience", 1000),
        "style": s("designStyle", 2000) or first.get("name", ""),
        "c1": s("primaryColor", 20) or first.get("primaryColor", ""),
        "c2": s("secondaryColor", 20) or first.get("secondaryColor", ""),
        "c3": s("accentColor", 20) or first.get("accentColor", ""),
        "c4": s("bgColor", 20) or first.get("bgColor", ""),
        "styles": styles,
        "style_id": first.get("id", ""),
        "needs_model": bool(nm),
        "model_prompt": s("modelPrompt", 1000),
        "arch_ids": ids[:MAX_PAGES],
        "arch_extra": extra[:MAX_PAGES],
    }


# ---------------- 步骤3：生成提示词（不计费） ----------------
@app.post("/api/prompt")
async def make_prompt(req: Request):
    try:
        d = json.loads((await req.body()).decode("utf-8") or "{}") or {}
    except Exception:                             # noqa: BLE001
        d = {}
    uid = _uid(req)
    tid = str(d.get("tid") or "")
    rec = _load_rec(tid, uid) if tid else None
    if not rec:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    opt = _norm_opt(d)
    ids = opt["arch_ids"]
    # 硬校验：架构数必须恰好等于生成数量（与原站一致）。
    # 报错文案指向界面上的「一键对齐 / 自动补足」按钮，让用户知道下一步该点哪。
    if not ids:
        return JSONResponse({"ok": False, "error": "请先选择内容架构。"}, status_code=400)
    if len(ids) > MAX_PAGES:
        return JSONResponse({"ok": False, "error": "已选择 %d 个内容架构，超过单次上限 %d 张，请取消一些架构。"
                            % (len(ids), MAX_PAGES)}, status_code=400)
    if len(ids) > opt["count"]:
        return JSONResponse({"ok": False, "error": "已选择 %d 个内容架构，超过生成数量 %d 张。"
                            "可点「按已选 %d 个生成」一键对齐，或减少勾选。"
                            % (len(ids), opt["count"], len(ids))}, status_code=400)
    if len(ids) < opt["count"]:
        return JSONResponse({"ok": False, "error": "已选择 %d 个内容架构，少于生成数量 %d 张。"
                            "可点「自动补足到 %d 个」一键补齐，或继续勾选。"
                            % (len(ids), opt["count"], opt["count"])}, status_code=400)
    imgs = _src_bytes(rec)
    if not imgs and not opt["selling"]:
        return JSONResponse({"ok": False, "error": "请先上传产品图或输入核心卖点。"}, status_code=400)

    titles = "、".join((A.find(opt["kind"], i, opt["arch_extra"]) or {}).get("title", i)
                      for i in ids)
    system, user = P.plan_messages(opt, titles, IMAGE_MODEL)
    try:
        txt = chat_vision(system, user, images=imgs, json_mode=False, timeout=300)
    except Exception as e:                        # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=502)
    rec["opt"] = opt
    rec["prompt"] = txt
    _save_rec(rec)
    n = P.page_count(txt)
    return {"ok": True, "tid": tid, "prompt": txt, "pages": n,
            "warn": "" if n else "提示词里没识别到「第 N 页」标识，请编辑后再生成图片。"}


@app.post("/api/prompt/save")
async def save_prompt(req: Request):
    """用户手输/编辑提示词后保存（原站「输入详情页提示词」「完成编辑」）。"""
    try:
        d = json.loads((await req.body()).decode("utf-8") or "{}") or {}
    except Exception:                             # noqa: BLE001
        d = {}
    uid = _uid(req)
    tid = str(d.get("tid") or "")
    rec = _load_rec(tid, uid) if tid else None
    if not rec:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    txt = str(d.get("prompt") or "")
    if not txt.strip():
        return JSONResponse({"ok": False, "error": "提示词不能为空"}, status_code=400)
    if d.get("opt"):
        rec["opt"] = _norm_opt(d["opt"])
    rec["prompt"] = txt[:200000]
    _save_rec(rec)
    return {"ok": True, "pages": P.page_count(txt)}


# ---------------- 步骤4：逐页出图（计费 3 积分/张，父中间件先扣） ----------------
def _pub_pages(rec):
    out = []
    for i, pg in enumerate(rec.get("pages") or []):
        out.append({"idx": i + 1, "status": pg.get("status") or "pending",
                    "err": pg.get("err") or "", "w": pg.get("w") or 0, "h": pg.get("h") or 0,
                    "url": ("api/tasks/%s/png/%d" % (rec["tid"], i + 1)) if pg.get("rel") else "",
                    "prompt": pg.get("prompt") or "",
                    "title": pg.get("title") or ""})
    return out


@app.post("/api/generate")
async def generate(req: Request):
    """建出图任务。**张数由提示词里「第 N 页」命中数决定**（原站行为，不是 count）。

    计费在父中间件按 body 里的 n 先扣（accounts.amount_for 的 dtl_gen 分支）。
    """
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
        # 🔴 僵死自愈：本进程没有活线程 + 心跳过期 → 复位后放行，而不是永久拦死
        if _is_stale(rec):
            _reap_stale(rec, None)
        else:
            return JSONResponse({"ok": False, "error": "该会话正在生成中"}, status_code=400)
    prompt = str(d.get("prompt") or rec.get("prompt") or "")
    if not prompt.strip():
        return JSONResponse({"ok": False, "error": "请先生成或输入详情页提示词"}, status_code=400)
    opt = _norm_opt(d.get("opt") or d or rec.get("opt") or {})
    g, pages = P.split_pages(prompt)
    if not pages:
        return JSONResponse({"ok": False,
                             "error": "提示词里没有「第 N 页」标识，无法拆页。请保留该格式后重试。"},
                            status_code=400)
    if len(pages) > MAX_PAGES:
        pages = pages[:MAX_PAGES]
    rec["opt"] = opt
    rec["prompt"] = prompt
    rec["global_spec"] = g
    rec["status"] = "running"
    rec["err"] = ""
    rec["pages"] = [{"status": "pending", "rel": "", "err": "", "w": 0, "h": 0,
                     "prompt": "", "title": ""} for _ in pages]
    with _STOP_LOCK:
        _STOP.pop(tid, None)
    _beat(rec)
    _save_rec(rec)
    _job_begin(tid, 0)                            # 登记活线程（僵死判定依据）
    threading.Thread(target=_worker, args=(tid, rec["uid"], pages, g, opt),
                     daemon=True).start()
    return {"ok": True, "tid": tid, "n": len(pages)}


def _worker(tid, uid, pages, global_spec, opt):
    """逐页串行出图（原站 for 循环语义）：单页失败只标记该页，不中断整批。"""
    rec = _load_rec(tid, uid) or {"tid": tid, "uid": uid}
    imgs = _src_b64(rec)          # 🔴 生图必须 base64 串，不能用 bytes（见 _src_b64 注释）
    od = os.path.join(OUTPUT, "u{0}".format(uid), tid)
    os.makedirs(od, exist_ok=True)
    size = _ratio_px(opt["ratio"], opt["res"])
    key = ""
    try:
        key = load_key()
    except Exception:                             # noqa: BLE001
        pass
    done = 0
    try:
        with _GEN_SEM:
            for i, page_text in enumerate(pages):
                if _want_stop(tid):
                    break
                rec = _load_rec(tid, uid) or rec
                pgs = rec.get("pages") or []
                if i < len(pgs):
                    pgs[i]["status"] = "running"
                    pgs[i]["err"] = ""
                    rec["pages"] = pgs
                    _beat(rec)                # 心跳：证明本进程真在跑
                    _save_rec(rec)
                exec_prompt = P.page_exec_prompt(page_text, opt, IMAGE_MODEL,
                                                 global_spec=global_spec, size=size)
                main_t = P.title_of(P.field(page_text, "必须显示的画面文字") or page_text, "主标题")
                try:
                    u = gen_image(key, exec_prompt, size, refs=imgs or None)
                    raw = ai_sites.fetch_bytes(u)
                    raw = _fit_ratio(raw, opt["ratio"], size)
                    p = os.path.join(od, "p%d.png" % (i + 1))
                    with open(p, "wb") as f:
                        f.write(raw)
                    im = Image.open(io.BytesIO(raw))
                    rel = os.path.relpath(p, DATA).replace("\\", "/")
                    rec = _load_rec(tid, uid) or rec
                    pgs = rec.get("pages") or []
                    if i < len(pgs):
                        pgs[i].update({"status": "done", "rel": rel, "w": im.width,
                                       "h": im.height, "prompt": exec_prompt,
                                       "title": main_t, "err": ""})
                    rec["pages"] = pgs
                    cdn = _upload_cdn(uid, tid, [rel])
                    rec.setdefault("cdn", {}).update(cdn)
                    _save_rec(rec)
                    done += 1
                except Exception as e:            # noqa: BLE001
                    msg = ai_sites.sanitize(e)
                    sys.stderr.write("[dtl] 第%d页失败：%s\n" % (i + 1, str(e)[:200]))
                    rec = _load_rec(tid, uid) or rec
                    pgs = rec.get("pages") or []
                    if i < len(pgs):
                        pgs[i].update({"status": "failed", "err": msg,
                                       "prompt": exec_prompt, "title": main_t})
                    rec["pages"] = pgs
                    _save_rec(rec)
        rec = _load_rec(tid, uid) or rec
        stopped = _want_stop(tid)
        rec["status"] = "stopped" if stopped else "done"
        rec["done_ts"] = time.time()
        # 未出图的页标 skipped（前端显示「未生成」，且父中间件按实际张数退分由 /refund 处理）
        pgs = rec.get("pages") or []
        for pg in pgs:
            if pg.get("status") in ("pending", "running"):
                pg["status"] = "skipped"
        rec["pages"] = pgs
        rec["n_done"] = done
        _save_rec(rec)
    except Exception as e:                        # noqa: BLE001
        rec = _load_rec(tid, uid) or rec
        rec.update({"status": "failed", "err": ai_sites.sanitize(e)})
        _save_rec(rec)
    finally:
        with _STOP_LOCK:
            _STOP.pop(tid, None)
        _job_end(tid, 0)                          # 无论成败都释放登记


@app.post("/api/tasks/{tid}/stop")
def stop_task(tid: str, req: Request):
    """停止生成：保留已生成的图，未生成的标记 skipped（原站语义）。"""
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    with _STOP_LOCK:
        _STOP[tid] = True
    return {"ok": True, "msg": "已停止生图，正在为您保留已生成的图片，未生图的积分未扣除。"}


@app.post("/api/tasks/{tid}/refund")
def refund_unused(tid: str, req: Request):
    """把「未出图的张数」积分退回（停止 / 单页失败都要退，避免用户白扣分）。

    只对普通用户生效；管理员本来 0 扣分，直接返回 0。
    幂等：记录里写 refunded 张数，重复调用只补差额。
    """
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
        accounts.credit(rec["uid"], need * 3, "详情页生成·未出图退回 x%d" % need, tid)
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write("[dtl] 退分失败：%s\n" % e)
        return JSONResponse({"ok": False, "error": "退分失败，请联系管理员"}, status_code=500)
    rec["refunded"] = bad
    _save_rec(rec)
    return {"ok": True, "refunded": need * 3}


@app.get("/api/tasks/{tid}")
def get_task(tid: str, req: Request):
    if not _ID_RE.match(tid):
        return JSONResponse({"ok": False}, status_code=404)
    rec = _load_rec(tid, _uid(req))
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    pgs = rec.get("pages") or []
    done = len([p for p in pgs if p.get("status") == "done"])
    return {"ok": True, "tid": tid, "status": rec.get("status"), "err": rec.get("err") or "",
            "n": len(pgs), "done": done,
            "progress": int(round(done * 100.0 / max(1, len(pgs)))),
            "pages": _pub_pages(rec)}


@app.get("/api/tasks/{tid}/png/{idx}")
def get_png(tid: str, idx: int, req: Request):
    if not _ID_RE.match(tid):
        return JSONResponse({"ok": False}, status_code=404)
    rec = _load_rec(tid, _uid(req))
    pgs = (rec or {}).get("pages") or []
    if idx < 1 or idx > len(pgs) or not pgs[idx - 1].get("rel"):
        return JSONResponse({"ok": False}, status_code=404)
    return _img_resp(rec, pgs[idx - 1]["rel"])


@app.post("/api/tasks/{tid}/redo")
async def redo_page(tid: str, req: Request):
    """单页重新生成（原站每张图的「重新生成」）。计费走父中间件 dtl_redo = 3 分。"""
    try:
        d = json.loads((await req.body()).decode("utf-8") or "{}") or {}
    except Exception:                             # noqa: BLE001
        d = {}
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    try:
        idx = int(d.get("idx") or 0)
    except Exception:                             # noqa: BLE001
        idx = 0
    pgs = rec.get("pages") or []
    if idx < 1 or idx > len(pgs):
        return JSONResponse({"ok": False, "error": "序号无效"}, status_code=400)
    if pgs[idx - 1].get("status") == "running":
        # 🔴 僵死自愈：服务重启后 running 残留会让「重新生成」永久点不动（本次事故）
        if _is_stale(rec, idx):
            _reap_stale(rec, idx)
            rec = _load_rec(tid, uid) or rec
            pgs = rec.get("pages") or []
        else:
            return JSONResponse({"ok": False, "error": "该页正在生成中"}, status_code=400)
    opt = _norm_opt(d.get("opt") or rec.get("opt") or {})
    prompt = rec.get("prompt") or ""
    g, pages = P.split_pages(prompt)
    if idx > len(pages):
        return JSONResponse({"ok": False, "error": "提示词已变更，无法定位该页"}, status_code=400)
    pgs[idx - 1]["status"] = "running"
    pgs[idx - 1]["err"] = ""
    rec["pages"] = pgs
    _beat(rec)
    _save_rec(rec)
    _job_begin(tid, idx)                          # 登记活线程
    threading.Thread(target=_redo_worker,
                     args=(tid, rec["uid"], idx, pages[idx - 1], opt), daemon=True).start()
    return {"ok": True, "tid": tid, "idx": idx}


def _redo_worker(tid, uid, idx, page_text, opt):
    rec = _load_rec(tid, uid) or {}
    imgs = _src_b64(rec)          # 🔴 同上：生图走 base64
    od = os.path.join(OUTPUT, "u{0}".format(uid), tid)
    os.makedirs(od, exist_ok=True)
    size = _ratio_px(opt["ratio"], opt["res"])
    g = (rec.get("global_spec") or "").strip()
    if not g:
        g, _pgs = P.split_pages(rec.get("prompt") or "")
    exec_prompt = P.page_exec_prompt(page_text, opt, IMAGE_MODEL,
                                     global_spec=g, size=size)
    main_t = P.title_of(P.field(page_text, "必须显示的画面文字") or page_text, "主标题")
    try:
        key = load_key()
    except Exception:                             # noqa: BLE001
        key = ""
    try:
        with _GEN_SEM:
            u = gen_image(key, exec_prompt, size, refs=imgs or None)
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
            pgs[idx - 1].update({"status": "done", "rel": rel, "w": im.width, "h": im.height,
                                 "prompt": exec_prompt, "title": main_t, "err": ""})
        rec["pages"] = pgs
        rec.setdefault("cdn", {}).update(_upload_cdn(uid, tid, [rel]))
        _beat(rec)
        _save_rec(rec)
    except Exception as e:                        # noqa: BLE001
        rec = _load_rec(tid, uid) or rec
        pgs = rec.get("pages") or []
        if idx - 1 < len(pgs):
            pgs[idx - 1].update({"status": "failed", "err": ai_sites.sanitize(e)})
        rec["pages"] = pgs
        _beat(rec)
        _save_rec(rec)
    finally:
        # 🔴 必须释放：否则本进程内该页永远被 _job_alive 判为「在跑」，
        #    僵死自愈不会介入，用户又点不动重新生成。
        _job_end(tid, idx)
        # 整批状态若还挂 running 而本进程已无整批线程 → 复位，让 generate 也能再点
        try:
            r2 = _load_rec(tid, uid)
            if r2 and r2.get("status") == "running" and not _job_alive(tid, 0):
                _reap_stale(r2, None, why="单页重跑结束，整批状态已复位")
        except Exception:                         # noqa: BLE001
            pass


@app.get("/api/tasks/{tid}/zip")
def zip_task(tid: str, req: Request, idxs: str = ""):
    """批量下载 ZIP（D6：后端打包，图已在服务器，比前端拉回来再压快得多）。

    idxs = "1,3,5"，空 = 全部已完成的页。
    """
    if not _ID_RE.match(tid):
        return JSONResponse({"ok": False}, status_code=404)
    rec = _load_rec(tid, _uid(req))
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    pgs = rec.get("pages") or []
    want = []
    if idxs.strip():
        for x in idxs.split(","):
            try:
                i = int(x.strip())
            except Exception:                     # noqa: BLE001
                continue
            if 1 <= i <= len(pgs):
                want.append(i)
    else:
        want = [i + 1 for i, p in enumerate(pgs) if p.get("rel")]
    want = [i for i in want if pgs[i - 1].get("rel")]
    if not want:
        return JSONResponse({"ok": False, "error": "没有可下载的图片"}, status_code=400)
    od = os.path.join(OUTPUT, "u{0}".format(rec["uid"]), tid)
    os.makedirs(od, exist_ok=True)
    zp = os.path.join(od, "detail.zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for i in want:
            p = os.path.join(DATA, pgs[i - 1]["rel"])
            if os.path.isfile(p):
                z.write(p, "detail-%02d.png" % i)
    return FileResponse(zp, media_type="application/zip",
                        filename="detail-%s.zip" % tid)


@app.post("/api/tasks/delete")
async def delete_tasks(req: Request):
    uid = _uid(req)
    try:
        body = json.loads((await req.body()).decode("utf-8") or "{}") or {}
    except Exception:                             # noqa: BLE001
        body = {}
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return JSONResponse({"ok": False, "error": "ids 不能为空"}, status_code=400)
    deleted = []
    admin = _is_admin(uid)
    for tid in ids:
        tid = str(tid)
        rec = _load_rec(tid, uid)
        if not rec:
            continue
        owner = rec.get("uid")
        if not admin and owner != uid:
            continue
        shutil.rmtree(os.path.join(OUTPUT, "u{0}".format(owner), tid), ignore_errors=True)
        p = _rec_path(tid, owner)
        if p and os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass
        deleted.append(tid)
        if dtlstore is not None:
            try:
                dtlstore.delete_task(owner, tid)
            except Exception:                     # noqa: BLE001
                pass
    return {"ok": True, "deleted": deleted}


@app.get("/api/tasks")
def list_tasks(req: Request):
    """本用户的会话列表（素材库/历史恢复用）。"""
    uid = _uid(req)
    root = os.path.join(RECORDS, "u{0}".format(uid))
    recs = []
    dirs = []
    if _is_admin(uid) and os.path.isdir(RECORDS):
        dirs = [os.path.join(RECORDS, d) for d in os.listdir(RECORDS)]
    elif os.path.isdir(root):
        dirs = [root]
    for dd in dirs:
        if not os.path.isdir(dd):
            continue
        for fn in os.listdir(dd):
            if fn.endswith(".json"):
                try:
                    recs.append(json.load(open(os.path.join(dd, fn), encoding="utf-8")))
                except Exception:                 # noqa: BLE001
                    pass
    recs.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    out = []
    for r in recs[:60]:
        pgs = r.get("pages") or []
        out.append({"tid": r.get("tid"), "ts": r.get("ts"), "status": r.get("status"),
                    "product": (r.get("opt") or {}).get("product") or "",
                    "kind": (r.get("opt") or {}).get("kind") or "",
                    "n": len(pgs),
                    "done": len([p for p in pgs if p.get("status") == "done"])})
    return {"ok": True, "tasks": out}


# ---------------- 72h TTL 清理 ----------------
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
                            # 🔴 僵死 running 先复位（否则它会永久免删、又永久拦重生成）
                            if _is_stale(rec):
                                _reap_stale(rec, None)
                            else:
                                keep.add(_tid)
                                continue          # 真在跑 → 永不删
                        if now - (rec.get("ts") or 0) > TTL_HOURS * 3600:
                            shutil.rmtree(os.path.join(OUTPUT, d, _tid), ignore_errors=True)
                            try:
                                os.remove(p)
                            except OSError:
                                pass
                            n += 1
                            if dtlstore is not None:
                                try:
                                    dtlstore.delete_task(rec.get("uid") or d.lstrip("u"), _tid)
                                except Exception:  # noqa: BLE001
                                    pass
                        else:
                            keep.add(_tid)
            if dtlstore is not None and keep:
                try:
                    dtlstore.sweep(keep_tids=keep, ttl_hours=TTL_HOURS)
                except Exception as e:            # noqa: BLE001
                    sys.stderr.write("[dtl] COS 孤儿清理异常: %s\n" % e)
            if n:
                sys.stderr.write("[dtl] TTL 清理 %d 条过期会话\n" % n)
        except Exception as e:                    # noqa: BLE001
            sys.stderr.write("[dtl] 清理线程异常: %s\n" % e)


# 🔴 启动即复位僵死任务：进程刚起，任何 running 都不可能是本进程的活线程。
#    不做这一步，部署/重启后用户就再也点不动「重新生成」（2026-11-19 事故）。
_reap_all_on_boot()
threading.Thread(target=_sweep, daemon=True).start()