#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图案提取工作台 · 服务端
FastAPI + 后台线程任务。任务状态持久化在 data/<tid>/task.json（重启不丢）。
路由前缀由 Caddy 反代 /pattern/* → 127.0.0.1:17700
"""
import base64, hashlib, io, json, os, re, shutil, threading, time, urllib.parse, urllib.request, uuid, zipfile
from urllib.parse import quote
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

try:
    import storage as S           # 腾讯云 COS 对象存储（未配置 / SDK 缺失 → 自动回退本地文件服务）
except Exception:                 # noqa: BLE001
    S = None

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
LAYER_MODE_LABEL = {
    "merge2": "底纹层 + 主体层（归并）",
    "merge3": "底色层 + 底纹层 + 主体层",
    "fine": "小图标底图 + 主元素各占一层（精细）",
    "connected": "连通域精细 · 大元素各一层 + 小元素合并（适合支架/镜头）",
}
os.makedirs(DATA, exist_ok=True)

# ---- 产物保留策略（超期自动清理：内存记录 + 本地目录 + COS 前缀） ----
# 用户 2026-10-11 定：照片 / PSD 全部走 COS，且**保留 72 小时后自动从服务器删除**。
#   计时基准默认 = 最后活动时间（生成/提交/选版等动作会续期 → 不会误删正在用的任务）；
#   要改成「从创建时严格 72 小时」→ 设环境变量 PATTERN_TTL_MODE=created。
TASK_TTL_HOURS = float(os.environ.get("PATTERN_TTL_HOURS") or 72)
TTL_MODE = (os.environ.get("PATTERN_TTL_MODE") or "active").strip().lower()
CLEANUP_INTERVAL = int(os.environ.get("PATTERN_CLEANUP_SEC") or 900)   # 扫描周期（秒）
CLEANUP_LOG = os.path.join(DATA, "_cleanup.json")                      # 清理审计（保留最近 100 条）

import pipeline as P

app = FastAPI(title="图案提取工作台")
_tasks = {}       # tid -> {"lock": Lock, "state": dict}
_lock = threading.Lock()

# ---- 批量任务并发闸门（2026-10-13）----
# 「多张独立批量提取」一次可能提交十几张，若不限流 = 十几个线程同时打 AI 网关，
# 必然大量超时失败。这里放一个全局信号量：同一时刻最多 BATCH_CONCURRENCY 个批量任务在跑，
# 其余排队（stage=queued），跑完一个自动补位。单张任务不走闸门（用户自己在等，无需排队）。
BATCH_CONCURRENCY = max(1, int(os.environ.get("PATTERN_CONCURRENCY") or 3))
BATCH_MAX = max(1, int(os.environ.get("PATTERN_BATCH_MAX") or 20))
# 一个批次最多几个**任务**（每个任务 = 一个图案；每个图案可带 1-5 张多角度原图）。
# 2026-10-13 v2 用户定：前端「＋」最多加到 10 个任务。
BATCH_MAX_TASKS = max(1, int(os.environ.get("PATTERN_BATCH_TASKS") or 10))
_batch_sem = threading.BoundedSemaphore(BATCH_CONCURRENCY)
_q_lock = threading.Lock()
_q_wait_list = []          # 排队中的 tid（FIFO，仅用于给前端显示队列位次）


def _q_wait(tid, st):
    """抢一个并发槽；抢不到就停在信号量上，期间把任务标成 queued 让前端显示排队。"""
    with _q_lock:
        _q_wait_list.append(str(tid))
        pos = len(_q_wait_list)
        ahead = pos - 1
    if ahead > 0:
        st["stage"] = "queued"
        st["queue_pos"] = pos
        st["step"] = "排队中（队列第 %d 位 · 前面还有 %d 张，同时最多跑 %d 张）" % (
            pos, ahead, BATCH_CONCURRENCY)
        _save(tid, st)
    _batch_sem.acquire()
    with _q_lock:
        try:
            _q_wait_list.remove(str(tid))
        except ValueError:
            pass
    st.pop("queue_pos", None)

def _load_all():
    for d in os.listdir(DATA):
        tj = os.path.join(DATA, d, "task.json")
        if os.path.exists(tj):
            st = json.load(open(tj, encoding="utf-8"))
            if st.get("stage") in ("gen_running", "psd_running", "queued"):
                st["stage"] = "error"
                st["error"] = "服务重启导致任务中断，请重试"
                st.pop("queue_pos", None)
                _save(d, st, touch=False)     # 启动修状态不算「用户活动」，别给 TTL 续期
            _tasks[d] = {"lock": threading.Lock(), "state": st}

def _save(tid, st, touch=True):
    # 任务可能刚被删除（后台线程还在跑）→ 目录已不在就静默丢弃这次写入，别抛异常刷日志
    d = os.path.join(DATA, tid)
    if not os.path.isdir(d):
        return
    if touch:
        st["_touch"] = int(time.time())       # 最后活动时间（TTL 计时；下划线键不外发前端）
    try:
        json.dump(st, open(os.path.join(d, "task.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    except OSError:
        pass

def _task_ts(tid, st):
    """产物保留计时基准（epoch 秒）。

    默认取最后活动时间 `_touch`（任何生成/提交/选版动作都会刷新）；
    `PATTERN_TTL_MODE=created` 时改取创建时间。都没有则逐级回退：
    `created` 字符串（"%Y-%m-%d %H:%M"）→ 任务目录 mtime → now。
    """
    keys = ("_created_ts", "_touch") if TTL_MODE == "created" else ("_touch", "_created_ts")
    for k in keys:
        v = st.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    s = str(st.get("created") or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return float(time.mktime(time.strptime(s, fmt)))
        except Exception:                     # noqa: BLE001
            continue
    try:
        return float(os.path.getmtime(os.path.join(DATA, tid)))
    except OSError:
        return time.time()

def _public(st, tid=None):
    """给前端的视图（去掉内部字段；下划线开头的是内部临时键，不外发）"""
    out = {k: v for k, v in st.items() if k != "thread" and not k.startswith("_")}
    if tid is not None:
        try:
            age_h = (time.time() - _task_ts(tid, st)) / 3600.0
            out["ttl_h"] = TASK_TTL_HOURS
            out["expire_in_h"] = round(TASK_TTL_HOURS - age_h, 2)   # <0 = 已到期
        except Exception:                     # noqa: BLE001
            pass
    # 排队位次必须「读时实时算」：若在入队那一刻用名单长度算，前面那位可能已经抢到并发槽
    # 并把自己移出名单，结果明明在排队却显示不出位次（实测第 2 个线程必然踩到）。
    if st.get("stage") == "queued" and tid is not None:
        try:
            with _q_lock:
                pos = _q_wait_list.index(str(tid)) + 1 if str(tid) in _q_wait_list else 0
            out.pop("queue_pos", None)        # 入队瞬间写的老位次可能已过时，一律以实时值为准
            if pos:
                out["queue_pos"] = pos
                if pos == 1:
                    out["step"] = "排队中（队列第 1 位 · 前面 0 张，马上轮到，同时最多跑 %d 张）" % BATCH_CONCURRENCY
                else:
                    out["step"] = "排队中（队列第 %d 位 · 前面还有 %d 张，同时最多跑 %d 张）" % (
                        pos, pos - 1, BATCH_CONCURRENCY)
            elif not str(out.get("step") or "").startswith("排队中"):
                out["step"] = "排队中"
        except Exception:                     # noqa: BLE001
            pass
    return out


# ---- 任务归属隔离（2026-10-12）：每个账号只能看到/操作自己的任务 ----
# 归属写在 task.json 的 `_uid` 内部键里（_public 已自动剥离，不外发前端）；
# 无 _uid 的存量任务全部是隔离改造前 admin 跑的 → 视为 admin 所有。
def _own(st, uid):
    if uid and ACC.is_admin(uid):
        return True
    owner = st.get("_uid")
    return owner is not None and owner == uid


def _own_task(tid, request):
    """取任务 state 并校验归属；越权一律 404（不暴露任务存在性）。"""
    box = _tasks.get(str(tid))
    if not box or not _own(box["state"], request.scope.get("st_uid")):
        raise HTTPException(404, "任务不存在")
    return box["state"]

def _prog(tid, st, msg):
    """后台长任务给前端同步一行进度（写进 st.step 并落盘，前端轮询显示）。"""
    st["step"] = str(msg)[:300]
    _save(tid, st)

# ================= 产物服务：缓存 / 缩略图 / 对象存储 =================
_REV_RE = re.compile(r"_v\d+\.[A-Za-z0-9]+$")       # 历史版本命名：xxx_v1.png
_cdn_mt = {}          # tid -> {rel: 已上传过的 mtime}（避免重复上传大图）
THUMB_DIR = ".thumbs"  # 缩略图缓存目录（每个任务目录下）
THUMB_W = 480          # 缩略图宽度：卡片实际只显示 ~200px，480 够清晰、体积比 640 再降约 45%
_CDN_THUMB_DIR = "@thumbs"   # COS 上的缩略图子目录（同一任务前缀下，删任务时会被一起清掉）


def _resolve(d, fn):
    """解析产物引用 → 真实路径。
    兼容两种引用：① 相对路径（如 parts/图案_支架.png）；② 裸文件名（老任务）——
    裸文件名会**回退到 parts/ 子目录**再找一次（部件产物都落在 parts/）。"""
    cands = [fn]
    if "/" not in fn and "\\" not in fn:
        cands.append(os.path.join("parts", fn))
    for rel in cands:
        fp = os.path.normpath(os.path.join(d, rel))
        if fp.startswith(d) and os.path.isfile(fp):
            return fp
    return None


def _send_file(fp, request=None, media_type=None, max_age=300, immutable=False):
    """带 ETag / Cache-Control 的文件响应；命中 If-None-Match 直接 304。

    ⚠️ 老实现直接 `FileResponse(fp)`：Starlette 只写 ETag、**不比对** If-None-Match，
    且不带 Cache-Control → 每次打开任务都把 6MB 产物整包重下（实测带 ETag 重取仍 200）。
    这里自己算 ETag 并比对；URL 带 `?v=<mtime>` 时按不可变内容给长缓存。
    """
    try:
        stt = os.stat(fp)
    except OSError:
        raise HTTPException(404, "文件不存在")
    etag = '"%x-%x"' % (int(stt.st_mtime), stt.st_size)
    if request is not None and (request.query_params.get("v") or "").strip():
        max_age, immutable = 604800, True
    headers = {"Cache-Control": "public, max-age=%d%s" % (max_age, ", immutable" if immutable else ""),
               "ETag": etag}
    if request is not None:
        inm = (request.headers.get("if-none-match") or "").strip()
        if inm and inm in (etag, "W/" + etag):
            return Response(status_code=304, headers=headers)
    return FileResponse(fp, media_type=media_type, headers=headers)


def _thumb_path(tid, rel, w):
    h = hashlib.md5(("%s|%d" % (rel, w)).encode("utf-8")).hexdigest()[:16]
    return os.path.join(DATA, tid, THUMB_DIR, h + ".webp")


def _make_thumb(fp, tp, w=THUMB_W):
    """源图 → 宽 w 的 WebP 缩略图（透明底铺白，避免网页上发黑）。失败返回 None。"""
    from PIL import Image
    with Image.open(fp) as im:
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        if im.width > w:
            im = im.resize((w, max(1, int(round(im.height * w / float(im.width))))), Image.LANCZOS)
        os.makedirs(os.path.dirname(tp), exist_ok=True)
        tmp = tp + ".tmp"
        im.save(tmp, "WEBP", quality=82, method=4)
    os.replace(tmp, tp)
    return tp


def _vmap(tid):
    """任务目录下每个产物的 mtime → 前端拼 `?v=`（破缓存，同时让长缓存变安全）。"""
    out = {}
    d = os.path.join(DATA, tid)
    for root, dirs, fns in os.walk(d):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for f in fns:
            if f == "task.json":
                continue
            fp = os.path.join(root, f)
            try:
                out[os.path.relpath(fp, d).replace("\\", "/")] = int(os.path.getmtime(fp))
            except OSError:
                pass
    return out


def _archive(tid, rel):
    """把产物 rel 改名成 `<stem>_vN<ext>`（N 取当前未占用的最小序号），返回新相对路径。"""
    d = os.path.join(DATA, tid)
    rel = str(rel).replace("\\", "/")
    fp = os.path.join(d, rel.replace("/", os.sep))
    if not os.path.isfile(fp):
        return None
    stem, ext = os.path.splitext(rel)
    n = 1
    while os.path.isfile(os.path.join(d, ("%s_v%d%s" % (stem, n, ext)).replace("/", os.sep))):
        n += 1
    cand = "%s_v%d%s" % (stem, n, ext)
    try:
        os.replace(fp, os.path.join(d, cand.replace("/", os.sep)))
    except OSError:
        return None
    return cand


def _archive_versions(tid, st):
    """【重新生成】把上一版正式产物归档成 `_vN` 历史版本（不删，版本区可继续对比）。

    只归档「当前正式产物」；已是 `_vN` 的历史件原样保留，避免二次归档成 `xxx_v1_v1.png`。
    新一版仍写正式文件名 → 下游分层 / 交付逻辑零改动。
    """
    prev = []
    for rel in list(st.get("versions") or []):
        rel = str(rel).replace("\\", "/")
        if not rel:
            continue
        prev.append(rel if _REV_RE.search(rel) else (_archive(tid, rel) or rel))
    cl = st.get("clean")
    if cl and not _REV_RE.search(str(cl)):
        a = _archive(tid, cl)
        if a:
            st["clean"] = a          # 界面期间仍指向归档件不裂图；新一版会重写正式名
    st["_prev_versions"] = prev
    _save(tid, st)
    return prev


def _publish(tid, st, rels):
    """把产物推到腾讯云 COS，公网 URL（带 ?v=mtime）写进 st.cdn 供前端优先引用。

    COS 未配置 / 上传失败 → st.cdn 里没有该 rel，前端自动回退本地 `/api/tasks/.../file/...`。
    """
    if S is None or not S.enabled():
        return {}
    d = os.path.join(DATA, tid)
    mt = _cdn_mt.setdefault(tid, {})
    cdn = st.setdefault("cdn", {})
    got = {}
    for rel in rels:
        rel = str(rel).replace("\\", "/")
        fp = os.path.join(d, rel.replace("/", os.sep))
        if not os.path.isfile(fp):
            continue
        try:
            m = int(os.path.getmtime(fp))
        except OSError:
            continue
        if mt.get(rel) == m and cdn.get(rel):
            got[rel] = cdn[rel]
            continue
        url = S.upload_file(fp, S.key_for(tid, rel))
        if url:
            cdn[rel] = "%s?v=%d" % (url, m)
            mt[rel] = m
            got[rel] = cdn[rel]
    if got:
        _save(tid, st, touch=False)          # 上传产物不算用户活动（否则每次重启回填都续期）
    return got


_PUB_EXTRA = ("_预览.png", "_预览_合并.png", "图案_2048.png", "定位视图.png")
_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
_PUB_EXT = _IMG_EXT + (".psd",)        # COS 全量覆盖：所有位图 + PSD（PSD 也给直链下载）


def _publish_thumbs(tid, st, rels):
    """把每个位图产物的 WebP 缩略图也推到 COS，URL 写进 `st["cdn_thumb"]`。

    为什么必须做：**公网出口带宽才是本工作台的瓶颈**（2026-10-11 实测同一份 1.16MB 的
    图案_重绘版.png：走工作台 3.5s~20.1s、速率 58~328KB/s；走 COS 直链 0.40~0.49s、
    2.7~2.9MB/s → 快 8.8~41 倍）。但全尺寸图早已走 COS，**缩略图却一直走服务器**
    `/api/tasks/{tid}/thumb/` → 首屏网格里每一张小图都在硬吃最堵的那条通道
    （实测 28.7KB 的小图 TTFB 仅 0.4s 却要 1.5~2.1s 才下完，≈20KB/s）。

    这里复用本地 `.thumbs/` 缓存生成 WebP，再上传到 COS `<tid>/@thumbs/<rel>`；
    前端 `tlink()` 优先读 `st.cdn_thumb[rel]`，读不到自动回退本地接口 → 不会裂图。
    """
    if S is None or not S.enabled():
        return {}
    d = os.path.join(DATA, tid)
    mt = _cdn_mt.setdefault(tid, {})
    ct = st.setdefault("cdn_thumb", {})
    got = {}
    for rel in rels:
        rel = str(rel).replace("\\", "/")
        if os.path.splitext(rel)[1].lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            continue                                    # PSD 等非位图没有缩略图
        fp = os.path.join(d, rel.replace("/", os.sep))
        if not os.path.isfile(fp):
            continue
        try:
            m = int(os.path.getmtime(fp))
        except OSError:
            continue
        k = "t|" + rel                                   # 与全尺寸表共用，键加前缀区分
        if mt.get(k) == m and ct.get(rel):
            got[rel] = ct[rel]
            continue
        tp = _thumb_path(tid, rel, THUMB_W)
        try:
            if (not os.path.isfile(tp)) or os.path.getmtime(tp) < os.path.getmtime(fp):
                if not _make_thumb(fp, tp, THUMB_W):
                    continue
        except Exception:                                # noqa: BLE001
            continue
        url = S.upload_file(tp, S.key_for(tid, _CDN_THUMB_DIR + "/" + rel))
        if url:
            ct[rel] = "%s?v=%d" % (url, m)
            mt[k] = m
            got[rel] = ct[rel]
    if got:
        _save(tid, st, touch=False)
    return got


def _auto_rels(tid, st):
    """算出这个任务下「**该走 COS 的所有图片类数据**」的相对路径（去重、保序）。

    两道保险，缺一不可：
      ① 字段枚举 —— versions / clean / psd / hi_png / merged_psd / locate_view / locate_src /
         inputs（上传原图）+ 固定名产物 + `_vN` 历史件；
      ② 全目录兜底扫描 —— 任务目录下所有 `.png/.jpg/.jpeg/.webp/.psd`（排除 `.thumbs/` 缓存）。
    这样「以后新增任何图片产物」都会自动进 COS，不会再出现「某个字段忘了加 → 漏推本地」。
    """
    d = os.path.join(DATA, tid)
    rels = [r for r in (st.get("versions") or []) if r]
    for k in ("clean", "psd", "hi_png", "merged_psd", "locate_view", "locate_src"):
        if st.get(k):
            rels.append(st[k])
    rels.extend(st.get("inputs") or [])                  # 上传原图：原图01.jpg …
    for x in _PUB_EXTRA:
        if os.path.isfile(os.path.join(d, x)):
            rels.append(x)
    for root, dirs, fns in os.walk(d):                   # 兜底：全目录扫描（含 parts/ 子目录）
        dirs[:] = [x for x in dirs if not x.startswith(".")]   # 跳 `.thumbs/` 等隐藏目录
        for f in fns:
            if os.path.splitext(f)[1].lower() in _PUB_EXT:
                rels.append(os.path.relpath(os.path.join(root, f), d).replace("\\", "/"))
    seen, uniq = set(), []
    for r in rels:
        r = str(r).replace("\\", "/")
        if r and r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq


def _publish_auto(tid, st):
    """把该任务下**所有图片类数据**推到腾讯云 COS（字段枚举 + 全目录兜底，见 `_auto_rels`）。"""
    uniq = _auto_rels(tid, st)
    if not uniq:
        return {}
    got = _publish(tid, st, uniq)
    try:
        _publish_thumbs(tid, st, uniq)                   # 缩略图外链：尽力而为，失败不影响
    except Exception:                                    # noqa: BLE001
        pass
    return got


def _pub_safe(tid, st):
    """CDN 上传是「尽力而为」：任何异常都不许影响任务本身。"""
    try:
        _publish_auto(tid, st)
    except Exception:             # noqa: BLE001
        pass


def _cos_delete_many(tids):
    if S is None:
        return
    for t in tids:
        try:
            S.delete_task(t)
        except Exception:         # noqa: BLE001
            pass

def _run_gen(tid, st, archive=False, gate=False):
    def work():
        gated = False
        try:
            if gate:
                _q_wait(tid, st)          # 排队 + 抢并发槽（里面会自己写明排队位次）
                gated = True
                if str(tid) not in _tasks:
                    return                # 排队期间被删除 → 直接放槽走人
            d = os.path.join(DATA, tid)
            srcs = st["inputs"]
            tt = st.get("task_type", "shell")
            st["stage"] = "gen_running"; st["step"] = "准备输入"
            _save(tid, st)
            _pub_safe(tid, st)                       # 原图/定位图上传完即推 COS（不等出图/出 PSD）
            prev_v = []
            if archive:
                st["step"] = "归档上一版结果（保留为历史版本）"; _save(tid, st)
                prev_v = _archive_versions(tid, st)
                st["step"] = "重新生成中（旧结果已保留为历史版本）"; _save(tid, st)

            # ===== 方案 A：支架 = 直接拿原图定位部件，不过 AI（保真 100%、可复现）=====
            # 理由：Step1 的 AI「干净壳」只锁颜色/样式、不锁位置/尺寸，重绘后部件位置会漂，
            #      导致定位框与真实部件对不上。支架任务只需「在原图坐标系里框准一个圆」，
            #      用原图直抠即可，还省一次 AI 调用。
            if tt == "stand":
                want = P.PART_STAND
                st["parts_want"] = want
                st["locate_src"] = srcs[0]            # 定位 + 抠图都用这张原图
                st["step"] = "原图定位%s（方案A · 不经 AI 重绘）" % want; _save(tid, st)
                crop, cmeta = P.shell_crop_meta(os.path.join(d, srcs[0]))
                view = "定位视图.png"
                P.save_bgr(os.path.join(d, view), crop)
                st["clean"] = view                    # 兼容前端：预览直接读 st.clean
                st["locate_view"] = view              # 方案 A 显式字段（前端优先读它）
                st["crop_meta"] = cmeta
                det = [p for p in P.detect_parts(crop) if p.get("kind") == want]
                st["parts_detected"] = det
                st["stage"] = "parts_review"
                st["step"] = (("已自动定位 %s（原图直抠 · 未经 AI），可拖拽 / 缩放 / 手动重画后确认" % want)
                              if det else ("未自动定位到 %s，请在图上手动框选" % want))
                _save(tid, st)
                _pub_safe(tid, st)
                return

            # ===== 摄像头（大镜头框）：AI 干净壳 + 满幅重绘 + 「区域比例抠选」=====
            # 原图直抠会继承手/镜片遮挡 → 卡通图案抠不全；改为「满幅重绘版 + 区域比例抠选」：
            #   摄像头框横向几乎占满全宽、纵向只占顶部 ~27%，故抠选区 = 横向内容全宽 +
            #   纵向从内容顶延伸到「框底 + LENS_DOWN_EXT×框高」，把摄像头下方一圈图案抠齐全。
            if tt == "lens":
                key = P.load_key()
                st["step"] = "Step1 去水印/去手/互补 → 干净产品图"; _save(tid, st)
                p1 = P.STEP1_PROMPT.format(
                    imgs="%d 个角度的图" % len(srcs) if len(srcs) > 1 else "产品图",
                    multi="各角度" if len(srcs) > 1 else "（单图）")
                tid_api = _step1_clean(d, st, tid, srcs, p1)
                st["step"] = "Step1 生图中（约1-2分钟）"; _save(tid, st)
                url = P.wait_task(key, tid_api)
                clean = "干净壳.png"
                P.download(url, os.path.join(d, clean))
                st["clean"] = clean; _save(tid, st)
                # 定位大镜头框（供区域比例抠选用）
                crop, cmeta = P.shell_crop_meta(os.path.join(d, clean))
                det = [p for p in P.detect_parts(crop) if p.get("kind") == P.PART_LENS]
                st["parts_detected"] = det
                st["crop_meta"] = cmeta
                st["step"] = "Step2 AI 满幅重绘（版式死锁）"; _save(tid, st)
                red_fn = "图案_重绘版.png"
                rsize = _redraw_size(st, crop.shape[1], crop.shape[0])
                st["redraw_req"] = rsize
                try:
                    P.redraw_fullbleed(os.path.join(d, clean), os.path.join(d, red_fn), key, rsize,
                                       full_bg=bool(st.get("full_bg")),
                                       ratio=st.get("ratio"),
                                       long_edge=st.get("long_edge"))
                    st["redraw_ok"] = True
                except Exception as ex:
                    st["redraw_msg"] = "AI 满幅重绘失败：%s" % str(ex)[:160]
                    st["stage"] = "error"
                    st["error"] = st["redraw_msg"]
                    _save(tid, st)
                    return
                st["step"] = "区域比例抠选（横向全宽 + 向下延伸 %.2f×框高）" % P.LENS_DOWN_EXT
                _save(tid, st)
                region_fn = "摄像头图案_区域比例.png"
                try:
                    _, rc_meta = P.lens_region_crop(
                        os.path.join(d, clean), os.path.join(d, red_fn), parts=det,
                        down_ext=P.LENS_DOWN_EXT, out_png=os.path.join(d, region_fn))
                    st["region_meta"] = rc_meta
                    st["region_crop"] = True
                    st["down_ext"] = P.LENS_DOWN_EXT
                    st["versions"] = [region_fn] + prev_v
                    st["stage"] = "selecting"
                    st["step"] = "区域比例抠选完成，可手动调延伸系数后重新抠选"
                except Exception as ex:
                    st["stage"] = "error"
                    st["error"] = "区域比例抠选失败：%s" % str(ex)[:300]
                _save(tid, st)
                _pub_safe(tid, st)
                return

            # ===== 手机壳图案（满幅）：需要 AI 干净壳 =====
            key = P.load_key()
            # Step1 干净图（多张原图 → 真·多图参考，失败自动回退拼图，见 _step1_clean）
            st["step"] = "Step1 去水印/去手/互补 → 干净产品图"; _save(tid, st)
            p1 = P.STEP1_PROMPT.format(
                imgs="%d 个角度的图" % len(srcs) if len(srcs) > 1 else "产品图",
                multi="各角度" if len(srcs) > 1 else "（单图）")
            tid_api = _step1_clean(d, st, tid, srcs, p1)
            st["step"] = "Step1 生图中（约1-2分钟）"; _save(tid, st)
            url = P.wait_task(key, tid_api)
            clean = "干净壳.png"
            P.download(url, os.path.join(d, clean))
            st["clean"] = clean; _save(tid, st)
            # Step2 按任务类型分流（用户拍板：壳 / 支架 / 摄像头 = 三个独立任务）
            # 【手机壳图案（满幅）】用户拍板（2026-10-11）：**选版只出重绘版**。
            # 锁定版仍照旧生成 —— 它转做「后台角色」：① 重绘版版式校验的基准（layout_deviation
            # 必须以它为参照，否则误判）；② 重绘失败时的兜底候选。只是**不再进 st["versions"]**，
            # 因此不会出现在选版页、也不可被选去做 PSD。
            st["step"] = "Step2a 程序化提取整壳图案（锁定版）"; _save(tid, st)
            lock_fn = "图案_锁定版.png"
            lock_dir = os.path.join(d, "lock")
            crop, cmeta = P.shell_crop_meta(os.path.join(d, clean))
            det = P.detect_parts(crop)
            meta = P.extract_shell_full(os.path.join(d, clean), os.path.join(d, lock_fn),
                                        parts=det, hole_dir=lock_dir)
            st["extract_meta"] = meta
            st["parts_detected"] = det
            st["crop_meta"] = cmeta
            _save(tid, st)
            # 被遮挡处（摄像头模组 + 支架/镜头框占位）→ AI 同风格补全
            if st.get("inpaint", True):
                st["step"] = "Step2b AI 同风格补全被遮挡区（约1-2分钟）"; _save(tid, st)
                try:
                    ok, msg = P.inpaint_regions(os.path.join(d, lock_fn), key=key,
                                                hole_png=os.path.join(lock_dir, meta["hole_png"]),
                                                mode="ai", fill_all=True)
                    st["inpaint_msg"] = msg
                except Exception as ex:
                    st["inpaint_msg"] = "AI 补全失败（遮挡区保留透明）：%s" % str(ex)[:160]
                _save(tid, st)
            else:
                st["inpaint_msg"] = "遮挡区保持透明（未启用补全）"
            # 重绘版：AI 满幅重绘（prompt 死锁版式，尺寸按壳身实际比例）
            st["step"] = "Step2c AI 满幅重绘（版式死锁，约1-2分钟）"; _save(tid, st)
            red_fn = "图案_重绘版.png"
            try:
                cv = (meta.get("canvas") or [3, 4])
                rsize = _redraw_size(st, cv[0], cv[1])
                st["redraw_req"] = rsize
                P.redraw_fullbleed(os.path.join(d, clean), os.path.join(d, red_fn), key, rsize,
                                   full_bg=bool(st.get("full_bg")),
                                   ratio=st.get("ratio"),
                                   long_edge=st.get("long_edge"))
                # 只把重绘版放进候选版本（锁定版不进 versions → 选版页只有一张卡）
                st["versions"] = [red_fn] + prev_v
                try:
                    from PIL import Image as _I
                    with _I.open(os.path.join(d, red_fn)) as _im:
                        st["redraw_size"] = "%dx%d" % _im.size
                except Exception:
                    pass
            except Exception as ex:
                st["redraw_msg"] = "AI 满幅重绘失败：%s" % str(ex)[:160]
                # 兜底：重绘失败时锁定版是唯一可用产物 → 仍放出候选，免得选版页空无一项
                st["versions"] = [lock_fn] + prev_v
            _save(tid, st)
            # 版式校验：重绘版有没有偷偷重排元素
            # ⚠️ 基准必须是「锁定版」而不是「干净壳」：干净壳上还杵着支架圆环 + 摄像头模组，
            #    重绘版按设计会把配件去掉，拿带配件的原图当基准 → 好作品也被误判成「被重排」。
            if os.path.isfile(os.path.join(d, red_fn)):
                st["step"] = "版式校验（重绘版 vs 锁定版）"; _save(tid, st)
                try:
                    st["layout_check"] = P.layout_deviation(
                        os.path.join(d, lock_fn), os.path.join(d, red_fn),
                        ref_label="锁定版（后台基准 · 不参与选版）")
                except Exception as ex:
                    st["layout_check"] = {"note": "版式校验失败：%s" % str(ex)[:120]}
                _save(tid, st)
            st["stage"] = "selecting"; st["step"] = "等待选版"
            _save(tid, st)
            _pub_safe(tid, st)
        except Exception as e:
            st["stage"] = "error"; st["error"] = str(e)[:500]; _save(tid, st)
        finally:
            if gated:                              # 无论成功/失败/return 都要把并发槽还回去
                try:
                    _batch_sem.release()
                except ValueError:
                    pass
    t = threading.Thread(target=work, daemon=True)
    t.start()

def _parts_redraw(tid, st, out_dir, meta):
    """【支架 · AI 完全重绘】给每个已抠出的部件补一版 AI 重绘 PNG。

    输入 = 锁定版（全画布 RGBA 透明底，原图直抠，逐像素 100% 保真，但清晰度锁死在原图、
           手指/反光/白盘边缘会一起抠进来）；
    输出 = 同画布、同位置、内容 bbox 对齐回原位的透明底 PNG（见 P.redraw_part），
           可当锁定版的直接替身进分层 PSD。
    失败**不致命**：保留锁定版，把原因写进 redraw_msg，前端给「重新生成」按钮。
    返回成功生成的文件个数。
    """
    ok_n = 0
    key = None
    for it in meta.get("parts", []):
        kind = it.get("kind") or "支架"
        src = os.path.join(out_dir, it["png"])
        rn = "图案_%s_重绘版.png" % kind
        st["stage"] = "gen_running"
        st["step"] = "AI 完全重绘「%s」（版式死锁，约 1-2 分钟）" % kind
        _save(tid, st)
        try:
            if key is None:
                key = P.load_key()
            ok, msg, info = P.redraw_part(src, os.path.join(out_dir, rn), key=key,
                                          on_log=lambda m: _prog(tid, st, m))
            if ok:
                it["redraw"] = rn
                it["redraw_meta"] = info
                try:
                    from PIL import Image as _I
                    with _I.open(os.path.join(out_dir, rn)) as _im:
                        it["redraw_size"] = "%dx%d" % _im.size
                except Exception:
                    it["redraw_size"] = ""
                st["redraw_msg"] = msg
                ok_n += 1
        except Exception as ex:
            st["redraw_msg"] = "AI 完全重绘失败（已保留锁定版）：%s" % str(ex)[:200]
        _save(tid, st)
    return ok_n

def _run_extract(tid, st, parts):
    """【独立部件任务】按用户确认的框提取该部件的图案（版式锁定，逐像素复刻，不重排）。"""
    def work():
        try:
            d = os.path.join(DATA, tid)
            out_dir = os.path.join(d, "parts")
            want = st.get("parts_want") or P.PART_LENS
            # 方案 A：locate_src = 原图（不过 AI）；旧任务回落 st["clean"]
            src_fn = st.get("locate_src") or st.get("clean")
            st["stage"] = "gen_running"
            st["step"] = "按确认的框提取「%s」图案" % want; _save(tid, st)
            meta = P.extract_parts(os.path.join(d, src_fn), out_dir,
                                   parts=parts, only=want)
            if not meta.get("parts"):
                raise RuntimeError("未提取到「%s」图案，请确认框选范围是否覆盖了目标部件" % want)
            # ⚠️ 文件实际落在 parts/ 子目录 → 版本引用必须存「相对路径」，
            #    否则前端取图时 task_file 在根目录找不到 → 候选版本一片空白（不出图）。
            lock_vs = ["parts/" + it["png"] for it in meta["parts"]]
            st["parts_meta"] = meta; _save(tid, st)
            # ===== 用户拍板（2026-10-11）：支架 / 摄像头部件也「不要锁定版，只留另一个版本」=====
            # 原图直抠（锁定版 / 逐像素 100% 保真）仍照旧生成、留在 parts_meta 里 —— 它转做
            # **后台角色**：① AI 完全重绘失败时的兜底候选；② PSD 的兜底源图。
            # 但**不再进 st["versions"]** → 选版页只出现「AI 完全重绘版」一张卡。
            # ⚠️ 连带坑（记住）：凡「缩减 versions」的改动，都要回头查所有依赖
            #    `len(st["versions"])` / `versions` 内容的判断 —— 否则会被静默跳过。
            st["versions"] = []
            if want in (P.PART_STAND, P.PART_LENS) and st.get("want_redraw", True):
                if _parts_redraw(tid, st, out_dir, meta):
                    st["redraw_ok"] = True
                    extra = [it["redraw"] for it in meta["parts"] if it.get("redraw")]
                    st["versions"] = ["parts/" + n for n in extra]
                st["parts_meta"] = meta
            if not st["versions"]:
                st["versions"] = lock_vs                 # 重绘失败 / 未启用 → 锁定版兜底，页面不空
            st["stage"] = "selecting"; st["step"] = "等待确认"
            _save(tid, st)
            _pub_safe(tid, st)
        except Exception as e:
            st["stage"] = "error"; st["error"] = str(e)[:500]; _save(tid, st)
    threading.Thread(target=work, daemon=True).start()

def _run_psd_parts(tid, st, layer_mode="merge2", use_redraw=False):
    """【独立部件任务】为该部件出一份 PNG + 一份分层 PSD（裁到部件区域，不出合并版）。

    use_redraw=True → 用「AI 完全重绘版」当源图（线条更干净、清晰度更高、版式已死锁）；
    否则用「原图直抠（兜底版）」（逐像素 100% 保真，但清晰度锁死在原图）。两版同画布同位置，可无痛互换。

    ⚠️ 2026-10-11 拍板：支架 / 摄像头部件档的「锁定版」不再作为候选版本展示，只留「AI 重绘版」；
    锁定版仍照旧生成、留在 parts_meta 里，转做 ① AI 重绘的输入源 ② PSD 的兜底源图（use_redraw=False 走它）。
    """
    def work():
        try:
            from PIL import Image
            d = os.path.join(DATA, tid)
            out_dir = os.path.join(d, "parts")
            meta = st["parts_meta"]
            st["stage"] = "psd_running"
            st["selected"] = "redraw" if use_redraw else "parts"
            st["psd_src"] = "redraw" if use_redraw else "lock"
            _save(tid, st)
            outputs = []
            for it in meta["parts"]:
                kind = it["kind"]
                x, y, w, h = it["box"]
                src_name = (it.get("redraw") or it["png"]) if use_redraw else it["png"]
                st["step"] = "【%s】超分 + 分层（%s）" % (kind, LAYER_MODE_LABEL.get(layer_mode, layer_mode))
                _save(tid, st)
                im = Image.open(os.path.join(out_dir, src_name)).convert("RGBA")
                # 重绘版画布是「原画布等比放大」的（redraw_part 保住 AI 原生分辨率）→ 裁切框同比放大
                sc = float((it.get("redraw_meta") or {}).get("scale") or 1.0) \
                    if (use_redraw and it.get("redraw")) else 1.0
                bx = [int(round(v * sc)) for v in (x, y, w, h)]
                bx[0] = max(0, min(im.width - 1, bx[0]))
                bx[1] = max(0, min(im.height - 1, bx[1]))
                bx[2] = max(1, min(im.width - bx[0], bx[2]))
                bx[3] = max(1, min(im.height - bx[1], bx[3]))
                cp = os.path.join(out_dir, "C_%s.png" % kind)
                im.crop((bx[0], bx[1], bx[0] + bx[2], bx[1] + bx[3])).save(cp)
                hi = os.path.join(out_dir, "H_%s.png" % kind)
                _, warn = P.upscale_short(cp, hi)
                if warn:
                    st["upscale_warn"] = warn
                ldir = os.path.join(out_dir, "layers_%s" % kind)
                files, size = P.build_layers(hi, ldir, kind="cutout", mode=layer_mode)
                if not files:
                    raise RuntimeError("分层结果为空（%s），请更换分层粒度" % kind)
                mf = os.path.join(out_dir, "mf_%s.json" % kind)
                P.build_manifest(files, size, mf)
                psd = "%s_图案.psd" % kind
                P.assemble(mf, os.path.join(d, psd), os.path.join(d, "_预览.png"))
                P.inject_dpi(os.path.join(d, psd))
                ok, total = P.verify(os.path.join(d, psd), mf)
                outputs.append({"kind": kind, "psd": psd, "png": "parts/" + src_name, "box": bx,
                                "mb": round(os.path.getsize(os.path.join(d, psd)) / 1e6, 1),
                                "layers": [nm for nm, _ in files], "size": list(size),
                                "ppm": it.get("ppm"), "ents": it.get("ents"),
                                "src": ("AI 重绘版" if (use_redraw and it.get("redraw")) else "原图直抠（兜底版）"),
                                "src_scale": sc,
                                "note": it.get("note", ""),
                                "verify": "%d/%d 层像素零差异 %s" % (ok, total, "PASS" if ok == total else "FAIL")})
            st["parts_outputs"] = outputs
            st["psd"] = outputs[0]["psd"] if outputs else ""
            st["psd_mb"] = outputs[0]["mb"] if outputs else 0
            st["hi_png"] = outputs[0]["png"] if outputs else ""
            st["verify"] = outputs[0]["verify"] if outputs else ""
            st["layer_mode"] = layer_mode
            st["stage"] = "done"; st["step"] = "完成"
            _save(tid, st)
            _pub_safe(tid, st)
        except Exception as e:
            st["stage"] = "error"; st["error"] = str(e)[:500]; _save(tid, st)
    threading.Thread(target=work, daemon=True).start()

def _run_psd(tid, st, version_fn, strategy, layer_mode="merge2"):
    if st.get("parts_meta"):
        _run_psd_parts(tid, st, layer_mode, use_redraw=(version_fn == "redraw"))
        return
    def work():
        try:
            d = os.path.join(DATA, tid)
            src = os.path.join(d, version_fn)
            st["stage"] = "psd_running"; st["selected"] = version_fn
            st["step"] = "超分至短边 %d（EDSR x2）" % P.TARGET_SHORT; _save(tid, st)
            hi = os.path.join(d, "图案_2048.png")
            _, warn = P.upscale_short(src, hi)
            if warn:
                st["upscale_warn"] = warn
            st["step"] = "图案类型判别"; _save(tid, st)
            if version_fn.startswith("图案_锁定版") or st.get("region_crop"):
                kind, why = "cutout", (
                    "锁定版（透明底，逐像素复刻原图版式），按元素连通域分层"
                    if version_fn.startswith("图案_锁定版")
                    else "区域比例抠选（透明底，逐像素保真），按元素连通域分层")
            else:
                kind, why = (P.classify(hi) if strategy == "auto" else
                             (("white_bg", "手动指定") if strategy == "white_bg" else ("full_bleed", "手动指定")))
            st["pattern_kind"] = kind; st["pattern_kind_why"] = why; _save(tid, st)
            st["step"] = "分层中（%s）" % LAYER_MODE_LABEL.get(layer_mode, layer_mode)
            _save(tid, st)
            ldir = os.path.join(d, "layers")
            files, size = P.build_layers(hi, ldir, kind=kind, mode=layer_mode)
            if not files:
                raise RuntimeError("分层结果为空，请更换图案版本或分层粒度")
            st["layer_mode"] = layer_mode
            st["step"] = "组装 PSD（RLE 压缩·PS 兼容）"; _save(tid, st)
            mf = os.path.join(d, "manifest.json")
            P.build_manifest(files, size, mf)
            tag = ("锁定版" if version_fn.startswith("图案_锁定版")
                   else ("区域比例" if st.get("region_crop")
                         else ("重绘版" if "重绘版" in version_fn else "图案")))
            psd = "图案_分层_%s.psd" % tag
            P.assemble(mf, os.path.join(d, psd), os.path.join(d, "_预览.png"))
            st["step"] = "注入 150dpi + 逐层校验"; _save(tid, st)
            P.inject_dpi(os.path.join(d, psd))
            ok, total = P.verify(os.path.join(d, psd), mf)
            st["verify"] = "%d/%d 层像素零差异 %s" % (ok, total, "PASS" if ok == total else "FAIL")
            if ok != total:
                raise RuntimeError("逐层校验未通过: " + st["verify"])
            st["psd"] = psd
            st["psd_mb"] = round(os.path.getsize(os.path.join(d, psd)) / 1e6, 1)
            st["psd_layers"] = [nm for nm, _ in files]
            st["hi_png"] = "图案_2048.png"
            st["stage"] = "done"; st["step"] = "完成"
            _save(tid, st)
            _pub_safe(tid, st)
        except Exception as e:
            st["stage"] = "error"; st["error"] = str(e)[:500]; _save(tid, st)
    threading.Thread(target=work, daemon=True).start()

def base64dec(b):
    import base64
    return base64.b64decode(b)

# ---------------- API ----------------
@app.get("/api/health")
def health():
    return {"ok": True, "tasks": len(_tasks), "key": os.path.exists(P.KEY_FILE),
            "fsrcnn": os.path.exists(P.FSRCNN_MODEL), "edsr": os.path.exists(P.EDSR_MODEL)}

TASK_TYPES = ("shell", "stand", "lens")
TASK_TYPE_NAME = {"shell": "手机壳图案", "stand": "支架图案", "lens": "摄像头图案"}


def _step1_clean(d, st, tid, srcs, p1):
    """Step1「去手/去水印/互补 → 干净产品图」的唯一入口。

    2026-11-15（雪碧拍板「多图主力用 apib」）：多张原图**不再先拼图**，而是把
    **多张原图一起当参考图送进 AI**（真·多图参考，走 pipeline 的多图线 apib → huahuaapi）。
    实测这样比「横向拼成一张联络表」更贴近「各角度互相补全被手挡住的区域」的本意。

    安全网：多图线整条挂掉（两站都失败）时**自动回退**到老的「拼图 + 单图线」，
    保证不会因为多图通道故障导致整单失败；回退痕迹写进 st 供排查。
    """
    key = P.load_key()
    if len(srcs) == 1:
        return P.gen_image(key, p1, "1024x1024", P.img_to_b64(os.path.join(d, srcs[0])))
    st["step"] = "Step1 多图参考（%d 张一起送）" % len(srcs); _save(tid, st)
    refs = [P.img_to_b64(os.path.join(d, s)) for s in srcs]
    st["multi_ref"] = len(refs); _save(tid, st)
    try:
        return P.gen_image(key, p1, "1024x1024", refs=refs)
    except Exception as ex:                       # noqa: BLE001
        st["multi_ref_fallback"] = str(ex)[:160]
        st["step"] = "多图参考失败 → 回退拼图"; _save(tid, st)
        in_b64 = P.make_contact_sheet([os.path.join(d, s) for s in srcs])
        open(os.path.join(d, "_拼图输入.jpg"), "wb").write(base64dec(in_b64))
        return P.gen_image(key, p1, "1024x1024", in_b64)


def _norm_ratio(raw):
    """把客户端传来的「输出尺寸」归一化。

    2026-10-21：此前写死 `ratio not in ("1:1","3:4","9:16")` 就 400，且 ratio 收下后
    全程没人用（真正尺寸来自 fullbleed_size 按壳身实际比例算）。现在：
      · 支持 auto / 全部预置档位 / 自定义 WxH（256~4096）；
      · 非法值**降级为 auto 而不是 400** —— 尺寸是锦上添花项，绝不该把整条任务带崩；
      · 返回 (归一化标签, (rw,rh) 或 None, 请求像素或 '')，写进 st 供前端回显。
    """
    lbl, wh, px = P.parse_ratio(raw)
    return lbl, wh, px


def _norm_long_edge(raw):
    """「输出分辨率」区的长边像素归一化（2026-11-15 双区域控件配套）。

    只在**比例=自动**时才有意义：形状由壳身真实比例决定，用户只指定清晰度。
    比例给了确定像素（如 1536x2048）时前端不会传这个值，传了也会被 ratio 覆盖。
    · 空 / 非法 / 越界 → 0（= 用 1024 老行为），绝不 400；
    · 有效值钳到 256~4096（上游单边硬上限，超了必失败退费）。
    """
    try:
        v = int(str(raw or "").strip())
    except Exception:                              # noqa: BLE001
        return 0
    if v <= 0:
        return 0
    return max(256, min(4096, v))


def _redraw_size(st, case_w, case_h):
    """满幅重绘的请求尺寸（三处调用点唯一收口，避免逻辑分叉）。

    优先级：用户选的固定比例/像素 > 自动比例 + 指定长边 > 壳身实际比例（1024）。
    与改造前完全等价的那条路径是「ratio=auto 且 long_edge=0」→ fullbleed_size_for 内部
    直接回落 fullbleed_size(short=1024)，逐像素一致。
    """
    r = st.get("ratio")
    if r and r != P.RATIO_AUTO:
        _l, _w, _px = _norm_ratio(r)
        if _px:
            return _px
    return P.fullbleed_size_for(case_w, case_h, long_edge=_norm_long_edge(st.get("long_edge")))


@app.post("/api/tasks")
async def create_task(request: Request, name: str = Form("未命名任务"), ratio: str = Form("auto"),
                      versions: int = Form(2), task_type: str = Form("shell"),
                      inpaint: str = Form("1"), full_bg: str = Form("0"),
                      long_edge: str = Form(""),
                      images: list[UploadFile] = File(...)):
    if not (1 <= len(images) <= 5):
        raise HTTPException(400, "原图必须 1-5 张")
    ratio, _rwh, _rpx = _norm_ratio(ratio)
    # 比例已给出确定像素时，长边档无意义 → 归零，免得诊断字段里两个值互相矛盾
    _le = 0 if _rpx else _norm_long_edge(long_edge)
    if task_type not in TASK_TYPES:
        task_type = "shell"
    tid = time.strftime("%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    d = os.path.join(DATA, tid)
    os.makedirs(d, exist_ok=True)
    saved = []
    for i, f in enumerate(images):
        ext = os.path.splitext(f.filename or "")[1].lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            raise HTTPException(400, "仅支持 jpg/png/webp")
        fn = "原图%02d%s" % (i + 1, ext)
        with open(os.path.join(d, fn), "wb") as out:
            out.write(await f.read())
        saved.append(fn)
    st = {"id": tid, "name": name.strip() or "未命名任务", "ratio": ratio,
          "long_edge": _le,                      # 自动比例下的长边档（0=1024 老行为）
          "task_type": task_type, "task_name": TASK_TYPE_NAME[task_type],
          # 「遮挡区处理」已收进后台（2026-10-14）：前端不再有开关，一律 AI 同风格补全。
          # 仍保留 Form 参数以免旧客户端 422；客户端传值一律忽略（这里硬写 True 覆盖）。
          "inpaint": True,
          # 成品底色开关（2026-10-14）：True=整幅底图（保留原壳底色铺满）；False=镂空图案（白底）
          "full_bg": str(full_bg).lower() in ("1", "true", "yes"),
          "inputs": saved, "stage": "created",
          "step": "排队中", "logs": [],
          "created": time.strftime("%Y-%m-%d %H:%M"),
          "_uid": request.scope.get("st_uid"),      # 归属隔离：只对自己的账号可见
          "_created_ts": int(time.time()), "_touch": int(time.time())}
    _tasks[tid] = {"lock": threading.Lock(), "state": st}
    _save(tid, st)
    _run_gen(tid, st)
    return {"id": tid}

# ---------------- 多张独立 · 批量提取（2026-10-13） ----------------
# 与「同一产品多角度互补」的区别：这里**每张原图各自成一个独立任务**，各出各的图案，
# 互不参照。批次只是一层「分组标签」（batch_id / batch_index），落地仍是标准 task，
# 所以选版 / PSD / 重做 / 72h 清理全部复用既有链路，零特例。
def _safe_entry(name, fallback="item"):
    """ZIP 条目名净化：去掉路径分隔符与控制字符（防目录穿越 / 解压炸目录）。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name or "")).strip(" ._")
    return (s or fallback)[:60]


@app.post("/api/tasks/batch")
async def create_batch(request: Request, name: str = Form("未命名批次"), ratio: str = Form("auto"),
                       task_type: str = Form("shell"), inpaint: str = Form("1"),
                       full_bg: str = Form("0"),
                       long_edge: str = Form(""),
                       items: str = Form(""),
                       images: list[UploadFile] = File(...)):
    """批量提取：N 张原图 → M 个独立任务（同 batch_id），受并发闸门约束（同时最多 3 个在跑）。

    2026-10-13 v2 新增 `items`（JSON 数组）——**每个图案一个独立配置**：
        [{"name":"红玫瑰","task_type":"shell","inpaint":"1","count":2}, ...]
    `count` = 该图案占用几张原图（多角度互补，1-5 张），按顺序从 `images` 里切分。
    不传 items → 完全走旧逻辑（每张图各成一个 shell 任务），向后兼容。
    """
    ratio, _rwh, _rpx = _norm_ratio(ratio)
    _le = 0 if _rpx else _norm_long_edge(long_edge)   # 本批统一长边档（0=1024）

    # ---------------- 解析配置计划（无 items 则退化为「每张图一个 shell 任务」）----------------
    plan = []
    if (items or "").strip():
        try:
            arr = json.loads(items)
        except Exception:                            # noqa: BLE001
            raise HTTPException(400, "items 不是合法 JSON")
        if not isinstance(arr, list) or not arr:
            raise HTTPException(400, "items 必须是非空数组")
        if len(arr) > BATCH_MAX_TASKS:
            raise HTTPException(400, "批量最多 %d 个图案（任务）" % BATCH_MAX_TASKS)
        used = 0
        for i, it in enumerate(arr):
            if not isinstance(it, dict):
                raise HTTPException(400, "items[%d] 必须是对象" % i)
            tt = str(it.get("task_type") or "shell")
            if tt not in TASK_TYPES:
                raise HTTPException(400, "items[%d].task_type 不支持：%s" % (i, tt))
            try:
                cnt = int(it.get("count") or 1)
            except Exception:                        # noqa: BLE001
                raise HTTPException(400, "items[%d].count 必须是整数" % i)
            if not (1 <= cnt <= 5):
                raise HTTPException(400, "每个图案的原图需 1-5 张（items[%d]=%d）" % (i, cnt))
            plan.append({"name": str(it.get("name") or "").strip(), "task_type": tt,
                         "task_name": TASK_TYPE_NAME[tt],
                         "inpaint": it.get("inpaint", inpaint),
                         "full_bg": it.get("full_bg", full_bg), "count": cnt})
            used += cnt
        if used != len(images):
            raise HTTPException(400, "原图张数对不上：items 合计 %d 张，实际收到 %d 张"
                                     % (used, len(images)))
    else:
        if not (1 <= len(images) <= BATCH_MAX_TASKS):
            raise HTTPException(400, "批量张数需 1-%d 张" % BATCH_MAX_TASKS)
        if task_type != "shell":
            raise HTTPException(400, "批量模式仅支持「手机壳图案」")
        for _ in images:
            plan.append({"name": "", "task_type": "shell", "task_name": TASK_TYPE_NAME["shell"],
                         "inpaint": inpaint, "full_bg": full_bg, "count": 1})

    # ---------------- 扩展名先行校验（避免边写盘边失败，留下半截垃圾目录）----------------
    exts = []
    for f in images:
        ext = os.path.splitext(f.filename or "")[1].lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            raise HTTPException(400, "仅支持 jpg/png/webp：%s" % (f.filename or "?"))
        exts.append(ext)

    uid = request.scope.get("st_uid")
    bid = time.strftime("%m%d_%H%M%S") + "_b" + uuid.uuid4().hex[:6]
    base = (name or "").strip() or "未命名批次"
    now = int(time.time())
    made, idx = [], 0
    for k, pl in enumerate(plan):
        tid = "%s_%02d" % (bid, k + 1)
        d = os.path.join(DATA, tid)
        os.makedirs(d, exist_ok=True)
        saved = []
        for j in range(pl["count"]):                  # 该图案独占的 count 张原图（多角度互补）
            ext = exts[idx]
            fn = "原图%02d%s" % (j + 1, ext)
            with open(os.path.join(d, fn), "wb") as out:
                out.write(await images[idx].read())
            saved.append(fn)
            idx += 1
        tname = pl["name"] or ("%s · %02d" % (base, k + 1))   # 留空 → 批次名+序号兜底
        st = {"id": tid, "name": tname, "ratio": ratio,
              "long_edge": _le,                # 本批统一长边档（0=1024 老行为）
              "task_type": pl["task_type"], "task_name": pl["task_name"],
              "inpaint": True,   # 遮挡区处理已收进后台：一律 AI 同风格补全（忽略 items 传值）
              "full_bg": str(pl["full_bg"]).lower() in ("1", "true", "yes"),
              "inputs": saved, "stage": "queued",
              "step": "排队中", "logs": [],
              "batch_id": bid, "batch_name": base, "batch_index": k + 1, "batch_total": len(plan),
              "created": time.strftime("%Y-%m-%d %H:%M"),
              "_uid": uid, "_created_ts": now, "_touch": now}
        _tasks[tid] = {"lock": threading.Lock(), "state": st}
        _save(tid, st)
        made.append(tid)
    for tid in made:
        _run_gen(tid, _tasks[tid]["state"], gate=True)
    return {"batch_id": bid, "ids": made, "count": len(made)}


# ---------------- 自动命名（本地 CV：主色 + 形态，2026-10-13） ----------------
# 用户定：批量区每个图案「留空则自动识别命名」。为免烧 AI 额度，用本机 OpenCV 算：
#   主色 = k-means 取最大簇做背景 → 偏离背景的像素均值色；形态 = 连通域数 + 最大占比 + 投影起伏。
# 纯本地、不计费、不落盘、不建任务。
_HUE_BANDS = ((8, "红"), (22, "橙"), (33, "黄"), (78, "绿"), (95, "青"),
              (125, "蓝"), (148, "紫"), (172, "玫红"), (180, "红"))


def _cn_color(bgr):
    """单像素均值色（BGR）→ 中文色名。低饱和走黑/灰/白；低明度加深、高明度低饱和加浅。"""
    import colorsys
    b, g, r = [float(x) / 255.0 for x in bgr]
    mx, mn = max(r, g, b), min(r, g, b)
    v = mx
    s = 0.0 if mx <= 0 else (mx - mn) / mx
    if s < 0.16:
        if v < 0.25:
            return "黑"
        if v > 0.85:
            return "白"
        return "灰"
    hd = colorsys.rgb_to_hsv(r, g, b)[0] * 180.0     # 换到 OpenCV 0-180 色相刻度
    nm = "红"
    for lim, nn in _HUE_BANDS:
        if hd < lim:
            nm = nn
            break
    if v < 0.35:
        return "深" + nm
    if v > 0.78 and s < 0.45:
        return "浅" + nm
    return nm


def _cn_shape(mask):
    """前景掩码 → 形态词（本地连通域几何统计，只用 cv2+numpy）。

    关键判据（踩过的坑）：**规则网格排布的点阵也必须判成「波点」而不是「格纹」**——
    真实波点图案恰恰就是等距网格，不能只看行列投影有起伏。区分靠几何：
      条纹 = 连通域本身是贯穿性的**细长条**（轴对齐时 长 > 50% 图边且短 < 35% 另一边）；
              **斜向条纹轴对齐包围盒会同时很大** → 必须再用 minAreaRect 量真实细长比，
              否则斜条纹会被误判成「波点/小碎花」（2026-10-13 E2E 实测踩坑）；
              判据用**贯穿条面积占比**（≥ 0.45）而非块数占比 —— 斜条纹两侧的角部碎片
              会把连通域总数撑大（实测 cnt=23 / nlong=9），块数比会被噪点拉爆；
      波点 = 大量**大小一致**的离散小块（面积变异系数 < 0.30）；
      小碎花 = 大量**大小不一**的离散小块。
    """
    import cv2
    import numpy as np
    m = (mask > 0).astype("uint8")
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    if n <= 1:
        return "图案"
    H, W = m.shape[:2]
    areas = np.array([int(a) for a in stats[1:, 4]], dtype="float64")
    keep = areas > max(4.0, areas.sum() * 0.0015)     # 滤掉噪点
    if not keep.any():
        return "图案"
    idx = np.where(keep)[0] + 1
    a2 = areas[keep]
    total = float(a2.sum()) or 1.0
    big = float(a2.max()) / total
    cnt = int(len(a2))

    ws = stats[idx, 2].astype("float64")
    hs = stats[idx, 3].astype("float64")
    nv = int(np.sum((hs > 0.5 * H) & (ws < 0.35 * W)))    # 贯穿竖条
    nh = int(np.sum((ws > 0.5 * W) & (hs < 0.35 * H)))    # 贯穿横条
    if nv >= 3 and nv >= nh:
        return "竖条纹"
    if nh >= 3:
        return "横条纹"
    # 斜向 / 任意走向的贯穿细长条：轴对齐 bbox 两个方向都很大 → 上面两条规则必然漏判。
    # 对「bbox 够大」的候选块用 minAreaRect 量真实细长比（长 > 0.45×对角线 且 短 < 0.12×对角线）。
    diag = (float(W) * W + float(H) * H) ** 0.5
    cand = [int(i) for i, w_, h_ in zip(idx, ws, hs) if max(w_, h_) > 0.45 * diag]
    nlong = 0
    long_area = 0.0
    for i in cand:
        pts = cv2.findNonZero((labels == i).astype("uint8"))
        if pts is None or len(pts) < 5:
            continue
        _, (rw, rh), _ = cv2.minAreaRect(pts)
        L, S = max(rw, rh), min(rw, rh)
        if L > 0.45 * diag and S < 0.12 * diag:
            nlong += 1
            long_area += float(stats[int(i), 4])
    # 判据取「贯穿条**面积占比**」而非块数占比：斜条纹两侧的角部碎片会把连通域总数
    # 撑大（实测 cnt=23 / nlong=9），用 nlong*2>=cnt 会被噪点拉爆 → 漏判成小碎花。
    if nlong >= 3 and (long_area / total) >= 0.45:       # 主体面积就是贯穿条 → 条纹（含斜向）
        return "条纹"
    if big > 0.55:                                        # 单一压倒性大块
        return "主图案"
    if cnt >= 6 and big < 0.35:                           # 多块离散元素
        mu = float(a2.mean())
        cv_area = float(a2.std() / mu) if mu > 0 else 0.0
        if cv_area < 0.30:
            return "波点"                                 # 大小一致 → 规则排布
        return "小碎花" if cnt >= 10 else "图案组"
    if big >= 0.20:
        return "主图案"
    return "图案组"


def _sat(bgr):
    """单像素 BGR → 饱和度（0-1）。用于分辨「有彩色 / 无彩色」。"""
    try:
        b, g, r = float(bgr[0]), float(bgr[1]), float(bgr[2])
    except Exception:                                # noqa: BLE001
        return 0.0
    mx, mn = max(b, g, r), min(b, g, r)
    return 0.0 if mx <= 0 else (mx - mn) / mx


def _autoname_one(path):
    """一张原图 → 建议名（如「蓝色小碎花」）。读图走 imdecode（中文路径铁律）。"""
    import cv2
    import numpy as np
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return "图案"
    h, w = img.shape[:2]
    sc = 320.0 / float(max(h, w))
    if sc < 1.0:
        img = cv2.resize(img, (max(1, int(w * sc)), max(1, int(h * sc))),
                         interpolation=cv2.INTER_AREA)
    flat = img.reshape(-1, 3).astype("float32")
    try:                                             # 主色 = 面积最大的 k-means 簇（视为底）
        crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
        _, labels, centers = cv2.kmeans(flat, 4, None, crit, 3, cv2.KMEANS_PP_CENTERS)
        labels = labels.ravel()
        counts = np.bincount(labels, minlength=len(centers))
        bg = centers[int(np.argmax(counts))]
    except Exception:                                # noqa: BLE001
        bg = flat.mean(axis=0)
    dist = np.linalg.norm(flat - bg, axis=1)
    mask = (dist > 48).astype("uint8").reshape(img.shape[:2])
    ratio = float(mask.mean())
    col = flat[mask.ravel() > 0].mean(axis=0) if ratio >= 0.02 else bg
    base = _cn_color(col)
    if ratio < 0.02:
        return base + "纯色底"
    if ratio > 0.90:
        return base + "满印"
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    sh = _cn_shape(mask)
    # 条纹类：若前景比底色更「灰」，说明 k-means 把条纹本身当成了底色簇（大面积条纹常见），
    # 此时真正的图案色在底色上 → 取更饱和的一侧，避免命名成「白条纹」这类无彩色。（2026-10-13）
    if sh.endswith("条纹") and _sat(col) < _sat(bg):
        base = _cn_color(bg)
    return base + sh


@app.post("/api/tasks/autoname")
async def autoname(taken: str = Form("[]"), images: list[UploadFile] = File(...)):
    """本地 CV 自动命名（主色 + 形态）。不调 AI、不计费、不建任务。
    `taken` 传本批已占用的名字，命中则自动加 -2 / -3 后缀。"""
    if not images:
        raise HTTPException(400, "需要 1 张原图")
    f = images[0]
    ext = os.path.splitext(f.filename or "")[1].lower() or ".jpg"
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        raise HTTPException(400, "仅支持 jpg/png/webp")
    tmp = os.path.join(DATA, "_autoname_%s%s" % (uuid.uuid4().hex[:8], ext))
    try:
        with open(tmp, "wb") as out:
            out.write(await f.read())
        try:
            nm = _autoname_one(tmp)
        except Exception:                            # noqa: BLE001
            nm = "图案"
        try:
            used = [str(x) for x in (json.loads(taken or "[]") or [])]
        except Exception:                            # noqa: BLE001
            used = []
        if nm in used:
            k = 2
            while ("%s-%d" % (nm, k)) in used:
                k += 1
            nm = "%s-%d" % (nm, k)
        return {"name": nm}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


@app.get("/api/tasks/batch/{bid}")
def get_batch(bid: str, request: Request):
    """取一个批次的全部成员（按序号），供批量看板轮询。"""
    uid = request.scope.get("st_uid")
    items = []
    for tid, box in list(_tasks.items()):
        st = box["state"]
        if st.get("batch_id") != bid or not _own(st, uid):
            continue
        o = _public(st, tid)
        o["fver"] = _vmap(tid)
        items.append(o)
    if not items:
        raise HTTPException(404, "批次不存在")
    items.sort(key=lambda s: (s.get("batch_index") or 0))
    return {"batch_id": bid, "count": len(items), "items": items}


@app.post("/api/tasks/batch/zip")
def batch_zip(body: dict, request: Request):
    """把批次内的成品打包成一个 ZIP（每张一个文件夹）。
    分层 PSD 是付费下载（3 积分/张，首次）——打包同样按「本批里还没付过的张数」计费，
    余额不足回 402。PNG 成品已含在抠图费里，不再单独收费。"""
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "ids 必须是数组")
    want_psd = body.get("psd", True) not in (False, 0, "0", "false", "no")
    uid = request.scope.get("st_uid")
    picked = []
    for tid in ids[:BATCH_MAX]:
        box = _tasks.get(str(tid))
        if not box or not _own(box["state"], uid):
            continue
        st = box["state"]
        d = os.path.join(DATA, str(tid))
        if not os.path.isdir(d):
            continue
        picked.append((str(tid), st, d))
    if not picked:
        raise HTTPException(400, "没有可打包的任务")

    psds = []
    if want_psd:
        for tid, st, d in picked:
            p = st.get("psd")
            if p and os.path.isfile(os.path.join(d, p)):
                psds.append(tid)
    if psds and not ACC.is_admin(uid):
        unpaid = [t for t in psds if not ACC.psd_already_paid(t, uid)]
        amt = 3 * len(unpaid)
        if amt:
            ok, _bal = ACC.charge(uid, amt, "批量下载分层PSD x%d" % len(unpaid), "")
            if not ok:
                return JSONResponse({"ok": False, "need": amt,
                                     "error": "积分不足：本批有 %d 个分层 PSD 未下载过，"
                                              "打包需 %d 积分" % (len(unpaid), amt)}, status_code=402)
            for t in unpaid:
                ACC.mark_psd_paid(t, uid)

    buf = io.BytesIO()
    n_png = n_psd = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for tid, st, d in picked:
            idx = st.get("batch_index") or 0
            folder = "%02d_%s" % (idx, _safe_entry(st.get("name") or tid, tid))
            for rel in ("图案_2048.png", st.get("selected"), st.get("psd"), "_预览.png"):
                if not rel or not isinstance(rel, str):
                    continue
                fp = os.path.join(d, rel)
                if not os.path.isfile(fp):
                    continue
                if rel.lower().endswith(".psd") and not want_psd:
                    continue
                try:
                    z.write(fp, folder + "/" + _safe_entry(os.path.basename(rel)))
                    if rel.lower().endswith(".psd"):
                        n_psd += 1
                    elif rel == "图案_2048.png":
                        n_png += 1
                except OSError:
                    continue
    data = buf.getvalue()
    if not n_png and not n_psd:
        # 一个条目都没装上时不要回 200 —— 前端会静默下到一个 0 字节的空 zip，用户只看见
        # 「下载了但打不开」。这里明确回 400 + 可读原因（前端 alert 直接显示 detail）。
        n_done = sum(1 for _t, s, _d in picked if s.get("stage") in ("done", "psd_running"))
        raise HTTPException(400, "这批还没有可打包的成品：%d 张里 %d 张已出图。"
                                 "等卡片显示「已完成」（或先点「一键全部生成 PSD」）再打包。"
                                 % (len(picked), n_done))
    fname = "%s_批量图案_%d张.zip" % (
        time.strftime("%Y%m%d"), len(picked))
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''%s"
                             % quote(fname),
                             "Content-Length": str(len(data)),
                             "X-Zip-Summary": "psd=%d;png=%d" % (n_psd, n_png)})


@app.get("/api/tasks")
def list_tasks(request: Request):
    # 防御：个别 task.json 缺 created（历史/测试残留）时不要让整个列表 500
    uid = request.scope.get("st_uid")
    items = sorted(_tasks.items(),
                   key=lambda kv: kv[1]["state"].get("created") or "", reverse=True)
    # 归属隔离：只返回自己的任务（admin 组全量可见）
    return [_public(box["state"], tid) for tid, box in items
            if _own(box["state"], uid)]

@app.get("/api/tasks/{tid}")
def get_task(tid: str, request: Request):
    st = _own_task(tid, request)
    out = _public(st, tid)
    out["fver"] = _vmap(tid)      # 产物 mtime 表 → 前端拼 ?v= 破缓存（配合长缓存）
    return out

def _drop_task(tid):
    """把任务从内存表移除 + 删掉磁盘目录（幂等）。返回是否真的存在过。"""
    tid = str(tid)
    existed = tid in _tasks
    _tasks.pop(tid, None)
    _cdn_mt.pop(tid, None)
    d = os.path.join(DATA, tid)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
    return existed

@app.post("/api/tasks/delete")
def delete_tasks(body: dict, request: Request):
    """批量删除任务记录（含磁盘产物 + COS 上 pattern/<tid>/）。前端「历史任务」多选后调用。
    归属隔离：只删自己的任务，别人的 tid 静默跳过（不暴露存在性）。"""
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise HTTPException(400, "ids 必须是数组")
    uid = request.scope.get("st_uid")
    done = []
    for tid in ids[:500]:
        tid = str(tid)
        box = _tasks.get(tid)
        if not box or not _own(box["state"], uid):
            continue
        if _drop_task(tid):          # 只把「真的存在过并已删掉」的算进去
            done.append(tid)
    if done:
        threading.Thread(target=_cos_delete_many, args=(done,), daemon=True).start()
    return {"ok": True, "deleted": len(done), "ids": done}

@app.delete("/api/tasks/{tid}")
def delete_task(tid: str, request: Request):
    """删除单个任务记录（含磁盘产物 + COS）。幂等：已不存在也返回 ok（existed=False）。"""
    _own_task(tid, request)          # 归属校验：越权 404
    existed = _drop_task(tid)
    if existed:
        threading.Thread(target=_cos_delete_many, args=([str(tid)],), daemon=True).start()
    return {"ok": True, "id": tid, "existed": existed}

@app.post("/api/tasks/{tid}/parts")
def submit_parts(tid: str, body: dict, request: Request):
    """前端确认/微调部件框后提交，进入分区提取。"""
    st = _own_task(tid, request)
    if st["stage"] != "parts_review":
        raise HTTPException(400, "当前状态不可提交部件框: " + st["stage"])
    parts = body.get("parts")
    if not isinstance(parts, list):
        raise HTTPException(400, "parts 必须是数组")
    clean = []
    for p in parts[:6]:
        if not isinstance(p, dict):
            continue
        if p.get("shape") == "mask":
            # 涂抹抓取：异形/多边形支架（笔迹掩码即抓取范围）
            strokes = []
            for s in (p.get("strokes") or [])[:600]:
                if not isinstance(s, dict):
                    continue
                pts = []
                for v in (s.get("pts") or [])[:8000]:
                    if not isinstance(v, (list, tuple)) or len(v) < 2:
                        continue
                    try:
                        pts.append([int(round(float(v[0]))), int(round(float(v[1])))])
                    except (TypeError, ValueError):
                        continue
                if not pts:
                    continue
                try:
                    sw = int(round(float(s.get("w") or 0)))
                except (TypeError, ValueError):
                    sw = 0
                strokes.append({"w": max(2, min(600, sw or 40)), "pts": pts})
            if not strokes:
                continue
            want = st.get("parts_want") or P.PART_STAND
            clean.append({"kind": str(p.get("kind") or want), "shape": "mask",
                          "strokes": strokes,
                          "detected": False, "conf": float(p.get("conf", 1.0))})
        elif p.get("shape") == "circle":
            c = p.get("center") or []
            r = p.get("r")
            if len(c) != 2 or not r:
                continue
            clean.append({"kind": str(p.get("kind") or P.PART_STAND), "shape": "circle",
                          "center": [int(c[0]), int(c[1])], "r": int(r),
                          "r_inner": int(p.get("r_inner") or 0),
                          "detected": bool(p.get("detected", False)), "conf": float(p.get("conf", 0.8))})
        else:
            b = p.get("box") or []
            if len(b) != 4:
                continue
            clean.append({"kind": str(p.get("kind") or P.PART_LENS), "shape": "rect",
                          "box": [int(v) for v in b],
                          "detected": bool(p.get("detected", False)), "conf": float(p.get("conf", 0.8))})
    _run_extract(tid, st, clean)
    return {"ok": True, "n": len(clean)}

@app.post("/api/tasks/{tid}/select")
def select(tid: str, body: dict, request: Request):
    st = _own_task(tid, request)
    if st["stage"] != "selecting":
        raise HTTPException(400, "当前状态不可选版: " + st["stage"])
    version = body.get("version")
    if st.get("parts_meta"):
        # 部件任务只有两个"虚拟版本"：parts=原图直抠（**兜底版**，重绘失败时才展示）／redraw=AI 完全重绘版
        # （2026-10-11 拍板：锁定版不作为候选，正常路径下前端只渲染 redraw 一张卡）
        if version not in ("parts", "redraw"):
            raise HTTPException(400, "版本不存在")
        if version == "redraw" and not st.get("redraw_ok"):
            raise HTTPException(400, "AI 重绘版尚未生成，请先点「重新生成 AI 重绘版」")
    elif version not in st.get("versions", []):
        raise HTTPException(400, "版本不存在")
    strategy = body.get("strategy", "auto")
    if strategy not in ("auto", "white_bg", "full_bleed"):
        strategy = "auto"
    layer_mode = body.get("layer_mode", "merge2")
    if layer_mode not in ("merge2", "merge3", "fine", "connected"):
        layer_mode = "merge2"
    _run_psd(tid, st, version, strategy, layer_mode)
    return {"ok": True}

def _start_parts_redraw(tid, st, archive=True):
    """【部件】重跑 AI 完全重绘。archive=True 时旧重绘版归档为 `_vN` 历史版本。
    失败不阻断：仍保留锁定版，原因写进 redraw_msg，前端给「重新生成」按钮。"""
    def work():
        d = os.path.join(DATA, tid)
        try:
            meta = st["parts_meta"]
            st["stage"] = "gen_running"; st["step"] = "准备 AI 完全重绘…"; _save(tid, st)
            ren = {}
            if archive:
                for it in meta.get("parts", []):
                    rn = it.get("redraw")
                    if rn and not _REV_RE.search(str(rn)):
                        a = _archive(tid, "parts/" + rn)
                        if a:
                            ren["parts/" + rn] = a
            if ren:
                st["versions"] = [ren.get(v, v) for v in st.get("versions", [])]
                _save(tid, st)
            n = _parts_redraw(tid, st, os.path.join(d, "parts"), meta)
            st["parts_meta"] = meta
            if n:
                st["redraw_ok"] = True
                # 锁定版不再作候选（仅后台兜底 + PSD 兜底源图）→ 只留
                # ① 新的 AI 完全重绘版 ② 已归档的 `_vN` 历史版本件（含历史重绘版）。
                hist = [v for v in st.get("versions", [])
                        if _REV_RE.search(os.path.basename(str(v)))]
                extra = [it["redraw"] for it in meta["parts"] if it.get("redraw")]
                st["versions"] = ["parts/" + x for x in extra] + hist
            st["stage"] = "selecting"; st["step"] = "等待确认"
            _save(tid, st)
            _pub_safe(tid, st)
        except Exception as e:
            st["redraw_msg"] = "AI 完全重绘失败：%s" % str(e)[:200]
            st["stage"] = "selecting"; st["step"] = "等待确认"; _save(tid, st)
    threading.Thread(target=work, daemon=True).start()


def _regen_redraw(tid, st):
    """【手机壳 / 摄像头】只重跑「AI 满幅重绘」这一档（复用已有干净壳，省一次生图）。

    旧重绘版归档为 `_vN` 历史版本；新一版仍写正式文件名 → 下游分层流程零改动。
    """
    def work():
        d = os.path.join(DATA, tid)
        try:
            vers = [str(v).replace("\\", "/") for v in (st.get("versions") or [])]
            rest = []
            for rel in vers:
                bn = os.path.basename(rel)
                if bn in ("图案_重绘版.png", "摄像头图案_区域比例.png"):
                    rest.append(_archive(tid, rel) or rel)      # 旧那一版 → 历史
                elif bn == "图案_锁定版.png":
                    continue                                    # 锁定版不再作候选（仅后台基准），不入 versions
                else:
                    rest.append(rel)                            # 已有历史件，原样留
            key = P.load_key()
            clean = st.get("clean") or "干净壳.png"
            cp = os.path.join(d, clean)
            if not os.path.isfile(cp):
                raise RuntimeError("找不到干净产品图 %s，请改用「整单重跑」" % clean)
            rsize = st.get("redraw_req")
            if not rsize:
                cv = ((st.get("extract_meta") or {}).get("canvas") or [3, 4])
                rsize = _redraw_size(st, cv[0], cv[1])
            red_fn = "图案_重绘版.png"
            st["stage"] = "gen_running"
            st["step"] = "AI 满幅重绘（版式死锁，约 1-2 分钟）"; _save(tid, st)
            P.redraw_fullbleed(cp, os.path.join(d, red_fn), key, rsize,
                               full_bg=bool(st.get("full_bg")),
                               ratio=st.get("ratio"),
                               long_edge=st.get("long_edge"))
            try:
                from PIL import Image as _I
                with _I.open(os.path.join(d, red_fn)) as _im:
                    st["redraw_size"] = "%dx%d" % _im.size
            except Exception:
                pass
            if st.get("region_crop"):
                de = st.get("down_ext")
                de = P.LENS_DOWN_EXT if de is None else de
                st["step"] = "区域比例抠选（横向全宽 + 向下延伸 %.2f×框高）" % de; _save(tid, st)
                region_fn = "摄像头图案_区域比例.png"
                _, rc_meta = P.lens_region_crop(cp, os.path.join(d, red_fn),
                                                parts=st.get("parts_detected"), down_ext=de,
                                                out_png=os.path.join(d, region_fn))
                st["region_meta"] = rc_meta
                st["versions"] = [region_fn] + rest
            else:
                lock_fn = "图案_锁定版.png"
                # 用户拍板：只出重绘版 —— 锁定版仅作下面「版式校验」的基准，不进候选版本
                st["versions"] = [red_fn] + [x for x in rest
                                             if os.path.basename(x) != lock_fn]
                st["step"] = "版式校验（重绘版 vs 锁定版）"; _save(tid, st)
                try:
                    st["layout_check"] = P.layout_deviation(
                        os.path.join(d, lock_fn), os.path.join(d, red_fn),
                        ref_label="锁定版（后台基准 · 不参与选版）")
                except Exception:
                    pass
            st["redraw_msg"] = ""
            st["stage"] = "selecting"; st["step"] = "已重新生成，等待选版"; _save(tid, st)
            _pub_safe(tid, st)
        except Exception as e:
            st["redraw_msg"] = "重新生成失败：%s" % str(e)[:200]
            st["stage"] = "selecting"; st["step"] = "等待选版"; _save(tid, st)
    threading.Thread(target=work, daemon=True).start()


@app.post("/api/tasks/{tid}/redraw")
def retry_redraw(tid: str, request: Request):
    """【支架】AI 完全重绘的「重新生成」入口（首次生成失败、或想重画一版时用）。
    后台跑，stage 置 gen_running → 前端轮询切到进度页；跑完回 selecting 重新展示两版。"""
    st = _own_task(tid, request)
    if st["stage"] != "selecting" or not st.get("parts_meta"):
        raise HTTPException(400, "当前状态不可重绘: " + st["stage"])
    _start_parts_redraw(tid, st, archive=True)
    return {"ok": True}


@app.post("/api/tasks/{tid}/regenerate")
def regenerate(tid: str, request: Request, body: dict = None):
    """【重新生成】对不满意的产物重跑一版；旧结果自动归档为 `_vN` 历史版本，版本区可对比。
    target: full=整单重跑（重新出干净壳 + 锁定版 + 重绘版）／redraw=只重跑 AI 重绘版／parts=部件重绘。"""
    st = _own_task(tid, request)
    # error 也允许重跑：批量提取里某一张失败时，不该逼用户把整批重新上传一遍
    # （原图还在任务目录里，_run_gen 直接拿 st["inputs"] 重跑即可）。
    if st["stage"] not in ("selecting", "done", "error"):
        raise HTTPException(400, "当前状态不可重新生成: " + st["stage"])
    target = str((body or {}).get("target") or "full").lower()
    if target not in ("full", "redraw", "parts"):
        raise HTTPException(400, "target 仅支持 full / redraw / parts")
    if st.get("parts_meta"):
        _start_parts_redraw(tid, st, archive=True)
        return {"ok": True, "target": "parts"}
    if target == "full":
        # 批量成员重做同样排进闸门：一次点 5 个「重做」不至于 5 个线程齐打 AI 网关
        _run_gen(tid, st, archive=True, gate=bool(st.get("batch_id")))
    else:
        _regen_redraw(tid, st)
    return {"ok": True, "target": target}

@app.post("/api/tasks/{tid}/recrop")
def recrop(tid: str, body: dict, request: Request):
    """【lens 区域比例抠选】按新的「向下延伸系数」重新抠选（纯本地，不调 AI，秒级）。"""
    st = _own_task(tid, request)
    if not st.get("region_crop"):
        raise HTTPException(400, "当前任务不是区域比例抠选任务")
    try:
        de = float(body.get("down_ext", P.LENS_DOWN_EXT))
    except (TypeError, ValueError):
        de = P.LENS_DOWN_EXT
    de = max(0.0, min(2.0, de))
    d = os.path.join(DATA, tid)
    clean = st.get("clean") or "干净壳.png"
    red = "图案_重绘版.png"
    region_fn = "摄像头图案_区域比例.png"
    try:
        _, rc_meta = P.lens_region_crop(
            os.path.join(d, clean), os.path.join(d, red),
            parts=st.get("parts_detected"), down_ext=de,
            out_png=os.path.join(d, region_fn))
    except Exception as ex:
        raise HTTPException(400, "重新抠选失败：%s" % str(ex)[:300])
    st["region_meta"] = rc_meta
    st["down_ext"] = de
    st["versions"] = [region_fn]
    _save(tid, st)
    return {"ok": True, "down_ext": de, "meta": rc_meta}

@app.get("/api/tasks/{tid}/file/{fn:path}")
def task_file(tid: str, fn: str, request: Request):
    """取任务产物文件（带 ETag / Cache-Control，命中 If-None-Match 返回 304）。"""
    _own_task(tid, request)
    fp = _resolve(os.path.join(DATA, tid), fn)
    if not fp:
        raise HTTPException(404, "文件不存在")
    return _send_file(fp, request)

@app.get("/api/tasks/{tid}/thumb/{fn:path}")
def task_thumb(tid: str, fn: str, request: Request, w: int = THUMB_W):
    """取产物缩略图（宽 w 的 WebP，磁盘缓存）。

    ⚠️「打开历史任务图片很慢」的根因：首屏一次拉 6MB 全尺寸 PNG（干净壳 1.6M + 锁定版
    0.95M + 重绘版 3.5M + 预览 4.5M）。缩略图单张 ~30KB → 首屏从 6MB 降到 ~200KB，
    点「查看原图」才加载原尺寸。
    """
    _own_task(tid, request)
    d = os.path.join(DATA, tid)
    fp = _resolve(d, fn)
    if not fp:
        raise HTTPException(404, "文件不存在")
    if os.path.splitext(fp)[1].lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        return _send_file(fp, request)                  # 非位图（PSD 等）直接给原文件
    w = 160 if w < 160 else (1280 if w > 1280 else int(w))
    tp = _thumb_path(tid, os.path.relpath(fp, d).replace("\\", "/"), w)
    try:
        if (not os.path.isfile(tp)) or os.path.getmtime(tp) < os.path.getmtime(fp):
            if not _make_thumb(fp, tp, w):
                return _send_file(fp, request)
    except Exception:                                   # noqa: BLE001
        return _send_file(fp, request)
    return _send_file(tp, request, media_type="image/webp")

@app.get("/api/tasks/{tid}/raw/{fn:path}")
def task_raw(tid: str, fn: str, request: Request):
    """同 task_file（老前端/老链接兼容别名）。"""
    return task_file(tid, fn, request)

@app.get("/", response_class=HTMLResponse)
def index():
    # 前端页不缓存：改完前端刷新即生效（否则浏览器一直拿旧的 index.html）
    return HTMLResponse(
        open(os.path.join(BASE, "static", "index.html"), encoding="utf-8").read(),
        headers={"Cache-Control": "no-cache, must-revalidate"})

# 左侧总导航改造（方案 A）：根路径 = 外壳页（左总导航 + 右 iframe），
# 两个功能页作为 iframe 内容拆到 /pt/ 与 /wm/ 下。
#
# 板块页 URL 规范化铁律（2026-10-13 修）：
#   同一份 HTML 若同时挂在 "/x" 与 "/x/" 下，页内相对引用（如 ../accounts/static/ui.css）
#   必然只能对其中一种形式成立 —— 无尾斜杠时浏览器把 "/x" 当文件，目录回退一级到站点根，
#   "../accounts/static/ui.css" 就变成 "/accounts/static/ui.css"（线上落到 chule.top 根下
#   另一个应用 → 401），表现为「整页丢样式」且 CDN/日志里出现无前缀资源请求。
#   故：无尾斜杠一律 302 相对跳转到带尾斜杠形式，全站每个板块只保留一个规范 URL。
#   跳转必须用相对 Location（后端看不到线上 /pattern 前缀），浏览器按「替换最后一段」解析：
#   /pattern/pt + "pt/" → /pattern/pt/  ✓
def _board_trailing_slash(name: str):
    return RedirectResponse(url=name + "/", status_code=302)


@app.get("/pt", include_in_schema=False)
def pt_noslash():
    return _board_trailing_slash("pt")


@app.get("/pt/", response_class=HTMLResponse, include_in_schema=False)
def pt_index(request: Request):
    return _html_page(request, "pt.html")

# 静态交付页：deliver/ 目录（三任务交付对照 + 分层 PSD 下载）
# 线上访问：https://chule.top/pattern/deliver/
DELIVER = os.path.join(BASE, "deliver")
if os.path.isdir(DELIVER):
    from fastapi.staticfiles import StaticFiles
    app.mount("/deliver", StaticFiles(directory=DELIVER, html=True), name="deliver")

# 第二个版块：批量去水印工作台（FastAPI 子应用，挂载在 /wm 路径下）
# 线上访问：https://chule.top/pattern/wm/   （与图案提取共用同一服务、同一端口）
# 子应用自带产物目录 wm/data/（与图案提取 data/<tid>/ 物理隔离）与 72 小时清理线程；
# 挂载失败只影响去水印版块，不让主服务起不来。
try:
    from wm.app import app as wm_app
    app.mount("/wm", wm_app, name="wm")
except Exception as _e_wm:        # noqa: BLE001
    print("[wm] 批量去水印子应用挂载失败：%s" % _e_wm)

# 第三个版块：多图编辑工作台（duotu 整体搬入，Flask 应用经 WSGI 挂载在 /duotu 下）
# 线上访问：https://chule.top/pattern/duotu/parts.html   （与主站同服务同端口同域名）
# 代码在 app/duotu/（server.py + engine/ + static/），前后端 URL 已全部改为相对路径；
# 产物目录 duotu/data/ 与其他版块物理隔离。挂载失败只影响本版块，不拖垮主服务。
try:
    import importlib.util as _ilu
    from starlette.middleware.wsgi import WSGIMiddleware as _WSGI
    _dt_spec = _ilu.spec_from_file_location(
        "duotu_server", os.path.join(BASE, "duotu", "server.py"))
    _dt_mod = _ilu.module_from_spec(_dt_spec)
    _dt_spec.loader.exec_module(_dt_mod)
    app.mount("/duotu", _WSGI(_dt_mod.app), name="duotu")
except Exception as _e_dt:        # noqa: BLE001
    print("[duotu] 多图编辑子应用挂载失败：%s" % _e_dt)

# 第四个版块：高清修复工作台（FastAPI 子应用，挂载在 /hd 路径下，2026-10-13 雪碧需求）
# 线上访问：https://chule.top/pattern/hd/   —— gpt-image-2 1:1 重绘高清 + 亮度拆两层 PSD。
# 产物目录 hd/data/（records/u<uid>/ + output/u<uid>/）物理隔离，72h TTL；挂载失败只影响本版块。
try:
    from hd.app import app as hd_app
    app.mount("/hd", hd_app, name="hd")
except Exception as _e_hd:        # noqa: BLE001
    print("[hd] 高清修复子应用挂载失败：%s" % _e_hd)

# 第六个版块：详情页生成（FastAPI 子应用，挂载在 /dtl 路径下，2026-11-18 雪碧需求）
# 线上访问：https://chule.top/pattern/dtl/
# 复刻 dst.yemanai.cn「电商详情页 → image2详情页」：四步向导（上传信息 → 分析产品 →
# 提示词 → 生成套图），提示词从原站前端 bundle 提原文 1:1 移植（见 dtl/prompts.py）。
# AI 调用走本站站点池（pipeline.gen_image / chat_vision），计费 3 积分/张，
# 分析与提示词不收费。产物目录 dtl/data/ 物理隔离，72h TTL；挂载失败只影响本版块。
try:
    from dtl.app import app as dtl_app
    app.mount("/dtl", dtl_app, name="dtl")
except Exception as _e_dtl:       # noqa: BLE001
    print("[dtl] 详情页生成子应用挂载失败：%s" % _e_dtl)

# 第七个版块：AI 生图（FastAPI 子应用，挂载在 /aigc 路径下，2026-11-18 第二轮）
# 线上访问：https://chule.top/pattern/aigc/
# 复刻 dst.yemanai.cn「AI生图」：单张生图（≤6 张参考图，@图片N 融合 → 出 1 张）
# 与批量生图（≤10 张，每张各自出图 → 出 N 张）同页 tab 切换。
# 该版块原站**没有 AI 生成提示词**，只有执行层包装模板（画布锁定 + 用户原文 + 比例清晰度），
# 模板从原站 bundle 提原文照抄（见 aigc/app.py::wrap_prompt）。
# AI 调用走本站站点池（pipeline.gen_image，支持 models= 指定优先模型），计费 3 积分/张。
# 产物目录 aigc/data/ 物理隔离，72h TTL；挂载失败只影响本版块。
try:
    from aigc.app import app as aigc_app
    app.mount("/aigc", aigc_app, name="aigc")
except Exception as _e_aigc:      # noqa: BLE001
    print("[aigc] AI 生图子应用挂载失败：%s" % _e_aigc)

# 第八个版块：详情页复刻（FastAPI 子应用，挂载在 /fk 路径下，2026-11-18 雪碧需求）
# 线上访问：https://chule.top/pattern/fk/
# 做什么：上传别人的详情页（按屏截图或整张长图，长图可服务端等分切屏）+ 我的产品图
#        → AI 逐屏拆解版式/配色/字号层级 → 代码拼出逐屏复刻提示词 → 一键出整套同版式详情页。
# GitHub 调研结论落地：逐屏拆、逐屏出、最后按原顺序拼回（linkpix detail-page-clone /
# QIYU ecommerce-exact-replication 的共同配方），一次性出长图必然糊。
# AI 调用走本站站点池（pipeline.gen_image / chat_vision），出图 3 积分/张，
# 拆解与提示词不收费。产物目录 fk/data/ 物理隔离，72h TTL；挂载失败只影响本版块。
try:
    from fk.app import app as fk_app
    app.mount("/fk", fk_app, name="fk")
except Exception as _e_fk:       # noqa: BLE001
    print("[fk] 详情页复刻子应用挂载失败：%s" % _e_fk)

# 商品替换（2026-11-19）：图1 场景 + 图2 替换物品，两图融合。
# 只有「出图」和「单张重出」收费；上传/轮询/停止/退分/ZIP 免费。挂载失败只影响本版块。
try:
    from swap.app import app as swap_app
    app.mount("/swap", swap_app, name="swap")
except Exception as _e_swap:     # noqa: BLE001
    print("[swap] 商品替换子应用挂载失败：%s" % _e_swap)

# 第五个版块：系统设置（生图站点池 + duotu 推理/图文模型配置）
@app.get("/settings", include_in_schema=False)
def settings_noslash():
    return _board_trailing_slash("settings")


@app.get("/settings/", response_class=HTMLResponse, include_in_schema=False)
def settings_index(request: Request):
    return _html_page(request, "settings.html")


# 生图站点池（2026-10-15 雪碧需求：把两个站点模型加进后台设置 + 同模型跨站点轮询）。
# 唯一真源 app/ai_sites.py：pt/hd/wm 三个生图板块都按它排队，改这里即全站生效、免重启。
# GET 返回脱敏配置（key 只回掩码，绝不回明文）；POST 保存（掩码或空 key 自动保留旧值）。
# 仅主账号可读写 —— 已登记进下面的 _ADMIN_API_RULES（(None, "/api/ai_sites")）。
@app.get("/api/ai_sites", include_in_schema=False)
def ai_sites_get():
    from fastapi.responses import JSONResponse as _JR
    try:
        import ai_sites as _AS
        return _JR({"ok": True, "conf": _AS.public_conf()})
    except Exception as _e:                      # noqa: BLE001
        return _JR({"ok": False, "error": "读取站点池失败：%s" % _e}, status_code=500)


@app.post("/api/ai_sites", include_in_schema=False)
async def ai_sites_post(request: Request):
    from fastapi.responses import JSONResponse as _JR
    try:
        import ai_sites as _AS
        body = await request.json()
        conf = body.get("conf") if isinstance(body, dict) and "conf" in body else body
        _AS.save_conf(conf or {})
        return _JR({"ok": True, "conf": _AS.public_conf()})
    except Exception as _e:                      # noqa: BLE001
        return _JR({"ok": False, "error": "保存站点池失败：%s" % _e}, status_code=400)

# 素材库板块（2026-10-12 雪碧需求）：聚合三个功能板块的生成记录，
# 每个账号只能看到自己的记录（admin 全量可见，条目带 owner 标记），
# 可从素材库一键跳回对应板块继续任务。
@app.get("/library", include_in_schema=False)
def library_noslash():
    return _board_trailing_slash("library")


@app.get("/library/", response_class=HTMLResponse, include_in_schema=False)
def library_index(request: Request):
    return _html_page(request, "library.html")


def _dt_job_thumb(job):
    """duotu 生图记录缩略图（与 duotu/server.py _job_thumb 同逻辑的轻量副本：
    直接读磁盘 job JSON，不经过 HTTP；成品优先，没出图用主图）。"""
    r = job.get("result") or {}
    fin = r.get("final") or ((r.get("results") or [{}])[0] if r.get("results") else None)
    if not fin:
        for im in (r.get("images") or job.get("images") or []):
            ri = im.get("result") or {}
            f = ri.get("final") or ((ri.get("results") or [{}])[0] if ri.get("results") else None)
            if f and f.get("preview"):
                return f["preview"]
    if fin and fin.get("preview"):
        return fin["preview"]
    imgs = (job.get("payload") or {}).get("images") or []
    if imgs and isinstance(imgs[0], dict) and imgs[0].get("base_url"):
        return imgs[0]["base_url"]
    return (job.get("payload") or {}).get("base_url") or ""


# ---- 素材库提速共用件（2026-10-15 雪碧：「对比图打开好慢，还有哪里没接上，一起接上」）----
# 铁律：服务器出口实测 ~1Mbps，COS→客户端 ~2.2MB/s。凡图走服务器中转必慢。
# 素材库原来只有 pt 一个板块真接了 COS，wm/duotu/hd 三板块的对比图与缩略图全走站内路由
# 回源（`wm/api/out`、`duotu/api/file/...`、`hd/api/tasks/.../thumb`），这就是「慢」的根因。
# 下面两个工具把「已上传过 COS 的产物」就地升级成直链，缺 sidecar / 未配置则原样返回。
_DT_COS_MISS = object()
_DT_COS_CACHE = {}          # (kind, fn) → url 或 _DT_COS_MISS（负缓存，避免每张图都 stat）


def _cos_thumb(u, w=640):
    """COS 直链 → 追加 imageMogr2 让 CDN 侧出缩略图（素材库网格 / 对比格用）。
    非 COS 直链 / 非图片后缀 / 已带查询串 → 原样返回（宁可拉原图，也不拼出坏 URL）。"""
    if not u or "?" in u or not u.startswith("http"):
        return u
    low = u.lower().split("#", 1)[0]
    if not low.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")):
        return u
    if ".myqcloud.com" not in low:
        return u
    return u + "?imageMogr2/thumbnail/{0}x/format/webp".format(int(w))


def _dt_cos_of(kind, fn):
    """duotu 产物（kind=`outputs`/`uploads`，fn=文件名）→ COS 直链；无 `.cosok` → ""。

    新记录里 `fin["url"]/["preview"]` 已是 COS 直链（`duotu/server.py::_cos_put` 同步上传），
    但历史记录、以及「当时上传失败、之后补传成功只留 sidecar」的，存的还是
    `api/file/outputs/<fn>` 站内路由 —— 走服务器出口，对比图要等几秒。这里读 sidecar 升级。
    """
    if not fn:
        return ""
    ck = (kind, fn)
    v = _DT_COS_CACHE.get(ck)
    if v is _DT_COS_MISS:
        return ""
    if v is not None:
        return v
    url = ""
    try:
        base = os.path.join(BASE, "duotu", "data", kind)
        if os.path.isfile(os.path.join(base, fn + ".cosok")):
            from duotu.engine import cosstore as _CS       # 延迟导入：纯标准库，无副作用
            if _CS.ok():
                url = _CS.url_for(kind, fn) or ""
    except Exception:                                # noqa: BLE001
        url = ""
    _DT_COS_CACHE[ck] = url or _DT_COS_MISS
    return url


def _dt_url(u):
    """duotu 产物路径 → 素材库可用 URL：COS 直链原样；
    `api/file/<kind>/<fn>` 有 `.cosok` → 升级 COS 直链；其余 api/ 相对路径补 duotu/ 前缀。"""
    if not u:
        return ""
    if u.startswith("http") or u.startswith("data:"):
        return u
    if u.startswith("api/file/"):
        _kind, _, _fn = u[len("api/file/"):].partition("/")
        if _kind in ("outputs", "uploads") and _fn:
            _cu = _dt_cos_of(_kind, _fn)
            if _cu:
                return _cu
    if u.startswith("api/"):
        return "duotu/" + u
    return u


def _dt_thumb_url(u):
    """duotu 显示用 URL → 缩略：COS 直链交给 CDN 出缩略（原来原样返回 = 网格拉全尺寸，
    最慢的一处）；站内相对路由仍加 ?w=640 走本地缩略端点。"""
    if not u or u.startswith("data:"):
        return u
    if u.startswith("http"):
        return _cos_thumb(u, 640)
    return u + ("&" if "?" in u else "?") + "w=640"


def _dt_job_pairs(job):
    """duotu 记录的「原图⇄成品」对：单张取 src_file ↔ result.final；
    组任务取 src_files[k] ↔ result.images[k].result.final。老记录无原图 → src 空。
    out=缩略优先（preview 或 ?w=640），out_full=全尺寸 url。"""
    res = job.get("result") or {}
    pairs = []
    imgs = res.get("images") or []
    if imgs:                                       # 组任务
        srcs_l = job.get("src_files") or []
        for i, im in enumerate(imgs):
            ri = im.get("result") or {}
            fin = ri.get("final") or ((ri.get("results") or [{}])[0] if ri.get("results") else None) or {}
            full_u = _dt_url(fin.get("url") or "")
            prev_u = _dt_url(fin.get("preview") or "")
            outp = prev_u or _dt_thumb_url(full_u)
            sf = srcs_l[i] if i < len(srcs_l) else ""
            srcp = _dt_url(sf if (sf and (sf.startswith("http") or sf.startswith("data:")))
                           else ("api/file/outputs/" + sf if sf else ""))
            if outp:
                pairs.append(dict(src=_dt_thumb_url(srcp), out=outp,
                                  out_full=full_u or outp,
                                  label="图{0}".format(i + 1)))
        return pairs
    fin = res.get("final") or ((res.get("results") or [{}])[0] if res.get("results") else None) or {}
    full_u = _dt_url(fin.get("url") or "")
    prev_u = _dt_url(fin.get("preview") or "")
    outp = prev_u or _dt_thumb_url(full_u)
    if not outp:
        return pairs
    sf = job.get("src_file") or ""
    srcp = _dt_url(sf if (sf and (sf.startswith("http") or sf.startswith("data:")))
                   else ("api/file/outputs/" + sf if sf else ""))
    pairs.append(dict(src=_dt_thumb_url(srcp), out=outp,
                      out_full=full_u or outp, label="成品"))
    return pairs


# ---- 素材库：8 板块统一归集（2026-11-19 STEP1）----
# 原来只接了 pt / wm / duotu / hd 四个，真正出图最多的 aigc / dtl / fk / swap 四个板块
# 的记录根本进不来（各模块结构还不一样：items[] vs pages[]）。这里给每个模块一个 adapter，
# 统一吐同一种 item 形状，前端就不用按板块写分支：
#   {board,id,title,sub,ts,status,n,n_done,thumb,pairs[],down[],zip,owner}
# 🔴 draft 空会话必须过滤：线上 aigc 28 条记录里 25 条是「建了任务没生成」，全进来就是满屏空卡。
_LIB_PER_MOD = 60        # 每模块每用户最多取最近 N 条（目录变大后首屏不变慢）
_LIB_CAP = 400           # 返回总条数上限
_LIB_MAX_PAIRS = 12      # 一条记录最多 12 组对比（dtl 9 页 / fk 8 屏够用，防 payload 膨胀）
_LIB_TTL = 30.0          # 同一用户 30s 内复用（素材库会来回切板块，别每次都扫 8 个目录）
_LIB_CACHE = {}          # (uid, is_admin) -> (ts, items)
# 各板块状态词不统一（pt 是 stage 阶段名、hd 是 queued/done、dtl 有 stopped）→ 统一成前端认的几个词
_LIB_ST_MAP = {
    "done": "done", "error": "failed", "failed": "failed",
    "queued": "running", "gen_running": "running", "psd_running": "running",
    "parts_review": "partial", "selecting": "partial", "stopped": "stopped",
}


def _lib_st(v):
    v = str(v or "").lower()
    return _LIB_ST_MAP.get(v, v)


def _lib_dirs(mod, uid, is_admin):
    """模块记录目录：管理员看全部 u<uid>，普通用户只看自己那份。"""
    root = os.path.join(BASE, mod, "data", "records")
    if not os.path.isdir(root):
        return []
    if is_admin:
        return [os.path.join(root, d) for d in os.listdir(root) if re.match(r"^u\d+$", d)]
    return [os.path.join(root, "u{0}".format(uid))] if uid else []


def _lib_records(mod, uid, is_admin):
    """[(owner, rec)] —— 每个目录按 mtime 取最近 _LIB_PER_MOD 个 json。"""
    out = []
    for d in _lib_dirs(mod, uid, is_admin):
        if not os.path.isdir(d):
            continue
        files = []
        for fn in os.listdir(d):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(d, fn)
            try:
                files.append((os.path.getmtime(p), p))
            except OSError:
                continue
        files.sort(reverse=True)
        own = os.path.basename(d)
        for _mt, p in files[:_LIB_PER_MOD]:
            try:
                out.append((own, json.load(open(p, encoding="utf-8"))))
            except Exception:                      # noqa: BLE001
                continue
    return out


def _lib_cdn(rec, rel):
    """记录里的 cdn 映射 {rel: COS 直链}；老记录没有 → ""。"""
    if not rel:
        return ""
    return (rec.get("cdn") or {}).get(str(rel).replace("\\", "/").lstrip("/")) or ""


def _lib_pair(rec, src_rel, src_tail, out_rel, out_tail, label):
    """一组「原图 ⇄ 成品」。有 COS 直链就出 CDN 缩略，没有就退回站内路由（server 出口慢但能看）。"""
    su = _lib_cdn(rec, src_rel)
    ou = _lib_cdn(rec, out_rel)
    return dict(src=(_cos_thumb(su, 480) if su else src_tail),
                out=(_cos_thumb(ou, 480) if ou else out_tail),
                out_full=(ou or out_tail), label=label)


def _lib_board_aigc(uid, is_admin):
    out = []
    for own, rec in _lib_records("aigc", uid, is_admin):
        tid = str(rec.get("tid") or "")
        its = rec.get("items") or []
        done = [it for it in its if it.get("rel")]
        if not tid or not done:
            continue                               # 空会话 / 没出图 → 不进素材库
        srels = rec.get("src_rels") or []
        pairs, down = [], []
        for i, it in enumerate(its):
            if not it.get("rel") or len(pairs) >= _LIB_MAX_PAIRS:
                continue
            srel = srels[min(i, len(srels) - 1)] if srels else ""
            pairs.append(_lib_pair(
                rec, srel, ("aigc/api/session/{0}/src/{1}".format(tid, i + 1) if srels else ""),
                it["rel"], "aigc/api/tasks/{0}/png/{1}".format(tid, i + 1),
                "图{0}".format(i + 1)))
            down.append(dict(label="下载 图{0}".format(i + 1), url=pairs[-1]["out_full"],
                             fb="aigc/api/tasks/{0}/png/{1}".format(tid, i + 1)))
        prompt = (rec.get("prompt") or "").strip()
        out.append(dict(
            board="aigc", id=tid,
            title=(prompt[:60] or "AI 生图"),
            sub="AI 生图 · {0} · {1} 张".format(
                "批量" if (rec.get("mode") or "single") == "batch" else "单张", len(done)),
            ts=rec.get("ts") or 0, status=rec.get("status") or "",
            n=len(its), n_done=len(done),
            thumb=(pairs[0]["out"] if pairs else ""), pairs=pairs, down=down[:3],
            zip="aigc/api/tasks/{0}/zip".format(tid), owner=own))
    return out


def _lib_board_dtl(uid, is_admin):
    out = []
    for own, rec in _lib_records("dtl", uid, is_admin):
        tid = str(rec.get("tid") or "")
        pgs = rec.get("pages") or []
        done = [p for p in pgs if p.get("rel")]
        if not tid or not done:
            continue
        opt = rec.get("opt") or {}
        srels = rec.get("src_rels") or []
        pairs = []
        for i, pg in enumerate(pgs):
            if not pg.get("rel") or len(pairs) >= _LIB_MAX_PAIRS:
                continue
            pairs.append(_lib_pair(
                rec, (srels[0] if srels else ""),
                ("dtl/api/session/{0}/src/1".format(tid) if srels else ""),
                pg["rel"], "dtl/api/tasks/{0}/png/{1}".format(tid, i + 1),
                "第{0}页".format(i + 1)))
        out.append(dict(
            board="dtl", id=tid,
            title=((opt.get("product") or "").strip() or "详情页生成"),
            sub="详情页生成 · {0} · {1} 页".format(opt.get("kind") or "详情页", len(pgs)),
            ts=rec.get("ts") or 0, status=rec.get("status") or "",
            n=len(pgs), n_done=len(done),
            thumb=(pairs[0]["out"] if pairs else ""), pairs=pairs,
            down=[dict(label="下载第 1 页", url=pairs[0]["out_full"],
                       fb="dtl/api/tasks/{0}/png/1".format(tid))] if pairs else [],
            zip="dtl/api/tasks/{0}/zip".format(tid), owner=own))
    return out


def _lib_board_fk(uid, is_admin):
    out = []
    for own, rec in _lib_records("fk", uid, is_admin):
        tid = str(rec.get("tid") or "")
        pgs = rec.get("pages") or []
        done = [p for p in pgs if p.get("rel")]
        if not tid or not done:
            continue
        refs = rec.get("refs") or []
        name = (((rec.get("analysis") or {}).get("product") or {}).get("name") or "").strip()
        pairs = []
        for i, pg in enumerate(pgs):
            if not pg.get("rel") or len(pairs) >= _LIB_MAX_PAIRS:
                continue
            # 🔴 跳屏时 pages[i].no ≠ i+1：优先用该屏出图时实际用的那张参考图（ref_rel）
            if pg.get("ref_rel"):
                srel = pg["ref_rel"]
                stail = "fk/api/tasks/{0}/refpng/{1}".format(tid, i + 1)
            else:
                srel = refs[0] if refs else ""
                stail = ("fk/api/session/{0}/src/ref/1".format(tid) if refs else "")
            pairs.append(_lib_pair(rec, srel, stail, pg["rel"],
                                   "fk/api/tasks/{0}/png/{1}".format(tid, i + 1),
                                   "第{0}屏".format(i + 1)))
        out.append(dict(
            board="fk", id=tid,
            title=(name or "详情页复刻"),
            sub="详情页复刻 · {0} 屏 · {1} 参考 + {2} 产品".format(
                len(pgs), len(refs), len(rec.get("prods") or [])),
            ts=rec.get("ts") or 0, status=rec.get("status") or "",
            n=len(pgs), n_done=len(done),
            thumb=(pairs[0]["out"] if pairs else ""), pairs=pairs,
            down=[dict(label="下载第 1 屏", url=pairs[0]["out_full"],
                       fb="fk/api/tasks/{0}/png/1".format(tid))] if pairs else [],
            zip="fk/api/tasks/{0}/zip".format(tid), owner=own))
    return out


def _lib_board_swap(uid, is_admin):
    out = []
    for own, rec in _lib_records("swap", uid, is_admin):
        tid = str(rec.get("tid") or "")
        its = rec.get("items") or []
        done = [it for it in its if it.get("rel")]
        if not tid or not done:
            continue
        opt = rec.get("opt") or {}
        pairs = []
        for i, it in enumerate(its):
            if not it.get("rel") or len(pairs) >= _LIB_MAX_PAIRS:
                continue
            pairs.append(_lib_pair(
                rec, rec.get("src_rel") or "",
                ("swap/api/tasks/{0}/src/src".format(tid) if rec.get("src_rel") else ""),
                it["rel"], "swap/api/tasks/{0}/png/{1}".format(tid, i + 1),
                "第{0}张".format(i + 1)))
        out.append(dict(
            board="swap", id=tid,
            # 🔴 标题只能用用户自己填的「替换需求补充」；`prompt` 是服务端拼的四段骨架（图序声明/场景锁/…），
            #    拿它当标题会变成 60 字说明书，卡片完全没法看。
            title=((rec.get("extra") or "").strip()[:60]
                   or "商品替换 · {0}".format(opt.get("ratio") or "auto")),
            sub="商品替换 · {0} · {1} · {2} 张".format(
                opt.get("ratio") or "auto", opt.get("res") or "1K", len(done)),
            ts=rec.get("ts") or 0, status=rec.get("status") or "",
            n=len(its), n_done=len(done),
            thumb=(pairs[0]["out"] if pairs else ""), pairs=pairs,
            down=[dict(label="下载第 1 张", url=pairs[0]["out_full"],
                       fb="swap/api/tasks/{0}/png/1".format(tid))] if pairs else [],
            zip="swap/api/tasks/{0}/zip".format(tid), owner=own))
    return out


# ---- 素材库归档索引（2026-11-19 STEP2）----
# 为什么需要它：各板块都是 72h TTL，记录 json 和图片一起被 _sweep() 删掉 → 素材库永远只看得到近 3 天。
# 这里把「每条记录长什么样」落成 append-only 的 jsonl（纯元数据 + 缩略/COS 直链），
# 素材库 = 实时数据 + 索引补齐：同一 (board,id) 以实时为准（字段最新），索引只补已过期/已删除的。
# 后台每 10 分钟全站扫一遍即可 —— TTL 是 72 小时，不存在「还没来得及归档就被删」的情况。
_LIB_INDEX = os.path.join(DATA, "library_index.jsonl")
_LIB_INDEX_CACHE = {"mtime": 0.0, "map": {}}


def _lib_index_load():
    """{(board,id): item} —— 按 mtime 缓存，文件没变就不重复解析。"""
    try:
        mt = os.path.getmtime(_LIB_INDEX)
    except OSError:
        return {}
    if _LIB_INDEX_CACHE["mtime"] == mt and _LIB_INDEX_CACHE["map"]:
        return _LIB_INDEX_CACHE["map"]
    m = {}
    try:
        with open(_LIB_INDEX, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    it = json.loads(ln)
                except Exception:                   # noqa: BLE001
                    continue
                b, i = it.get("board"), it.get("id")
                if b and i:
                    m[(b, str(i))] = it
    except OSError:
        return {}
    _LIB_INDEX_CACHE["mtime"] = mt
    _LIB_INDEX_CACHE["map"] = m
    return m


def _lib_index_append(items):
    """把还没进索引的记录追加进去（已有 (board,id) 跳过）。返回新增条数。"""
    if not items:
        return 0
    old = _lib_index_load()
    add = []
    for it in items:
        k = (it.get("board"), str(it.get("id") or ""))
        if not k[0] or not k[1] or k in old:
            continue
        rec = dict(it)
        rec["archived"] = True
        rec["archived_ts"] = time.time()
        add.append(rec)
    if not add:
        return 0
    with open(_LIB_INDEX, "a", encoding="utf-8") as f:
        for rec in add:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _LIB_INDEX_CACHE["mtime"] = 0.0                 # 下次强制重载
    _LIB_INDEX_CACHE["map"] = {}
    return len(add)


def _lib_merge_index(items, uid, is_admin):
    """实时数据 + 索引补齐。归属过滤必须做：普通用户只能看自己归档的那份。"""
    idx = _lib_index_load()
    if not idx:
        return items
    seen = set((it.get("board"), str(it.get("id") or "")) for it in items)
    own = "u{0}".format(uid) if uid else ""
    add = []
    for k, it in idx.items():
        if k in seen:
            continue
        if not is_admin and (it.get("owner") or "") != own:
            continue
        add.append(dict(it))
    if add:
        items = list(items) + add
    return items


def _lib_apply_marks(items, uid):
    """把当前用户的收藏/标签贴到 item 上（前端只读 fav/tags 两个字段）。"""
    m = _lib_marks_of(uid)
    if not m:
        return items
    for it in items:
        v = m.get("{0}|{1}".format(it.get("board"), it.get("id")))
        if v:
            it["fav"] = bool(v.get("fav"))
            it["tags"] = list(v.get("tags") or [])
    return items


# ---- 素材库：收藏 / 标签（2026-11-19 STEP3）----
# 存 data/library_marks.json：{"u<uid>": {"board|id": {"fav": bool, "tags": [...], "ts": ...}}}
# 为什么不用数据库：量小（一人几十条）、要能直接看、能随 git 外备份；tmp+os.replace 原子写。
_LIB_MARK = os.path.join(DATA, "library_marks.json")
_LIB_MARK_LOCK = threading.Lock()


def _lib_marks_all():
    try:
        with open(_LIB_MARK, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:                               # noqa: BLE001
        return {}


def _lib_marks_of(uid):
    if not uid:
        return {}
    return _lib_marks_all().get("u{0}".format(uid)) or {}


@app.get("/api/library/tags")
def api_library_tags(request: Request):
    """当前用户用过的标签 + 条数（前端筛选下拉用）。"""
    uid = request.scope.get("st_uid")
    cnt = {}
    for v in (_lib_marks_of(uid) or {}).values():
        for t in (v.get("tags") or []):
            cnt[t] = cnt.get(t, 0) + 1
    return {"ok": True,
            "tags": [{"tag": k, "n": n} for k, n in sorted(cnt.items(), key=lambda x: -x[1])]}


@app.post("/api/library/mark")
async def api_library_mark(request: Request):
    """打/取消收藏、改标签。body: {board,id,fav?,tags?}；fav=false 且无标签 → 整条删掉。"""
    uid = request.scope.get("st_uid")
    if not uid:
        return {"ok": False, "error": "请先登录"}
    try:
        d = json.loads((await request.body()).decode("utf-8") or "{}") or {}
    except Exception:                               # noqa: BLE001
        d = {}
    board, tid = str(d.get("board") or ""), str(d.get("id") or "")
    if not board or not tid:
        return {"ok": False, "error": "参数不全"}
    key = "{0}|{1}".format(board, tid)
    with _LIB_MARK_LOCK:
        allm = _lib_marks_all()
        uk = "u{0}".format(uid)
        mine = allm.get(uk) or {}
        cur = mine.get(key) or {}
        if "fav" in d:
            cur["fav"] = bool(d.get("fav"))
        if "tags" in d:
            tags = [str(t).strip()[:12] for t in (d.get("tags") or []) if str(t).strip()]
            cur["tags"] = list(dict.fromkeys(tags))[:6]
        cur["ts"] = time.time()
        if cur.get("fav") or cur.get("tags"):
            mine[key] = cur
        else:
            mine.pop(key, None)
        allm[uk] = mine
        tmp = _LIB_MARK + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(allm, f, ensure_ascii=False)
        os.replace(tmp, _LIB_MARK)
    _LIB_CACHE.clear()                              # 标记变了 → 列表缓存必须失效
    return {"ok": True, "fav": bool(cur.get("fav")), "tags": cur.get("tags") or []}


def _lib_index_sweep():
    """全站扫一遍，把还没进索引的记录追加进去。返回新增条数。"""
    try:
        n = _lib_index_append(_lib_collect(None, True))
        if n:
            _log_srv("[library] 归档 {0} 条 → {1}".format(n, _LIB_INDEX))
        return n
    except Exception as e:                          # noqa: BLE001
        _log_srv("[library] 归档失败: {0}".format(e))
        return 0


def _lib_index_loop():
    while True:
        time.sleep(600)                             # 首次归档在启动时已同步跑过
        _lib_index_sweep()


def _lib_collect(uid, is_admin):
    """8 个板块 → 统一 item 列表。api_library 与后台归档扫描共用同一份，绝不写两套。

    uid=None + is_admin=True 时返回全站所有用户的记录（归档扫描用）。
    """
    items = []
    # ① 图案提取：内存任务表（含磁盘回扫），归属规则同 list_tasks
    for tid, box in _tasks.items():
        st = box["state"]
        if not (is_admin or _own(st, uid)):
            continue
        cdt = st.get("cdn_thumb") or {}
        thumb = ""
        for rel in (st.get("hi_png"), (st.get("inputs") or [None])[0],
                    "_预览.png", "原图01.jpg"):
            if rel and cdt.get(rel):
                thumb = cdt[rel]
                break
        if not thumb:
            rel = st.get("hi_png") or (st.get("inputs") or [""])[0]
            if rel:
                thumb = "api/tasks/{0}/thumb/{1}".format(tid, quote(rel))
        try:
            ts = time.mktime(time.strptime(st.get("created") or "", "%Y-%m-%d %H:%M"))
        except Exception:                     # noqa: BLE001
            ts = 0.0
        # 素材库对比/下载提速（2026-10-14）：服务器出口实测仅 58~328KB/s，COS 直链 2.2MB/s。
        #   对比格 → 缩略（cdn_thumb 优先，缺了退本地 thumb 端点；绝不再给全尺寸 file 路由）
        #   放大/预览 → *_full（cdn 全尺寸直链优先，缺了退本地 file 路由）
        #   下载 → url=COS 直链优先，fb=本地 file 路由兜底（PSD 12MB 走服务器 40s~3min，走 COS 5~6s）
        cdn = st.get("cdn") or {}
        pt_pairs, pt_down = [], []
        inp = (st.get("inputs") or [""])[0] or ""
        out_rel = st.get("hi_png") or ""

        def _pt_full(rel):
            return cdn.get(rel) or "api/tasks/{0}/file/{1}".format(tid, quote(rel))

        def _pt_th(rel):
            return cdt.get(rel) or "api/tasks/{0}/thumb/{1}?w=640".format(tid, quote(rel))

        if inp and out_rel:
            pt_pairs.append(dict(src=_pt_th(inp), out=_pt_th(out_rel),
                                 src_full=_pt_full(inp), out_full=_pt_full(out_rel),
                                 label="成品"))
        if out_rel:
            pt_down.append(dict(label="下载 PNG", url=_pt_full(out_rel),
                                fb="api/tasks/{0}/file/{1}".format(tid, quote(out_rel))))
        if st.get("psd"):
            pt_down.append(dict(label="下载 PSD", url=_pt_full(st["psd"]),
                                fb="api/tasks/{0}/file/{1}".format(tid, quote(st["psd"]))))
        # 标题：pt 的 name 常年是「未命名任务」，三张卡会一模一样 → 退回 task_name + tid 尾号区分
        _pt_nm = (st.get("name") or "").strip()
        _pt_tk = (st.get("task_name") or "").strip()
        _pt_title = _pt_nm if (_pt_nm and not _pt_nm.startswith("未命名")) else \
            ("{0} · {1}".format(_pt_tk, tid[-4:]) if _pt_tk else (tid or "图案提取"))
        items.append(dict(board="pt", id=tid,
                          title=_pt_title,
                          sub="{0} · {1}".format(st.get("task_name") or "", st.get("stage") or ""),
                          ts=ts, thumb=thumb, pairs=pt_pairs, down=pt_down,
                          status=_lib_st(st.get("stage")), n=1,
                          n_done=(1 if out_rel else 0),
                          zip="",
                          owner=("u{0}".format(st.get("_uid")) if st.get("_uid") else "")))
    # ② 批量去水印：commit 落盘的处理记录（wm/data/records/u<uid>/*.json）
    wm_root = os.path.join(BASE, "wm", "data", "records")
    wm_dirs = []
    if is_admin and os.path.isdir(wm_root):
        wm_dirs = [os.path.join(wm_root, d) for d in os.listdir(wm_root)
                   if re.match(r"^u\d+$", d)]
    elif uid:
        wm_dirs = [os.path.join(wm_root, "u{0}".format(uid))]
    for d in wm_dirs:
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.endswith(".json"):
                continue
            try:
                r = json.load(open(os.path.join(d, fn), encoding="utf-8"))
            except Exception:                 # noqa: BLE001
                continue
            # 对比对：names[i] ↔ srcs[i]（commit 时对齐落盘；老记录 srcs 缺失 → src 空，前端降级）
            _srcs = r.get("srcs") or []
            _own_dir = os.path.basename(d)          # u<uid>（admin 全量视图取他人成品要用）
            _wcdn = r.get("cdn") or {}              # commit 后台上传回写的 {rel: COS 直链}
            wm_pairs = []
            for _i, _nm in enumerate(r.get("names") or []):
                _s = _srcs[_i] if _i < len(_srcs) else ""
                # 成品 rel（相对 wm/data）= output/u<uid>/<name>，与 _bg_commit 的上传 rel 一致
                _ore = "output/{0}/{1}".format(_own_dir, _nm)
                _ou = _wcdn.get(_ore) or ""
                if _ou:                             # 已落 COS → 直连，客户端不走服务器出口
                    _of = _ou
                    _o = _cos_thumb(_ou, 640)       # 对比格交给 CDN 出缩略
                else:
                    _of = "wm/api/out?n={0}".format(quote(_nm))
                    if is_admin:
                        _of += "&u=" + quote(_own_dir)
                    _o = _of + "&w=640"             # 缩略由 wm 端点现做（老记录兜底）
                _su = (_wcdn.get(_s) if _s else "") or ""
                wm_pairs.append(dict(src=(_su or (("wm/" + _s) if _s else "")),
                                     out=_o, out_full=_of,
                                     label="图{0}".format(_i + 1)))
            items.append(dict(board="wm", id=r.get("id") or fn[:-5],
                              title="去水印成品 {0} 张".format(r.get("count") or 0),
                              sub="批量去水印 · 成品留 72 小时",
                              ts=r.get("ts") or 0,
                              thumb=(r.get("thumbs") or [""])[0],
                              names=r.get("names") or [],
                              pairs=wm_pairs,
                              down=[dict(label="打包下载 zip", act="zip")],
                              status="done", n=len(r.get("names") or []),
                              n_done=len(r.get("names") or []), zip="",
                              owner=os.path.basename(d)))
    # ③ 多图编辑：生图后台任务记录（duotu/data/parts_jobs/*.json）
    dt_dir = os.path.join(BASE, "duotu", "data", "parts_jobs")
    if os.path.isdir(dt_dir):
        for fn in os.listdir(dt_dir):
            if not fn.endswith(".json"):
                continue
            try:
                j = json.load(open(os.path.join(dt_dir, fn), encoding="utf-8"))
            except Exception:                 # noqa: BLE001
                continue
            owner = j.get("uid")
            own = is_admin
            if not own and uid and owner is not None:
                try:
                    own = int(owner) == uid
                except Exception:             # noqa: BLE001
                    own = False
            if not own:
                continue
            thumb = _dt_url(_dt_job_thumb(j))
            if thumb and thumb.startswith("api/"):   # 老记录（无 .cosok）→ 补 duotu/ 挂载前缀
                thumb = "duotu/" + thumb
            thumb = _cos_thumb(thumb, 640)           # COS 直链 → CDN 出缩略（网格别拉全尺寸）
            _dt_pairs = _dt_job_pairs(j)
            _dt_down = []
            for _i, _p in enumerate(_dt_pairs):
                if _p.get("out"):
                    _dt_down.append(dict(label="下载 {0}".format(_p.get("label") or "图"),
                                         url=_p["out"]))
            items.append(dict(board="duotu", id=j.get("id") or fn[:-5],
                              title=j.get("note") or "多图编辑",
                              sub="{0} · {1}".format(
                                  {"group_detect": "组批量识别", "group_run": "组批量生成"}.get(
                                      j.get("kind"), "分部位修改"),
                                  j.get("status") or ""),
                              ts=j.get("created") or 0, thumb=thumb,
                              pairs=_dt_pairs, down=_dt_down,
                              status=_lib_st(j.get("status")), n=len(_dt_pairs),
                              n_done=len([p for p in _dt_pairs if p.get("out")]), zip="",
                              owner=("u{0}".format(owner) if owner else "")))
    # ④ 高清修复：hd/data/records/u<uid>/<tid>.json（与 wm 同布局）
    hd_root = os.path.join(BASE, "hd", "data", "records")
    hd_dirs = []
    if is_admin and os.path.isdir(hd_root):
        hd_dirs = [os.path.join(hd_root, d) for d in os.listdir(hd_root)
                   if re.match(r"^u\d+$", d)]
    elif uid:
        hd_dirs = [os.path.join(hd_root, "u{0}".format(uid))]
    for d in hd_dirs:
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.endswith(".json"):
                continue
            try:
                r = json.load(open(os.path.join(d, fn), encoding="utf-8"))
            except Exception:                 # noqa: BLE001
                continue
            st = r.get("status") or ""
            _hd_id = r.get("tid") or fn[:-5]
            _hcdn = r.get("cdn") or {}               # hdstore 上传后回写 {rel: COS 直链}

            def _hd_u(_rel, _tail):
                """rel 有 COS 直链 → 用直链；否则退回站内 hd/api/tasks/<tid>/<tail>。

                🔴 必须剥掉 `|err=` 尾巴：记录里 `thumb_rel` 是 `output/u1/<tid>/thumb.png|err=0.123`
                （拆层还原误差，前端展示时才 split），带尾巴去查 `cdn` 映射永远查不到 →
                素材库 hd 缩略图会莫名其妙退回服务器慢链。
                """
                _rel = str(_rel or "").split("|")[0]
                return ((_hcdn.get(_rel) or "") if _rel else "") or \
                    "hd/api/tasks/{0}/{1}".format(_hd_id, _tail)

            _hd_pairs, _hd_down = [], []
            if st == "done":
                if r.get("src_rel"):
                    _hd_pairs.append(dict(src=_cos_thumb(_hd_u(r.get("src_rel"), "src"), 640),
                                          out=_cos_thumb(_hd_u(r.get("thumb_rel"), "thumb"), 640),
                                          out_full=_hd_u(r.get("png_rel"), "png"),
                                          label="高清成品"))
                if r.get("png_rel"):
                    _hd_down.append(dict(label="下载 PNG",
                                         url=_hd_u(r.get("png_rel"), "png"),
                                         fb="hd/api/tasks/{0}/png".format(_hd_id)))
                if r.get("psd_rel"):
                    _hd_down.append(dict(label="下载 PSD",
                                         url=_hd_u(r.get("psd_rel"), "psd"),
                                         fb="hd/api/tasks/{0}/psd".format(_hd_id)))
            _hd_thumb = ""
            if st == "done" and r.get("thumb_rel"):
                _hd_thumb = (_cos_thumb(_hd_u(r.get("thumb_rel"), "thumb"), 480)
                             or "hd/api/tasks/{0}/thumb".format(_hd_id))
            items.append(dict(board="hd", id=_hd_id,
                              title=r.get("name") or "高清修复",
                              sub="高清修复 · " + {"queued": "排队中", "running": "生成中",
                                                   "done": "已完成", "failed": "失败"}.get(st, st),
                              ts=r.get("ts") or 0,
                              thumb=_hd_thumb,
                              pairs=_hd_pairs, down=_hd_down,
                              status=_lib_st(st), n=1,
                              n_done=(1 if st == "done" else 0), zip="",
                              owner=os.path.basename(d)))
    # ⑤ AI 生图 / 详情页生成 / 详情页复刻 / 商品替换（2026-11-19 STEP1 接入）
    #    单个板块读挂了不能把整个素材库搞成白页 —— 逐个 try，坏的只是结果里缺一块。
    for _fn in (_lib_board_aigc, _lib_board_dtl, _lib_board_fk, _lib_board_swap):
        try:
            items.extend(_fn(uid, is_admin))
        except Exception as _e:                    # noqa: BLE001
            print("[library] %s 读取失败: %s" % (_fn.__name__, _e))
    return items


@app.get("/api/library")
def api_library(request: Request):
    uid = request.scope.get("st_uid")
    is_admin = bool(uid and ACC.is_admin(uid))
    ck = (uid, is_admin)
    now = time.time()
    hit = _LIB_CACHE.get(ck)
    if hit and (now - hit[0]) < _LIB_TTL:
        return {"items": hit[1], "admin": is_admin, "total": len(hit[1])}
    items = _lib_collect(uid, is_admin)
    items = _lib_merge_index(items, uid, is_admin)
    items = _lib_apply_marks(items, uid)
    items.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    items = items[:_LIB_CAP]
    _LIB_CACHE[ck] = (now, items)
    if len(_LIB_CACHE) > 32:                    # 防缓存无界（正常也就几个用户）
        for _k in list(_LIB_CACHE)[:-24]:
            _LIB_CACHE.pop(_k, None)
    return {"items": items, "admin": is_admin, "total": len(items)}


# ---- 素材库对比「裁白边」接口（2026-10-15 雪碧需求）----
# 需求原话：「原图和需要对比的图，裁掉空白区域，让两张图 0 缝隙紧挨一起」。
# 为什么必须放后端裁：素材库的图绝大部分是 COS 直链（跨域），前端 <canvas> 取像素会被
#   污染（SecurityError），根本读不到；而且四个板块的图源五花八门（COS 直链 / 站内
#   file・thumb・wm・duotu 相对路由 / data: 内联），只有服务端一条通路能全覆盖。
# 「空白」判定铁律：只认「整行 / 整列**全部**是背景色」才切边 —— 绝不按单像素判，
#   否则图案内部的白点、白描边会被当背景挖穿。背景色两种：有透明区 → 全透明；
#   否则 → 近白（RGB 三通道 >247）。裁到内容包围盒 + pad 缓冲，绝不裁到 0。
_TRIM_DIR = os.path.join(DATA, "trim_cache")
# 防 SSRF：只允许公网主机（自己人也别拿这个接口去探内网）
_PRIV_HOST = re.compile(
    r"^(localhost|127\.|10\.|192\.168\.|169\.254\.|0\.0\.0\.0|::1|\[::1\]|172\.(1[6-9]|2\d|3[01])\.)",
    re.I)
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# 原图字节 → 文件后缀 / 媒体类型（透传分支必须按**真实**格式标注，否则 webp 标成 jpeg 会白图）
_FMT_EXT = {"JPEG": (".jpg", "image/jpeg"), "JPG": (".jpg", "image/jpeg"),
            "PNG": (".png", "image/png"), "WEBP": (".webp", "image/webp"),
            "BMP": (".bmp", "image/bmp"), "GIF": (".gif", "image/gif"),
            "TIFF": (".tif", "image/tiff")}
_FMT_FALLBACK = (".png", "image/png")

# ---- 裁切结果回传 COS（2026-10-15 提速）----
# 真瓶颈不是「算」而是「传」：服务器出口实测 ~1Mbps，素材库一屏几十张对比图同时回源，
#   字节全从这条窄管子出去 → 首开极慢。改造后裁完**不把字节喂给客户端**，而是推一份到
#   COS 再 302 过去：客户端直连 CDN，服务器出口只发一个 302 头（几十字节）。
#   二次访问读 sidecar 直接 302 —— 零计算、零回源、零带宽。
#   任何一步失败 → 照旧流式返回（与改造前一字不差），前端完全无感。
#   熔断：连续 3 次上传失败 → 关 10 分钟，避免 COS 抖动时每张图都白等一次上传超时。
_TRIM_COS = (os.environ.get("PATTERN_TRIM_COS", "1") or "1").strip().lower() \
    not in ("0", "false", "no", "off")
_TRIM_COS_SUB = "trim/"                    # 相对 storage.PREFIX → pattern/trim/ ｜ pattern_dev/trim/
_TRIM_COS_FAIL = [0, 0.0]                  # [连续失败数, 熔断截止 epoch]

# ---- 裁切规则版本 + 尺寸上限（2026-10-15 二次提速）----
# 为什么必须限尺寸：线上实测**服务器 → COS 上行只有 200~500KB/s**，一张 2048×3072 的成品
#   PNG 裁完仍有 10MB，推一次要 52 秒（真机实测），比不接 COS 还糟；素材库对比格最宽也就
#   ~800px、放大灯箱两格并排每格 ≤45vw（4K 屏 ≈1700px），1400px 足够清晰。
#   限到 1400 后同一张图从 10MB 掉到几十~几百 KB，上传 0.3~0.5s。
# 为什么带版本号：老缓存（10MB 全尺寸）与新规则产物尺寸不一致，若共用 key 会混用；
#   进位 v2 → 老缓存自然失效（由 _trim_prune 按体积/数量回收），新规则干净生效。
_TRIM_VER = "v2"
_TRIM_MAX_PX = 1400


def _trim_cos_sidecar(key, ext):
    return os.path.join(_TRIM_DIR, key + ext + ".cos")


def _trim_cos_url(key, ext):
    """读 sidecar 拿 COS 直链；未启用 / 熔断中 / 无 sidecar → ""。"""
    if not _TRIM_COS or time.time() < _TRIM_COS_FAIL[1]:
        return ""
    try:
        p = _trim_cos_sidecar(key, ext)
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                return f.read().strip()
    except OSError:
        pass
    return ""


def _trim_cos_put(buf, key, ext, mt):
    """裁后字节推 COS + 写 sidecar，成功返回直链，失败 ""（调用方回退流式）。"""
    if not _TRIM_COS or time.time() < _TRIM_COS_FAIL[1]:
        return ""
    try:
        import storage as _ST
        if not _ST.enabled():
            return ""
        url = _ST.upload_bytes(buf, _ST.PREFIX + _TRIM_COS_SUB + key + ext, mt)
        if not url:
            raise RuntimeError("上传返回空")
        with open(_trim_cos_sidecar(key, ext), "w", encoding="utf-8") as f:
            f.write(url)
        _TRIM_COS_FAIL[0] = 0
        return url
    except Exception:                              # noqa: BLE001
        _TRIM_COS_FAIL[0] += 1
        if _TRIM_COS_FAIL[0] >= 3:
            _TRIM_COS_FAIL[1] = time.time() + 600
            _TRIM_COS_FAIL[0] = 0
        return ""


def _http_get(url, cookie=None, timeout=30, direct=False):
    hdr = {"User-Agent": "pattern-workbench/trimmer"}
    if cookie:
        hdr["Cookie"] = cookie
    req = urllib.request.Request(url, headers=hdr)
    # 铁律：urlopen 是**函数**、OpenerDirector 是**对象**，两者调用姿势不同 ——
    #   旧写法 `op(req, ...)` 对 OpenerDirector 会抛 TypeError，直接把回环分支打死（2026-10-15 踩过）。
    #   统一走 .open()（urlopen 内部也是 opener.open），一个姿势通吃。
    op = _NO_PROXY_OPENER.open if direct else urllib.request.urlopen
    with op(req, timeout=timeout) as r:            # noqa: S310
        return r.read()


def _has_alpha(im):
    return im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)


def _trim_bounds(im, pad):
    """返回裁切框 (l, t, r, b)；判定不可靠或本来就无边 → None（不裁）。"""
    import numpy as np
    a = np.asarray(im.convert("RGBA"))
    alpha = a[:, :, 3]
    rgb = a[:, :, :3]
    if float((alpha <= 8).mean()) > 0.02:          # 抠图成品：透明即背景
        content = alpha > 8
    else:                                          # 白底图：近白即背景
        content = ~((rgb[:, :, 0] > 247) & (rgb[:, :, 1] > 247) & (rgb[:, :, 2] > 247))
    if not content.any():
        return None
    xs = np.flatnonzero(content.any(axis=0))
    ys = np.flatnonzero(content.any(axis=1))
    x0, x1 = max(0, int(xs[0]) - pad), min(im.width - 1, int(xs[-1]) + pad)
    y0, y1 = max(0, int(ys[0]) - pad), min(im.height - 1, int(ys[-1]) + pad)
    if x0 == 0 and y0 == 0 and x1 == im.width - 1 and y1 == im.height - 1:
        return None                                # 四周本来就没有空白
    if (x1 - x0 + 1) * (y1 - y0 + 1) < im.width * im.height * 0.05:
        return None                                # 裁完只剩 <5% → 判定多半有问题，宁可不裁
    return (x0, y0, x1 + 1, y1 + 1)


_TRIM_PRUNE_N = [0]


def _trim_prune(max_bytes=256 * 1024 * 1024, max_files=1500, every=20):
    """裁切缓存兜底清理：文件数**或**总体积超限 → 按 mtime 丢最老的一批。

    2026-10-15 补体积闸：素材库不少「成品」本身满幅无白边，会原样透传缓存，
       单张就能到 10MB，只按文件数（4000）限会滚到 40GB。改成体积为主闸。
    every：每 N 次写入才真扫一次目录 —— 素材库一屏可能同时 warm 几十张，
       每次都 scandir 上千文件会把 IO 打满。
    """
    _TRIM_PRUNE_N[0] += 1
    if _TRIM_PRUNE_N[0] % max(1, every) != 0:
        return
    try:
        fs = []
        total = 0
        with os.scandir(_TRIM_DIR) as it:
            for e in it:
                if not e.is_file():
                    continue
                st = e.stat()
                fs.append((st.st_mtime, st.st_size, e.path))
                total += st.st_size
        n = len(fs)
        if n <= max_files and total <= max_bytes:
            return
        fs.sort(key=lambda t: t[0])                 # 老的在前
        i = 0
        while i < n and (n - i > max_files or total > max_bytes):
            _, sz, p = fs[i]
            try:
                os.remove(p)
                total -= sz
            except OSError:
                pass
            i += 1
    except OSError:
        pass


@app.get("/api/trim", include_in_schema=False)
def api_trim(request: Request, u: str = "", pad: int = 2, fb: str = ""):
    """裁掉源图四周空白（纯白 / 全透明），返回裁后图；任何一步失败 → 302 回原始 URL（前端无感降级）。"""
    def _fallback():
        # fb = 客户端本来就在用的显示 URL（浏览器可解析）；没有则退回绝对源地址
        tgt = fb or (u if u.startswith(("http://", "https://", "data:")) else "")
        if tgt:
            return RedirectResponse(tgt, status_code=302)
        raise HTTPException(404, "无法取到源图")

    if not u:
        raise HTTPException(400, "缺少 u")
    try:
        pad = max(0, min(24, int(pad)))
    except (TypeError, ValueError):
        pad = 2
    uid = request.scope.get("st_uid") or 0
    key = hashlib.sha1("{0}|{1}|{2}|{3}".format(_TRIM_VER, uid, u, pad).encode("utf-8")).hexdigest()
    os.makedirs(_TRIM_DIR, exist_ok=True)
    for _ext, _mt in _FMT_EXT.values():           # 缓存命中：按后缀全表覆盖（含 webp/bmp/gif/tif）
        _p = os.path.join(_TRIM_DIR, key + _ext)
        if os.path.isfile(_p):
            _cu = _trim_cos_url(key, _ext)
            if _cu:                               # 已落 COS → 302 直链（客户端走 CDN，不占服务器出口）
                return RedirectResponse(_cu, status_code=302,
                                        headers={"Cache-Control": "private, max-age=3600"})
            # 存量缓存自愈：COS 回推上线前落盘的老缓存没有侧车，若只 FileResponse，
            # 它会**永远**走服务器窄出口（线上 23 份老缓存实测即如此）。
            # 这里就地补推一次：成功 → 转直链；失败 → 照旧流式（_trim_cos_put 自带熔断闸）。
            try:
                with open(_p, "rb") as _f:
                    _cu2 = _trim_cos_put(_f.read(), key, _ext, _mt)
            except OSError:
                _cu2 = ""
            if _cu2:
                return RedirectResponse(_cu2, status_code=302,
                                        headers={"Cache-Control": "private, max-age=3600"})
            return FileResponse(_p, media_type=_mt,
                                headers={"Cache-Control": "private, max-age=3600"})

    # ① 取源图字节
    try:
        if u.startswith("data:"):
            data = base64.b64decode(u.split(",", 1)[1] or "")
        elif u.startswith(("http://", "https://")):
            if _PRIV_HOST.match(urllib.parse.urlparse(u).hostname or ""):
                return _fallback()
            data = _http_get(u)
        else:
            # 站内相对路由（api/... · wm/api/... · duotu/api/...）→ 回环自取，带上登录 Cookie
            srv = request.scope.get("server") or ("127.0.0.1", 8000)
            port = srv[1] or 8000
            data = _http_get("http://127.0.0.1:{0}/{1}".format(port, u.lstrip("/")),
                             request.headers.get("cookie"), direct=True)
    except Exception:                              # noqa: BLE001
        return _fallback()
    if not data:
        return _fallback()

    # ② 裁切 + 编码（抠图带 alpha → PNG 保透明；照片 → JPEG 省体积）
    try:
        from PIL import Image
        src = Image.open(io.BytesIO(data))
        src.load()
        # 先按最长边压到 _TRIM_MAX_PX 再找白边：素材库/灯箱的展示尺寸远小于原图，
        # 全尺寸既没必要又会把 10MB 的字节喂进服务器窄上行（见 _TRIM_MAX_PX 注释）。
        _shrunk = False
        if max(src.width, src.height) > _TRIM_MAX_PX:
            src.thumbnail((_TRIM_MAX_PX, _TRIM_MAX_PX), Image.LANCZOS)
            _shrunk = True
        box = _trim_bounds(src, pad)
    except Exception:                              # noqa: BLE001
        return _fallback()

    if not box and not _shrunk:
        # 四周本来就没有空白 **且没缩过尺** → 原样透传。必须按**真实**格式标注后缀/类型，不能一律当 jpg。
        ext, mt = _FMT_EXT.get((src.format or "").upper(), _FMT_FALLBACK)
        buf = data
    else:
        # 有白边可裁，或虽无边但已降采样（原字节尺寸不对了）→ 必须重新编码
        out = src.crop(box) if box else src
        b = io.BytesIO()
        if _has_alpha(src):
            out.convert("RGBA").save(b, "PNG", optimize=True)
            ext, mt = ".png", "image/png"
        else:
            out.convert("RGB").save(b, "JPEG", quality=92, optimize=True, progressive=True)
            ext, mt = ".jpg", "image/jpeg"
        buf = b.getvalue()
    try:
        with open(os.path.join(_TRIM_DIR, key + ext), "wb") as f:
            f.write(buf)
        _trim_prune()
    except OSError:
        pass
    # 裁完先推 COS 再 302 过去：客户端直连 CDN，服务器出口只发 302 头（提速核心）。
    # 推不动（未配置/抖动/欠费）→ 照旧流式，行为与改造前完全一致。
    _cu = _trim_cos_put(buf, key, ext, mt)
    if _cu:
        return RedirectResponse(_cu, status_code=302,
                                headers={"Cache-Control": "private, max-age=3600"})
    return Response(buf, media_type=mt, headers={"Cache-Control": "private, max-age=3600"})


# ---- 用户 / 积分体系（2026-10-12 上线：注册送 10 积分，四板块统一登录门 + 计费）----
# 价格与规则集中在 accounts.py（PACKAGES / PRICE_LIST / match_charge / amount_for）。
import accounts as ACC
from fastapi.responses import JSONResponse, RedirectResponse

ACC.init()
app.include_router(ACC.router)

# 免登录白名单：登录页 / 登录注册接口 / 健康检查 / 全站设计令牌 CSS（登录页自身也引用）；其余一律要求登录
_AUTH_OPEN_EXACT = {"/login", "/api/health", "/favicon.ico", "/favicon.svg",
                    "/favicon.png", "/accounts/static/ui.css"}
# 对比组件按前缀放行（cmp.js 及所有 cmp-<版本>.js）：组件每改一次行为就换文件名
# （新 URL = 绕过一切浏览器/代理缓存），若用精确集合登记，换名时漏登记 → 未登录态 401。
# 2026-10-14 踩坑：换成 cmp-20261014c.js 后精确白名单没同步 → 公网 404，四页全加载不到组件。
_AUTH_OPEN_PREFIX = ("/accounts/api/login", "/accounts/api/register",
                     # 注册页要在「还没登录」时校验邀请码（只回脱敏用户名）
                     "/accounts/api/invite/check",
                     # 收款码图片：公开的收款二维码，登录页/充值页都要能取；前端在未登录态
                     # 也可能加载（如被直接打开），不放行会 401 导致图裂。
                     "/accounts/static/pay_",
                     "/accounts/static/cmp")

# 仅主账号（admin 组）：系统设置 / 管理后台页面 + 配置写入类 API（2026-10-12 收口）。
# 页面类非 admin → 302 相对跳回应用首页；API 类 → 403 JSON。
# GET /wm/api/config、GET /duotu/api/ai_config 放行（前端初始化依赖），密钥脱敏在子应用内做。
_ADMIN_PAGE_EXACT = {"/settings", "/settings/", "/admin", "/admin/"}
_ADMIN_API_RULES = (            # (method 或 None=全部方法, path 前缀)
    ("POST", "/wm/api/config"),
    ("POST", "/wm/api/models"),
    (None, "/wm/api/cleanup/run"),
    ("POST", "/duotu/api/ai_config"),
    (None, "/api/ai_sites"),        # 生图站点池：读写都只给主账号（GET 也会带出站点名/模型清单）
)


@app.middleware("http")
async def auth_and_billing(request: Request, call_next):
    path = request.url.path
    method = request.method.upper()
    if path in _AUTH_OPEN_EXACT or any(path.startswith(p) for p in _AUTH_OPEN_PREFIX):
        return await call_next(request)
    uid = ACC.uid_from_request(request)
    if not uid:
        # 页面类请求跳登录页；API 类请求回 401 JSON。
        # 注意：本应用挂在子路径（线上 /pattern/*）下，后端看不到前缀，
        # 必须用「相对跳转」按当前页目录深度回退，绝不能用 /login 绝对路径。
        if method == "GET" and "text/html" in (request.headers.get("accept") or ""):
            segs = [s for s in path.split("/") if s]
            ups = len(segs) if path.endswith("/") else max(0, len(segs) - 1)
            return RedirectResponse("../" * ups + "login", status_code=302)
        return JSONResponse({"ok": False, "error": "未登录，请先登录"}, status_code=401)
    request.scope["st_uid"] = uid     # 供 wm 子应用识别身份（FastAPI 挂载，scope 直达）
    # duotu 是 WSGI 挂载（Flask），看不到 scope 自定义键 → 身份改走请求头注入；
    # 先剥离客户端自带的同名头防伪造，再写服务端鉴权结果。
    _grp = "admin" if ACC.is_admin(uid) else "user"
    _h = [(k, v) for k, v in request.scope.get("headers", [])
          if k.lower() not in (b"x-st-uid", b"x-st-grp")]
    _h += [(b"x-st-uid", str(uid).encode()), (b"x-st-grp", _grp.encode())]
    request.scope["headers"] = _h
    if _grp != "admin":
        if path in _ADMIN_PAGE_EXACT:
            segs = [s for s in path.split("/") if s]
            ups = len(segs) if path.endswith("/") else max(0, len(segs) - 1)
            return RedirectResponse("../" * ups or "./", status_code=302)
        for m, p in _ADMIN_API_RULES:
            if (m is None or method == m) and path.startswith(p):
                return JSONResponse(
                    {"ok": False, "error": "仅主账号可操作系统设置"}, status_code=403)
    # 计费：命中规则先扣后做；响应 4xx/5xx 自动退积分
    rule = ACC.match_charge(method, path)
    if rule:
        body = b""
        if rule.get("need_body"):
            body = await request.body()
            async def _recv():           # 重新注入请求体，让下游正常读取
                return {"type": "http.request", "body": body, "more_body": False}
            request = Request(request.scope, _recv)
        amount, reason, ref = ACC.amount_for(rule, body, uid)
        if amount > 0:
            ok, _bal = ACC.charge(uid, amount, reason, ref)
            if not ok:
                # 只回一个 402 数字用户看不懂「差多少」。这里把余额与差额一并说明，
                # 前端 alert 直接可读，并据此弹出「去充值」引导。
                _u = ACC.get_user(uid) or {}
                _bal_now = int(_u.get("points") or 0)
                return JSONResponse(
                    {"ok": False,
                     "error": "积分不足：本次需 %d 积分，当前余额 %d 积分，还差 %d 积分。请先充值。"
                              % (amount, _bal_now, max(0, amount - _bal_now)),
                     "need": amount, "balance": _bal_now}, status_code=402)
            if rule["kind"] in ("psd", "hd_psd"):
                ACC.mark_psd_paid(rule["tid"], uid)
            resp = await call_next(request)
            if resp.status_code >= 400:
                ACC.refund(uid, amount, "失败退回:" + reason, ref)
            return resp
    return await call_next(request)


def _html_page(request, name):
    """返回 static/<name>，带 **ETag + 304**（2026-11-15 提速）。

    背景：服务器出口只有 ~1Mbps 且与同机其它服务抢带宽，pt.html 原始 124KB（gzip 41KB）
    每次切换板块都要重传一遍。这里保留 `no-cache`（**必须每次验证**，否则改完前端用户看不到新版），
    但补上 ETag：内容没变就回 **304 空响应**（约 0.2KB），省掉 99% 流量。
    mtime 或 size 一变 ETag 就变 → 改完立即生效，不会出现「改了没反应」。
    """
    p = os.path.join(BASE, "static", name)
    hdr = {"Cache-Control": "no-cache, must-revalidate"}
    etag = None
    try:
        st = os.stat(p)
        etag = 'W/"%x-%x"' % (int(st.st_mtime), int(st.st_size))
        hdr["ETag"] = etag
    except Exception:                                   # noqa: BLE001
        pass
    if etag:
        inm = (request.headers.get("if-none-match") or "").strip()
        if inm:
            tags = [t.strip() for t in inm.split(",")]
            if inm == "*" or etag in tags:
                return Response(status_code=304, headers=hdr)
    with open(p, encoding="utf-8") as f:
        body = f.read()
    return HTMLResponse(body, headers=hdr)


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request):
    return _html_page(request, "login.html")


# 账户静态资源白名单：ui.css（全站设计令牌）+ cmp*.js（通用对比组件）+ 收款码图片（未配置时 404 → 前端显示占位提示）
@app.get("/accounts/static/{fn}", include_in_schema=False)
def accounts_static(fn: str, request: Request):
    # cmp 用「版本化文件名」放行：cmp.js（旧名，旧缓存仍在请求，永久保留）+
    # cmp-<任意版本>.js（每次改组件行为就换名，新 URL 无法被任何浏览器/代理缓存命中）。
    # 2026-10-14 踩坑：换成 cmp-20261014c.js 后这里没同步放行 → 公网 404，四页全加载不到组件。
    # 故此处写成通用后缀，以后再换名不必再动白名单。
    if not re.match(r"^(ui\.css|cmp(-[\w-]+)?\.js|pay_(wechat|alipay)\.(png|jpe?g))$", fn or "", re.I):
        raise HTTPException(404)
    p = os.path.join(BASE, "static", fn)
    if not os.path.isfile(p):
        raise HTTPException(404)
    # 禁强缓存：这四个资源（尤其 cmp.js）是四板块共用的行为文件，
    # 一旦被浏览器强缓存，改完前端页面刷新也拿不到新逻辑（2026-10-14 实测：
    # 并排改造上线后用户端仍渲染旧滑块版，根因就是这里没有 Cache-Control）。
    # 各页引用还带了 ?v=<版本>，双保险。
    # 2026-11-15 补 ETag：仍 no-cache（必须验证），但内容没变回 304，省掉 cmp.js 28KB 重传。
    hdr = {"Cache-Control": "no-cache, must-revalidate"}
    etag = None
    try:
        st = os.stat(p)
        etag = 'W/"%x-%x"' % (int(st.st_mtime), int(st.st_size))
        hdr["ETag"] = etag
    except Exception:                                   # noqa: BLE001
        pass
    if etag:
        inm = (request.headers.get("if-none-match") or "").strip()
        if inm and (inm == "*" or etag in [t.strip() for t in inm.split(",")]):
            return Response(status_code=304, headers=hdr)
    return FileResponse(p, headers=hdr)


# 站点图标（favicon）：浏览器打开任意页面都会自动请求根路径 /favicon.ico，
# 原先只把该路径放进了鉴权白名单却没给路由 → 每次加载都白拿一个 404。
# 这里补齐三个入口：.ico 走 PNG（兼容性最好）、.svg / .png 直接给出源文件。
# 三个路径均已加入 _AUTH_OPEN_EXACT，未登录也能取到，且不参与任何计费。
def _favicon_response(fn: str, media_type: str):
    p = os.path.join(BASE, "static", fn)
    if not os.path.isfile(p):
        raise HTTPException(404)
    return FileResponse(p, media_type=media_type,
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    return _favicon_response("favicon.png", "image/png")


@app.get("/favicon.png", include_in_schema=False)
def favicon_png():
    return _favicon_response("favicon.png", "image/png")


@app.get("/favicon.svg", include_in_schema=False)
def favicon_svg():
    return _favicon_response("favicon.svg", "image/svg+xml")


@app.get("/recharge", include_in_schema=False)
def recharge_noslash():
    return _board_trailing_slash("recharge")


@app.get("/recharge/", response_class=HTMLResponse, include_in_schema=False)
def recharge_page(request: Request):
    return _html_page(request, "recharge.html")


# 「我的邀请」页（2026-11-15 邀约返利机制）：显示邀请码 / 分享链接 / 已邀请人数 / 返利明细
@app.get("/invite", include_in_schema=False)
def invite_noslash():
    return _board_trailing_slash("invite")


@app.get("/invite/", response_class=HTMLResponse, include_in_schema=False)
def invite_page(request: Request):
    return _html_page(request, "invite.html")


@app.get("/admin", include_in_schema=False)
def admin_noslash():
    return _board_trailing_slash("admin")


@app.get("/admin/", response_class=HTMLResponse, include_in_schema=False)
def admin_page(request: Request):
    return _html_page(request, "admin.html")


_load_all()


def _backfill_cdn():
    """启动后把「历史任务」的产物补推到 COS（新任务在生成完自动推）。

    只在 CDN 就绪时跑；进程内 `_cdn_mt` 按 mtime 去重 → 同一文件不会重复上传。
    正跑着的任务跳过（别和生成流程抢 IO）。整段尽力而为，任何异常都不影响服务启动。
    """
    try:
        if S is None or not S.enabled():
            return
        for tid, box in list(_tasks.items()):
            try:
                st = box["state"]
                if st.get("stage") in ("gen_running", "psd_running"):
                    continue
                # 幂等：只有「该推的 rel **全部**已在 cdn 里」才算补过 → 跳过；否则再补一轮。
                # （比旧的「cdn 且 cdn_thumb 都在就跳过」更精细：新增字段 / 目录里多了文件时
                #   也能把漏掉的补上，同时又不会每次重启都重传全部产物。）
                try:
                    need = set(_auto_rels(tid, st)) - set((st.get("cdn") or {}).keys())
                except Exception:                # noqa: BLE001
                    need = set()
                if ("cdn_thumb" in st) and not need:
                    continue
                _publish_auto(tid, st)
            except Exception:                       # noqa: BLE001
                pass
        _log_srv("CDN 历史任务回填完成")
    except Exception:                               # noqa: BLE001
        pass


def _log_srv(msg):
    try:
        print(("[pattern] " + str(msg)).encode("ascii", "replace").decode("ascii"), flush=True)
    except Exception:                               # noqa: BLE001
        pass


# ================= 产物保留 72 小时 · 自动清理 =================
def _dir_mb(p):
    n = 0
    try:
        for root, _dirs, fns in os.walk(p):
            for f in fns:
                try:
                    n += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return n / 1048576.0


def _cleanup_log_append(rep):
    """把一次清理结果追加进 `data/_cleanup.json`（保留最近 100 条，便于事后核对）。"""
    try:
        rows = []
        if os.path.isfile(CLEANUP_LOG):
            try:
                rows = json.load(open(CLEANUP_LOG, encoding="utf-8"))
            except Exception:                       # noqa: BLE001
                rows = []
            if not isinstance(rows, list):
                rows = []
        rows.append(rep)
        json.dump(rows[-100:], open(CLEANUP_LOG, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    except Exception:                               # noqa: BLE001
        pass


def _sweep_expired(ttl_hours=None, dry_run=False):
    """清理超过保留期的产物：**内存记录 + 本地目录 + COS 前缀**，顺带清 COS 孤儿对象。

    - 过期判据：`now - _task_ts(tid, st) >= ttl`（默认 72 小时；见 `TASK_TTL_HOURS` / `TTL_MODE`）。
    - 正在生成 / 出 PSD 的任务永不删（stage 为 gen_running / psd_running）。
    - COS 侧两步：① 过期任务 → 删整个 `pattern/<tid>/` 前缀；
      ② 孤儿兜底 → 删「已无对应任务且对象最后修改时间也超期」的残留（`keep_tids` 为空时跳过）。
    - `dry_run=True` 只报告不落地（供上线前预演核对）。
    """
    ttl = float(TASK_TTL_HOURS if ttl_hours is None else ttl_hours)
    now = time.time()
    rep = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "ttl_h": ttl, "mode": TTL_MODE,
           "dry_run": bool(dry_run), "expired": [], "kept": 0, "local_mb": 0.0,
           "cos_prefix_tasks": 0, "cos_orphan_keys": 0, "cos_orphan_mb": 0.0,
           "cos_deleted": 0, "cos_error": ""}
    expired, keep = [], []
    for tid, box in list(_tasks.items()):
        st = box["state"]
        if st.get("stage") in ("gen_running", "psd_running"):
            keep.append(tid)
            continue
        age_h = (now - _task_ts(tid, st)) / 3600.0
        if age_h >= ttl:
            expired.append(tid)
        else:
            keep.append(tid)
    rep["expired"] = expired
    rep["kept"] = len(keep)
    for tid in expired:
        rep["local_mb"] += _dir_mb(os.path.join(DATA, tid))
    if not dry_run:
        for tid in expired:
            _drop_task(tid)                          # 内存 + 本地目录（幂等）
    if S is None or not S.enabled():
        rep["cos_error"] = "COS 未启用"
        return rep
    # ① 过期任务的整个 COS 前缀
    if not dry_run:
        for tid in expired:
            try:
                S.delete_task(tid)
                rep["cos_prefix_tasks"] += 1
            except Exception:                        # noqa: BLE001
                pass
    # ② 孤儿兜底（测试残留 / 已删任务残留 / 上传成功但任务丢了的对象）
    try:
        r = S.sweep_prefix(ttl_hours=ttl, keep_tids=keep, dry_run=dry_run)
        rep["cos_orphan_keys"] = int(r.get("matched") or 0)
        rep["cos_orphan_mb"] = round((r.get("bytes") or 0) / 1048576.0, 2)
        rep["cos_deleted"] = int(r.get("deleted") or 0)
        if r.get("skipped"):
            rep["cos_error"] = str(r["skipped"])
    except Exception as e:                           # noqa: BLE001
        rep["cos_error"] = str(e)[:160]
    return rep


def _cleanup_loop():
    """常驻清理线程：启动后延迟 2 分钟（避开回填高峰），此后每 CLEANUP_INTERVAL 秒扫一次。

    按**绝对时间**判断过期 → 服务重启 / 停机期间的欠账会在下次启动时自然补清。
    """
    time.sleep(120)
    while True:
        try:
            rep = _sweep_expired()
            if rep.get("expired") or rep.get("cos_deleted") or rep.get("cos_error"):
                _log_srv("TTL %.0fh 清理: 任务 %d 个 / 本地 %.1fMB / COS 前缀 %d 个 / "
                         "孤儿 %d 个 %.1fMB / 已删 %d 个 %s"
                         % (rep.get("ttl_h", 0), len(rep.get("expired") or []),
                            rep.get("local_mb", 0), rep.get("cos_prefix_tasks", 0),
                            rep.get("cos_orphan_keys", 0), rep.get("cos_orphan_mb", 0),
                            rep.get("cos_deleted", 0),
                            ("(" + rep["cos_error"] + ")") if rep.get("cos_error") else ""))
                _cleanup_log_append(rep)
        except Exception as e:                       # noqa: BLE001
            _log_srv("TTL 清理异常: %s" % e)
        time.sleep(max(60, CLEANUP_INTERVAL))


if os.environ.get("PATTERN_NO_THREADS"):
    _log_srv("PATTERN_NO_THREADS=1 → 诊断模式，跳过后台回填/清理线程")
else:
    # 🔴 首次归档必须在 TTL 清理之前**同步**跑完：重启那一刻正好满 72h 的记录，
    #    若先让 _cleanup_loop 扫一遍就被删了，永远进不了索引（2026-11-19 实测丢过 3 条 pt）。
    #    全站扫一遍只有几十毫秒，放启动同步里不影响起服务。
    try:
        _lib_index_sweep()
    except Exception as e:                           # noqa: BLE001
        _log_srv("素材库首次归档异常: %s" % e)
    threading.Thread(target=_backfill_cdn, daemon=True).start()
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    threading.Thread(target=_lib_index_loop, daemon=True).start()   # 素材库归档（STEP2，首次已同步跑）
