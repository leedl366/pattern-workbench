"""高清修复工作台 —— FastAPI 子应用（挂载在父应用 /hd 路径下）。

功能：上传 1~N 张满幅图案 → gpt-image-2 按「版式死锁」prompt 1:1 重绘高清版
→ 按亮度（Otsu 自动阈值）拆「主体（亮纹）/ 底纹（暗底）」两层 → 组装分层 PSD。

计费（父应用 accounts 中间件）：POST /hd/api/fix = 3 分/张；PSD 首次下载 3 分/张。
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
import urllib.request
import uuid

import numpy as np
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from PIL import Image

_HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(_HERE)                     # app/ 根（复用 pipeline / image2psd）
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from pipeline import API_BASE, API_MODEL_CHAIN, _multipart, as_str, load_key  # noqa: E402
import ai_sites                                             # noqa: E402 —— 站点池（同模型跨站点轮询 + 错误脱敏）

# ---- COS 图床（2026-10-15 接入）----
# hd 此前是四板块里**唯一零 COS** 的：pn/g/thumb/src/psd 全走服务器出口回源，
# 出口带宽实测 ~1Mbps → 素材库对比图与 hd 页本身都慢。此处接上与 pt/wm/duotu 同规则：
# 本地 data/ 双写兜底，COS 传成功则对外给直链（端点 302、记录写 `cdn` 映射）。
try:
    from hd import hdstore                       # noqa: E402
except Exception:                                # noqa: BLE001
    try:
        import hdstore                           # noqa: E402 —— 独立运行时同目录
    except Exception:                            # noqa: BLE001
        hdstore = None
        print("[hd] hdstore 不可用，COS 关闭（全走本地文件服务）")

DATA = os.path.join(_HERE, "data")
RECORDS = os.path.join(DATA, "records")
OUTPUT = os.path.join(DATA, "output")
FRONTEND = os.path.join(_HERE, "frontend")
for _d in (RECORDS, OUTPUT):
    os.makedirs(_d, exist_ok=True)

MAX_IMAGES = 20                                   # 单次最多张数（对齐批量模式）
MAX_BYTES = 12 * 1024 * 1024                      # 单张上限
TTL_HOURS = float(os.environ.get("HD_TTL_HOURS", "72"))
CLEANUP_INTERVAL = int(os.environ.get("HD_CLEANUP_SEC", "900"))
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# ---------------- 生图 prompt（与 2026-10-13 实测 corr=0.880 的「AI高清版3」完全同款） ----------------
PROMPT = ('这是一张满幅图案。请把它重绘成一张高清、干净的同款满幅图案：'
          '每一个元素的形状、位置、大小、数量、朝向、相对间距必须完全不变，像描图一样 1:1 复刻；'
          '禁止新增任何元素，禁止删除任何元素，禁止移动、旋转、缩放、重排任何元素，'
          '禁止改变画面构图、留白与疏密布局。允许做的：只把线条画干净、颜色涂均匀、'
          '去掉噪点杂色与模糊，大幅提升清晰度与细节质感。输出：整幅画布就是图案本身，不做任何版式改动。')

# ---------------- 产品实拍图专用 prompt（2026-10-21 新增）----------------
# 起因：雪碧拿一张「手持手机壳放在大理石桌面上」的实拍照片来修清晰度，
#       结果被上面的 PROMPT 判成「满幅图案」，把壳子/手/桌面全删了、图案提纯铺满画布 ——
#       即所谓「抓取图案」。根因不是提示词被污染，而是**上面那条只为平铺图案图而写**：
#       首句「这是一张满幅图案」+ 尾句「整幅画布就是图案本身」＝直接指示模型把图案当画布。
# 本条的差别（每一条都是在堵一种跑偏）：
#   · 开门就否定「图案平铺图」这个理解，并明令「不要提取图案」→ 堵住抓取；
#   · 逐项点名必须原样保留的东西（主体/手/背景/光影/透视/构图/位置/颜色）→ 堵住重排与换背景；
#   · 只允许「去噪 / 去模糊 / 锐化 / 提细节」→ 把改动收窄到画质维度；
#   · 收尾再强调「比原图更清晰、其余完全一致」→ 给模型一个明确的成败判据。
# ⚠️ 已知副作用（已与雪碧确认接受）：AI 重绘实拍图，壳体轮廓、手指、桌面纹理、图案细线
#    仍可能有轻微微调；文字/logo 尤其可能被「重写成相似的形状」。要 100% 不动内容只能走本地锐化。
PROMPT_PHOTO = ('这是一张产品实拍照片，不是图案平铺图，不要提取图案。'
                '请只对这张照片做画质修复：去掉噪点与压缩杂色、消除模糊、锐化边缘，'
                '让画面变成一张清晰的高清照片，细节与质感明显提升。'
                '照片里的一切必须保持原样：主体产品、手部、桌面与背景、光影与反光、'
                '拍摄视角与透视、构图与物体位置、颜色与材质，全部严格不变；'
                '禁止把图案从产品上抽取或平铺出来，禁止更换或简化背景，'
                '禁止增删任何物体，禁止改变任何物体的形状、大小、位置与朝向，'
                '禁止移动或裁切画面。输出：一张比原图更清晰、其余完全一致的照片。')
# 模型优先级（外层）：**与 pt / wm 同源**（2026-10-15 修复）。
# 旧值 ("gpt-image-2", "gpt-image-2.5") 是四板块里**唯一没有 gemini 兜底**的链：
# 一旦两个 gpt-image 型号在上游同时不可用（lensapi 中继池过载 477 + grsai「模型修复中」400），
# hd 必然 4 跳全挂 → 前端固定报「生图失败（错误码 400）」。
# 现直接引用 pipeline.API_MODEL_CHAIN（默认含 gemini-3-pro-image-preview 兜底，
# 且可被 env PATTERN_API_MODELS 统一覆盖）——三板块同源，改链只改一处。
MODEL_CHAIN = API_MODEL_CHAIN


def hd_size(case_w, case_h, short=1024, lo=0.40, hi=3.00, cap=2048):
    """高清修复专用请求尺寸：**按源图真实长宽比**出图（2026-10-15 修正）。

    与 pipeline.fullbleed_size 的关键区别：
      fullbleed_size 会把长宽比**强行夹在 1.5~2.6**（为手机壳满幅场景设计），
      于是 500×500 的方图被算成 1024×1536（2:3 竖版）——方图被硬拉变形，
      与高清修复「1:1 复刻原版式」的目标直接冲突（成品不会缩回源尺寸）。
      这里改为只防极端比例（0.40~3.00），其余一律按源图真实比例：
        方图 500×500   → 1024×1024  （修正前：1024×1536 ✗）
        竖图 500×1000  → 1024×2048
        横图 1000×500  → 2048×1024→压回 cap 后等比 2048×1024
    短边固定 short，长边 = short×比例；长边超 cap 时整体等比压回（保比例，绝不拉伸）。
    """
    try:
        w, h = float(case_w), float(case_h)
    except Exception:
        return "%dx%d" % (short, short)
    if w <= 0 or h <= 0:
        return "%dx%d" % (short, short)
    r = h / w
    r = min(max(r, lo), hi)
    if r >= 1.0:
        W, H = float(short), short * r
    else:
        W, H = short / r, float(short)
    m = max(W, H)
    if m > cap:
        s = cap / m
        W, H = W * s, H * s
    W = max(2, int(round(W / 2.0) * 2))
    H = max(2, int(round(H / 2.0) * 2))
    return "%dx%d" % (W, H)

app = FastAPI(title="高清修复工作台")


# ============================================================================
# 输出比例 / 精确像素（2026-10-21 新增，用户可选）
# ----------------------------------------------------------------------------
# 设计原则（刻意如此，勿改）：
#   ① **AI 请求尺寸完全不变** —— 仍按源图真实比例走 hd_size()，提示词的「版式死锁」
#      一个字都不动。理由：模型实际不听 size（实测请求 1024×1024 实出 1254×1254），
#      让它改比例出图只会引入重排风险，把「1:1 复刻」这个核心承诺打掉。
#   ② 用户选的「比例 / 长边像素」全部在**出图之后**做：等比缩放到目标画布 + 居中 +
#      四周补边（补边色取图像四边中位色，比纯白纯黑自然）。主体绝不拉伸。
#   ③ 代价（已与用户确认并写进前端文案）：选非原图比例时成品不是满幅，四周会留底色。
# ============================================================================
# 2026-11-15：前端改成「双区域」（比例 × 分辨率），与图案提取板块同一套交互。
#   · 比例池补齐 2:3 / 3:2，并支持「自定义宽高」（直接给 WxH，此时长边被忽略）；
#   · 长边池补 2048（2K）；同时放开成「256~4096 任意偶数」以支持「自定义长边」。
#   老值（800/1024/1500/2000/3000/4096）全在范围内，历史任务与 redo 行为不变。
HD_SIDE_MIN, HD_SIDE_MAX = 256, 4096
_RATIO_POOL = ("1:1", "3:4", "4:3", "9:16", "16:9", "2:3", "3:2")   # 空串 = 原图比例
_LONG_POOL = (0, 800, 1024, 1500, 2000, 2048, 3000, 4096)   # 0 = 自动（保持 AI 原始像素）；2048=2K / 4096=4K
_MODE_POOL = ("pattern", "photo")                         # pattern=满幅图案 / photo=产品实拍图
_RATIO_PX_RE = re.compile(r"^\s*(\d{2,5})\s*[x×X*]\s*(\d{2,5})\s*$")


def _parse_ratio_px(s):
    """「1280x720」→ (1280, 720)。非该格式或越界（256~4096）→ None。

    2026-11-15 新增：前端「自定义宽高」直接把成品像素给过来，此时不再走「比例 + 长边」推算。
    宽高各自向下取偶（与图案提取板块同规则，避免出奇数像素）。
    """
    m = _RATIO_PX_RE.match(str(s or ""))
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    if not (HD_SIDE_MIN <= w <= HD_SIDE_MAX and HD_SIDE_MIN <= h <= HD_SIDE_MAX):
        return None
    return (max(2, w - w % 2), max(2, h - h % 2))


def _parse_ratio(s):
    """「3:4」→ 4/3（返回 **h/w**，与 hd_size 内部口径一致）。空 / 非法 → None。"""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*[:：/]\s*(\d+(?:\.\d+)?)\s*$", str(s or ""))
    if not m:
        return None
    a, b = float(m.group(1)), float(m.group(2))
    if a <= 0 or b <= 0:
        return None
    return b / a


def _norm_ratio(s):
    """比例校验：_RATIO_POOL 里的比例直接放行；自定义 WxH（256~4096）归一化后放行；
    其余（含空）一律回落「原图比例」。

    2026-11-15：从「只认白名单」放宽到「也认自定义像素」—— 双区域控件里比例区有
    「自定义宽高」，后端必须能收。归一化把全角 ×/X/* 统一成小写 x，存进记录后回显才一致。
    """
    v = str(s or "").strip().replace("：", ":").replace("／", "/")
    if v in _RATIO_POOL:
        return v
    px = _parse_ratio_px(v)
    if px:
        return "%dx%d" % px
    return ""


def _norm_long(v):
    """长边像素校验。

    · 白名单值（0/800/1024/1500/2000/2048/3000/4096）原样放行；
    · 其余落在 256~4096 的值按「向下取偶」接受（支持「自定义长边」）；
    · 越界 / 非法 / 空 → 0（自动，保持 AI 原始像素）。
    向下取偶而非四舍五入：用户填 3001 时不能悄悄变成 3002（超出他自己填的值）。
    """
    try:
        iv = int(v)
    except Exception:                             # noqa: BLE001
        return 0
    if iv in _LONG_POOL:
        return iv
    if HD_SIDE_MIN <= iv <= HD_SIDE_MAX:
        return iv - iv % 2
    return 0


def _norm_mode(s):
    """输入类型：pattern（满幅图案）/ photo（产品实拍图）。非法值回落 pattern（=改动前行为）。"""
    v = str(s or "").strip().lower()
    return v if v in _MODE_POOL else "pattern"


def _target_dim(src_w, src_h, ratio, long_side):
    """算出用户要的成品像素 (宽, 高)。

    · ratio 是自定义 WxH（如 1200x1600）→ **直接返回该像素**，长边被忽略
      （用户已经明确说了宽高，再拿长边去推会得到另一组数，属于覆盖用户意图）；
    · 否则 long_side=0 → None（不对齐，保持 AI 原始尺寸，历史行为）；
    · 否则按 long_side（长边）+ ratio（空=跟随源图）等比推算。
    """
    px = _parse_ratio_px(ratio)
    if px:
        return px
    L = _norm_long(long_side)
    if L <= 0:
        return None
    r = _parse_ratio(ratio)
    if r is None:                                 # 原图比例
        try:
            r = float(src_h) / float(src_w)
        except Exception:                         # noqa: BLE001
            r = 1.0
    if r <= 0:
        r = 1.0
    r = min(max(r, 0.10), 10.0)
    if r >= 1.0:                                  # 竖图/方图 → 长边是「高」
        H, W = float(L), L / r
    else:                                         # 横图 → 长边是「宽」
        W, H = float(L), L * r
    return (max(2, int(round(W))), max(2, int(round(H))))


def _edge_color(im):
    """图像四边像素的中位色 —— 补边用。比纯白/纯黑更贴图案底色，过渡不突兀。"""
    try:
        a = np.asarray(im.convert("RGB"))
        px = np.concatenate([a[0, :, :], a[-1, :, :], a[:, 0, :], a[:, -1, :]], axis=0)
        med = np.median(px, axis=0)
        return tuple(int(min(255, max(0, round(float(x))))) for x in med)
    except Exception:                             # noqa: BLE001
        return (255, 255, 255)


def _fit_to_target(png_bytes, tw, th):
    """把 AI 成品对齐到用户指定的精确像素。

    · 比例已一致（误差 ≤1px）→ 直接 Lanczos 重采样到精确尺寸（纯放大，绝不改构图）；
    · 比例有偏差 → **等比**放进目标画布、居中、四周补 _edge_color()，
      多出来的地方是留白，主体一个像素都不拉伸。
    任何异常 → 原样返回，保证「选错了也不能把图弄坏」。
    """
    if not tw or not th:
        return png_bytes
    try:
        im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        if im.width == tw and im.height == th:
            return png_bytes
        s = min(float(tw) / im.width, float(th) / im.height)
        nw = max(1, int(round(im.width * s)))
        nh = max(1, int(round(im.height * s)))
        if abs(nw - tw) <= 1 and abs(nh - th) <= 1:
            out = im.resize((tw, th), Image.LANCZOS)
        else:
            out = Image.new("RGB", (tw, th), _edge_color(im))
            out.paste(im.resize((nw, nh), Image.LANCZOS), ((tw - nw) // 2, (th - nh) // 2))
        buf = io.BytesIO()
        out.save(buf, "PNG")
        return buf.getvalue()
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write("[hd] 尺寸对齐失败（已回退 AI 原始尺寸）：%s\n" % e)
        return png_bytes


def _uid(req: Request):
    """经主站中间件进入时 scope 带 st_uid（登录必设）；独立调试时 0（本地 u0 目录）。"""
    return int(req.scope.get("st_uid") or 0)


def _is_admin(uid):
    try:
        import accounts
        return accounts.is_admin(uid)
    except Exception:                             # noqa: BLE001
        return uid == 0


def _rec_path(tid, uid=None):
    if uid is None:
        hits = []
        if os.path.isdir(RECORDS):
            for d in os.listdir(RECORDS):
                p = os.path.join(RECORDS, d, tid + ".json")
                if os.path.isfile(p):
                    hits.append(p)
        return hits[0] if hits else None
    return os.path.join(RECORDS, "u{0}".format(uid), tid + ".json")


def _load_rec(tid, uid):
    """带属主校验的记录读取；不存在或越权一律 None（对外 404）。"""
    p = _rec_path(tid, uid)
    if p and os.path.isfile(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:                         # noqa: BLE001
            return None
    if _is_admin(uid):                            # admin 全量可见
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


SRC_SIDE = 1200                                   # 原图缩略长边上限


def _save_src(raw, uid, tid, max_side=SRC_SIDE):
    """落盘原图缩略（长边 ≤1200）到 output/u<uid>/<tid>/src.jpg。

    用途：前端「原图 ⇄ 高清图」对比滑块（cmp.js）的原图层。
    取缩略而非原图——体积小 8~20 倍、拖动对齐更顺（滑块两层图都 object-fit:contain，
    容器宽高比按【成品】锁定，两端尺寸差异不影响观感）。

    落在任务目录里 → 随 72h TTL 与产物一起清，不新增任何清理逻辑。
    """
    from PIL import Image as _Im
    im = _Im.open(io.BytesIO(raw))
    im.load()
    im = im.convert("RGB")
    w, h = im.size
    m = max(w, h)
    if m > max_side:
        s = float(max_side) / float(m)
        im = im.resize((max(1, int(round(w * s))), max(1, int(round(h * s)))), _Im.LANCZOS)
    od = os.path.join(OUTPUT, "u{0}".format(uid), tid)
    os.makedirs(od, exist_ok=True)
    p = os.path.join(od, "src.jpg")
    im.save(p, "JPEG", quality=88)
    return os.path.relpath(p, DATA).replace("\\", "/")


# ---------------- 并发闸门 + FIFO 排队（位次读时实时算，铁律） ----------------
_GEN_SEM = threading.BoundedSemaphore(int(os.environ.get("HD_CONCURRENCY", "2")))
_q_lock = threading.Lock()
_q_wait_list = []                                 # 排队中的 tid（FIFO）


def _queue_pos(tid):
    with _q_lock:
        try:
            return _q_wait_list.index(tid) + 1
        except ValueError:
            return 0


# ---------------- 页面 ----------------
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(FRONTEND, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/ping")
def ping():
    return {"ok": True, "board": "hd"}


# ---------------- 建任务 ----------------
@app.post("/api/fix")
async def fix(req: Request, n: int = Form(0), images: list[UploadFile] = File(...),
              ratio: str = Form(""), long_side: int = Form(0), mode: str = Form("pattern")):
    """建任务。

    2026-10-21 新增三个可选参数（都可省略 → 行为与改动前**完全一致**）：
      mode      输入类型：pattern=满幅图案（默认，原行为）/ photo=产品实拍图（只提画质、保构图）
      ratio     成品比例：1:1 / 3:4 / 4:3 / 9:16 / 16:9 / 2:3 / 3:2，
                或**自定义宽高**（如 1200x1600，256~4096，此时 long_side 被忽略）；空 = 跟随源图
      long_side 成品长边像素：0（自动）/ 800 / 1024 / 1500 / 2000 / 2048 / 3000 / 4096，
                或 256~4096 之间任意值（**向下取偶**）；0 = 保持 AI 原始像素
    ratio / long_side 只影响**出图之后**的等比适配（见 _fit_to_target）；mode 决定用哪条提示词。
    """
    uid = _uid(req)
    _ratio = _norm_ratio(ratio)
    _long = _norm_long(long_side)
    # 自定义宽高时像素已经完全确定，长边没有意义 → 归零。
    # 不归零的话记录会显示「自定义 1200x1600 · 长边 4096px」，回显自相矛盾。
    if _parse_ratio_px(_ratio):
        _long = 0
    _mode = _norm_mode(mode)
    imgs = [f for f in images if f.filename]
    if not imgs:
        return JSONResponse({"ok": False, "error": "没有收到任何图片"}, status_code=400)
    if len(imgs) > MAX_IMAGES:
        return JSONResponse({"ok": False, "error": "单次最多 %d 张" % MAX_IMAGES}, status_code=400)
    tasks = []
    for f in imgs:
        raw = await f.read()
        if len(raw) > MAX_BYTES:
            return JSONResponse({"ok": False, "error": "%s 超过单张 12MB 上限" % f.filename},
                                status_code=400)
        try:
            im = Image.open(io.BytesIO(raw)); im.load()
        except Exception:                         # noqa: BLE001
            return JSONResponse({"ok": False, "error": "%s 不是有效图片" % f.filename},
                                status_code=400)
        tid = uuid.uuid4().hex[:12]
        # 原图缩略必须写在这里（上传时），不能写进 _worker：
        # 「重新生成」是用上一版高清图当输入重跑的，若在 worker 里落盘，
        # 重做一次后 src.jpg 就变成高清图，对比滑块就自己跟自己比了。
        try:
            src_rel = _save_src(raw, uid, tid)
        except Exception:                         # noqa: BLE001
            src_rel = ""
        rec = {"tid": tid, "uid": uid, "name": f.filename, "status": "queued", "stage": "排队中",
               "err": "", "ts": time.time(), "done_ts": 0,
               "src_w": im.width, "src_h": im.height, "src_rel": src_rel,
               "opt_ratio": _ratio, "opt_long": _long, "opt_mode": _mode,
               "out_w": 0, "out_h": 0, "png_rel": "", "psd_rel": "", "thumb_rel": ""}
        _save_rec(rec)
        with _q_lock:
            _q_wait_list.append(tid)
        threading.Thread(target=_worker, args=(tid, uid, raw, (im.width, im.height)),
                         daemon=True).start()
        tasks.append({"tid": tid, "name": f.filename})
    return {"ok": True, "tasks": tasks, "n": n or len(imgs)}


# ---------------- COS 回推 / 直链读取（2026-10-15） ----------------
def _upload_cdn(uid, tid, rels):
    """把一个任务的产物批量推到 COS → `{rel: 直链}`。

    未启用 COS / 上传失败 → 返回 `{}`，调用方按「无直链」回退站内路由（行为与改造前一致）。
    图床抖动绝不能让任务变失败，所以这里吞掉所有异常。
    """
    if hdstore is None:
        return {}
    try:
        return hdstore.upload_task(uid, tid, rels) or {}
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write("[hd] COS 回推失败（已回退本地）：%s\n" % e)
        return {}


def _cdn_of(rec, rel):
    """读记录里的 `cdn` 映射拿直链；无 / 老记录（无 cdn 字段）→ ""。"""
    try:
        return (rec.get("cdn") or {}).get(str(rel).replace("\\", "/").lstrip("/")) or ""
    except Exception:                             # noqa: BLE001
        return ""


def _img_resp(rec, rel, media_type=None, missing=None):
    """图片类端点统一出口：有 COS 直链 → 302 过去（客户端直连 CDN，服务器只发头）；
    否则照旧由本机 FileResponse 流式返回。"""
    _cu = _cdn_of(rec, rel)
    if _cu:
        return RedirectResponse(_cu, status_code=302,
                                headers={"Cache-Control": "private,max-age=3600"})
    p = os.path.join(DATA, rel)
    if not os.path.isfile(p):
        return JSONResponse({"ok": False, "error": missing or "产物不存在（可能已过 72h 清理）"},
                            status_code=404)
    if media_type:
        return FileResponse(p, media_type=media_type)
    return FileResponse(p)


def _worker(tid, uid, raw, src_size):
    outdir = os.path.join(OUTPUT, "u{0}".format(uid), tid)
    try:
        rec = _load_rec(tid, uid) or {}
        rec.update({"status": "running", "stage": "AI 重绘中"})
        _save_rec(rec)
        with _GEN_SEM:
            with _q_lock:
                if tid in _q_wait_list:
                    _q_wait_list.remove(tid)
            # 比例/像素从记录里读（而不是从参数传）：这样 /redo 重跑也能沿用当初的选择，
            # 不会出现「重做一次就悄悄回到默认尺寸」。
            png = _gen_hd(raw, src_size, rec.get("opt_ratio") or "", rec.get("opt_long") or 0,
                          rec.get("opt_mode") or "pattern")
        os.makedirs(outdir, exist_ok=True)
        png_path = os.path.join(outdir, "hd.png")
        with open(png_path, "wb") as f:
            f.write(png)
        rec = _load_rec(tid, uid) or rec
        im = Image.open(io.BytesIO(png))
        rec.update({"out_w": im.width, "out_h": im.height,
                    "stage": "拆层组 PSD 中", "png_rel": os.path.relpath(png_path, DATA).replace("\\", "/")})
        _save_rec(rec)
        # ---- 拆两层 + PSD ----
        psd_rel, thumb_rel = _split_and_psd(png, outdir)
        rec = _load_rec(tid, uid) or rec
        # ---- 回推 COS：src / png / psd / thumb 全量上传，直链写进记录的 `cdn` 映射 ----
        # 上传在「落 done 记录」之前完成 → 记录一落地就带直链，前端/素材库首个请求即命中 CDN。
        cdn = _upload_cdn(uid, tid, [rec.get("src_rel"), rec.get("png_rel"),
                                     psd_rel, thumb_rel.split("|")[0]])
        rec.update({"status": "done", "stage": "完成", "done_ts": time.time(),
                    "psd_rel": psd_rel, "thumb_rel": thumb_rel, "cdn": cdn})
        _save_rec(rec)
    except SystemExit as e:                       # image2psd.fail() 可能 sys.exit
        rec = _load_rec(tid, uid) or {"tid": tid, "uid": uid}
        rec.update({"status": "failed", "stage": "失败", "err": str(e)[:300]})
        _save_rec(rec)
    except Exception as e:                        # noqa: BLE001
        rec = _load_rec(tid, uid) or {"tid": tid, "uid": uid}
        rec.update({"status": "failed", "stage": "失败", "err": str(e)[:300]})
        _save_rec(rec)
    finally:
        with _q_lock:
            if tid in _q_wait_list:
                _q_wait_list.remove(tid)


def _gen_hd(raw, src_size, opt_ratio="", opt_long=0, opt_mode="pattern"):
    """走「站点池」出图：同模型跨站点轮询，全挂才换下一个模型（2026-10-15 双协议）。

    - openai 协议（lensapi / grsai）：同步打 /v1/images/edits（multipart），响应即成品。
    - apib 协议（api.apib.ai）    ：私有异步 —— gpt-image 系**不能**走 edits（只服务 grok 系），
      参考图必须塞进 /v1/images/generations 的 image 数组，再轮询 /v1/tasks/<id> 取图。

    站点/模型清单见 ai_sites.py（后台设置页可改）；失败文案统一脱敏为
    「生图失败（错误码 xxx），请稍后重试。」——不暴露站点名/模型名/欠费等细节。

    2026-10-21：新增 opt_ratio / opt_long。**只作用于出图之后**（_fit_to_target 等比适配 + 补边），
    请求尺寸 size 一律不动 —— 保住「版式死锁」这条核心承诺。
    2026-10-21 晚：新增 opt_mode —— pattern 用「满幅图案」死锁提示词（原行为）；
    photo 用「产品实拍图」提示词（只提画质、保构图，禁止提取图案）。这是**唯一**会切提示词的地方。
    """
    key = load_key()
    size = hd_size(src_size[0], src_size[1])     # 2026-10-15：改用「按源图真实比例」（方图不再被拉成 2:3）
    tgt = _target_dim(src_size[0], src_size[1], opt_ratio, opt_long) or (0, 0)
    prompt = PROMPT_PHOTO if _norm_mode(opt_mode) == "photo" else PROMPT

    def _fit(b):
        return _fit_to_target(b, tgt[0], tgt[1])

    last_exc = None
    for site, base, skey, model in ai_sites.plan_order(MODEL_CHAIN):
        try:
            if ai_sites.protocol_of(site) == "apib":
                url = ai_sites.apib_gen(base, skey or key, model, prompt, size, images=[raw])
                out = ai_sites.fetch_bytes(url)
                ai_sites.remember(site, model)
                return _fit(out)
            bd, body = _multipart({"model": model, "prompt": prompt, "size": size, "n": "1"},
                                  [("image", "in.png", "image/png", raw)])
            req = urllib.request.Request(base.rstrip("/") + "/v1/images/edits", data=body, method="POST")
            req.add_header("Content-Type", "multipart/form-data; boundary=" + bd)
            req.add_header("Authorization", "Bearer " + (skey or key))
            req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PatternWorkbench/2.0")
            with urllib.request.urlopen(req, timeout=420) as r:
                resp = json.loads(r.read().decode("utf-8"))
            d = (resp.get("data") or [None])[0]
            if not isinstance(d, dict):
                raise RuntimeError("响应无 data")
            u = as_str(d.get("url"))
            b64 = as_str(d.get("b64_json"))
            if u and u.startswith("http"):
                with urllib.request.urlopen(urllib.request.Request(
                        u, headers={"User-Agent": "Mozilla/5.0"}), timeout=180) as r2:
                    out = r2.read()
                ai_sites.remember(site, model)
                return _fit(out)
            if b64:
                out = base64.b64decode(b64)
                ai_sites.remember(site, model)
                return _fit(out)
            raise RuntimeError("响应无图片（url/b64_json 均空）")
        except Exception as e:                    # noqa: BLE001
            last_exc = e
            cooled = ai_sites.note_fail(site, model)   # 方案 B：连续失败 → 该组合冷却，后续任务不再白等
            sys.stderr.write("[hd] %s / %s 失败%s：%s\n" % (
                site, model, "（已熔断）" if cooled else "", str(e)[:140]))
            time.sleep(2)
    raise RuntimeError(ai_sites.sanitize(last_exc))


# ---------------- 拆两层 + 组 PSD（Otsu 自动阈值，回退 64；软过渡 ±8） ----------------
def _split_and_psd(png_bytes, outdir):
    from PIL import Image as _Im
    im = _Im.open(io.BytesIO(png_bytes)).convert("RGB")
    a = np.asarray(im).astype(float)
    L = 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
    thr = _otsu(L)
    if not (8.0 < thr < 248.0):
        thr = 64.0
    LO, HI = thr - 8.0, thr + 8.0
    t = np.clip((L - LO) / max(HI - LO, 1e-6), 0.0, 1.0)
    u8 = a.astype(np.uint8)
    dark = np.dstack([u8, ((1.0 - t) * 255).astype(np.uint8)])
    lite = np.dstack([u8, (t * 255).astype(np.uint8)])
    f_dark = os.path.join(outdir, "layer_dark.png")
    f_lite = os.path.join(outdir, "layer_light.png")
    _Im.fromarray(dark, "RGBA").save(f_dark)
    _Im.fromarray(lite, "RGBA").save(f_lite)
    # 还原校验（软过渡 alpha 合成应与原图一致）
    ab = dark[..., 3:4] / 255.0; at = lite[..., 3:4] / 255.0
    ao = at + ab * (1 - at)
    rgb = (lite[..., :3] * at + dark[..., :3] * ab * (1 - at)) / np.maximum(ao, 1e-6)
    err = float(np.abs(rgb - a).mean())
    manifest = {
        "canvas": {"width": im.width, "height": im.height, "composite_background": "#ffffff"},
        "output": "hd.psd", "preview": "hd.preview.png", "save_layers_dir": "layers",
        "layers": [{"name": "底纹（暗底）", "file": os.path.basename(f_dark), "remove_background": "none"},
                   {"name": "主体（亮纹）", "file": os.path.basename(f_lite), "remove_background": "none"}],
    }
    mp = os.path.join(outdir, "manifest.json")
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    import image2psd
    os.makedirs(os.path.join(outdir, "layers"), exist_ok=True)
    image2psd.main(["assemble", "--manifest", mp,
                    "--output", os.path.join(outdir, "hd.psd"),
                    "--preview", os.path.join(outdir, "hd.preview.png"),
                    "--save-layers", os.path.join(outdir, "layers")])
    # 缩略图（宽 320）
    th = im.copy(); th.thumbnail((320, 100000))
    thumb = os.path.join(outdir, "thumb.png")
    th.save(thumb)
    return (os.path.relpath(os.path.join(outdir, "hd.psd"), DATA).replace("\\", "/"),
            os.path.relpath(thumb, DATA).replace("\\", "/") + "|err=%.3f" % err)


def _otsu(L):
    hist, _ = np.histogram(L, bins=256, range=(0, 256))
    hist = hist.astype(float)
    total = hist.sum()
    sum_all = (np.arange(256) * hist).sum()
    sum_b = 0.0; w_b = 0.0; best = 0.0; thr = 0.0
    for i in range(256):
        w_b += hist[i]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += i * hist[i]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        v = w_b * w_f * (m_b - m_f) ** 2
        if v > best:
            best = v; thr = i
    return float(thr)


# ---------------- 查询 / 下载 / 删除 ----------------
def _pub(rec):
    out = dict(rec)
    out.pop("uid", None)
    out["queue_pos"] = _queue_pos(rec["tid"])     # 读时实时算（批量模式铁律）
    if rec.get("thumb_rel"):
        out["thumb_rel"] = rec["thumb_rel"].split("|")[0]
    out["has_src"] = bool(rec.get("src_rel"))     # 前端据此决定是否给「⇄ 对比原图」按钮
    out.pop("src_rel", None)                      # 内部相对路径不外泄（取图走 /src 端点）
    return out


@app.get("/api/tasks")
def list_tasks(req: Request):
    uid = _uid(req)
    root = os.path.join(RECORDS, "u{0}".format(uid))
    recs = []
    if _is_admin(uid):
        if os.path.isdir(RECORDS):
            for d in os.listdir(RECORDS):
                dd = os.path.join(RECORDS, d)
                if not os.path.isdir(dd):
                    continue
                for fn in os.listdir(dd):
                    if fn.endswith(".json"):
                        try:
                            recs.append(json.load(open(os.path.join(dd, fn), encoding="utf-8")))
                        except Exception:     # noqa: BLE001
                            pass
    elif os.path.isdir(root):
        for fn in os.listdir(root):
            if fn.endswith(".json"):
                try:
                    recs.append(json.load(open(os.path.join(root, fn), encoding="utf-8")))
                except Exception:             # noqa: BLE001
                    pass
    recs.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return {"ok": True, "tasks": [_pub(r) for r in recs[:100]]}


def _file_resp(rec, rel):
    p = os.path.join(DATA, rel)
    if not os.path.isfile(p):
        return JSONResponse({"ok": False, "error": "产物不存在（可能已过 72h 清理）"}, status_code=404)
    return FileResponse(p, filename=rec.get("name") or "hd")


@app.get("/api/tasks/{tid}/thumb")
def thumb(tid: str, req: Request):
    if not _ID_RE.match(tid):
        return JSONResponse({"ok": False}, status_code=404)
    rec = _load_rec(tid, _uid(req))
    if not rec or not rec.get("thumb_rel"):
        return JSONResponse({"ok": False}, status_code=404)
    return _img_resp(rec, rec["thumb_rel"].split("|")[0])


@app.get("/api/tasks/{tid}/png")
def get_png(tid: str, req: Request):
    if not _ID_RE.match(tid):
        return JSONResponse({"ok": False}, status_code=404)
    rec = _load_rec(tid, _uid(req))
    if not rec or not rec.get("png_rel"):
        return JSONResponse({"ok": False}, status_code=404)
    return _img_resp(rec, rec["png_rel"])


@app.get("/api/tasks/{tid}/src")
def get_src(tid: str, req: Request):
    """原图缩略（对比滑块的原图层）。与 /png /psd 同一套属主校验。"""
    if not _ID_RE.match(tid):
        return JSONResponse({"ok": False}, status_code=404)
    rec = _load_rec(tid, _uid(req))
    if not rec or not rec.get("src_rel"):
        return JSONResponse({"ok": False}, status_code=404)
    return _img_resp(rec, rec["src_rel"], media_type="image/jpeg",
                     missing="原图缩略不存在（可能已过 72h 清理）")


@app.get("/api/tasks/{tid}/psd")
def get_psd(tid: str, req: Request):
    if not _ID_RE.match(tid):
        return JSONResponse({"ok": False}, status_code=404)
    rec = _load_rec(tid, _uid(req))
    if not rec or not rec.get("psd_rel"):
        return JSONResponse({"ok": False}, status_code=404)
    return _file_resp(rec, rec["psd_rel"])


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
            continue                              # 越权/不存在 → 静默跳过（同批量契约）
        owner = rec.get("uid")
        if not admin and owner != uid:
            continue
        try:
            shutil.rmtree(os.path.join(OUTPUT, "u{0}".format(owner), tid), ignore_errors=True)
            p = _rec_path(tid, owner)
            if p and os.path.isfile(p):
                os.remove(p)
            deleted.append(tid)
            # 同步清 COS（尽力而为，失败不阻塞删除结果）
            if hdstore is not None:
                try:
                    hdstore.delete_task(owner, tid)
                except Exception:                 # noqa: BLE001
                    pass
        except Exception:                         # noqa: BLE001
            pass
    return {"ok": True, "deleted": deleted, "ids": ids}


@app.post("/api/tasks/{tid}/redo")
async def redo_task(tid: str, req: Request):
    """单张重做：重新跑生成 + 拆层（同 tid，覆盖旧产物）。"""
    uid = _uid(req)
    rec = _load_rec(tid, uid)
    if not rec:
        return JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    if rec.get("status") in ("running", "queued"):
        return JSONResponse({"ok": False, "error": "任务正在进行中"}, status_code=400)
    png_rel = rec.get("png_rel")
    if not png_rel or not os.path.isfile(os.path.join(DATA, png_rel)):
        return JSONResponse({"ok": False, "error": "原图产物已过期，请重新上传"}, status_code=400)
    raw = open(os.path.join(DATA, png_rel), "rb").read()
    rec.update({"status": "queued", "stage": "排队中", "err": "", "ts": time.time(), "done_ts": 0})
    _save_rec(rec)
    with _q_lock:
        _q_wait_list.append(tid)
    threading.Thread(target=_worker, args=(tid, rec["uid"], raw,
                                           (rec.get("src_w") or 1024, rec.get("src_h") or 1024)),
                     daemon=True).start()
    return {"ok": True, "tid": tid}


# ---------------- 72h TTL 清理（records 与 output 同 TTL，双闸防误删） ----------------
def _sweep():
    while True:
        time.sleep(CLEANUP_INTERVAL)
        try:
            now = time.time()
            n = 0
            keep = set()                          # 未过期 / 进行中的 tid → 供 COS 孤儿兜底
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
                        except Exception:     # noqa: BLE001
                            continue
                        _tid = rec.get("tid") or fn[:-5]
                        if rec.get("status") in ("running", "queued", "gen", "psd_running"):
                            keep.add(_tid)
                            continue          # 进行中永不删（防误删闸 1）
                        if now - (rec.get("ts") or 0) > TTL_HOURS * 3600:
                            shutil.rmtree(os.path.join(OUTPUT, d, rec.get("tid", "")),
                                          ignore_errors=True)
                            try:
                                os.remove(p)
                            except OSError:
                                pass
                            n += 1
                            # 同步清 COS（尽力而为）
                            if hdstore is not None:
                                try:
                                    hdstore.delete_task(rec.get("uid") or d.lstrip("u"),
                                                        _tid)
                                except Exception:  # noqa: BLE001
                                    pass
                        else:
                            keep.add(_tid)
            # 兜底：清「本地已无、COS 仍留」的孤儿（带双闸：keep 为空不执行、单次有上限）
            if hdstore is not None and keep:
                try:
                    hdstore.sweep(keep_tids=keep, ttl_hours=TTL_HOURS)
                except Exception as e:            # noqa: BLE001
                    sys.stderr.write("[hd] COS 孤儿清理异常: %s\n" % e)
            if n:
                sys.stderr.write("[hd] TTL 清理 %d 条过期任务\n" % n)
        except Exception as e:                    # noqa: BLE001
            sys.stderr.write("[hd] 清理线程异常: %s\n" % e)


threading.Thread(target=_sweep, daemon=True).start()


# ---------------- 启动后台补推（历史记录回填 COS） ----------------
def _backfill_cdn():
    """把「接入 COS 之前生成的」历史 done 记录补传产物 + 回写 `cdn`。

    幂等：只有「该记录的 rel **全部**已在 cdn 里」才跳过；否则再补一轮。
    上传全失败（COS 未配置 / 抖动 / 欠费）→ 不写记录，行为与改造前一致，下次启动再试。
    与父应用 `_backfill_cdn`（pt）同一思路，只是数据源换成 hd 自己的 records/。
    """
    if hdstore is None or not hdstore.enabled():
        return
    try:
        n = 0
        for d in (os.listdir(RECORDS) if os.path.isdir(RECORDS) else []):
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
                if rec.get("status") != "done" or not rec.get("uid"):
                    continue
                rels = [rec.get("src_rel"), rec.get("png_rel"), rec.get("psd_rel"),
                        (rec.get("thumb_rel") or "").split("|")[0]]
                rels = [r for r in rels if r]
                if not rels:
                    continue
                cdn = rec.get("cdn") or {}
                if all(r in cdn for r in rels):    # 已补全 → 跳过（幂等）
                    continue
                got = _upload_cdn(rec.get("uid"), rec.get("tid"), rels)
                if not got:
                    continue                      # COS 不可用 → 不动记录，下次启动再试
                cdn.update(got)
                rec["cdn"] = cdn
                _save_rec(rec)
                n += 1
        sys.stderr.write("[hd] COS 历史产物回填完成（%d 条）\n" % n)
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write("[hd] COS 回填异常: %s\n" % e)


threading.Thread(target=_backfill_cdn, daemon=True).start()
