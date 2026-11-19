# -*- coding: utf-8 -*-
"""AI 生图工作台 —— FastAPI 子应用（挂载在父应用 /aigc 路径下）。

复刻自 dst.yemanai.cn「AI生图」版块（2026-11-18 第二轮，雪碧「按推荐来」）。

════════ 两个模式，语义完全不同（不是简单的一变多）════════
标题叫「单张生图 / 批量生图」，容易误会成张数区别，实际是**两条链路**：

  单张生图（参考图 ≤ 6）           → 输出 **1 张**
      用 `@图片1 @图片2` 可以把多张图**融合**成一张 —— 这才是「多图参考生图」。
      不写 @ 就把所有上传图都当参考。

  批量生图（参考图 ≤ 10）          → 输出 **N 张，N = 参考图张数**（只读，不可改）
      每张**用自己的图**当参考，各出一张；同一句提示词套到每一张。
      这是「批量各自生成」，**不融合**。

════════ 提示词：原站没有 AI 生成环节 ════════
详情页有两段很重的 AI 提示词生成（分析产品 + 视觉总监），AI 生图**完全没有**——
用户自己写提示词，后端只套一层执行模板。模板是原站 bundle 里 `Ko()` 的原文：

    【最高优先级：画布比例】
    {画布锁定文案}

    【生图提示词】：{用户原文，@图片N 改成 图片N}

    尺寸比例：3:4，清晰度：1K

（不选比例时那行变成「清晰度：1K」；banana 系模型且选了比例时，最前面再加一段
 CANVAS_ASPECT_RATIO 硬约束）。文案全部复用 dtl/prompts.py 的 canvas_block /
 banana_prefix，与详情页同一套，改一处两边都生效。

════════ @ 引用（原站 Uv / fj 原文逻辑）════════
  解析：`/@图片(\\d+)/g` → 去重 → 减 1 得 0-based 索引
  越界：后端拒（「提示词引用的图片不存在」），前端也会先拦
  删图：被删的那张引用置空，比它大的编号减 1，比它小的不动
        —— 不复刻这条，删一张图整个提示词的引用就全错位

════════ 计费（父应用 accounts 中间件）════════
  出图 POST /aigc/api/generate = 3 积分/张（single 1 张、batch N 张）
  单张重生成 POST /aigc/api/tasks/<tid>/redo = 3 积分
  未出图的张数走 /refund 自动退回；管理员不扣分。
记录隔离：records/u<uid>/ + output/u<uid>/，72h TTL。
"""
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

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from PIL import Image

_HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(_HERE)                     # app/ 根
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import ai_sites                                   # noqa: E402
from pipeline import gen_image, load_key          # noqa: E402

try:                                              # 画布锁定 / banana 前缀与详情页共用一套
    from dtl import prompts as P                  # noqa: E402
except Exception:                                 # noqa: BLE001
    P = None
    print("[aigc] dtl.prompts 不可用，画布锁定将回退到内置精简版")

try:
    from aigc import aigcstore
except Exception:                                 # noqa: BLE001
    try:
        import aigcstore                          # noqa: E402
    except Exception:                             # noqa: BLE001
        aigcstore = None
        print("[aigc] aigcstore 不可用，COS 关闭（全走本地文件服务）")

DATA = os.path.join(_HERE, "data")
RECORDS = os.path.join(DATA, "records")
OUTPUT = os.path.join(DATA, "output")
FRONTEND = os.path.join(_HERE, "frontend")
for _d in (RECORDS, OUTPUT):
    os.makedirs(_d, exist_ok=True)

# ── 原站常量（bundle 原文核对过）────────────────────────────────
MODES = ("single", "batch")
MAX_IMAGES = {"single": 6, "batch": 10}           # 原站 tr(): aigc → single?6:10
RATIOS = ["不选择", "1:1", "3:4", "4:3", "9:16", "16:9"]   # 原站 pc 的 aigc 分支
RESOLUTIONS = ["1K", "2K"]                        # 原站 Br
MAX_BYTES = 12 * 1024 * 1024
SRC_SIDE = 1400                                   # 送 AI 的参考图长边上限
TTL_HOURS = float(os.environ.get("AIGC_TTL_HOURS", "72"))
CLEANUP_INTERVAL = int(os.environ.get("AIGC_CLEANUP_SEC", "900"))
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# ── 模型清单 ───────────────────────────────────────────────────
# 原站列 5 个（gpt-image-2 / gpt-image-2.5 / Seedream 5.5 / banana 2.1 / Nano banana pro），
# 但**工作台站点池里没有 Seedream 5.5，也没有 banana pro**（2026-11-18 实测三站 /models）。
# 列出来选了也出不来图，所以按「原站有 & 工作台也有」的原则落成下面 5 条，
# 并把 Seedream 5.5 换成工作台更新一代的 2.6（原站没有但体验更好，雪碧 D2「后台可调」）。
MODELS = [
    {"id": "gpt-image-2", "label": "GPT-Image-2", "note": "原站默认 · 最稳"},
    {"id": "gpt-image-2.5", "label": "GPT-Image-2.5", "note": "细节更细"},
    {"id": "gpt-image-2.6", "label": "GPT-Image-2.6", "note": "最新一代"},
    {"id": "gemini-nano-banana-2.1", "label": "Banana 2.1", "note": "原站 banana 2.1"},
    {"id": "gemini-3-pro-image-preview", "label": "Nano Banana Pro", "note": "Pro 级质感"},
]
DEFAULT_MODEL = os.environ.get("AIGC_DEFAULT_MODEL", "gpt-image-2")
# 显式指定不生效时的兜底提示（不写死在前端，改这里即可）
MODEL_FALLBACK_NOTE = "所选模型当前不可用，已自动切换到可用模型出图。"

