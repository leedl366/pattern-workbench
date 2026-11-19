# -*- coding: utf-8 -*-
"""速图AIGC · 商品替换（两图融合）子应用  —— 挂载在 /swap

产品逻辑：图1 = 场景底图（保留背景/光影/构图），图2 = 要换进去的物品。
提示词由服务端统一拼装（前端只传「替换需求补充」，留空则不塞任何额外要求），
避免前端各自拼、拼出四种口径。

计费：父中间件按 `/swap/api/generate` 的 body.n 扣 3×n（accounts.swap_gen）；
      单张重出按 `/swap/api/tasks/{tid}/redo` 扣 3（accounts.swap_redo）。
      未出图的张数由前端调 /refund 退回。

🔴 参考图编码契约：`pipeline.gen_image(refs=)` 只接受 **base64 字符串**（内部 b64decode），
   编码只做一次，调用点不许再套一层。
"""
from __future__ import annotations

import io
import json
import os
import secrets
import sys
import threading
import time
import zipfile

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from PIL import Image

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_HERE)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from pipeline import gen_image, load_key          # noqa: E402
import ai_sites                                   # noqa: E402

try:
    import storage                                # noqa: E402
except Exception:                                 # noqa: BLE001
    storage = None

DATA = os.path.join(_HERE, "data")
RECORDS = os.path.join(DATA, "records")
OUTPUT = os.path.join(DATA, "output")
UPLOADS = os.path.join(DATA, "uploads")
for _d in (DATA, RECORDS, OUTPUT, UPLOADS):
    os.makedirs(_d, exist_ok=True)

app = FastAPI(title="速图AIGC · 商品替换")

MAX_N = 4
MAX_UPLOAD = 10 * 1024 * 1024          # 单张 10MB
ANA_SIDE = 2048                        # 送模型前的最长边（省流量、够清晰）
RES_SHORT = {"1K": 1024, "2K": 2048}
RATIOS = ("1:1", "3:4", "4:3", "9:16", "16:9")

# 服务端统一骨架：四段式（角色声明 / 场景锁 / 物品锁 / 落地细节）
BONE = (
    "【图序声明】图1 = 场景底图（保留背景、光影、构图、相机角度与整体风格）；"
    "图2 = 要替换进去的物品。两张图职责不可混淆。\n"
    "【场景锁】背景、光位、投影方向、色调、景深、构图全部沿用图1，"
    "禁止重绘背景、禁止更换场景、禁止改变相机角度。\n"
    "【物品锁】替换进去的物品 100% 来自图2：颜色、材质、纹理、logo、细节必须与图2完全一致，"
    "不得用想象中的物品替换，不得改款、不得变形。\n"
    "【落地细节】先移除图1 中的原物体（保留其接触阴影与环境反射），再把图2 物品放到原物体位置；"
    "尺寸与原物体大小匹配，透视角度一致，补上合理接触阴影与边缘过渡，无重影、无拼接感、无描边。"
)
DEFAULT_EXTRA = "保持图1 的版式与视觉关系不变，只做物体替换。"


# ---------------------------------------------------------------- 基础
def _uid(req: Request) -> int:
    return int(req.scope.get("st_uid") or 0)


def _is_admin(uid: int) -> bool:
    try:
        import accounts
        return accounts.is_admin(uid)
    except Exception:                             # noqa: BLE001
        return uid == 0


def _rec_path(tid: str, uid: int | None = None) -> str:
    if uid is None:
        return os.path.join(RECORDS, tid + ".json")
    return os.path.join(RECORDS, "u{0}".format(uid), tid + ".json")


