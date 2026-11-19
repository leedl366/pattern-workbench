"""批量去水印工作台 —— FastAPI 子应用（挂载在父应用 /wm 路径下）。

独立运行时（`uvicorn wm.app:app`）也完全可用；挂载到父应用后
所有对外 URL 自动带上 root_path 前缀（/wm/...）。
"""
import base64
import copy
import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
import uuid
import zipfile

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config as cfgmod
from . import storage as wmstore
from .engines import RETIRED, get_engine, list_engines
from .pipeline import (
    extract_template, find_watermark, make_comparison, process_image,
    read_image, rel_of, safe_rel, save_image, scan_images,
    template_mask_for_image,
)

import requests as _requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
PREVIEW = os.path.join(DATA, "preview")
RESULTS = os.path.join(PREVIEW, "results")
FRONTEND = os.path.join(HERE, "frontend")
DEFAULT_INPUT = os.path.join(DATA, "input")
OUTPUT_DIR = os.path.join(DATA, "output")
TEMPLATES_ROOT = os.path.join(DATA, "wm_templates")   # 水印样例库（每个样例一个子目录）
OLD_TEMPLATE_DIR = os.path.join(DATA, "wm_template")  # 旧版单一样例（自动迁移）
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
for _d in (RESULTS, DEFAULT_INPUT, OUTPUT_DIR):
    os.makedirs(_d, exist_ok=True)

# ---------- 产物保留 N 小时 · 自动清理（与图案提取工作台同一策略） ----------
TTL_HOURS = float(os.environ.get("WM_TTL_HOURS", "72"))
CLEANUP_INTERVAL = int(os.environ.get("WM_CLEANUP_SEC", "900"))
CLEANUP_LOG = os.path.join(DATA, "_cleanup.json")
SWEEP_DIRS = ("input", "preview", "output", "_chunks")  # wm_templates（用户样例库）不参与清理；_chunks=分片上传暂存（孤儿分片同 TTL 清理）

# ---------- 图床：与图案提取同规则（双写兜底 + 独立前缀 + 同一 TTL） ----------


def _local_rels():
    """本地全部产物的相对路径（相对 DATA，POSIX 斜杠）——上传与 COS 清理的保留集。"""
    rels = []
    for sub in SWEEP_DIRS:
        base = os.path.join(DATA, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, _dn, filenames in os.walk(base):
            for fn in filenames:
                if fn.startswith("."):
                    continue
                p = os.path.join(dirpath, fn)
                rels.append(os.path.relpath(p, DATA).replace("\\", "/"))
    return rels


def _keep_rels():
    """COS 保留集：三大产物目录 + 水印样例库。

    样例库（wm_templates）属用户资产、不参与 72h 清理，但**已上传 COS 作冷备份**，
    必须显式进保留集，否则孤儿兜底会在 72h 后把备份删掉。
    """
    out = _local_rels()
    if os.path.isdir(TEMPLATES_ROOT):
        for dirpath, _dn, filenames in os.walk(TEMPLATES_ROOT):
            for fn in filenames:
                if fn.startswith("."):
                    continue
                p = os.path.join(dirpath, fn)
                out.append(os.path.relpath(p, DATA).replace("\\", "/"))
    return out


def _cos_sweep(dry_run=False):
    """COS 前缀级孤儿兜底清理。本地三大产物目录全不在 → 拒执行（防误删）。"""
    if not any(os.path.isdir(os.path.join(DATA, s)) for s in SWEEP_DIRS) and not dry_run:
        return {"scanned": 0, "matched": 0, "deleted": 0, "bytes": 0, "keys": [],
                "dry_run": False, "skipped": "本地产物目录缺失，跳过（防误删）"}
    return wmstore.sweep(_keep_rels(), TTL_HOURS, dry_run=dry_run)

app = FastAPI(title="批量去水印工作台")
app.mount("/preview", StaticFiles(directory=PREVIEW), name="preview")


def _root(req):
    """子应用挂载前缀（如 /wm）；独立运行时为空串。"""
    return (req.scope.get("root_path") or "").rstrip("/")


def _md5(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:12]


def _thumb(img, max_side=1200):
    """等比缩到长边 ≤ max_side 的缩略图。

    用途：前端「原图 ⇄ 成品」对比滑块的原图层。
    取缩略而非原图——体积小 8~20 倍、拖动对齐更顺，且两端尺寸差异不影响观感
    （滑块两层图都 object-fit:contain，容器宽高比按【成品】锁定）。
    """
    if img is None or getattr(img, "size", 0) == 0:
        return img
    h, w = img.shape[:2]
    m = max(h, w)
    if m <= max_side:
        return img
    s = float(max_side) / float(m)
    return cv2.resize(img, (max(1, int(round(w * s))), max(1, int(round(h * s)))),
                      interpolation=cv2.INTER_AREA)


@app.get("/", response_class=HTMLResponse)
def index():
    # 每次请求现读 + 禁缓存：改前端文件即时生效，无需重启子应用
    with open(os.path.join(FRONTEND, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(
            f.read(), headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/ping")
def ping(req: Request):
    """自检用：确认子应用已正确挂载。"""
    return {"ok": True, "module": "wm", "root": _root(req),
            "engines": [e["name"] for e in list_engines()],
            "ttl_hours": TTL_HOURS, "cleanup_interval": CLEANUP_INTERVAL,
            "cos": wmstore.info()}


@app.get("/api/engines")
def engines():
    return {"engines": list_engines()}


def _is_admin_req(req):
    """经主站中间件进入时 scope 带 st_uid（登录必设）；独立运行（无账号体系）视为 admin 不脱敏。"""
    try:
        import accounts as ACC
    except Exception:
        return True
    uid = req.scope.get("st_uid")
    if not uid:
        return True
    return ACC.is_admin(uid)


def _mask_key(k):
    k = str(k or "")
    return (k[:6] + "..." + k[-4:]) if len(k) > 12 else ("***" if k else "")


# ---- 归属隔离（2026-10-12）：成品按账号分目录 + 处理记录落盘（素材库） ----
def _req_uid(req):
    """当前账号 uid（主站中间件注入 scope）；独立运行无账号体系返回 None。"""
    try:
        return int(req.scope.get("st_uid") or 0) or None
    except Exception:
        return None


def _default_output(req):
    """服务端默认成品目录：按账号隔离（output/u<uid>/，72h TTL 由 _sweep 递归覆盖）；
    未带身份（独立运行/本机场景）维持共享目录。"""
    uid = _req_uid(req)
    if uid:
        return os.path.join(OUTPUT_DIR, "u{0}".format(uid))
    return OUTPUT_DIR


def _resolve_output(req, body):
    """成品目录最终解析（安全收口）：多账号在线模式（带身份）强制按账号隔离，
    一律忽略客户端自定义 output_dir——否则登录用户可指向他人 u<uid>/ 目录
    越权读写，或指向服务器任意绝对路径覆盖文件；无身份（本机独立运行）才允许自定义。"""
    uid = _req_uid(req)
    if uid:
        return _default_output(req)
    return (body.get("output_dir") or "").strip() or OUTPUT_DIR


def _records_root():
    return os.path.join(DATA, "records")


def _record_thumbs(output_dir, names, n=3):
    """记录缩略图：前 n 张成品压成 ≤160px JPEG base64 嵌进记录 JSON——
    成品文件 72h 过期后，素材库里记录仍然能显示小图。"""
    thumbs = []
    for nm in names[:n]:
        try:
            rel = safe_rel(nm)
            if not rel:
                continue
            p = os.path.join(output_dir, *rel.split("/"))
            img = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            h, w = img.shape[:2]
            s = 160.0 / max(h, w)
            if s < 1.0:
                img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))))
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                thumbs.append("data:image/jpeg;base64,"
                              + base64.b64encode(buf.tobytes()).decode("ascii"))
        except Exception:
            pass
    return thumbs