app = FastAPI(title="AI 生图", docs_url=None, redoc_url=None)


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


# ---------------- 参数归一化（非法值降级，不 400） ----------------
def _pick(v, allowed, default):
    v = str(v or "").strip()
    return v if v in allowed else default


def _norm_mode(v):
    return "batch" if str(v or "").strip() == "batch" else "single"


def _norm_opt(d):
    mode = _norm_mode(d.get("mode"))
    return {
        "mode": mode,
        "ratio": _pick(d.get("ratio"), RATIOS, "3:4"),
        "res": _pick(d.get("res"), RESOLUTIONS, "1K"),
        "model": _pick(d.get("model"), [m["id"] for m in MODELS], DEFAULT_MODEL),
        "prompt": str(d.get("prompt") or "")[:8000],
    }


# ---------------- @ 引用（原站 Uv / fj 原文逻辑） ----------------
RE_REF = re.compile(r"@图片(\d+)")


def refs_of(prompt):
    """原站 `Uv()`：/@图片(\\d+)/g → 去重 → 减 1 → 0-based 索引列表（保持出现顺序）。"""
    out = []
    for m in RE_REF.finditer(prompt or ""):
        try:
            v = int(m.group(1)) - 1
        except Exception:                         # noqa: BLE001
            continue
        if v not in out:
            out.append(v)
    return out


def reindex_refs(prompt, removed):
    """原站 `fj(h, s)`：删掉第 s 张（0-based）后重排引用。

    被删那张 → 置空；编号更大的 → 减 1；更小的 → 原样。
    🔴 不照抄这条，删一张图后整段提示词的 @图片 全错位（原站为此专门写了函数）。
    """
    def rep(m):
        try:
            d = int(m.group(1)) - 1
        except Exception:                         # noqa: BLE001
            return m.group(0)
        if d == removed:
            return ""
        if d > removed:
            return "@图片%d" % d
        return m.group(0)
    return RE_REF.sub(rep, prompt or "")


# ---------------- 提示词包装（原站 Ko 原文） ----------------
def _canvas_fallback(ratio, obj):
    """dtl.prompts 不可用时的精简兜底（文案与 prompts.canvas_lock 一致）。"""
    r = str(ratio or "").strip()
    if not r or r == "不选择":
        return "不限制画布比例。"
    return ("最高优先级：必须严格生成 %s 画布，%s必须输出所选尺寸比例对应的画布。"
            "不得输出其它比例，不允许生成结果更高、更矮、更宽或更窄；"
            "构图、主体占比、文字安全区和参考图版式都必须适配 %s。"
            "如果参考图或上传图原始比例与 %s 不一致，只能通过重排构图、裁切或补背景适配目标画布，"
            "不得改变最终画布比例。") % (r, obj, r, r)


def wrap_prompt(prompt, ratio, res, batch, model):
    """原站 `Ko(h, E)` 原文组装。

        `${ar(批量?"所有批量生成图片":"图片")}`
        `【生图提示词】：${h.replace(/@图片(\\d+)/g,"图片$1")}`
        `${E}清晰度：${on}`   ← E = 选了比例时 "尺寸比例：X，"，否则 ""

    banana 系模型且选了比例 → 最前面再加 CANVAS_ASPECT_RATIO 硬约束（原站 et()）。
    """
    obj = "所有批量生成图片" if batch else "图片"
    if P is not None:
        canvas = P.canvas_block(ratio, obj)
        ban = P.banana_prefix(ratio, model)
    else:
        t = _canvas_fallback(ratio, obj)
        canvas = "【最高优先级：画布比例】\n%s\n\n【画布比例复核】\n%s" % (t, t)
        r = str(ratio or "").strip()
        if "banana" in str(model or "").lower() and r and r != "不选择":
            ban = "\n".join([
                "CANVAS_ASPECT_RATIO=%s" % r,
                "最高优先级：最终图片必须严格为 %s 画布比例。" % r,
                "不要输出其他比例，不要因为参考图比例、产品图比例或版式参考改变最终画布比例。",
                "如参考图比例不一致，只重排构图和留白来适配目标比例，最终画布仍必须保持上述比例。",
            ])
        else:
            ban = ""
    body = RE_REF.sub(lambda m: "图片" + m.group(1), prompt or "")
    tail = ("尺寸比例：%s，" % ratio) if (ratio and ratio != "不选择") else ""
    out = "%s\n\n【生图提示词】：%s\n\n%s清晰度：%s" % (canvas, body, tail, res)
    return (ban + "\n\n" + out) if ban else out