def _load_rec(tid: str, uid: int | None = None) -> dict | None:
    p = _rec_path(tid, uid)
    if os.path.isfile(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:                         # noqa: BLE001
            return None
    if uid is not None and _is_admin(uid):
        p = _rec_path(tid)
        if os.path.isfile(p):
            try:
                return json.load(open(p, encoding="utf-8"))
            except Exception:                     # noqa: BLE001
                return None
    return None


def _save_rec(rec: dict) -> None:
    p = _rec_path(rec["tid"], rec.get("uid") or 0)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(tmp, p)


def _read(rel: str) -> bytes:
    p = os.path.join(DATA, str(rel).replace("/", os.sep))
    try:
        return open(p, "rb").read()
    except Exception:                             # noqa: BLE001
        return b""


def _cdn_of(rec: dict, rel: str) -> str:
    try:
        return (rec.get("cdn") or {}).get(str(rel).replace("\\", "/").lstrip("/")) or ""
    except Exception:                             # noqa: BLE001
        return ""


def _img_resp(rec: dict, rel: str):
    cu = _cdn_of(rec, rel)
    if cu:
        return RedirectResponse(cu, status_code=302,
                                headers={"Cache-Control": "private,max-age=3600"})
    p = os.path.join(DATA, str(rel).replace("/", os.sep))
    if not os.path.isfile(p):
        return JSONResponse({"ok": False, "error": "图片不存在（可能已过清理期）"}, status_code=404)
    return FileResponse(p)


def _cdn_push(uid: int, tid: str, rels):
    if storage is None:
        return {}
    try:
        base = DATA
        return storage.upload_task_files(tid, base, rels) or {}
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write("[swap] COS 回推失败（回落本地）：%s\n" % e)
        return {}


# ---------------------------------------------------------------- 僵尸自愈
_LIVE = {}                    # tid -> 线程登记（begin/end 配对，finally 释放）
_LIVE_LOCK = threading.Lock()
_STOP = set()
_STOP_LOCK = threading.Lock()
STALE_SEC = 900               # 心跳超过 15 分钟视为僵尸


def _job_begin(tid: str) -> None:
    with _LIVE_LOCK:
        _LIVE[tid] = time.time()


def _job_end(tid: str) -> None:
    with _LIVE_LOCK:
        _LIVE.pop(tid, None)


def _alive(tid: str) -> bool:
    with _LIVE_LOCK:
        return tid in _LIVE


def _beat(rec: dict) -> None:
    rec["beat_ts"] = time.time()
    with _LIVE_LOCK:
        if rec.get("tid") in _LIVE:
            _LIVE[rec["tid"]] = time.time()


def _is_stale(rec: dict) -> bool:
    if _alive(rec.get("tid") or ""):
        return False
    return (time.time() - float(rec.get("beat_ts") or rec.get("ts") or 0)) > STALE_SEC


def _reap(rec: dict) -> bool:
    """把僵尸 running 复位成 failed，返回是否改动过。"""
    if rec.get("status") != "running" or not _is_stale(rec):
        return False
    rec["status"] = "failed"
    rec["err"] = "生成中断（服务重启或超时），请重新生成"
    for it in rec.get("items") or []:
        if it.get("status") in ("running", "pending"):
            it["status"] = "failed"
            it.setdefault("err", "生成中断")
    return True


def _reap_all_on_boot() -> None:
    n = 0
    for root, _dirs, files in os.walk(RECORDS):
        for fn in files:
            if not fn.endswith(".json"):
                continue
            try:
                rec = json.load(open(os.path.join(root, fn), encoding="utf-8"))
            except Exception:                     # noqa: BLE001
                continue
            if _reap(rec):
                _save_rec(rec)
                n += 1
    if n:
        sys.stderr.write("[swap] 启动复位僵尸任务 %d 条\n" % n)


def _reap_sweep() -> None:
    while True:
        time.sleep(300)
        try:
            for root, _dirs, files in os.walk(RECORDS):
                for fn in files:
                    if not fn.endswith(".json"):
                        continue
                    try:
                        rec = json.load(open(os.path.join(root, fn), encoding="utf-8"))
                    except Exception:             # noqa: BLE001
                        continue
                    if _reap(rec):
                        _save_rec(rec)
        except Exception:                         # noqa: BLE001
            pass


def _want_stop(tid: str) -> bool:
    with _STOP_LOCK:
        return tid in _STOP


# ---------------------------------------------------------------- 尺寸
def _ratio_size(ratio: str, res: str) -> str:
    """比例 + 清晰度 → 请求像素。只求比例对、落在模型允许区间，出图后 _fit_ratio 精确校正。"""
    short = RES_SHORT.get(str(res).upper(), 1024)
    r = str(ratio or "1:1")
    try:
        a, b = r.split(":")
        aw, ah = float(a), float(b)
    except Exception:                             # noqa: BLE001
        aw, ah = 1.0, 1.0
    if aw <= ah:
        w, h = short, int(round(short * ah / aw))
    else:
        h, w = short, int(round(short * aw / ah))
    w = max(256, int(round(w / 16.0)) * 16)
    h = max(256, int(round(h / 16.0)) * 16)
    return "%dx%d" % (w, h)


def _target_px(ratio: str, res: str):
    size = _ratio_size(ratio, res)
    w, h = size.split("x")
    return int(w), int(h)


def _edge_color(im: Image.Image):
    """取四边中位色，用于补边，避免出现白边。"""
    try:
        w, h = im.size
        pts = []
        for x in range(0, w, max(1, w // 24)):
            pts.append(im.getpixel((x, 0)))
            pts.append(im.getpixel((x, h - 1)))
        for y in range(0, h, max(1, h // 24)):
            pts.append(im.getpixel((0, y)))
            pts.append(im.getpixel((w - 1, y)))
        n = len(pts) or 1
        return (sum(p[0] for p in pts) // n, sum(p[1] for p in pts) // n, sum(p[2] for p in pts) // n)
    except Exception:                             # noqa: BLE001
        return (255, 255, 255)


def _fit_ratio(raw: bytes, ratio: str, res: str) -> bytes:
    """出图后精确落到目标比例与像素（等比 contain + 边缘色补边）。

    🔴 上游会把请求尺寸就近吸附，只保比例不保像素 —— 不校正就会出现「选了 3:4 给 1:1」。
    """
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
        im = im.convert("RGB")
    except Exception:                             # noqa: BLE001
        return raw
    tw, th = _target_px(ratio, res)
    w, h = im.size
    k = min(float(tw) / w, float(th) / h)
    nw, nh = max(1, int(round(w * k))), max(1, int(round(h * k)))
    if (nw, nh) != (w, h):
        im = im.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (tw, th), _edge_color(im))
    canvas.paste(im, ((tw - nw) // 2, (th - nh) // 2))
    buf = io.BytesIO()
    canvas.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------- 上传
@app.post("/api/upload")
async def upload(req: Request, file: UploadFile = File(...)):
    uid = _uid(req)
    if not uid:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    raw = await file.read()
    if not raw:
        return JSONResponse({"ok": False, "error": "文件为空"}, status_code=400)
    if len(raw) > MAX_UPLOAD:
        return JSONResponse({"ok": False, "error": "单张不能超过 10MB"}, status_code=400)
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
        im = im.convert("RGB")
    except Exception:                             # noqa: BLE001
        return JSONResponse({"ok": False, "error": "不是有效的 JPG/PNG 图片"}, status_code=400)
    w, h = im.size
    if max(w, h) > ANA_SIDE:
        k = float(ANA_SIDE) / max(w, h)
        im = im.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    rel = "uploads/u{0}/{1}.jpg".format(uid, secrets.token_hex(8))
    p = os.path.join(DATA, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(buf.getvalue())
    return {"ok": True, "rel": rel, "w": im.size[0], "h": im.size[1],
            "kb": round(len(buf.getvalue()) / 1024.0, 1)}


# ---------------------------------------------------------------- 生成
def _pub_items(rec: dict):
    out = []
    tid = rec["tid"]
    for i, it in enumerate(rec.get("items") or []):
        out.append({"idx": i + 1, "status": it.get("status") or "pending",
                    "err": it.get("err") or "", "w": it.get("w") or 0, "h": it.get("h") or 0,
                    "url": ("api/tasks/%s/png/%d" % (tid, i + 1)) if it.get("rel") else ""})
    return out


@app.post("/api/generate")
async def generate(req: Request):
    """建出图任务。计费在父中间件按 body.n 先扣（accounts.swap_gen = 3 分/张）。"""
    try:
        d = json.loads((await req.body()).decode("utf-8") or "{}") or {}
    except Exception:                             # noqa: BLE001
        d = {}
    uid = _uid(req)
    if not uid:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    src_rel = str(d.get("src") or "")
    item_rel = str(d.get("item") or "")
    if not src_rel or not item_rel:
        return JSONResponse({"ok": False, "error": "图1 场景图 和 图2 替换物品图 都要上传"},
                            status_code=400)
    if not _read(src_rel) or not _read(item_rel):
        return JSONResponse({"ok": False, "error": "图片已失效，请重新上传"}, status_code=400)
    ratio = str(d.get("ratio") or "1:1")
    if ratio not in RATIOS:
        ratio = "1:1"
    res = str(d.get("res") or "1K").upper()
    if res not in RES_SHORT:
        res = "1K"
    try:
        n = int(d.get("n") or 1)
    except Exception:                             # noqa: BLE001
        n = 1
    n = max(1, min(n, MAX_N))
    extra = str(d.get("prompt") or "").strip()[:600]
    prompt = BONE + "\n【本单具体要求】" + (extra or DEFAULT_EXTRA)

    tid = "sw" + time.strftime("%y%m%d%H%M%S") + secrets.token_hex(3)
    rec = {"tid": tid, "uid": uid, "ts": time.time(), "status": "running", "err": "",
           "src_rel": src_rel, "item_rel": item_rel, "prompt": prompt, "extra": extra,
           "opt": {"ratio": ratio, "res": res, "n": n},
           "items": [{"status": "pending", "rel": "", "err": "", "w": 0, "h": 0} for _ in range(n)],
           "n_done": 0, "refunded": False}
    with _STOP_LOCK:
        _STOP.discard(tid)
    _beat(rec)
    _save_rec(rec)
    _job_begin(tid)
    threading.Thread(target=_worker, args=(tid, uid), daemon=True).start()
    return {"ok": True, "tid": tid, "n": n, "size": _ratio_size(ratio, res)}


def _b64(items):
    """bytes → base64 字符串列表。gen_image 内部会 b64decode，编码只能做一次。"""
    import base64
    return [base64.b64encode(b).decode("ascii") for b in items if b]


def _worker(tid: str, uid: int) -> None:
    rec = _load_rec(tid, uid)
    if not rec:
        _job_end(tid)
        return
    try:
        key = load_key()
    except Exception as e:                        # noqa: BLE001
        rec["status"] = "failed"
        rec["err"] = "生图密钥不可用：%s" % str(e)[:80]
        _save_rec(rec)
        _job_end(tid)
        return
    src_b = _read(rec["src_rel"])
    item_b = _read(rec["item_rel"])
    refs = _b64([src_b, item_b])
    opt = rec.get("opt") or {}
    size = _ratio_size(opt.get("ratio") or "1:1", opt.get("res") or "1K")
    od = os.path.join(OUTPUT, "u{0}".format(uid), tid)
    os.makedirs(od, exist_ok=True)
    done = 0
    try:
        for i in range(len(rec.get("items") or [])):
            if _want_stop(tid):
                break
            rec = _load_rec(tid, uid) or rec
            items = rec.get("items") or []
            if i < len(items):
                items[i]["status"] = "running"
                items[i]["err"] = ""
                rec["items"] = items
                _beat(rec)
                _save_rec(rec)
            try:
                u = gen_image(key, rec["prompt"], size, refs=refs or None)
                raw = ai_sites.fetch_bytes(u)
                png = _fit_ratio(raw, opt.get("ratio") or "1:1", opt.get("res") or "1K")
                rel = "output/u{0}/{1}/p{2}.png".format(uid, tid, i + 1)
                p = os.path.join(DATA, rel.replace("/", os.sep))
                with open(p, "wb") as f:
                    f.write(png)
                im = Image.open(io.BytesIO(png))
                rec = _load_rec(tid, uid) or rec
                items = rec.get("items") or []
                if i < len(items):
                    items[i].update({"status": "done", "rel": rel, "err": "",
                                     "w": im.size[0], "h": im.size[1]})
                    rec["items"] = items
                done += 1
                rec["n_done"] = done
                cdn = _cdn_push(uid, tid, [rel])
                if cdn:
                    rec.setdefault("cdn", {}).update(cdn)
                _beat(rec)
                _save_rec(rec)
            except Exception as e:                # noqa: BLE001
                rec = _load_rec(tid, uid) or rec
                items = rec.get("items") or []
                if i < len(items):
                    items[i].update({"status": "failed", "err": _friendly(e)})
                    rec["items"] = items
                _beat(rec)
                _save_rec(rec)
        rec = _load_rec(tid, uid) or rec
        left = [it for it in (rec.get("items") or []) if it.get("status") in ("pending", "running")]
        for it in left:
            it["status"] = "skipped"
            it["err"] = "已停止，未出图"
        rec["n_done"] = done
        rec["status"] = "done" if (done + 0) and not left else ("stopped" if left else "done")
        if done == 0:
            rec["status"] = "failed"
            if not rec.get("err"):
                rec["err"] = "一张都没出成功"
        _beat(rec)
        _save_rec(rec)
    finally:
        _job_end(tid)                             # 🔴 必须 finally 释放，否则僵尸锁死


def _friendly(e: Exception) -> str:
    s = str(e)
    low = s.lower()
    if "invalid_image" in low or "图片格式" in s or "无法识别" in s:
        return "参考图处理失败，请重新上传两张图"
    if "402" in s or "余额" in s or "insufficient" in low or "credits" in low:
        return "生图站点额度不足，本次未出图（积分已退回）"
    if "timeout" in low or "超时" in s:
        return "生图超时，可点「重出这一张」再试"
    return "生图失败：%s" % s[:120]


# ---------------------------------------------------------------- 查询
@app.get("/api/tasks/{tid}")
def get_task(tid: str, req: Request):
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    if _reap(rec):
        _save_rec(rec)
    return {"ok": True, "tid": tid, "status": rec.get("status"), "err": rec.get("err") or "",
            "items": _pub_items(rec), "n_done": rec.get("n_done") or 0,
            "prompt": rec.get("prompt") or "", "extra": rec.get("extra") or "",
            "opt": rec.get("opt") or {},
            "src": "api/tasks/%s/src/src" % tid, "item": "api/tasks/%s/src/item" % tid,
            "ts": rec.get("ts"), "refunded": bool(rec.get("refunded"))}


@app.get("/api/tasks/{tid}/png/{idx}")
def get_png(tid: str, idx: int, req: Request):
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    items = rec.get("items") or []
    if idx < 1 or idx > len(items) or not items[idx - 1].get("rel"):
        return JSONResponse({"ok": False, "error": "没有这张图"}, status_code=404)
    return _img_resp(rec, items[idx - 1]["rel"])


@app.get("/api/tasks/{tid}/src/{slot}")
def get_src(tid: str, slot: str, req: Request):
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    rel = rec.get("item_rel") if slot == "item" else rec.get("src_rel")
    if not rel:
        return JSONResponse({"ok": False, "error": "没有这张图"}, status_code=404)
    return _img_resp(rec, rel)


# ---------------------------------------------------------------- 重出 / 停止 / 退分
@app.post("/api/tasks/{tid}/redo")
async def redo(tid: str, req: Request):
    """单张重出（父中间件按路径扣 3 分）。"""
    try:
        d = json.loads((await req.body()).decode("utf-8") or "{}") or {}
    except Exception:                             # noqa: BLE001
        d = {}
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    items = rec.get("items") or []
    try:
        idx = int(d.get("idx") or 0)
    except Exception:                             # noqa: BLE001
        idx = 0
    if idx < 1 or idx > len(items):
        return JSONResponse({"ok": False, "error": "没有这一张"}, status_code=400)
    if rec.get("status") == "running" and _alive(tid):
        return JSONResponse({"ok": False, "error": "正在生成中，请等这一轮结束"}, status_code=400)
    items[idx - 1].update({"status": "pending", "err": "", "rel": ""})
    rec["items"] = items
    rec["status"] = "running"
    rec["err"] = ""
    with _STOP_LOCK:
        _STOP.discard(tid)
    _beat(rec)
    _save_rec(rec)
    _job_begin(tid)
    threading.Thread(target=_redo_worker, args=(tid, uid, idx), daemon=True).start()
    return {"ok": True, "tid": tid, "idx": idx}


def _redo_worker(tid: str, uid: int, idx: int) -> None:
    rec = _load_rec(tid, uid)
    if not rec:
        _job_end(tid)
        return
    try:
        key = load_key()
        refs = _b64([_read(rec["src_rel"]), _read(rec["item_rel"])])
        opt = rec.get("opt") or {}
        size = _ratio_size(opt.get("ratio") or "1:1", opt.get("res") or "1K")
        items = rec.get("items") or []
        items[idx - 1]["status"] = "running"
        rec["items"] = items
        _beat(rec)
        _save_rec(rec)
        try:
            u = gen_image(key, rec["prompt"], size, refs=refs or None)
            raw = ai_sites.fetch_bytes(u)
            png = _fit_ratio(raw, opt.get("ratio") or "1:1", opt.get("res") or "1K")
            rel = "output/u{0}/{1}/p{2}.png".format(uid, tid, idx)
            p = os.path.join(DATA, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(png)
            im = Image.open(io.BytesIO(png))
            rec = _load_rec(tid, uid) or rec
            items = rec.get("items") or []
            items[idx - 1].update({"status": "done", "rel": rel, "err": "",
                                   "w": im.size[0], "h": im.size[1]})
            rec["items"] = items
            rec["n_done"] = len([x for x in items if x.get("status") == "done"])
            cdn = _cdn_push(uid, tid, [rel])
            if cdn:
                rec.setdefault("cdn", {}).update(cdn)
        except Exception as e:                    # noqa: BLE001
            rec = _load_rec(tid, uid) or rec
            items = rec.get("items") or []
            if idx - 1 < len(items):
                items[idx - 1].update({"status": "failed", "err": _friendly(e)})
                rec["items"] = items
        rec["status"] = "done"
        _beat(rec)
        _save_rec(rec)
    finally:
        _job_end(tid)


@app.post("/api/tasks/{tid}/stop")
def stop(tid: str, req: Request):
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    with _STOP_LOCK:
        _STOP.add(tid)
    return {"ok": True}


@app.post("/api/tasks/{tid}/refund")
async def refund_unmade(tid: str, req: Request):
    """把没出图的张数退回（幂等，只退一次）。"""
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    if rec.get("refunded"):
        return {"ok": True, "refunded": 0, "already": True}
    if rec.get("status") == "running" and _alive(tid):
        return JSONResponse({"ok": False, "error": "还在生成中"}, status_code=400)
    n_total = len(rec.get("items") or [])
    n_done = len([x for x in (rec.get("items") or []) if x.get("status") == "done"])
    n_fail = n_total - n_done
    if n_fail <= 0:
        rec["refunded"] = True
        _save_rec(rec)
        return {"ok": True, "refunded": 0}
    try:
        import accounts
        accounts.refund(uid, 3 * n_fail, "商品替换·未出图%d张" % n_fail, tid)
    except Exception as e:                        # noqa: BLE001
        return JSONResponse({"ok": False, "error": "退分失败：%s" % str(e)[:80]}, status_code=500)
    rec["refunded"] = True
    _save_rec(rec)
    return {"ok": True, "refunded": 3 * n_fail, "n": n_fail}


@app.get("/api/tasks")
def list_tasks(req: Request, limit: int = 12):
    uid = _uid(req)
    out = []
    for root, _dirs, files in os.walk(RECORDS):
        for fn in files:
            if not fn.endswith(".json"):
                continue
            try:
                rec = json.load(open(os.path.join(root, fn), encoding="utf-8"))
            except Exception:                     # noqa: BLE001
                continue
            if int(rec.get("uid") or 0) != uid:
                continue
            thumb = ""
            for i, it in enumerate(rec.get("items") or []):
                if it.get("status") == "done" and it.get("rel"):
                    thumb = "api/tasks/%s/png/%d" % (rec["tid"], i + 1)
                    break
            out.append({"tid": rec["tid"], "ts": rec.get("ts") or 0,
                        "status": rec.get("status"), "thumb": thumb,
                        "n": len(rec.get("items") or []), "n_done": rec.get("n_done") or 0})
    out.sort(key=lambda x: x["ts"], reverse=True)
    return {"ok": True, "tasks": out[:max(1, min(limit, 50))]}


@app.get("/api/tasks/{tid}/zip")
def zip_task(tid: str, req: Request):
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    od = os.path.join(OUTPUT, "u{0}".format(uid), tid)
    os.makedirs(od, exist_ok=True)
    zp = os.path.join(od, "swap_%s.zip" % tid)
    n = 0
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for i, it in enumerate(rec.get("items") or []):
            if it.get("status") != "done" or not it.get("rel"):
                continue
            fp = os.path.join(DATA, it["rel"].replace("/", os.sep))
            if os.path.isfile(fp):
                z.write(fp, "第%02d张.png" % (i + 1))
                n += 1
        for slot, name in (("src", "图1_场景原图.jpg"), ("item", "图2_替换物品原图.jpg")):
            rel = rec.get("src_rel") if slot == "src" else rec.get("item_rel")
            fp = os.path.join(DATA, (rel or "").replace("/", os.sep))
            if rel and os.path.isfile(fp):
                z.write(fp, name)
    if not n:
        try:
            os.remove(zp)
        except OSError:
            pass
        return JSONResponse({"ok": False, "error": "还没有出图，无法打包"}, status_code=400)
    return FileResponse(zp, media_type="application/zip", filename="商品替换_%s.zip" % tid)


@app.get("/api/ping")
def ping():
    return {"ok": True, "app": "swap"}


@app.get("/", response_class=HTMLResponse)
def index():
    p = os.path.join(_HERE, "frontend", "index.html")
    if not os.path.isfile(p):
        return HTMLResponse("<h1>swap frontend missing</h1>", status_code=500)
    return HTMLResponse(open(p, encoding="utf-8").read())


_reap_all_on_boot()
threading.Thread(target=_reap_sweep, daemon=True).start()