@app.get("/api/config")
def get_config(req: Request):
    cfg = cfgmod.load_config()
    if _is_admin_req(req):
        return cfg
    # 非主账号：脱敏副本（页面初始化需要引擎/路径，但绝不给密钥）
    cfg = copy.deepcopy(cfg)
    for sec in ("lensapi", "modelscope", "wanx", "qwen", "custom"):
        s = cfg.get(sec)
        if isinstance(s, dict) and s.get("api_key"):
            s["api_key"] = _mask_key(s["api_key"])
    return cfg


@app.post("/api/config")
async def set_config(req: Request):
    body = await req.json()
    # 掩码 key 回写保护：收到脱敏副本里的掩码值时保留旧 key，防覆盖真密钥
    # （非 admin 的 POST 已被主站中间件 403 拦截，这里是纵深防御）。
    old = cfgmod.load_config()
    for sec in ("lensapi", "modelscope", "wanx", "qwen", "custom"):
        s = body.get(sec)
        if isinstance(s, dict) and isinstance(s.get("api_key"), str) \
                and "..." in s["api_key"]:
            s["api_key"] = (old.get(sec) or {}).get("api_key", "")
    cfgmod.save_config(body)
    return {"ok": True}


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...), label: str = Form(""),
                 paths: str = Form("")):
    """批量上传图片：每次上传存进独立子目录，互不混杂（线上版用）。
    paths=与 files 对齐的相对路径 JSON 数组（webkitdirectory 整夹上传时带目录结构），
    按源结构多层落盘；散图/拖拽无路径则平铺（行为与旧版一致）。"""
    import time as _time
    sub = "upload-" + _time.strftime("%Y%m%d-%H%M%S") + "-" + _md5(str(_time.time()))[:4]
    dest = os.path.join(DEFAULT_INPUT, sub)
    os.makedirs(dest, exist_ok=True)
    rel_paths = []
    if paths:
        try:
            rel_paths = json.loads(paths)
            if not isinstance(rel_paths, list):
                rel_paths = []
        except Exception:
            rel_paths = []
    saved = []
    for i, f in enumerate(files):
        name = os.path.basename(f.filename or "")
        ext = os.path.splitext(name)[1].lower()
        if ext not in {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}:
            continue
        data = await f.read()
        arr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            continue
        # 多层结构：优先用前端给的相对路径（清洗防穿越），散图退回文件名平铺
        rel = safe_rel(rel_paths[i]) if i < len(rel_paths) else ""
        if not rel or not os.path.basename(rel):
            rel = name
        fp = os.path.join(dest, *rel.split("/"))
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        if save_image(fp, img):
            saved.append(rel)
    # 图床：原图 COS 冷备份改为后台异步，绝不阻塞上传响应。
    # COS 慢（欠费/跨地域可达数十秒）会把请求拖到超时 → 前端 fetch 报「Failed to fetch」。
    # 前端不消费 cdn（original/preview/commit 均读本地兜底），异步化零影响。
    if saved:
        def _bg_upload():
            for _nm in saved:
                try:
                    wmstore.upload_rel(DATA, "input/%s/%s" % (sub, _nm))
                except Exception:
                    pass
        threading.Thread(target=_bg_upload, daemon=True).start()
    return {"ok": True, "saved": saved, "input_dir": dest,
            "label": label or "上传图片", "cdn": {}}


# ---------- 分片上传（弱网/移动网络抗断线） ----------
# 背景：整包 multipart 上传（几十 MB 一条请求）在移动/弱网环境下常在传输层被掐断，
# 浏览器只能报「Failed to fetch」，服务器甚至收不到请求（日志零 POST）。
# 改为 4MB 一片、逐片独立请求+前端自动重试、最后合并落盘：
# 单片几秒传完，断线只重传一片，彻底摆脱「一条巨型请求必须全程不断」的脆弱性。
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_BATCH_DIRS = {}          # batch_id -> (dest_dir, last_ts)
_BATCH_LOCK = threading.Lock()
_IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}


def _batch_dir(batch_id):
    """同一批次的所有文件落同一个 input 子目录（与整包上传的目录结构一致）。"""
    now = time.time()
    with _BATCH_LOCK:
        for k in list(_BATCH_DIRS):
            if now - _BATCH_DIRS[k][1] > 7200:
                _BATCH_DIRS.pop(k, None)
        ent = _BATCH_DIRS.get(batch_id)
        if not ent:
            sub = "upload-" + time.strftime("%Y%m%d-%H%M%S") + "-" + _md5(batch_id)[:4]
            dest = os.path.join(DEFAULT_INPUT, sub)
            os.makedirs(dest, exist_ok=True)
            ent = (dest, now)
            _BATCH_DIRS[batch_id] = ent
        else:
            _BATCH_DIRS[batch_id] = (ent[0], now)
        return ent[0]