# ---------------- 尺寸 ----------------
def _ratio_px(ratio, res):
    """比例 + 清晰度 → 请求像素。

    🔴 上游（apib/lensapi）会把尺寸就近吸附，只保比例不保像素 —— pt/hd/dtl 早踩过。
    这里给的像素只求「比例正确 + 落在模型允许区间」，出图后由 _fit_ratio 精确校正。
    gpt-image-2 要求宽高为 16 的倍数、总像素 655,360~8,294,400。
    「不选择」→ 方形默认（原站此时不传 aspect_ratio 交给模型，我们的上游必须给值）。
    """
    r = str(ratio or "").strip()
    if not r or r == "不选择":
        return "1024x1024"
    try:
        a, b = r.split(":")
        aw, ah = float(a), float(b)
    except Exception:                             # noqa: BLE001
        aw, ah = 3.0, 4.0
    short = 1024 if str(res).upper() == "1K" else 2048
    if aw <= ah:
        w, h = short, int(round(short * ah / aw))
    else:
        w, h = int(round(short * aw / ah)), short
    w = max(256, int(round(w / 16.0)) * 16)
    h = max(256, int(round(h / 16.0)) * 16)
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


def _fit_ratio(png_bytes, ratio):
    """出图后按比例精确校正：等比缩放 + 边缘色补边（绝不拉伸变形）。

    「不选择」直接原样返回（用户没要求比例，不该硬掰）。
    """
    r = str(ratio or "").strip()
    if not r or r == "不选择":
        return png_bytes
    try:
        a, b = r.split(":")
        aw, ah = float(a), float(b)
        if aw <= 0 or ah <= 0:
            return png_bytes
    except Exception:                             # noqa: BLE001
        return png_bytes
    try:
        im = Image.open(io.BytesIO(png_bytes))
        im.load()
        im = im.convert("RGB")
        w, h = im.size
        want = aw / ah
        got = float(w) / float(h)
        if abs(got - want) < 0.004:               # 已经对了就不动（避免无谓重编码）
            return png_bytes
        if got > want:                            # 太宽 → 补上下
            tw, th = w, max(1, int(round(w / want)))
        else:                                     # 太高 → 补左右
            tw, th = max(1, int(round(h * want))), h
        edge = im.getpixel((0, 0))
        out = Image.new("RGB", (tw, th), edge)
        out.paste(im, ((tw - w) // 2, (th - h) // 2))
        buf = io.BytesIO()
        out.save(buf, "PNG")
        return buf.getvalue()
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write("[aigc] 比例校正失败（已回退原始尺寸）：%s\n" % e)
        return png_bytes


# ---------------- 参考图落盘 ----------------
def _save_src(raw, uid, tid, idx):
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


def _src_bytes(rec):
    """按上传顺序读回参考图 bytes。缺失的跳过（索引会错位，所以宁可全读）。"""
    out = []
    for rel in (rec.get("src_rels") or []):
        p = os.path.join(DATA, rel)
        if os.path.isfile(p):
            try:
                out.append(open(p, "rb").read())
            except Exception:                     # noqa: BLE001
                out.append(b"")
        else:
            out.append(b"")
    # 尾部空的裁掉（保持索引对应前 N 张）
    while out and not out[-1]:
        out.pop()
    return out


# ---------------- COS ----------------
def _upload_cdn(uid, tid, rels):
    if aigcstore is None:
        return {}
    try:
        return aigcstore.upload_task(uid, tid, rels) or {}
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write("[aigc] COS 回推失败（已回退本地）：%s\n" % e)
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
_GEN_SEM = threading.BoundedSemaphore(int(os.environ.get("AIGC_CONCURRENCY", "2")))
_STOP = {}
_STOP_LOCK = threading.Lock()


def _want_stop(tid):
    with _STOP_LOCK:
        return bool(_STOP.get(tid))


# ---------------- 模型可用性 ----------------
def _pool_models():
    """站点池里真实存在的图像模型全集（用于标注哪些模型现在能用）。"""
    out = set()
    try:
        for s in ai_sites.load_sites():
            if not s.get("enabled", True):
                continue
            for m in (s.get("models") or []):
                if m:
                    out.add(str(m))
    except Exception:                             # noqa: BLE001
        pass
    return out


def _model_chain(want):
    """把「用户选的模型」翻译成 gen_image 的 models 参数。

    池里有 → (所选,)（gen_image 内部会自动拼上配置链兜底）；
    池里没有 → None（走默认链，并让调用方回一句「已切换」）。
    """
    want = str(want or "").strip()
    if not want:
        return None
    return (want,) if want in _pool_models() else None


# ---------------- 页面 / 元信息 ----------------
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(FRONTEND, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/ping")
def ping():
    return {"ok": True, "board": "aigc"}


@app.get("/api/meta")
def meta():
    """前端初始化的全部选项。模型带 available 标记，前端据此置灰。"""
    pool = _pool_models()
    models = []
    for m in MODELS:
        d = dict(m)
        d["available"] = (m["id"] in pool) if pool else True
        models.append(d)
    return {"ok": True, "ratios": RATIOS, "resolutions": RESOLUTIONS,
            "maxImages": MAX_IMAGES, "models": models, "defaultModel": DEFAULT_MODEL,
            "fallbackNote": MODEL_FALLBACK_NOTE}


# ---------------- 会话（上传参考图） ----------------
@app.post("/api/session")
async def new_session(req: Request, images: list[UploadFile] = File(default=[]), mode: str = "single"):
    """建会话，可同时带参考图。不计费。上限按 mode 走（single 6 / batch 10）。"""
    uid = _uid(req)
    md = _norm_mode(mode)
    cap = MAX_IMAGES[md]
    imgs = [f for f in (images or []) if f.filename]
    if len(imgs) > cap:
        return JSONResponse({"ok": False, "error": "%s最多上传 %d 张参考图"
                            % ("单张生图" if md == "single" else "批量生图", cap)}, status_code=400)
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
    rec = {"tid": tid, "uid": uid, "ts": time.time(), "status": "draft", "mode": md,
           "src_rels": rels, "opt": {}, "prompt": "", "items": [], "cdn": {}, "refunded": 0}
    rec["cdn"] = _upload_cdn(uid, tid, rels)
    _save_rec(rec)
    return {"ok": True, "tid": tid, "n": len(rels),
            "images": [{"idx": i + 1, "url": "api/session/%s/src/%d" % (tid, i + 1)}
                       for i in range(len(rels))]}


@app.post("/api/session/{tid}/upload")
async def add_images(tid: str, req: Request, images: list[UploadFile] = File(...), mode: str = ""):
    """追加参考图（总数不超该模式上限）。"""
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    md = _norm_mode(mode or rec.get("mode"))
    cap = MAX_IMAGES[md]
    rels = list(rec.get("src_rels") or [])
    imgs = [f for f in images if f.filename]
    if len(rels) + len(imgs) > cap:
        return JSONResponse({"ok": False,
                             "error": "%s最多 %d 张，当前已有 %d 张"
                                      % ("单张生图" if md == "single" else "批量生图", cap, len(rels))},
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
    rec["mode"] = md
    rec.setdefault("cdn", {}).update(_upload_cdn(rec["uid"], tid, rels))
    _save_rec(rec)
    return {"ok": True, "n": len(rels)}


@app.post("/api/session/{tid}/remove")
async def remove_image(tid: str, req: Request):
    """删一张参考图（body: {idx: 1-based}）。

    返回**重排后的提示词建议**：前端拿它覆盖输入框，保证 @图片N 引用不错位。
    前端自己也会算一遍（即时反馈），后端这层是兜底 —— 两边算法同源（reindex_refs）。
    """
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
    # 重命名后续文件，保证 src1..srcN 连续（URL 稳定，前端缓存不串）
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
    prompt = str(body.get("prompt") or "")
    new_prompt = reindex_refs(prompt, idx - 1) if prompt else ""
    if prompt:
        rec["prompt"] = new_prompt
    _save_rec(rec)
    return {"ok": True, "n": len(new_rels), "prompt": new_prompt}


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
    """回读会话（刷新页面恢复状态）。"""
    if not _ID_RE.match(tid):
        return JSONResponse({"ok": False}, status_code=404)
    rec = _load_rec(tid, _uid(req))
    if not rec:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    return {"ok": True, "tid": tid, "mode": rec.get("mode") or "single",
            "opt": rec.get("opt") or {}, "prompt": rec.get("prompt") or "",
            "n_images": len(rec.get("src_rels") or []),
            "status": rec.get("status"), "items": _pub_items(rec)}


@app.post("/api/session/{tid}/mode")
async def set_mode(tid: str, req: Request):
    """切模式（单张/批量）。上限不同，已传图超过新上限时明确拒绝而不是静默丢图。"""
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
    try:
        body = json.loads((await req.body()).decode("utf-8") or "{}") or {}
    except Exception:                             # noqa: BLE001
        body = {}
    md = _norm_mode(body.get("mode"))
    n = len(rec.get("src_rels") or [])
    if n > MAX_IMAGES[md]:
        return JSONResponse({"ok": False,
                             "error": "已上传 %d 张，%s最多 %d 张，请先删掉多余的图"
                                      % (n, "单张生图" if md == "single" else "批量生图",
                                         MAX_IMAGES[md])}, status_code=400)
    rec["mode"] = md
    _save_rec(rec)
    return {"ok": True, "mode": md, "n": n}


# ---------------- 出图 ----------------
def _pub_items(rec):
    out = []
    for i, it in enumerate(rec.get("items") or []):
        out.append({"idx": i + 1, "status": it.get("status") or "pending",
                    "err": it.get("err") or "", "w": it.get("w") or 0, "h": it.get("h") or 0,
                    "url": ("api/tasks/%s/png/%d" % (rec["tid"], i + 1)) if it.get("rel") else "",
                    "prompt": it.get("prompt") or "",
                    "ref": it.get("ref") or ""})
    return out


def _plan_count(mode, n_imgs):
    """张数：单张恒 1；批量 = max(1, 参考图数)。（原站 cn() 的 aigc 分支）"""
    return 1 if mode == "single" else max(1, int(n_imgs))


@app.post("/api/generate")
async def generate(req: Request):
    """建出图任务。计费在父中间件按 body.n 先扣（accounts.aigc_gen = 3 分/张）。

    n 与后端算出的张数不一致时：**以后端为准**，多扣的部分由 /refund 退回。
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
        return JSONResponse({"ok": False, "error": "该会话正在生成中"}, status_code=400)
    opt = _norm_opt(d)
    mode = opt["mode"]
    prompt = opt["prompt"]
    if not prompt.strip():
        return JSONResponse({"ok": False, "error": "请输入生图提示词"}, status_code=400)
    imgs_n = len(rec.get("src_rels") or [])
    if imgs_n == 0:
        # 原站：没图也不是完全不能生成（纯文生图），但这条版块主打参考图生图，
        # 给一个明确提示而不是静默失败。
        if mode == "batch":
            return JSONResponse({"ok": False, "error": "批量生图请先上传参考图（张数按图数计算）"},
                                status_code=400)
    if imgs_n > MAX_IMAGES[mode]:
        return JSONResponse({"ok": False, "error": "%s最多 %d 张参考图，当前 %d 张"
                            % ("单张生图" if mode == "single" else "批量生图",
                               MAX_IMAGES[mode], imgs_n)}, status_code=400)
    # @ 引用越界（原站先拦，避免挂错图/挂空）
    refs = refs_of(prompt)
    bad = [r for r in refs if r < 0 or r >= imgs_n]
    if bad:
        return JSONResponse({"ok": False,
                             "error": "提示词引用的图片不存在，请重新输入 @ 选择已上传的参考图。"},
                            status_code=400)
    n = _plan_count(mode, imgs_n)
    rec["opt"] = opt
    rec["mode"] = mode
    rec["prompt"] = prompt
    rec["status"] = "running"
    rec["err"] = ""
    rec["refunded"] = 0
    rec["items"] = [{"status": "pending", "rel": "", "err": "", "w": 0, "h": 0,
                     "prompt": "", "ref": ""} for _ in range(n)]
    with _STOP_LOCK:
        _STOP.pop(tid, None)
    _save_rec(rec)
    threading.Thread(target=_worker, args=(tid, rec["uid"], mode, prompt, opt, refs),
                     daemon=True).start()
    return {"ok": True, "tid": tid, "n": n, "mode": mode}


def _b64_refs(items):
    """参考图 bytes → base64 字符串列表。

    🔴 必须转（2026-11-19 实测）：`pipeline._gen_once` 的 openai 协议分支（lensapi / huahuaapi）
       对 refs 做 `base64.b64decode(r)`，假定传进来是 b64 串；直接丢原始 JPEG 字节会解出垃圾，
       站点报 `invalid_image 图片格式无效/输入图片无法识别或解码`（lensapi 400）或 500（huahua）。
       apib 一停用，本版块就 100% 出不了图 —— 这不是上游故障，是我们传错类型。
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


def _pick_refs(imgs, refs, mode, i):
    """原站挂图逻辑（那段三元表达式）的等价实现。

    单张：@ 引用非空 → 只挂引用的；否则挂全部上传图。
    批量：@ 引用非空 → **每张都挂引用的那几张**（用户明确指定了素材）；
          否则第 i 张挂 imgs[i]，越界回落 imgs[0]。
    """
    if refs:
        out = [imgs[k] for k in refs if 0 <= k < len(imgs) and imgs[k]]
        if out:
            return out
    if mode == "batch":
        if i < len(imgs) and imgs[i]:
            return [imgs[i]]
        for x in imgs:
            if x:
                return [x]
        return []
    return [x for x in imgs if x]


def _worker(tid, uid, mode, prompt, opt, refs):
    """逐张串行出图（原站 for 循环语义）：单张失败只标记该张，不中断整批。"""
    rec = _load_rec(tid, uid) or {"tid": tid, "uid": uid}
    imgs = _src_bytes(rec)
    od = os.path.join(OUTPUT, "u{0}".format(uid), tid)
    os.makedirs(od, exist_ok=True)
    size = _ratio_px(opt["ratio"], opt["res"])
    chain = _model_chain(opt.get("model"))
    used_model = opt.get("model") if chain else ""
    text = wrap_prompt(prompt, opt["ratio"], opt["res"], mode == "batch", opt.get("model"))
    key = ""
    try:
        key = load_key()
    except Exception:                             # noqa: BLE001
        pass
    n = len(rec.get("items") or [])
    done = 0
    try:
        with _GEN_SEM:
            for i in range(n):
                if _want_stop(tid):
                    break
                rec = _load_rec(tid, uid) or rec
                its = rec.get("items") or []
                if i < len(its):
                    its[i]["status"] = "running"
                    rec["items"] = its
                    _save_rec(rec)
                use = _pick_refs(imgs, refs, mode, i)
                try:
                    u = gen_image(key, text, size, refs=_b64_refs(use) or None, models=chain)
                    raw = ai_sites.fetch_bytes(u)
                    raw = _fit_ratio(raw, opt["ratio"])
                    p = os.path.join(od, "g%d.png" % (i + 1))
                    with open(p, "wb") as f:
                        f.write(raw)
                    im = Image.open(io.BytesIO(raw))
                    rel = os.path.relpath(p, DATA).replace("\\", "/")
                    rec = _load_rec(tid, uid) or rec
                    its = rec.get("items") or []
                    if i < len(its):
                        its[i].update({"status": "done", "rel": rel, "w": im.width,
                                       "h": im.height, "prompt": text, "err": "",
                                       # 记录这张用了哪几张参考图（1-based，便于事后核对）
                                       "ref": ",".join(str(k + 1) for k in refs) if refs
                                              else ("%d" % (i + 1) if mode == "batch" else "全部")})
                    rec["items"] = its
                    rec["used_model"] = used_model
                    cdn = _upload_cdn(uid, tid, [rel])
                    rec.setdefault("cdn", {}).update(cdn)
                    _save_rec(rec)
                    done += 1
                except Exception as e:            # noqa: BLE001
                    msg = ai_sites.sanitize(e)
                    sys.stderr.write("[aigc] 第%d张失败：%s\n" % (i + 1, str(e)[:200]))
                    rec = _load_rec(tid, uid) or rec
                    its = rec.get("items") or []
                    if i < len(its):
                        its[i].update({"status": "failed", "err": msg, "prompt": text})
                    rec["items"] = its
                    _save_rec(rec)
        rec = _load_rec(tid, uid) or rec
        stopped = _want_stop(tid)
        rec["status"] = "stopped" if stopped else "done"
        rec["done_ts"] = time.time()
        its = rec.get("items") or []
        for it in its:
            if it.get("status") in ("pending", "running"):
                it["status"] = "skipped"
        rec["items"] = its
        rec["n_done"] = done
        _save_rec(rec)
    except Exception as e:                        # noqa: BLE001
        rec = _load_rec(tid, uid) or rec
        rec.update({"status": "failed", "err": ai_sites.sanitize(e)})
        _save_rec(rec)
    finally:
        with _STOP_LOCK:
            _STOP.pop(tid, None)


@app.post("/api/tasks/{tid}/stop")
def stop_task(tid: str, req: Request):
    """停止生成：保留已出的图，未出的标 skipped（原站语义）。"""
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    with _STOP_LOCK:
        _STOP[tid] = True
    return {"ok": True, "msg": "已停止生图，已生成的图片会保留，未出图的积分不会扣。"}


@app.post("/api/tasks/{tid}/refund")
def refund_unused(tid: str, req: Request):
    """把「未出图的张数」积分退回（停止 / 失败都要退）。幂等，只补差额。"""
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    if rec.get("status") == "running":
        return JSONResponse({"ok": False, "error": "任务还在进行中"}, status_code=400)
    its = rec.get("items") or []
    bad = len([p for p in its if p.get("status") != "done"])
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
        accounts.credit(rec["uid"], need * 3, "AI 生图·未出图退回 x%d" % need, tid)
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write("[aigc] 退分失败：%s\n" % e)
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
    its = rec.get("items") or []
    done = len([p for p in its if p.get("status") == "done"])
    return {"ok": True, "tid": tid, "status": rec.get("status"), "err": rec.get("err") or "",
            "mode": rec.get("mode") or "single", "n": len(its), "done": done,
            "progress": int(round(done * 100.0 / max(1, len(its)))),
            "used_model": rec.get("used_model") or "",
            "items": _pub_items(rec)}


@app.get("/api/tasks/{tid}/png/{idx}")
def get_png(tid: str, idx: int, req: Request):
    if not _ID_RE.match(tid):
        return JSONResponse({"ok": False}, status_code=404)
    rec = _load_rec(tid, _uid(req))
    its = (rec or {}).get("items") or []
    if idx < 1 or idx > len(its) or not its[idx - 1].get("rel"):
        return JSONResponse({"ok": False}, status_code=404)
    return _img_resp(rec, its[idx - 1]["rel"])


@app.post("/api/tasks/{tid}/redo")
async def redo_item(tid: str, req: Request):
    """单张重新生成。计费走父中间件 aigc_redo = 3 分。"""
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
    its = rec.get("items") or []
    if idx < 1 or idx > len(its):
        return JSONResponse({"ok": False, "error": "序号无效"}, status_code=400)
    if its[idx - 1].get("status") == "running":
        return JSONResponse({"ok": False, "error": "该张正在生成中"}, status_code=400)
    opt = _norm_opt(d.get("opt") or rec.get("opt") or {})
    prompt = str(opt.get("prompt") or rec.get("prompt") or "")
    if not prompt.strip():
        return JSONResponse({"ok": False, "error": "提示词已丢失，请重新生成"}, status_code=400)
    imgs_n = len(rec.get("src_rels") or [])
    refs = refs_of(prompt)
    bad = [r for r in refs if r < 0 or r >= imgs_n]
    if bad:
        return JSONResponse({"ok": False,
                             "error": "提示词引用的图片不存在，请重新输入 @ 选择已上传的参考图。"},
                            status_code=400)
    its[idx - 1]["status"] = "running"
    rec["items"] = its
    rec["opt"] = opt
    rec["prompt"] = prompt
    _save_rec(rec)
    threading.Thread(target=_redo_worker,
                     args=(tid, rec["uid"], idx, prompt, opt, refs, rec.get("mode") or "single"),
                     daemon=True).start()
    return {"ok": True, "tid": tid, "idx": idx}


def _redo_worker(tid, uid, idx, prompt, opt, refs, mode):
    rec = _load_rec(tid, uid) or {}
    imgs = _src_bytes(rec)
    od = os.path.join(OUTPUT, "u{0}".format(uid), tid)
    os.makedirs(od, exist_ok=True)
    size = _ratio_px(opt["ratio"], opt["res"])
    chain = _model_chain(opt.get("model"))
    text = wrap_prompt(prompt, opt["ratio"], opt["res"], mode == "batch", opt.get("model"))
    use = _pick_refs(imgs, refs, mode, idx - 1)
    try:
        key = load_key()
    except Exception:                             # noqa: BLE001
        key = ""
    try:
        with _GEN_SEM:
            u = gen_image(key, text, size, refs=_b64_refs(use) or None, models=chain)
            raw = ai_sites.fetch_bytes(u)
            raw = _fit_ratio(raw, opt["ratio"])
        p = os.path.join(od, "g%d.png" % idx)
        with open(p, "wb") as f:
            f.write(raw)
        im = Image.open(io.BytesIO(raw))
        rel = os.path.relpath(p, DATA).replace("\\", "/")
        rec = _load_rec(tid, uid) or rec
        its = rec.get("items") or []
        if idx - 1 < len(its):
            its[idx - 1].update({"status": "done", "rel": rel, "w": im.width, "h": im.height,
                                 "prompt": text, "err": ""})
        rec["items"] = its
        rec.setdefault("cdn", {}).update(_upload_cdn(uid, tid, [rel]))
        _save_rec(rec)
    except Exception as e:                        # noqa: BLE001
        rec = _load_rec(tid, uid) or rec
        its = rec.get("items") or []
        if idx - 1 < len(its):
            its[idx - 1].update({"status": "failed", "err": ai_sites.sanitize(e)})
        rec["items"] = its
        _save_rec(rec)


@app.get("/api/tasks/{tid}/zip")
def zip_task(tid: str, req: Request, idxs: str = ""):
    """批量下载 ZIP（后端打包，图已在服务器，比前端拉回来再压快得多）。"""
    if not _ID_RE.match(tid):
        return JSONResponse({"ok": False}, status_code=404)
    rec = _load_rec(tid, _uid(req))
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    its = rec.get("items") or []
    want = []
    if idxs.strip():
        for x in idxs.split(","):
            try:
                i = int(x.strip())
            except Exception:                     # noqa: BLE001
                continue
            if 1 <= i <= len(its):
                want.append(i)
    else:
        want = [i + 1 for i, p in enumerate(its) if p.get("rel")]
    want = [i for i in want if its[i - 1].get("rel")]
    if not want:
        return JSONResponse({"ok": False, "error": "没有可下载的图片"}, status_code=400)
    od = os.path.join(OUTPUT, "u{0}".format(rec["uid"]), tid)
    os.makedirs(od, exist_ok=True)
    zp = os.path.join(od, "aigc.zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for i in want:
            p = os.path.join(DATA, its[i - 1]["rel"])
            if os.path.isfile(p):
                z.write(p, "aigc-%02d.png" % i)
    return FileResponse(zp, media_type="application/zip", filename="aigc-%s.zip" % tid)


@app.get("/api/tasks")
def list_tasks(req: Request, limit: int = 30):
    """当前用户最近的生成记录（刷新后能找回上一批）。"""
    uid = _uid(req)
    out = []
    d = os.path.join(RECORDS, "u{0}".format(uid))
    if os.path.isdir(d):
        files = []
        for fn in os.listdir(d):
            if fn.endswith(".json"):
                files.append(os.path.join(d, fn))
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for p in files[:max(1, min(100, int(limit or 30)))]:
            try:
                rec = json.load(open(p, encoding="utf-8"))
            except Exception:                     # noqa: BLE001
                continue
            its = rec.get("items") or []
            out.append({"tid": rec.get("tid"), "ts": rec.get("ts"),
                        "mode": rec.get("mode") or "single", "status": rec.get("status"),
                        "n": len(its),
                        "done": len([x for x in its if x.get("status") == "done"]),
                        "prompt": (rec.get("prompt") or "")[:80]})
    return {"ok": True, "items": out}


# ---------------- 清理 ----------------
def _sweep():
    """72h TTL：删超期的记录 + 图（COS 侧另有桶生命周期兜底）。"""
    cutoff = time.time() - TTL_HOURS * 3600.0
    killed = 0
    if not os.path.isdir(RECORDS):
        return killed
    for u in os.listdir(RECORDS):
        d = os.path.join(RECORDS, u)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(d, fn)
            try:
                if os.path.getmtime(p) >= cutoff:
                    continue
                rec = json.load(open(p, encoding="utf-8"))
            except Exception:                     # noqa: BLE001
                continue
            if rec.get("status") == "running":
                continue
            tid = rec.get("tid") or fn[:-5]
            try:
                if aigcstore is not None:
                    aigcstore.delete_task(rec.get("uid"), tid)
            except Exception:                     # noqa: BLE001
                pass
            od = os.path.join(OUTPUT, u, tid)
            shutil.rmtree(od, ignore_errors=True)
            try:
                os.remove(p)
            except OSError:
                pass
            killed += 1
    return killed


def _sweep_loop():
    time.sleep(600)                               # 启动先等 10 分钟，避免 import 期误清
    while True:
        try:
            _sweep()
        except Exception as e:                    # noqa: BLE001
            sys.stderr.write("[aigc] 清理异常：%s\n" % e)
        time.sleep(max(60, CLEANUP_INTERVAL))


threading.Thread(target=_sweep_loop, daemon=True).start()

# ---------------- 启动即复位僵死任务（2026-11-19 新增）----------------
# 现象：出图途中服务重启（部署/崩溃），worker 线程被杀，记录永远停在 status=running、
#       items 里那张停在 running → 前端轮询看到 running 就**永远显示「生图中」**，
#       而且重出接口会以「正在生成中」把用户拦死。
# 判据：本进程刚起，任何 running 都不可能是本进程的活线程 → 一律复位成 failed。
def _reap_stale_on_boot():
    n = 0
    base = os.path.join(DATA, "records")
    if not os.path.isdir(base):
        return 0
    for ud in os.listdir(base):
        dd = os.path.join(base, ud)
        if not os.path.isdir(dd):
            continue
        for fn in os.listdir(dd):
            if not fn.endswith(".json"):
                continue
            fp = os.path.join(dd, fn)
            try:
                rec = json.load(open(fp, encoding="utf-8"))
            except Exception:                     # noqa: BLE001
                continue
            if rec.get("status") != "running":
                continue
            msg = "服务重启导致生成中断，请点「重新生成」重试。"
            its = rec.get("items") or []
            for it in its:
                if it.get("status") in ("running", "pending"):
                    it["status"] = "failed"
                    it["err"] = it.get("err") or msg
            rec["items"] = its
            rec["status"] = "failed"
            rec["err"] = msg
            rec["done_ts"] = time.time()
            try:
                tmp = fp + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(rec, f, ensure_ascii=False)
                os.replace(tmp, fp)
                n += 1
            except Exception:                     # noqa: BLE001
                pass
    if n:
        sys.stderr.write("[aigc] 启动时复位 %d 条僵死任务（可重新生成）\n" % n)
    return n


def _reap_stale_items():
    """记录已终结（done/failed/stopped）但 items 仍卡 running/pending → 按终态补正。

    事故（2026-11-19 12:36 那单）：status=done + refunded=1，但 items[0].status 还是 running，
    前端只看 items 的 running → 永远「生图中」，用户以为一直在跑。
    """
    n = 0
    base = os.path.join(DATA, "records")
    if not os.path.isdir(base):
        return 0
    for ud in os.listdir(base):
        dd = os.path.join(base, ud)
        if not os.path.isdir(dd):
            continue
        for fn in os.listdir(dd):
            if not fn.endswith(".json"):
                continue
            fp = os.path.join(dd, fn)
            try:
                rec = json.load(open(fp, encoding="utf-8"))
            except Exception:                     # noqa: BLE001
                continue
            if rec.get("status") not in ("done", "failed", "stopped"):
                continue
            its = rec.get("items") or []
            hit = False
            for it in its:
                if it.get("status") == "running":
                    it["status"] = "failed"
                    it["err"] = it.get("err") or "生成被中断，请点「重新生成」重试。"
                    hit = True
                elif it.get("status") == "pending":
                    it["status"] = "skipped"
                    hit = True
            if not hit:
                continue
            rec["items"] = its
            rec["n_done"] = len([x for x in its if x.get("status") == "done"])
            try:
                tmp = fp + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(rec, f, ensure_ascii=False)
                os.replace(tmp, fp)
                n += 1
            except Exception:                     # noqa: BLE001
                pass
    if n:
        sys.stderr.write("[aigc] 启动时补正 %d 条「已终结但卡 running」的记录\n" % n)
    return n


try:
    _reap_stale_on_boot()
    _reap_stale_items()
except Exception as _e:                            # noqa: BLE001
    sys.stderr.write("[aigc] 启动僵死清理异常：%s\n" % _e)