@app.post("/api/upload_chunk")
async def upload_chunk(req: Request):
    """接收一个分片：form 字段 batch_id/file_id/index + chunk（文件）。"""
    form = await req.form()
    batch_id = str(form.get("batch_id") or "")
    file_id = str(form.get("file_id") or "")
    try:
        index = int(form.get("index") or 0)
    except (TypeError, ValueError):
        index = -1
    chunk = form.get("chunk")
    if (not _SAFE_ID.match(batch_id) or not _SAFE_ID.match(file_id)
            or index < 0 or index > 4096 or chunk is None):
        return JSONResponse({"ok": False, "error": "参数非法"}, status_code=400)
    cdir = os.path.join(DATA, "_chunks", batch_id, file_id)
    os.makedirs(cdir, exist_ok=True)
    data = await chunk.read()
    with open(os.path.join(cdir, "%06d.part" % index), "wb") as fp:
        fp.write(data)
    return {"ok": True, "index": index, "size": len(data)}


@app.get("/api/upload_status")
async def upload_status(batch_id: str = "", file_id: str = ""):
    """断点续传查询：返回某文件已成功接收的分片序号列表（前端只补传缺的片）。"""
    if not _SAFE_ID.match(batch_id or "") or not _SAFE_ID.match(file_id or ""):
        return JSONResponse({"ok": False, "error": "参数非法"}, status_code=400)
    cdir = os.path.join(DATA, "_chunks", batch_id, file_id)
    got = []
    if os.path.isdir(cdir):
        for fn in os.listdir(cdir):
            m = re.match(r"^(\d{6})\.part$", fn)
            if m:
                got.append(int(m.group(1)))
    got.sort()
    return {"ok": True, "received": got}


@app.post("/api/upload_merge")
async def upload_merge(req: Request):
    """合并某文件的全部分片 → 落 input 批次目录 → 校验可解码 → COS 异步冷备。"""
    body = await req.json()
    batch_id = str(body.get("batch_id") or "")
    file_id = str(body.get("file_id") or "")
    name = os.path.basename(str(body.get("name") or "").strip())
    label = str(body.get("label") or "")
    try:
        total = int(body.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    if not _SAFE_ID.match(batch_id) or not _SAFE_ID.match(file_id) or not name:
        return JSONResponse({"ok": False, "error": "参数非法"}, status_code=400)
    if os.path.splitext(name)[1].lower() not in _IMG_EXT:
        return JSONResponse({"ok": False, "error": "不支持的图片格式"}, status_code=400)
    if total <= 0 or total > 4096:
        return JSONResponse({"ok": False, "error": "分片数非法"}, status_code=400)
    dest = _batch_dir(batch_id)
    out = os.path.join(dest, name)
    cdir = os.path.join(DATA, "_chunks", batch_id, file_id)
    parts = [os.path.join(cdir, "%06d.part" % i) for i in range(total)]
    if not all(os.path.isfile(p) for p in parts):
        # 幂等：分片已被前一次成功合并清掉且成品已在 → 直接算成功（前端网络重试安全）
        if os.path.isfile(out) and not os.path.isdir(cdir):
            return {"ok": True, "name": name, "input_dir": dest,
                    "label": label or "上传图片", "dedup": True}
        return JSONResponse({"ok": False, "error": "分片不完整，请重传"}, status_code=400)
    with open(out, "wb") as fp:
        for p in parts:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, fp, 1024 * 1024)
    shutil.rmtree(cdir, ignore_errors=True)
    # 校验可解码（np.fromfile 兼容中文路径）
    arr = np.fromfile(out, np.uint8)
    if arr.size == 0 or cv2.imdecode(arr, cv2.IMREAD_COLOR) is None:
        try:
            os.remove(out)
        except OSError:
            pass
        return JSONResponse({"ok": False, "error": "图片无法解码"}, status_code=400)
    # 图床：COS 冷备后台异步（与整包上传同策略，绝不阻塞响应）
    def _bg(_rel=os.path.relpath(out, DATA).replace("\\", "/")):
        try:
            wmstore.upload_rel(DATA, _rel)
        except Exception:
            pass
    threading.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "name": name, "input_dir": dest,
            "label": label or "上传图片"}


@app.post("/api/scan")
async def scan(req: Request):
    body = await req.json()
    input_dir = body.get("input_dir") or DEFAULT_INPUT
    files = scan_images(input_dir)
    # 来源标签：默认输入目录（上传的图）显示「上传图片」，其余显示文件夹名
    if os.path.abspath(input_dir) == os.path.abspath(DEFAULT_INPUT):
        label = "上传图片"
    else:
        label = os.path.basename(os.path.abspath(input_dir.rstrip("\\/"))) or "图片"
    return {"files": [rel_of(f, input_dir) for f in files], "count": len(files),
            "input_dir": input_dir, "label": label}


@app.post("/api/original")
async def original(req: Request):
    """返回输入文件夹内某张原图（base64），用于前端拖框预览。仅限 input_dir 内。"""
    body = await req.json()
    input_dir = body.get("input_dir") or DEFAULT_INPUT
    filename = body.get("filename", "")
    if not input_dir or not filename:
        return JSONResponse({"ok": False, "error": "参数缺失"}, status_code=400)
    base = os.path.abspath(input_dir)
    path = os.path.abspath(os.path.join(input_dir, filename))
    if not path.startswith(base):
        return JSONResponse({"ok": False, "error": "越界访问"}, status_code=403)
    img = read_image(path)
    if img is None:
        return JSONResponse({"ok": False, "error": "无法读取图片"}, status_code=404)
    ok, buf = cv2.imencode(".jpg", img)
    b64 = base64.b64encode(buf.tobytes()).decode("utf-8")
    return {"ok": True, "data": "data:image/jpeg;base64," + b64,
            "width": img.shape[1], "height": img.shape[0]}


@app.post("/api/models")
async def list_models(req: Request):
    """按填好的 Base URL + API Key 从平台拉取可用模型列表（OpenAI 兼容 GET /models）。"""
    body = await req.json()
    base_url = (body.get("base_url") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    if not base_url:
        return JSONResponse({"ok": False, "error": "请先填写 Base URL"}, status_code=400)
    if not api_key:
        return JSONResponse({"ok": False, "error": "请先填写 API Key"}, status_code=400)
    base = base_url.rstrip("/")
    # 兼容用户填完整 endpoint 的情况：剥掉已知的路径后缀，回归 /v1 层
    for suffix in ("/chat/completions", "/images/edits", "/images/generations", "/models"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    url = base + "/models"
    headers = {"Authorization": "Bearer " + api_key}
    try:
        resp = _requests.get(url, headers=headers, timeout=20)
    except Exception as e:
        return JSONResponse({"ok": False, "error": "连接失败：%s" % str(e)[:200]}, status_code=502)
    if resp.status_code != 200:
        return JSONResponse(
            {"ok": False, "error": "HTTP %s %s" % (resp.status_code, resp.text[:200])},
            status_code=502,
        )
    try:
        data = resp.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "返回不是 JSON，请检查 Base URL"}, status_code=502)
    # OpenAI 兼容：{data:[{id:...},...]}；也兜底处理纯数组 / {models:[...]}
    raw = []
    if isinstance(data, dict):
        raw = data.get("data") or data.get("models") or []
    elif isinstance(data, list):
        raw = data
    ids = []
    for it in raw:
        if isinstance(it, str):
            ids.append(it)
        elif isinstance(it, dict):
            mid = it.get("id") or it.get("name") or it.get("model")
            if mid:
                ids.append(str(mid))
    ids = sorted(set(ids))
    return {"ok": True, "models": ids, "count": len(ids)}


def _migrate_old_template():
    """旧版单一样例（data/wm_template）一次性迁移为样例库里的第一个样例。"""
    old_tp = os.path.join(OLD_TEMPLATE_DIR, "template.png")
    old_mp = os.path.join(OLD_TEMPLATE_DIR, "mask.png")
    if not (os.path.exists(old_tp) and os.path.exists(old_mp)):
        return
    os.makedirs(TEMPLATES_ROOT, exist_ok=True)
    if os.listdir(TEMPLATES_ROOT):
        return  # 库里已有样例，不迁移
    tid = "migrated-01"
    dst = os.path.join(TEMPLATES_ROOT, tid)
    os.makedirs(dst, exist_ok=True)
    shutil.copyfile(old_tp, os.path.join(dst, "template.png"))
    shutil.copyfile(old_mp, os.path.join(dst, "mask.png"))
    meta = {"id": tid, "name": "导入的旧样例", "sample_file": "",
            "width": 0, "height": 0, "threshold": 0.65, "created_at": ""}
    old_meta = os.path.join(OLD_TEMPLATE_DIR, "meta.json")
    if os.path.exists(old_meta):
        try:
            with open(old_meta, "r", encoding="utf-8") as f:
                m = json.load(f)
            meta["sample_file"] = m.get("sample_file", "")
            meta["threshold"] = float(m.get("threshold", 0.65))
        except Exception:
            pass
    t = read_image(old_tp)
    if t is not None:
        meta["width"], meta["height"] = int(t.shape[1]), int(t.shape[0])
    with open(os.path.join(dst, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _tpl_preview_b64(template, tmask):
    """模板 + 红色涂抹形状叠加 → jpg base64（列表缩略图 / 保存回显共用）。"""
    ov = template.copy()
    if tmask is not None and tmask.shape[:2] == template.shape[:2]:
        ov[tmask > 0] = (0, 0, 255)
    ok, buf = cv2.imencode(".jpg", ov, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return base64.b64encode(buf.tobytes()).decode("utf-8")


def _tpl_dir(tid):
    return os.path.join(TEMPLATES_ROOT, tid)


def _valid_tid(tid):
    return bool(tid) and bool(_ID_RE.match(tid)) and os.path.isdir(_tpl_dir(tid))


@app.get("/api/templates")
def list_templates():
    """水印样例库列表（含缩略图）。首次访问时迁移旧版单一样例。"""
    _migrate_old_template()
    items = []
    if os.path.isdir(TEMPLATES_ROOT):
        for tid in sorted(os.listdir(TEMPLATES_ROOT)):
            if not _valid_tid(tid):
                continue
            meta_path = os.path.join(_tpl_dir(tid), "meta.json")
            meta = {}
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                pass
            template, tmask = _load_template(tid)
            if template is None:
                continue
            items.append({
                "id": tid,
                "name": meta.get("name") or tid,
                "sample_file": meta.get("sample_file", ""),
                "width": int(template.shape[1]),
                "height": int(template.shape[0]),
                "threshold": float(meta.get("threshold", 0.65)),
                "created_at": meta.get("created_at", ""),
                "preview": "data:image/jpeg;base64," + _tpl_preview_b64(template, tmask),
            })
    items.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return {"ok": True, "templates": items, "count": len(items)}


@app.post("/api/save_template")
async def save_template(req: Request):
    """涂抹窗口保存：生成一个可命名的水印样例，独立子目录存放（可存多个）。"""
    body = await req.json()
    input_dir = body.get("input_dir") or DEFAULT_INPUT
    filename = body.get("filename", "")
    strokes = body.get("strokes") or []
    threshold = float(body.get("threshold", 0.65))
    name = (body.get("name") or "").strip()
    if not filename:
        return JSONResponse({"ok": False, "error": "请先选一张样图"}, status_code=400)
    base = os.path.abspath(input_dir)
    path = os.path.abspath(os.path.join(input_dir, filename))
    if not path.startswith(base):
        return JSONResponse({"ok": False, "error": "越界访问"}, status_code=403)
    img = read_image(path)
    if img is None:
        return JSONResponse({"ok": False, "error": "无法读取样图"}, status_code=404)
    if not strokes:
        return JSONResponse({"ok": False, "error": "请先在图上涂抹水印"}, status_code=400)
    template, tmask = extract_template(img, strokes)
    if template is None:
        return JSONResponse({"ok": False, "error": "涂抹区域无效"}, status_code=400)
    # 样例名默认自动编号
    if not name:
        n = 1
        existing = _existing_template_names()
        while "样例%d" % n in existing:
            n += 1
        name = "样例%d" % n
    tid = time.strftime("%Y%m%d%H%M%S") + "-%04d" % (int(time.time() * 1000) % 10000)
    d = _tpl_dir(tid)
    os.makedirs(d, exist_ok=True)
    save_image(os.path.join(d, "template.png"), template)
    ok, buf = cv2.imencode(".png", tmask)
    with open(os.path.join(d, "mask.png"), "wb") as f:
        f.write(buf.tobytes())
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({
            "id": tid,
            "name": name,
            "sample_file": filename,
            "width": int(template.shape[1]),
            "height": int(template.shape[0]),
            "threshold": threshold,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, f, ensure_ascii=False, indent=2)
    # 图床：样例库冷备份（用户资产，永久保留、不参与 72h 清理）
    for fn in ("template.png", "mask.png"):
        wmstore.upload_rel(DATA, "wm_templates/%s/%s" % (tid, fn))
    return {
        "ok": True,
        "id": tid,
        "name": name,
        "preview": "data:image/jpeg;base64," + _tpl_preview_b64(template, tmask),
        "width": int(template.shape[1]),
        "height": int(template.shape[0]),
        "sample_file": filename,
        "threshold": threshold,
    }


def _existing_template_names():
    names = set()
    if not os.path.isdir(TEMPLATES_ROOT):
        return names
    for tid in os.listdir(TEMPLATES_ROOT):
        mp = os.path.join(_tpl_dir(tid), "meta.json")
        try:
            with open(mp, "r", encoding="utf-8") as f:
                names.add(json.load(f).get("name", ""))
        except Exception:
            pass
    return names


@app.post("/api/delete_template")
async def delete_template(req: Request):
    body = await req.json()
    tid = (body.get("id") or "").strip()
    if not _valid_tid(tid):
        return JSONResponse({"ok": False, "error": "样例不存在"}, status_code=404)
    shutil.rmtree(_tpl_dir(tid), ignore_errors=True)
    wmstore.delete_rels(["wm_templates/%s/template.png" % tid,
                         "wm_templates/%s/mask.png" % tid])
    return {"ok": True, "id": tid}


@app.post("/api/templates/update")
async def update_template(req: Request):
    """更新样例的名称 / 匹配灵敏度（滑块改动回存）。"""
    body = await req.json()
    tid = (body.get("id") or "").strip()
    if not _valid_tid(tid):
        return JSONResponse({"ok": False, "error": "样例不存在"}, status_code=404)
    meta_path = os.path.join(_tpl_dir(tid), "meta.json")
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        meta = {"id": tid}
    if "name" in body:
        name = (body.get("name") or "").strip()
        if name:
            meta["name"] = name
    if "threshold" in body:
        try:
            meta["threshold"] = float(body.get("threshold"))
        except (TypeError, ValueError):
            pass
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return {"ok": True, "id": tid, "name": meta.get("name", ""), "threshold": meta.get("threshold", 0.65)}


def _load_template(tid=None):
    """读取指定水印样例；tid 为空时取库里最新的一个（兜底）。"""
    if tid and not _valid_tid(tid):
        return None, None
    if not tid:
        _migrate_old_template()
        if not os.path.isdir(TEMPLATES_ROOT):
            return None, None
        cands = sorted(os.listdir(TEMPLATES_ROOT), reverse=True)
        for c in cands:
            if _valid_tid(c):
                tid = c
                break
        if not tid:
            return None, None
    tp = os.path.join(_tpl_dir(tid), "template.png")
    mp = os.path.join(_tpl_dir(tid), "mask.png")
    if not (os.path.exists(tp) and os.path.exists(mp)):
        return None, None
    template = read_image(tp)
    data = np.fromfile(mp, dtype=np.uint8)
    tmask = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    if template is None or tmask is None:
        return None, None
    return template, tmask


@app.post("/api/preview")
async def preview(req: Request):
    body = await req.json()
    input_dir = body.get("input_dir") or DEFAULT_INPUT
    region = body.get("region", {"x": 0, "y": 0, "w": 0, "h": 0})
    engine_name = body.get("engine", "lensapi")
    engine_config = body.get("engine_config", {})
    if engine_name in RETIRED:
        # 本地引擎已下线（依赖已删除的样例涂抹定位）
        return JSONResponse(
            {"results": [], "total": 0, "ok_count": 0,
             "error": "本地引擎（OpenCV / LaMa）已下线，请改用云端引擎（魔搭 / 万相 / Qwen / 自定义模型）"},
            status_code=400)
    engine = get_engine(engine_name)

    files = scan_images(input_dir)
    # 支持只处理指定文件（前端逐张调用时传 files: [相对路径]；兼容旧版传纯文件名）
    only = body.get("files") or []
    if only:
        wanted = {safe_rel(f) or os.path.basename(f) for f in only}
        files = [f for f in files
                 if rel_of(f, input_dir) in wanted or os.path.basename(f) in wanted]
    results = []
    # 模板匹配模式：按样例 ID 载入水印样例（未传 ID 则取最新一个兜底）
    template = tmask = None
    is_template_mode = (region.get("type") or "").lower() == "template"
    if is_template_mode:
        threshold = float(body.get("threshold", 0.65))
        template_id = (body.get("template_id") or "").strip()
        template, tmask = _load_template(template_id or None)
        if template is None:
            return JSONResponse(
                {"results": [], "total": 0, "ok_count": 0,
                 "error": "还没有水印样例：点「打开涂抹窗口」，在一张图上涂抹水印并保存样例"},
                status_code=400)
    # 72h 清理会把空的 results/ 目录 rmdir（只保护 preview/ 根目录），写入前确保目录存在
    # 否则运行中被删后 buf.tofile 找不到父目录，抛 [Errno 2] No such file or directory
    os.makedirs(RESULTS, exist_ok=True)
    for f in files:
        rel = rel_of(f, input_dir)
        name = os.path.basename(f)
        key = _md5("{0}|{1}".format(_req_uid(req) or "", rel))
        # 多层结构下同名文件按相对路径区分；带身份时混入 uid 盐——RESULTS 目录全局共享，
        # 不加盐则他人可按常见文件名 md5 反推 result_key，经 commit/preview 越权取走别人的处理结果
        ext = os.path.splitext(name)[1].lower() or ".png"
        try:
            prebuilt_mask = None
            used_region = region
            if is_template_mode:
                img0 = read_image(f)
                if img0 is None:
                    raise RuntimeError("无法读取图片")
                score, top_left, scale = find_watermark(img0, template, tmask, threshold=threshold)
                if top_left is None:
                    results.append({
                        "name": name, "rel": rel, "ok": False,
                        "error": "未检测到同款水印（相似度 %.2f 低于阈值 %.2f），已跳过，不会误抹" % (score, threshold),
                        "skipped": True, "score": round(score, 3),
                    })
                    continue
                prebuilt_mask = template_mask_for_image(img0.shape, template, tmask, top_left, scale)
                th_, tw_ = tmask.shape[:2]
                used_region = {
                    "type": "rect",
                    "x": int(top_left[0]), "y": int(top_left[1]),
                    "w": int(round(tw_ * scale)), "h": int(round(th_ * scale)),
                }
            original, result = process_image(f, engine, used_region, engine_config, prebuilt_mask=prebuilt_mask)
            result_key = "result_" + key + ext
            result_path = os.path.join(RESULTS, result_key)
            save_image(result_path, result)

            comp = make_comparison(original, result)
            comp_name = "compare_" + key + ".jpg"
            save_image(os.path.join(PREVIEW, comp_name), comp)

            # 原图缩略：供前端「原图 ⇄ 成品」对比滑块（cmp.js）拖动对齐。
            # 与结果图同生命周期——都落在 preview/ 下，随 72h TTL 一起清，不新增清理逻辑。
            src_name = ""
            try:
                src_name = "src_" + key + ".jpg"
                save_image(os.path.join(PREVIEW, src_name), _thumb(original, 1200))
            except Exception:
                src_name = ""

            # 图床：结果图 + 对比图双写 COS 改为后台异步，绝不阻塞逐张批量处理。
            # COS 慢会把 preview 拖到超时；前端对产物图本就有「COS 失效 → 本地 /preview 兜底」。
            result_rel = "preview/results/" + result_key
            comp_rel = "preview/" + comp_name
            src_rel = ("preview/" + src_name) if src_name else ""
            def _bg_cdn(_rr=result_rel, _cr=comp_rel, _sr=src_rel):
                try:
                    wmstore.upload_rel(DATA, _rr)
                    wmstore.upload_rel(DATA, _cr)
                    if _sr:
                        wmstore.upload_rel(DATA, _sr)
                except Exception:
                    pass
            threading.Thread(target=_bg_cdn, daemon=True).start()
            item = {
                "name": name,
                "rel": rel,
                "result_key": result_key,
                "result_url": result_rel,          # 成品图（对比滑块上层/底层用）
                "src_url": src_rel,                # 原图缩略（老记录可能为空 → 前端退回拼接图）
                "compare_url": "preview/" + comp_name,
                "cdn": "",
                "ok": True,
            }
            if is_template_mode:
                item["score"] = round(score, 3)
            results.append(item)
        except Exception as e:
            results.append({"name": name, "rel": rel, "ok": False, "error": str(e)})

    ok_count = sum(1 for r in results if r.get("ok"))
    return {"results": results, "total": len(results), "ok_count": ok_count}


# ---------------- 输出放大档位（2026-11-15 新增） ----------------
# 背景：去水印的成品必须能贴回原图，所以**比例绝不能改**（改了位置就对不上）。
# 用户唯一合理的诉求是「输出大一点」，这里只做等比放大，不动构图。
# ⚠️ 放大是 LANCZOS 插值，**不会增加细节**（那不是超分模型），只是像素变多、打印更清晰。
WM_UP = {"1": 0, "2k": 2048, "4k": 4096}


def upscale_for_output(img, up="1"):
    """按目标长边等比放大；**只放大不缩小**（原图已更大就原样返回，绝不让用户越存越小）。"""
    long_side = WM_UP.get(str(up or "1").strip().lower(), 0)
    if not long_side or img is None:
        return img
    h, w = img.shape[:2]
    m = max(w, h)
    if m <= 0 or m >= long_side:
        return img
    sc = float(long_side) / m
    nw, nh = max(1, int(round(w * sc))), max(1, int(round(h * sc)))
    return cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LANCZOS4)


@app.post("/api/commit")
async def commit(req: Request):
    body = await req.json()
    output_dir = _resolve_output(req, body)
    items = body.get("items", [])  # [{name, result_key, rel?}]
    up_tier = str(body.get("up") or "1").strip().lower()   # 输出放大：1 / 2k / 4k（非法值=不放大）
    if up_tier not in WM_UP:
        up_tier = "1"
    os.makedirs(output_dir, exist_ok=True)
    written = []
    for it in items:
        name = it.get("name", "")
        key = it.get("result_key", "")
        # 多层结构：优先按源相对路径镜像还原（清洗防穿越），无 rel 退回旧平铺行为
        rel = safe_rel(it.get("rel", ""))
        out_name = rel if (rel and os.path.basename(rel)) else name
        src = os.path.join(RESULTS, key)
        if not out_name or not key or not os.path.exists(src):
            continue
        img = None
        try:
            data = np.fromfile(src, dtype=np.uint8)
            img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        except Exception:
            img = None
        if img is not None:
            img = upscale_for_output(img, up_tier)   # 只放大尺寸，不动比例
            dst = os.path.join(output_dir, *out_name.split("/"))
            os.makedirs(os.path.dirname(dst) or output_dir, exist_ok=True)
            if save_image(dst, img):
                written.append(out_name)
    # 处理记录落盘（素材库）：仅服务端成品目录（DATA 内）才记录；
    # 用户自定义绝对路径属本机场景，不落记录。
    uid = _req_uid(req)
    rec_path = ""
    srcs = []
    if uid and written and os.path.abspath(output_dir).startswith(os.path.abspath(DATA)):
        try:
            rid = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
            rdir = os.path.join(_records_root(), "u{0}".format(uid))
            os.makedirs(rdir, exist_ok=True)
            # 每张成品对齐收集原图缩略（素材库「原图⇄成品」平铺对比用）：
            # 只接受 preview/src_*.jpg 形态且文件真实存在，防客户端伪造路径。
            for it in items:
                srel = it.get("src_url") or ""
                if (srel and re.match(r"^preview/src_[A-Za-z0-9_.\-]+\.jpg$", srel)
                        and os.path.isfile(os.path.join(DATA, *srel.split("/")))):
                    srcs.append(srel)
                else:
                    srcs.append("")
            rec = {"id": rid, "ts": time.time(), "count": len(written),
                   "names": written, "thumbs": _record_thumbs(output_dir, written),
                   "srcs": srcs, "cdn": {}}
            rec_path = os.path.join(rdir, rid + ".json")
            with open(rec_path, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False)
        except Exception:
            rec_path = ""
    # 图床：成品双写 COS 改为后台异步，绝不阻塞打包/提交响应（与 upload/preview 一致）。
    # 前端打包下载走 /api/download，不依赖 COS 直链；COS 失效也有本地 /preview 兜底。
    #
    # 🔴 2026-10-15（雪碧：素材库对比图打开好慢）：上传完必须把 {rel: COS 直链} **回写进记录**。
    #   改造前记录里 `cdn` 恒为空串，素材库只能拼 `wm/api/out?n=...` 回源 —— 服务器出口实测
    #   ~1Mbps，一张对比图要等好几秒；有了这份映射，素材库直接给 CDN 直链，秒开。
    #   线程放在记录落盘**之后**启动：否则上传可能先于 JSON 写盘完成 → 回写时文件还不存在。
    if written and os.path.abspath(output_dir).startswith(os.path.abspath(DATA)):
        _outs = []
        for _nm in written:
            try:
                _outs.append(os.path.relpath(
                    os.path.join(output_dir, _nm), DATA).replace("\\", "/"))
            except Exception:
                pass
        _srcs_all = [s for s in srcs if s]

        def _bg_commit(_outs=_outs, _srs=_srcs_all, _rp=rec_path):
            cdn = {}
            for _rel in list(_outs) + list(_srs):
                try:
                    _u = wmstore.upload_rel(DATA, _rel)
                    if _u:
                        cdn[_rel] = _u
                except Exception:
                    pass
            if cdn and _rp and os.path.isfile(_rp):
                try:                                  # 原子替换，防读到半截 JSON
                    with open(_rp, encoding="utf-8") as _f:
                        _rec = json.load(_f)
                    _rec["cdn"] = cdn
                    _tmp = _rp + ".tmp"
                    with open(_tmp, "w", encoding="utf-8") as _f:
                        json.dump(_rec, _f, ensure_ascii=False)
                    os.replace(_tmp, _rp)
                except Exception:
                    pass

        threading.Thread(target=_bg_commit, daemon=True).start()
    return {"ok": True, "written": written, "cdn": {}}


def _out_thumb(p: str, owner: str, w: int):
    """成品缩略（素材库对比格用，2026-10-14）：WebP 落 output/u<uid>/.thumbs/ 缓存。
    同源同尺寸直接复用；源文件更新后自动重建。任何失败返回 None（端点退回原图）。"""
    try:
        w = max(160, min(1280, int(w)))
        tdir = os.path.join(OUTPUT_DIR, owner, ".thumbs")
        os.makedirs(tdir, exist_ok=True)   # 目录可能被 72h 清理 rmdir，写前必建
        tp = os.path.join(tdir, "{0}_w{1}.webp".format(
            os.path.splitext(os.path.basename(p))[0], w))
        if os.path.isfile(tp) and os.path.getmtime(tp) >= os.path.getmtime(p):
            return tp
        from PIL import Image
        im = Image.open(p)
        im.load()
        if im.width > w:
            im = im.resize((w, max(1, round(im.height * w / im.width))), Image.LANCZOS)
        im.save(tp, "WEBP", quality=82)
        return tp
    except Exception:                      # noqa: BLE001
        return None


@app.get("/api/out")
def out_file(req: Request, n: str = "", u: str = "", w: int = 0):
    """素材库取成品图（按账号隔离）：只读 output/u<uid>/ 下的 commit 成品。
    普通用户只能读自己的；admin 可带 u=u<uid> 读他人成品（素材库 admin 全量视图）。
    n 为 commit 记录里的成品名，走 safe_rel 防目录穿越。
    w>0 时返回 WebP 缩略（素材库对比格专用；服务器出口仅 58~328KB/s，全尺寸太慢）。"""
    uid = _req_uid(req)
    if not uid:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    owner = "u{0}".format(uid)
    if u and _is_admin_req(req) and re.match(r"^u\d+$", u):
        owner = u
    rel = safe_rel(n)
    if not rel:
        return JSONResponse({"ok": False, "error": "参数不合法"}, status_code=404)
    p = os.path.join(OUTPUT_DIR, owner, *rel.split("/"))
    if not os.path.isfile(p):
        return JSONResponse({"ok": False, "error": "成品不存在（可能已过 72h 清理）"},
                            status_code=404)
    if w > 0:
        tp = _out_thumb(p, owner, w)
        if tp:
            return FileResponse(tp, media_type="image/webp",
                                headers={"Cache-Control": "private, max-age=3600"})
    return FileResponse(p, filename=os.path.basename(p))


# ---- 处理记录查询/删除（素材库数据源；成品文件本身按 72h TTL 自动清理） ----
@app.get("/api/records")
def list_records(req: Request):
    """普通用户只看自己的记录；admin 看全部用户的（记录里带 owner 标记）。"""
    uid = _req_uid(req)
    root = _records_root()
    dirs = []
    if _is_admin_req(req):
        try:
            dirs = [os.path.join(root, d) for d in os.listdir(root)
                    if re.match(r"^u\d+$", d)]
        except Exception:
            dirs = []
    elif uid:
        dirs = [os.path.join(root, "u{0}".format(uid))]
    recs = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        owner = os.path.basename(d)
        for fn in os.listdir(d):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    r = json.load(f)
                r["owner"] = owner
                recs.append(r)
            except Exception:
                pass
    recs.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return {"records": recs[:100]}


@app.post("/api/records/delete")
async def delete_records(req: Request):
    """删处理记录（只删记录 JSON，成品文件随 72h TTL 自然过期）。
    body {ids:[...]} 或 {all:true}；非 admin 只能删自己目录下的。"""
    body = await req.json()
    uid = _req_uid(req)
    root = _records_root()
    ids = [r for r in (body.get("ids") or []) if _ID_RE.match(str(r))]
    targets = []
    if _is_admin_req(req):
        cand = []
        try:
            cand = [os.path.join(root, d) for d in os.listdir(root)
                    if re.match(r"^u\d+$", d)]
        except Exception:
            cand = []
        for dd in cand:
            if body.get("all"):
                targets += [os.path.join(dd, fn) for fn in os.listdir(dd)
                            if fn.endswith(".json")]
            else:
                targets += [os.path.join(dd, r + ".json") for r in ids]
    else:
        if not uid:
            return {"ok": False, "error": "未登录"}
        dd = os.path.join(root, "u{0}".format(uid))
        if body.get("all") and os.path.isdir(dd):
            targets = [os.path.join(dd, fn) for fn in os.listdir(dd)
                       if fn.endswith(".json")]
        else:
            targets = [os.path.join(dd, r + ".json") for r in ids]
    deleted, skipped = 0, 0
    for p in targets:
        ap = os.path.abspath(p)
        if not ap.startswith(os.path.abspath(root) + os.sep) or not os.path.isfile(ap):
            skipped += 1
            continue
        try:
            os.remove(ap)
            deleted += 1
        except Exception:
            skipped += 1
    return {"ok": True, "deleted": deleted, "skipped": skipped}


@app.post("/api/download")
async def download(req: Request):
    """把输出目录里本次写入的图片打包成 zip 下载（线上版闭环用）。
    names 支持带子目录的相对路径 → zip 内保留源文件夹结构，解压即得镜像新文件夹。"""
    body = await req.json()
    output_dir = _resolve_output(req, body)
    names = body.get("names", [])
    os.makedirs(output_dir, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            rel = safe_rel(name)
            if not rel:
                continue
            p = os.path.join(output_dir, *rel.split("/"))
            if os.path.exists(p):
                zf.write(p, arcname=rel)
    buf.seek(0)
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="watermark_clean.zip"'},
    )


# ============================================================
# 产物保留 N 小时 · 自动清理（input / preview / output）
# ============================================================

def _sweep(ttl_hours=None, dry_run=False):
    """删除超过保留期的产物文件（按文件 mtime 判断），返回统计。

    - 覆盖：上传的原图（input/）、结果图与对比图（preview/）、写入的成品（output/）
    - 不动：wm_templates（用户涂抹样例库，属配置资产）
    - 保护 input/preview/output 三个根目录本身不被删
    """
    ttl = TTL_HOURS if ttl_hours is None else float(ttl_hours)
    cutoff = time.time() - ttl * 3600
    removed = 0
    freed = 0
    dirs = 0
    dead = []          # 本地已删的相对路径 → 同步删 COS 同 key
    for sub in SWEEP_DIRS:
        base = os.path.join(DATA, sub)
        if not os.path.isdir(base):
            continue
        base_abs = os.path.abspath(base)
        for dirpath, _dirnames, filenames in os.walk(base, topdown=False):
            for fn in filenames:
                if fn.startswith("."):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    stt = os.stat(p)
                except OSError:
                    continue
                if stt.st_mtime >= cutoff:
                    continue
                if not dry_run:
                    try:
                        os.remove(p)
                    except OSError:
                        continue
                    dead.append(os.path.relpath(p, DATA).replace("\\", "/"))
                removed += 1
                freed += stt.st_size
            if os.path.abspath(dirpath) == base_abs:
                continue  # 保护根目录本身
            try:
                if not os.listdir(dirpath):
                    if not dry_run:
                        os.rmdir(dirpath)
                    dirs += 1
            except OSError:
                pass
    # 处理记录（records/u*/*.json）与成品同 TTL：成品没了记录留着也没意义
    rroot = _records_root()
    if os.path.isdir(rroot):
        for dirpath, _dn, filenames in os.walk(rroot, topdown=False):
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                try:
                    if os.path.getmtime(p) < cutoff:
                        if not dry_run:
                            os.remove(p)
                        removed += 1
                except OSError:
                    pass
            if os.path.abspath(dirpath) != os.path.abspath(rroot):
                try:
                    if not os.listdir(dirpath):
                        if not dry_run:
                            os.rmdir(dirpath)
                        dirs += 1
                except OSError:
                    pass
    # 图床同步：本地删掉的文件，COS 上的对象一并清掉（与图案提取同规则）
    cos = wmstore.delete_rels(dead) if (dead and not dry_run) else {"deleted": 0, "keys": 0}
    return {"removed": removed, "dirs": dirs,
            "freed_mb": round(freed / 1048576.0, 2), "ttl_hours": ttl,
            "cos_keys": cos.get("keys", 0), "cos_deleted": cos.get("deleted", 0)}


def _log_clean(stat):
    rec = dict(stat)
    rec["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        hist = []
        if os.path.exists(CLEANUP_LOG):
            with open(CLEANUP_LOG, "r", encoding="utf-8") as f:
                hist = json.load(f)
            if not isinstance(hist, list):
                hist = []
        hist.append(rec)
        with open(CLEANUP_LOG, "w", encoding="utf-8") as f:
            json.dump(hist[-100:], f, ensure_ascii=False, indent=1)
    except Exception:
        pass


@app.get("/api/cleanup/status")
def cleanup_status():
    hist = []
    try:
        if os.path.exists(CLEANUP_LOG):
            with open(CLEANUP_LOG, "r", encoding="utf-8") as f:
                hist = json.load(f)
    except Exception:
        hist = []
    return {"ttl_hours": TTL_HOURS, "interval_sec": CLEANUP_INTERVAL,
            "sweep_dirs": list(SWEEP_DIRS),
            "cos": wmstore.info(),
            "history": hist[-10:] if isinstance(hist, list) else []}


@app.post("/api/cleanup/run")
async def cleanup_run(req: Request):
    """手动触发一次清理；dry_run=true 只预演不删。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    dry = bool(body.get("dry_run"))
    stat = _sweep(dry_run=dry)
    stat["cos_sweep"] = _cos_sweep(dry_run=dry)   # 前缀级孤儿兜底（含样例库保留）
    if not dry:
        _log_clean(stat)
    return {"ok": True, "dry_run": dry, **stat}


_clean_thread = None


def _cleanup_loop():
    time.sleep(120)  # 启动缓冲，避免与首个请求抢 IO
    while True:
        try:
            stat = _sweep()
            stat["cos_sweep"] = _cos_sweep()
            if stat["removed"] or stat["cos_sweep"].get("deleted"):
                _log_clean(stat)
        except Exception:
            pass
        time.sleep(CLEANUP_INTERVAL)


def start_cleanup():
    """启动后台清理线程（幂等）。WM_NO_THREADS=1 时不启（诊断模式）。"""
    global _clean_thread
    if os.environ.get("WM_NO_THREADS") == "1":
        return
    if _clean_thread is not None and _clean_thread.is_alive():
        return
    _clean_thread = threading.Thread(target=_cleanup_loop, name="wm-cleanup", daemon=True)
    _clean_thread.start()


start_cleanup()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("WM_PORT", "3010")))
