# -*- coding: utf-8 -*-
"""多图融合编辑工作台 · 本地服务"""
import os, sys, json, time, uuid, traceback, io, gc, threading, tempfile
from flask import Flask, request, jsonify, send_from_directory, Response, redirect

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from engine import planner, editor as E
from engine import aigen
from engine import parts as P
from engine import cosstore as COS
from PIL import Image

UP = os.path.join(HERE, "data", "uploads")
OUT = os.path.join(HERE, "data", "outputs")
QUEUE = os.path.join(HERE, "data", "ai_queue.jsonl")
SESSION = os.path.join(HERE, "data", "session.json")
AICONF = os.path.join(HERE, "data", "ai_config.json")
for d in (UP, OUT):
    os.makedirs(d, exist_ok=True)

DEFAULT_AI = dict(
    api_key="REPLACE_WITH_YOUR_API_KEY",
    base="https://api.apib.ai/v1",
    model="gpt-image-2",
    vision="gpt-5.4",
    size="1024x1024",
    n=2,
    timeout=300,
)


def load_ai_conf():
    c = dict(DEFAULT_AI)
    if os.path.exists(AICONF):
        try:
            c.update(json.load(open(AICONF, encoding="utf-8")))
        except Exception:
            pass
    return c


def save_ai_conf(c):
    cur = load_ai_conf()
    cur.update({k: v for k, v in c.items() if v not in (None, "")})
    json.dump(cur, open(AICONF, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return cur


def ai_client(conf=None):
    c = conf or load_ai_conf()
    return aigen.Client(api_key=c["api_key"], base=c.get("base") or DEFAULT_AI["base"],
                        model=c.get("model") or DEFAULT_AI["model"],
                        vision=c.get("vision") or DEFAULT_AI["vision"]), c

app = Flask(__name__, static_folder=None)


def _ok(d):
    return jsonify(d)


def _attach_paths(obj):
    """给返回结果里的每张图补上本地绝对路径（用于回填图位 / 删除）"""
    if isinstance(obj, dict):
        f = obj.get("file")
        if f and not obj.get("path"):
            obj["path"] = os.path.join(OUT, os.path.basename(f))
        for k in ("results", "version"):
            v = obj.get(k)
            if isinstance(v, list):
                for x in v:
                    _attach_paths(x)
            elif isinstance(v, dict):
                _attach_paths(v)
    return obj


@app.after_request
def _cors(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "*"
    r.headers["Access-Control-Allow-Methods"] = "*"
    # 页面/接口不缓存（迭代要即时生效、API 状态要新鲜）；3xx 跳转和带显式
    # 缓存头的图片响应放行。原来这里一刀切 no-store，把 /api/file 的图片
    # 缓存也覆盖掉 —— 同一张图每看一次都重新全量下载，1Mbps 出口反复卡 15s。
    if r.status_code < 300 and ("Cache-Control" not in r.headers
                                or r.headers.get("Content-Type", "").startswith("text/html")):
        r.headers["Cache-Control"] = "no-store"
    return r


@app.get("/")
def index():
    # 2026-10-11 雪碧定稿：分部位编辑页就是唯一主页（旧主工作台 index.html 已备份下线）
    return send_from_directory(os.path.join(HERE, "static"), "parts.html")


@app.get("/index.html")
def old_index():
    # 旧主工作台地址：直接给分部位工作台内容（不能 301——Caddy 剥前缀后 Location 会错指站根）
    return send_from_directory(os.path.join(HERE, "static"), "parts.html")


@app.get("/help.html")
def help_page():
    return send_from_directory(os.path.join(HERE, "static"), "help.html")


@app.get("/plan.html")
def plan_page():
    return send_from_directory(os.path.join(HERE, "static"), "plan.html")


@app.get("/parts.html")
def parts_page():
    return send_from_directory(os.path.join(HERE, "static"), "parts.html")


@app.get("/progress.html")
def progress_page():
    return send_from_directory(os.path.join(HERE, "static"), "progress.html")


@app.get("/plan7.html")
def plan7_page():
    return send_from_directory(os.path.join(HERE, "static"), "plan7.html")


@app.get("/api/healthz")
def healthz():
    # 给体检报告页做在线探针（Caddy 把 /api/* 透传到 8770，不剥前缀）
    return _ok(dict(ok=True, ts=time.time()))


@app.get("/health")
def health():
    return _ok(dict(ok=True, ts=time.time()))


def _post_upload_job(fp):
    """上传/导入完成后的重活（后台线程，不挡响应）：
    缩略图生成 + 异步转传 COS。主色/中性度统计前端不消费，不再计算。
    （4K 图全量解码 + LANCZOS 缩略图同步做要 ~1s+，是上传等待的一部分）"""
    try:
        im = E.imread(fp)
        thumb = im.copy()
        thumb.thumbnail((360, 360), E.LANCZOS)
        tfn = "thumb_" + os.path.basename(fp).rsplit(".", 1)[0] + ".png"
        thumb.save(os.path.join(os.path.dirname(fp), tfn))
        _cos_put_async("uploads", os.path.join(os.path.dirname(fp), tfn))
        im.close()
    except Exception:
        traceback.print_exc()
    _cos_put_async("uploads", fp)


@app.post("/api/upload")
def upload():
    slot = int(request.form.get("slot", 1))
    f = request.files.get("file")
    if not f:
        return _ok(dict(error="no file")), 400
    ext = os.path.splitext(f.filename or "")[1].lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        ext = ".png"
    fn = f"slot{slot}_{uuid.uuid4().hex[:8]}{ext}"
    fp = os.path.join(UP, fn)
    f.save(fp)
    # 只读图片 header 拿尺寸（不解码像素，亚毫秒级）；打不开在这里就报出去
    try:
        with Image.open(fp) as im:
            w, h = im.size
    except Exception as ex:
        try:
            os.remove(fp)
        except Exception:
            pass
        return _ok(dict(error="图片打不开：" + str(ex))), 400
    # 缩略图 / 转传 COS 全部后台做，响应立即返回 → 前端 blob 预览即刻落地
    threading.Thread(target=_post_upload_job, args=(fp,), daemon=True).start()
    return _ok(dict(slot=slot, path=fp, url=_pub_url("uploads", fn),
                    thumb=_pub_url("uploads", "thumb_" + fn.rsplit(".", 1)[0] + ".png"),
                    name=f.filename, info=dict(w=w, h=h)))


@app.post("/api/import")
def import_local():
    """把本机已有文件导入到某个图位（复制）"""
    data = request.get_json(force=True) or {}
    slot = int(data.get("slot", 1))
    src = data.get("path", "")
    if not os.path.exists(src):
        return _ok(dict(error="文件不存在: %s" % src)), 400
    import shutil
    ext = os.path.splitext(src)[1].lower() or ".png"
    fn = f"slot{slot}_{uuid.uuid4().hex[:8]}{ext}"
    fp = os.path.join(UP, fn)
    shutil.copy(src, fp)
    # 同 upload：header 读尺寸，重活后台
    try:
        with Image.open(fp) as im:
            w, h = im.size
    except Exception as ex:
        try:
            os.remove(fp)
        except Exception:
            pass
        return _ok(dict(error="图片打不开：" + str(ex))), 400
    threading.Thread(target=_post_upload_job, args=(fp,), daemon=True).start()
    return _ok(dict(slot=slot, path=fp,
                    url=_pub_url("uploads", fn),
                    thumb=_pub_url("uploads", "thumb_" + fn.rsplit(".", 1)[0] + ".png"),
                    name=os.path.basename(src), info=dict(w=w, h=h)))


@app.post("/api/run")
def run():
    data = request.get_json(force=True) or {}
    slots = data.get("slots", [])
    prompt = data.get("prompt", "")
    roles = data.get("roles")
    params = data.get("params") or {}
    try:
        res = planner.run(slots, prompt, OUT, roles=roles, params_override=params)
        res["prompt"] = prompt
        _attach_paths(res)
        json.dump(dict(slots=slots, roles=res["roles"], acts=res["acts"],
                       params=res["params"], prompt=prompt,
                       base_file=res.get("base_file")),
                  open(SESSION, "w", encoding="utf-8"), ensure_ascii=False)
        return _ok(res)
    except Exception as ex:
        traceback.print_exc()
        return _ok(dict(error=str(ex), trace=traceback.format_exc()[-1200:])), 500


def _pick_base(data, sess, ai=False):
    """本轮从哪张图起改 —— 优先级：用户显式选的基准 > 前端回传 > session 里的底图。

    修 bug：以前这里写反了（session 的 _base.png 永远优先），导致用户点了
    「设为基准继续改」之后聊天修改根本没用上他选的那张图，出来的还是原图重跑。
    """
    cands = []
    bp = data.get("base_pick")
    if bp:
        cands.append(os.path.join(OUT, os.path.basename(bp)))
    bf = data.get("base_file")
    if bf:
        cands.append(bf)
    for k in (("ai_base", "base_file") if ai else ("base_file", "ai_base")):
        v = sess.get(k)
        if v:
            cands.append(v)
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


@app.post("/api/chat")
def chat():
    """两阶段：phase=plan 只出方案不跑图；phase=exec 拿着方案执行。"""
    data = request.get_json(force=True) or {}
    phase = data.get("phase") or "plan"
    msg = data.get("message", "")
    slots = data.get("slots") or []
    roles = data.get("roles")
    acts = data.get("acts")
    params = data.get("params") or {}
    engine = data.get("engine", "prog")
    is_ai = engine == "ai"
    try:
        sess = json.load(open(SESSION, encoding="utf-8")) if os.path.exists(SESSION) else {}
        # 前端没回传参考图时，退回上一轮的参考图（聊天迭代不该要求用户重新传一遍）
        if not slots:
            slots = [s for s in (sess.get("slots") or []) if s.get("path") and os.path.isfile(s["path"])]
        base_file = _pick_base(data, sess, ai=is_ai)
        bn = os.path.basename(base_file) if base_file else ""
        if not base_file:
            base_label = "还没有底图，本轮按参考图整张重做"
        elif bn == "_base.png":
            base_label = "原始底图（从原图重跑，不会越改越糊）"
        else:
            base_label = bn + "（你选的基准图，本轮在它上面叠加）"

        # ---------------- AI 引擎：先给方案，确认后才出图（省钱省时间） ----------------
        if is_ai:
            used = [s for s in slots if s.get("path")]
            model = data.get("model") or ""
            n = data.get("n") or 1
            size = data.get("size") or ""
            vc = data.get("vision_check", True)
            if phase == "plan":
                steps = [
                    dict(no=1, act="ai_base", title="本轮基准图", detail=base_label,
                         why=("严格以这张为基础，只改下面写到的地方，其余一律保持不变。"
                              if base_file else "没选基准图，只按参考图重画。")),
                    dict(no=2, act="ai_edit", title="本轮修改要求", detail=msg,
                         why="AI 只会照这句改，写得越具体越准。"),
                    dict(no=3, act="ai_gen", title="调用出图",
                         detail="模型 %s，出 %s 张，尺寸 %s" % (model or "默认", n, size or "自动"),
                         why="AI 出图要钱要时间，所以先给你方案，你确认了才跑。"),
                    dict(no=4, act="ai_verify", title="AI 自检",
                         detail=("出完图让视觉模型回看一遍，把问题写进对话。" if vc else "本轮不自检。"),
                         why="免得跑偏了还不知道。"),
                ]
                return _ok(dict(reply=dict(kind="plan", engine="ai",
                                           text="AI 重绘方案 · 基准：" + base_label,
                                           plan=steps, acts=[], params=params,
                                           base_file=base_file, base_label=base_label,
                                           message=msg, hits=[])))
            payload = dict(slots=used, prompt=(sess.get("prompt") or "") + "\n本轮修改：" + msg,
                           extra_images=[base_file] if base_file else [],
                           extra=("额外传入的图片（排在参考图之前）是上一版结果，请严格以它为基础做本轮修改，"
                                  "只允许改本轮要求的地方，其余一律保持不变。" if base_file else ""),
                           roles=roles or sess.get("roles"), acts=acts or sess.get("acts"),
                           vision_check=vc, n=n)
            if model:
                payload["model"] = model
            if size:
                payload["size"] = size
            r = do_ai_gen(payload)
            if isinstance(r, tuple) and r[1] != 200:
                return _ok(dict(error=r[0].get("error"), trace=r[0].get("trace"))), 500
            res = r[0] if isinstance(r, tuple) else r
            v = (res.get("results") or [None])[0]
            if not v:
                return _ok(dict(error="AI 未返回图片")), 500
            sess["ai_base"] = os.path.join(OUT, v["file"])
            json.dump(sess, open(SESSION, "w", encoding="utf-8"), ensure_ascii=False)
            return _ok(dict(reply=_attach_paths(dict(
                kind="ai", engine="ai", text="已用 AI 重绘（" + res.get("model", "") + "）",
                version=v, vision=v.get("vision"))),
                params=sess.get("params") or {}, hits=[]))

        # ---------------- 程序引擎 ----------------
        roles = roles or sess.get("roles")
        acts = acts or sess.get("acts") or ["material_replace", "pattern_transfer"]
        base_params = dict(planner.DEFAULT_PARAMS)
        base_params.update(sess.get("params") or {})
        base_params.update(params)
        if phase == "exec" and params:
            # 执行阶段用回传的参数（plan 阶段已经调过一次了，不能再调一遍，否则「再密一点」会被乘两次）
            newp = dict(base_params)
            hits = data.get("hits") or []
        else:
            newp, hits = planner.refine_params(base_params, msg)
            if not hits:
                a2, _ = planner.parse_intent(msg)
                if a2:
                    acts = a2
        acts_iter = [a for a in acts if a != "set_shape"] or list(acts)
        plan = planner.make_plan(acts_iter, slots, roles, newp)

        if phase == "plan":
            head = "程序引擎方案 · 基准：" + base_label
            head += ("\n命中微调：" + "、".join(hits[:4]) if hits
                     else "\n没命中微调词，按整句话重新理解 → " + " → ".join(acts_iter))
            return _ok(dict(reply=dict(kind="plan", engine="prog", text=head, plan=plan,
                                       acts=acts_iter, params=newp, base_file=base_file,
                                       base_label=base_label, message=msg, hits=hits)))

        # ---- exec：按方案从基准图重跑，出 1~3 个版本并打分 ----
        if base_file and os.path.isfile(base_file):
            res = planner.iterative(base_file, slots, roles, acts_iter, newp, OUT, "迭代版")
            reply = dict(kind="batch", engine="prog",
                         text="已按方案执行（基准：" + base_label + "）：",
                         plan=res["plan"], results=res["results"], roles=res["roles"],
                         acts=res["acts"], params=res["params"], base_file=base_file)
        else:
            res = planner.run(slots, msg, OUT, roles=roles, params_override=newp)
            res["prompt"] = msg
            reply = dict(kind="batch", engine="prog", text="按新需求重新生成：",
                         plan=res["plan"], results=res["results"], roles=res["roles"],
                         acts=res["acts"], params=res["params"], base_file=res.get("base_file"))
            base_file = res.get("base_file")
        json.dump(dict(slots=slots, roles=reply["roles"], acts=reply["acts"],
                       params=reply["params"], prompt=msg, base_file=base_file),
                  open(SESSION, "w", encoding="utf-8"), ensure_ascii=False)
        return _ok(dict(reply=_attach_paths(reply), params=reply["params"], hits=hits))
    except Exception as ex:
        traceback.print_exc()
        return _ok(dict(error=str(ex), trace=traceback.format_exc()[-1200:])), 500


@app.post("/api/ai_queue")
def ai_queue():
    """需要 AI（ImageGen）处理的任务入队，由 AI 在对话中读取执行"""
    data = request.get_json(force=True) or {}
    data["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(QUEUE, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")
    return _ok(dict(queued=True, file=QUEUE))


@app.get("/api/ai_queue")
def ai_queue_list():
    if not os.path.exists(QUEUE):
        return _ok(dict(items=[]))
    items = [json.loads(l) for l in open(QUEUE, encoding="utf-8") if l.strip()]
    return _ok(dict(items=items[-20:]))


# ---------------- AI 引擎（apib.ai · gpt-image-2 等） ----------------
VISION_Q = ("你是电商产品图质检员。用中文简要回答：1) 壳体材质与颜色 2) 卡通图案是否与参考图一致、位置在哪 "
            "3) 底部有没有托架/底座 4) 摄像头区是否干净无图案 5) 背景是否纯净 6) 整体是否可直接上架。"
            "最后给一句最有价值的改进建议。")


@app.get("/api/ai_config")
def ai_config_get():
    c = load_ai_conf()
    k = c.get("api_key") or ""
    c = dict(c)
    c["api_key"] = (k[:6] + "..." + k[-4:]) if len(k) > 12 else k
    return _ok(dict(conf=c))


@app.post("/api/ai_config")
def ai_config_post():
    data = request.get_json(force=True) or {}
    return _ok(dict(ok=True, conf=save_ai_conf(data)))


# ================= 腾讯云 COS 存储（图片上传/下载/存放经过 COS） =================
def _cos_put(kind, fp):
    """本地文件传 COS（key=<prefix>/<kind>/<文件名>）。成功后写 sidecar 标记
    （<文件名>.cosok）并返回公网 URL；sidecar 已存在则直接给 URL 不重复传
    （单环节重跑/复用存档场景）。失败返回 None，调用方回退本地地址。"""
    if not os.path.isfile(fp):
        return None
    if os.path.isfile(fp + ".cosok") and COS.ok():
        u = COS.url_for(kind, os.path.basename(fp))
        if u:
            return u
    u = COS.put(fp, kind)
    if u:
        try:
            open(fp + ".cosok", "w").close()
        except Exception:
            pass
    return u


def _pub_url(kind, fn):
    """对外 URL：文件已传过 COS（有 sidecar）且配置可用 → COS 公网直链；
    否则回退本地 /api/file/...（历史老图、未配置 COS、上传失败都能打开）。"""
    try:
        fp = os.path.join({"uploads": UP, "outputs": OUT}[kind], fn)
        if os.path.isfile(fp + ".cosok") and COS.ok():
            u = COS.url_for(kind, fn)
            if u:
                return u
    except Exception:
        pass
    return "/api/file/{0}/{1}".format(kind, fn)


def _cos_put_async(kind, fp):
    """上传/导入场景：后台线程转传 COS，不阻塞响应。
    （服务器出口 ~1Mbps，同步转传 3MB 要 ~25s；后台传完写 sidecar，
    之后下载/列表自然变 COS 直链。失败只打日志回退本地，不影响使用。）"""
    if not COS.ok():
        return
    def _job():
        try:
            _cos_put(kind, fp)
        except Exception:
            traceback.print_exc()
    threading.Thread(target=_job, daemon=True).start()


@app.get("/api/cos_conf")
def cos_conf_get():
    c = COS.load_conf()
    # SecretId 本身不是密钥（相当于账号 ID），原样回显方便修改；
    # SecretKey 永不回显，面板留空＝沿用已保存的值。
    return _ok(dict(conf=dict(
        enabled=bool(c.get("enabled")),
        region=c.get("region") or "",
        bucket=c.get("bucket") or "",
        prefix=c.get("prefix") or "",
        base=c.get("base") or "",
        secret_id=c.get("secret_id") or "",
        has_key=bool(c.get("secret_key")),
        ready=COS.ok())))


@app.post("/api/cos_conf")
def cos_conf_post():
    """保存 COS 配置并自动探针验证（PUT+DELETE 一个小文件）。
    secret_key 留空时保留旧值（面板不回显密钥，改其他字段不用重填）。"""
    data = request.get_json(force=True) or {}
    keep = COS.load_conf()
    c = dict(
        enabled=bool(data.get("enabled", True)),
        region=(data.get("region") or "").strip(),
        bucket=(data.get("bucket") or "").strip(),
        prefix=(data.get("prefix") or "").strip(),
        base=(data.get("base") or "").strip(),
        secret_id=(data.get("secret_id") or "").strip(),
        secret_key=(data.get("secret_key") or "").strip() or keep.get("secret_key") or "",
    )
    if c["enabled"] and not (c["bucket"] and c["region"] and c["secret_id"] and c["secret_key"]):
        return _ok(dict(error="配置不完整：region / bucket / SecretId / SecretKey 都要填")), 400
    COS.save_conf(c)
    if not c["enabled"]:
        return _ok(dict(ok=True, saved=True, tested=False, msg="已保存（未启用 COS）"))
    okflag, msg = COS.probe()
    return _ok(dict(ok=okflag, saved=True, tested=True, msg=msg,
                    error=None if okflag else msg))


_MCACHE = {"ts": 0.0, "items": []}


@app.get("/api/ai_models")
def ai_models():
    if _MCACHE["items"] and time.time() - _MCACHE["ts"] < 900:
        return _ok(dict(items=_MCACHE["items"], cached=True))
    try:
        cl, _ = ai_client()
        ids = cl.list_models(True)
        _MCACHE.update(ts=time.time(), items=ids)
        return _ok(dict(items=ids, cached=False))
    except Exception as ex:
        return _ok(dict(items=_MCACHE["items"], error=str(ex)))


@app.post("/api/ai_build_prompt")
def ai_build_prompt():
    """只生成 AI 出图提示词（不花钱），先在界面上给用户看/改"""
    data = request.get_json(force=True) or {}
    slots = data.get("slots", [])
    prompt = data.get("prompt", "")
    roles = data.get("roles") or planner.guess_roles(slots, prompt)
    acts, _ = planner.parse_intent(prompt)
    params = dict(planner.DEFAULT_PARAMS)
    params.update(data.get("params") or {})
    plan = planner.make_plan(acts, slots, roles, params)
    txt = aigen.build_fuse_prompt(prompt, slots, roles, acts, plan, data.get("extra", ""))
    return _ok(dict(prompt=txt, roles=roles, acts=acts, plan=plan))


def do_ai_gen(data):
    """调用 apib.ai 出图：多参考图 + 提示词 → 异步任务 → 落盘 + 指标 + 视觉自检"""
    try:
        conf = load_ai_conf()
        for k in ("model", "size", "vision", "api_key", "base"):
            if data.get(k):
                conf[k] = data[k]
        if data.get("n"):
            conf["n"] = int(data["n"])
        cl, _ = ai_client(conf)
        slots = [s for s in (data.get("slots") or []) if s.get("path")]
        paths = []
        for p in (data.get("extra_images") or []):
            if p and os.path.exists(p) and p not in paths:
                paths.append(p)
        for s in slots:
            p = s.get("path")
            if p and os.path.exists(p) and p not in paths:
                paths.append(p)
        if not paths:
            return _ok(dict(error="没有可用参考图")), 400

        prompt = data.get("prompt_text") or ""
        roles = data.get("roles")
        acts = data.get("acts")
        if not prompt:
            roles = roles or planner.guess_roles(slots, data.get("prompt", ""))
            acts = acts or planner.parse_intent(data.get("prompt", ""))[0]
            params = dict(planner.DEFAULT_PARAMS)
            params.update(data.get("params") or {})
            plan = planner.make_plan(acts, slots, roles, params)
            prompt = aigen.build_fuse_prompt(data.get("prompt", ""), slots, roles, acts,
                                             plan, data.get("extra", ""))
        else:
            plan = data.get("plan") or []

        uris = [aigen.data_uri(p) for p in paths]
        ts = time.strftime("%H%M%S")
        n = max(1, min(4, int(conf.get("n", 1))))
        items, raw = cl.generate(prompt, uris, OUT, f"{ts}_AI",
                                 size=conf.get("size") or "1024x1024", n=n,
                                 timeout=int(conf.get("timeout", 300)))
        results = []
        for it in items:
            fp = it["path"]
            im = E.imread(fp)
            body = E.find_body(im)
            cam = E.find_camera(im, body)
            try:
                m = E.metrics(im, body, cam)
                m["body"] = list(body)
                m["cam"] = list(cam) if cam else None
                score = E.grade(m, "mixed")
            except Exception as ex:
                m, score = dict(error=str(ex)), 0
            fn = os.path.basename(fp)
            prev = im.copy()
            prev.thumbnail((420, 420), Image.Resampling.LANCZOS)
            prev.save(os.path.join(OUT, "prev_" + fn))
            url = _cos_put("outputs", fp) or f"/api/file/outputs/{fn}"
            preview = _cos_put("outputs", os.path.join(OUT, "prev_" + fn)) or f"/api/file/outputs/prev_{fn}"
            r = dict(name=os.path.splitext(fn)[0], file=fn, path=fp,
                     url=url,
                     preview=preview, metrics=m, score=score,
                     ai=True, model=conf.get("model"), remote=it.get("remote"))
            if data.get("vision_check"):
                r["vision"] = cl.describe(fp, data.get("vision_question") or VISION_Q)
            results.append(r)
        results.sort(key=lambda r: -r["score"])

        sess = {}
        if os.path.exists(SESSION):
            try:
                sess = json.load(open(SESSION, encoding="utf-8"))
            except Exception:
                sess = {}
        sess.update(dict(slots=slots, roles=roles, acts=acts, prompt=data.get("prompt", ""),
                         ai_base=os.path.join(OUT, results[0]["file"]) if results else None,
                         ai_model=conf.get("model")))
        if not sess.get("base_file"):
            sess["base_file"] = None
        json.dump(sess, open(SESSION, "w", encoding="utf-8"), ensure_ascii=False)
        return dict(results=results, plan=plan, prompt=prompt, roles=roles, acts=acts,
                    model=conf.get("model"), ai_base=sess.get("ai_base"))
    except Exception as ex:
        traceback.print_exc()
        return dict(error=str(ex), trace=traceback.format_exc()[-1500:]), 500


@app.post("/api/ai_gen")
def ai_gen():
    data = request.get_json(force=True) or {}
    r = do_ai_gen(data)
    if isinstance(r, tuple):
        return _ok(r[0]), r[1]
    return _ok(r)


@app.post("/api/ai_vision")
def ai_vision():
    """让视觉模型看一张图并回答（用于结果自检）"""
    data = request.get_json(force=True) or {}
    p = data.get("path", "")
    if not os.path.exists(p):
        cand = os.path.join(OUT, os.path.basename(p))
        p = cand if os.path.exists(cand) else p
    if not os.path.exists(p):
        return _ok(dict(error="文件不存在: %s" % p)), 400
    try:
        cl, conf = ai_client()
        txt = cl.describe(p, data.get("question") or VISION_Q,
                          model=data.get("model") or conf.get("vision"))
        return _ok(dict(text=txt))
    except Exception as ex:
        return _ok(dict(error=str(ex))), 500


@app.get("/api/presets")
def presets():
    """素材库：扫描项目已有素材/产出，供一键载入"""
    roots = [
        os.path.join(HERE, "..", "素材原图"),
        os.path.join(HERE, "..", "产出图片"),
        UP,
    ]
    items = []
    for r in roots:
        r = os.path.abspath(r)
        if not os.path.isdir(r):
            continue
        for f in sorted(os.listdir(r)):
            if not f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                continue
            if f.startswith("thumb_") or f.startswith("prev_"):
                continue
            items.append(dict(name=f, path=os.path.join(r, f),
                              group=os.path.basename(r),
                              thumb="/api/thumb?p=" + os.path.join(r, f)))
    return _ok(dict(items=items[:60]))


@app.get("/api/thumb")
def thumb():
    """任意白名单内图片的缩略图"""
    p = os.path.abspath(request.args.get("p", ""))
    allow = [os.path.abspath(os.path.join(HERE, "..", "素材原图")),
             os.path.abspath(os.path.join(HERE, "..", "产出图片")), UP, OUT]
    if not any(p.startswith(a) for a in allow) or not os.path.exists(p):
        return "denied", 403
    try:
        im = E.imread(p)
        im.thumbnail((300, 300), E.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return Response(buf.getvalue(), mimetype="image/png")
    except Exception as ex:
        return str(ex), 500


@app.get("/api/file/<kind>/<name>")
def file_(kind, name):
    d = {"uploads": UP, "outputs": OUT}.get(kind)
    if not d:
        return "bad kind", 404
    fp = os.path.abspath(os.path.join(d, os.path.basename(name)))
    if not fp.startswith(os.path.abspath(d) + os.sep) or not os.path.isfile(fp):
        return "not found", 404
    # 已传过 COS（sidecar 在）且本地文件此后没被改过 → 302 直链。
    # 服务器出口仅 ~1Mbps，2MB 图本地回传要 ~16s，COS 直链亚秒级。
    # sidecar mtime 必须 >= 文件 mtime 才跳：固定名文件（_base.png）被
    # 覆盖更新后 sidecar 还是旧的，此时走本地，避免 302 到 COS 旧版本。
    sc = fp + ".cosok"
    try:
        if (COS.ok() and os.path.isfile(sc)
                and os.path.getmtime(sc) >= os.path.getmtime(fp) - 1):
            u = COS.url_for(kind, os.path.basename(fp))
            if u:
                r = redirect(u, code=302)
                # 302 也缓存：同 URL 后续浏览器直接打 COS，零服务器往返。
                # 文件名带 uuid 不会重名；带 ?t= 的请求 URL 变化天然绕过。
                r.headers["Cache-Control"] = "public, max-age=3600"
                return r
    except Exception:
        pass
    # 一次性读进内存再返回，而不是「边读边发」：
    # Windows 上边读边发会一直占着文件句柄，紧接着的删除就会失败
    # （表现：清空后原图删了、prev_ 缩略图留下一堆）
    try:
        with open(fp, "rb") as fh:
            data = fh.read()
    except Exception:
        return "read error", 500
    ext = os.path.splitext(fp)[1].lower()
    mt = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
          ".webp": "image/webp"}.get(ext, "application/octet-stream")
    # 原来是 no-store（每次看图都全量重下，1Mbps 出口下等于反复卡 15s）。
    # 改 60s 缓存：图片内容不变（uuid 文件名），更新场景由前端 ?t= 时间戳破缓存。
    return Response(data, mimetype=mt, headers={"Cache-Control": "public, max-age=60"})


@app.get("/api/outputs")
def outputs():
    fs = sorted([f for f in os.listdir(OUT)
                 if f.startswith("prev_") and not f.endswith(".cosok")],
                key=lambda x: -os.path.getmtime(os.path.join(OUT, x)))
    items = []
    for f in fs[:80]:
        fn = f[5:]
        # 原图没了、只剩缩略图（删一半失败时会这样）→ 顺手清掉孤儿，避免图廊出现打不开的幽灵图
        if not os.path.isfile(os.path.join(OUT, fn)):
            try:
                os.remove(os.path.join(OUT, f))
            except Exception:
                pass
            try:
                os.remove(os.path.join(OUT, f + ".cosok"))
            except Exception:
                pass
            continue
        items.append(dict(name=os.path.splitext(fn)[0], file=fn,
                          path=os.path.join(OUT, fn),
                          preview=_pub_url("outputs", f),
                          url=_pub_url("outputs", fn),
                          ts=time.strftime("%H:%M:%S",
                                           time.localtime(os.path.getmtime(os.path.join(OUT, f))))))
    return _ok(dict(items=items))


def _out_path(name):
    name = os.path.basename(name or "")
    fp = os.path.abspath(os.path.join(OUT, name))
    if not fp.startswith(os.path.abspath(OUT) + os.sep):
        return None
    return fp


def _rm(fp, tries=3, gap=0.05):
    """Windows 上文件可能被暂驻句柄占用 → 带重试的删除。
    注意：这里千万不能调 gc.collect()，那是纯 Python 的垃圾回收，
    对 Windows 的文件句柄毫无帮助，却要花 0.3~0.5s —— 清空 20 张就这么被拖到 10 秒。"""
    if not os.path.isfile(fp):
        return True, ""
    err = ""
    for attempt in range(tries):
        try:
            os.remove(fp)
            return True, ""
        except PermissionError as ex:
            err = str(ex) or "文件被占用（PermissionError）"
        except Exception as ex:
            err = str(ex)
        if attempt == 0:
            try:
                os.chmod(fp, 0o666)   # 只读属性会让删除失败，先清掉
            except Exception:
                pass
        time.sleep(gap)
    return False, err


def _trash_dirs():
    """优先系统临时目录：实测本机在工作区里 os.remove 要 0.56s/个（被文件监控/杀软盯），
    临时目录只要 0.045s/个；而 os.rename 只要 0.003s —— 所以先改名搬走，再慢慢删。"""
    cands = []
    try:
        cands.append(os.path.join(tempfile.gettempdir(), "duotu_trash"))
    except Exception:
        pass
    cands.append(os.path.join(OUT, "_trash"))
    return cands


def _trash(fp):
    """把文件改名挪进回收站目录。改名是元数据操作（毫秒级），搬走后页面立刻就看不到它了，
    真正的删除交给后台线程慢慢做，绝不让前端干等。"""
    for td in _trash_dirs():
        try:
            os.makedirs(td, exist_ok=True)
            dst = os.path.join(td, uuid.uuid4().hex[:8] + "_" + os.path.basename(fp))
            os.rename(fp, dst)
            return dst
        except Exception:
            continue
    return None


def _rm_later(paths):
    """后台慢慢删：文件可能被浏览器/杀软短暂占住，不值得让前端干等。
    paths 里既可以是绝对路经（_trash 里的），也可以是文件名（outputs 里的）。"""
    for p in list(paths):
        fp = p if os.path.isabs(p) else (_out_path(p) or "")
        if not fp:
            continue
        for _ in range(30):
            if not os.path.isfile(fp):
                break
            ok, _e = _rm(fp, tries=1, gap=0)
            if ok:
                break
            time.sleep(0.4)


@app.post("/api/delete_output")
def delete_output():
    """删除单张产出图（连同缩略图）；all=true 时清空全部"""
    data = request.get_json(force=True) or {}
    killed, failed, trashed = [], [], []
    t0 = time.time()
    try:
        targets = []
        if data.get("all"):
            for f in os.listdir(OUT):
                if f in ("_base.png", "prev__base.png"):
                    continue
                if f.startswith("prev_") or f.endswith(".cosok") or \
                        f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    targets.append(f)
        else:
            raw = data.get("file") or data.get("path") or ""
            name = os.path.basename(raw)
            if not name:
                return _ok(dict(error="缺少文件名")), 400
            targets = [name, "prev_" + name]
        for n in targets:
            fp = _out_path(n)
            if not fp:
                failed.append(dict(name=n, error="非法路径"))
                continue
            if not os.path.isfile(fp):
                continue
            # 同步删 COS 对象 + sidecar 标记（COS 失败只打日志，不阻塞本地删除）
            COS.delete("outputs", n)
            if os.path.isfile(fp + ".cosok"):
                _rm(fp + ".cosok")
            # 先改名搬走（毫秒级，页面立刻看不到）；改名失败（被浏览器占住）才硬删
            dst = _trash(fp)
            if dst:
                trashed.append(dst)
                killed.append(n)
                continue
            ok, err = _rm(fp, tries=2, gap=0.03)
            (killed.append(n) if ok else failed.append(dict(name=n, error=err)))
        # 搬进回收站的那批交给后台线程慢慢真删（本机删文件慢，绝不能堵着前端）
        if trashed:
            threading.Thread(target=_rm_later, args=(trashed,), daemon=True).start()
        names = set(os.path.basename(k) for k in killed)
        names |= set(os.path.basename(f["name"]) for f in failed)
        # 清掉 session 里指向被删文件的引用
        if os.path.exists(SESSION):
            try:
                sess = json.load(open(SESSION, encoding="utf-8"))
                for k in ("ai_base", "base_file"):
                    v = sess.get(k)
                    if v and os.path.basename(v) in names:
                        sess[k] = None
                json.dump(sess, open(SESSION, "w", encoding="utf-8"), ensure_ascii=False)
            except Exception:
                pass
        # 少数删不掉的（被占用）交给后台线程慢慢重试，不让前端干等
        pending = [f["name"] for f in failed]
        if pending:
            threading.Thread(target=_rm_later, args=(pending,), daemon=True).start()
        return _ok(dict(ok=not failed, deleted=killed, count=len(killed), failed=failed,
                        pending=pending, ms=int((time.time() - t0) * 1000),
                        error=(failed[0]["error"] if failed else None)))
    except Exception as ex:
        traceback.print_exc()
        return _ok(dict(error=str(ex))), 500


# ================= 分部位编辑（部位识别 / 提示词库 / 分层管线 / 融合） =================
def _resolve_img(p):
    """主图/参考图路径兜底：相对 outputs、uploads 目录也能找到"""
    if not p:
        return None
    if os.path.isfile(p):
        return p
    for d in (OUT, UP):
        g = os.path.join(d, os.path.basename(p))
        if os.path.isfile(g):
            return g
    return None


def _img_info(fp, with_prev=True):
    """parts 管线所有产出图的统一出口：落盘即双写 COS（已传过不重复），URL 优先 COS。"""
    fp = os.path.abspath(fp)
    fn = os.path.basename(fp)
    d = dict(file=fn, path=fp,
             url=_cos_put("outputs", fp) or f"/api/file/outputs/{fn}")
    if with_prev:
        pv = os.path.join(os.path.dirname(fp), "prev_" + fn)
        if os.path.isfile(pv):
            d["preview"] = _cos_put("outputs", pv) or f"/api/file/outputs/prev_{fn}"
        else:
            d["preview"] = f"/api/file/outputs/prev_{fn}"
    return d


def _make_prev(fp):
    try:
        im = Image.open(fp)
        im.thumbnail((420, 420), Image.Resampling.LANCZOS)
        im.save(os.path.join(OUT, "prev_" + os.path.basename(fp)))
    except Exception:
        pass


def _act_zh(part, action):
    return dict(P.ACTIONS.get(part, [])).get(action, action or "")


def _parse_refs(s):
    """step 里的 refs 升级为带用途标签：[{path, tag}]（tag: pattern/material/look）。
    兼容旧格式（纯路径字符串，视为 look）。返回 [(path, tag), ...] 去重。"""
    out = []
    for r in (s.get("refs") or []):
        if isinstance(r, dict):
            rp, tag = _resolve_img(r.get("path")), (r.get("tag") or "look")
        else:
            rp, tag = _resolve_img(r), "look"
        if rp and (rp, tag) not in out:
            out.append((rp, tag))
    return out


def _ref_header(refs, tail=None):
    """拼「第N张图是什么用途」的编号说明行 + 严格复刻约束（禁止 AI 联想修改）。

    每张参考图按 tag 注入 STRICT_REFS 硬约束（图案=1:1 复刻禁重新设计 /
    材质=1:1 复刻禁替换近似材质 / 外观=原样复刻 / 兄弟锚点=同款同材质只调透视），
    末尾再加全局兜底句。tail 可传 SERIES_TAIL（产品组模式）替换默认的 STRICT_TAIL。
    约束在 header 里，所以用户自定义提示词的环节也照样生效。"""
    bits = ["Image 1 is the product photo to edit."]
    for j, (_p, tag) in enumerate(refs):
        bits.append(f"Image {j + 2} is {P.REF_TAGS.get(tag, 'a reference image')}.")
        strict = P.STRICT_REFS.get(tag)
        if strict:
            bits.append(strict.format(n=j + 2))
    if refs:
        bits.append(tail or P.STRICT_TAIL)
    return " ".join(bits)


@app.get("/api/parts/prompts")
def parts_prompts():
    return _ok(dict(lib=P.PROMPT_LIB, actions=P.ACTIONS, parts=P.PARTS))


@app.post("/api/parts/detect")
def parts_detect():
    """视觉模型识别部位 bbox（默认 gpt-5.4，之前实测过格式可靠）"""
    data = request.get_json(force=True) or {}
    img = _resolve_img(data.get("image"))
    if not img:
        return _ok(dict(error="请先上传主图")), 400
    cl, conf = ai_client()
    vm = data.get("vision_model") or conf.get("vision") or aigen.DEFAULT_VISION
    t0 = time.time()
    # 网络瞬断兜底重试一次（间隔 3s）
    try:
        res = P.detect_parts(cl, img, vm)
    except Exception as ex:
        time.sleep(3)
        try:
            res = P.detect_parts(cl, img, vm)
        except Exception as ex2:
            return _ok(dict(error=f"识别失败: {ex2}"))
    return _ok(dict(image=img, parts=res["parts"], raw=res["raw"],
                    vision_model=vm, elapsed=round(time.time() - t0, 1)))


@app.post("/api/parts/group_spec")
def parts_group_spec():
    """产品组规格卡提取：视觉模型对基准图跑 SPEC_Q，产出 4 行产品定义。
    前端拿到后可编辑再锁定，之后组执行/单张重做都注入同一份（文本锁，不漂移）。"""
    data = request.get_json(force=True) or {}
    img = _resolve_img(data.get("image"))
    if not img:
        return _ok(dict(error="请先上传主图并指定基准图")), 400
    cl, conf = ai_client()
    vm = data.get("vision_model") or conf.get("vision") or aigen.DEFAULT_VISION
    t0 = time.time()
    try:
        res = P.extract_spec(cl, img, vm)
    except Exception as ex:
        return _ok(dict(error=f"规格卡提取失败: {ex}"))
    return _ok(dict(image=img, spec=res["spec"], raw=res["raw"],
                    vision_model=vm, elapsed=round(time.time() - t0, 1)))


def _parts_pipeline(data, progress=None):
    """分部位执行管线主体（同步逻辑，/api/parts/run 与后台任务共用）。
    mode=layered：逐部位出图 + bbox 外像素用原图贴回（保底，区域外一个像素都不动）
    mode=fusion ：所有部位指令拼一条，整图一次出图（整体最协调，但区域外不保证不变）
    from_index  ：从第几步开始重跑（单环节重改），之前的步用 prev_outputs 存档直接复用
    progress(msg, steps_so_far)：后台任务的进度落盘回调（None=同步调用，不落盘）
    """
    # 注意：请求里的 base_img 是主图；AI 接口地址的配置键叫 base，两者不能撞名
    base = _resolve_img(data.get("base_img") or data.get("image"))
    if not base:
        return _ok(dict(error="请先上传主图")), 400
    steps = [s for s in (data.get("steps") or []) if s.get("action") and s["action"] != "none"]
    if not steps:
        return _ok(dict(error="没有要执行的修改：先把某个部位的操作选成非「不动」")), 400
    mode = data.get("mode") or "layered"
    # 产品组模式扩展：spec=规格卡文本（文本锁）；sibling=兄弟锚点图（视觉锁）。
    # 两者任一存在即视为组模式 → 参考图说明的兜底句换 SERIES_TAIL（强调全系列一致）。
    # features=人工特征标注（出厂档案，权重最高）：幂等并入规格卡文本最前面。
    # 换材质 / 贴回任务：规格卡描述的是原图旧壳，与目标锚定冲突 → 标注作废再并入特征
    _swap = any(s["part"] == "phone_case" and s.get("action") in
                ("material", "material_pick", "pattern_material", "pattern_restore")
                for s in steps)
    spec = P.supersede_old_material(data.get("spec")) if _swap else data.get("spec")
    # 动到手机壳/摄像头区 → 注入摄像头框一致性锁（用户没锚定框色时给默认值）
    _cam_lock = any(s["part"] in ("phone_case", "camera_area") for s in steps)
    spec = P.merge_features(spec, data.get("features"), cam_lock=_cam_lock)
    sibling = _resolve_img(data.get("sibling"))
    series = bool(spec or sibling)
    conf = load_ai_conf()
    for k in ("model", "vision", "api_key"):
        if data.get(k):
            conf[k] = data[k]
    if not (conf.get("api_key") or "").strip():
        return _ok(dict(error="还没配置 AI 接口 Key（先在工作台首页 AI 面板里填一次，两边共用）")), 400
    try:
        cl, _ = ai_client(conf)
    except Exception as ex:
        return _ok(dict(error=f"AI 客户端初始化失败: {ex}"))
    size = data.get("size") or P.gen_size_for(base)
    timeout = int(conf.get("timeout", 300))
    ts = time.strftime("%H%M%S")
    t_all = time.time()

    # ---------- 整图融合：一条提示词 + 全部参考图，一次出图 ----------
    if mode == "fusion":
        refs, ref_tags = [], []
        for s in steps:
            for (rp, tag) in _parse_refs(s):
                if rp not in refs:
                    refs.append(rp)
                    ref_tags.append(tag)
        # 组模式：兄弟锚点图作为附加参考传入（视觉锁）
        if sibling and sibling not in refs:
            refs.append(sibling)
            ref_tags.append("sibling")
        zh_bits = []
        for s in steps:
            zh = P.PART_MAP.get(s["part"], {}).get("zh", s["part"])
            # 手机壳「换图案+材质」在融合模式下也拆成两条编号指令（材质先、图案后）
            if s["part"] == "phone_case" and s.get("action") == "pattern_material":
                mp_ = (s.get("mat_prompt") or "").strip() or P.render_prompt("phone_case", "material", "")
                zh_bits.append(f"{len(zh_bits) + 1}. 【{zh}·{_act_zh('phone_case', 'material')}】"
                               f"{mp_.strip()} {P.CAM_FRAME_LOCK}")
                # 图案步：用户自己写的优先（原样发送），否则系统模板+补充描述
                pat_custom = (s.get("pat_prompt") or "").strip()
                p_bit = pat_custom or P.render_prompt("phone_case", "pattern", "")
                d_ = (s.get("desc") or "").strip()
                if d_ and not pat_custom:
                    p_bit += f" Extra requirement for the new pattern: {d_}."
                zh_bits.append(f"{len(zh_bits) + 1}. 【{zh}·{_act_zh('phone_case', 'pattern')}】{p_bit}")
            else:
                # 前端总会带 prompt，但单张重做 / 老任务回放可能没有 → 模板兜底，避免指令体为空
                pr = (s.get("prompt") or "").strip() or P.render_prompt(s["part"], s.get("action"), "")
                if s["part"] == "phone_case" and s.get("action") in ("pattern", "pattern_restore"):
                    # pattern / pattern_restore 模板均无 {DESC}：空 prompt 一律模板兜底，补充描述显式追加
                    d_ = (s.get("desc") or "").strip()
                    pr = (pr or P.render_prompt("phone_case", s.get("action"), "")).strip()
                    if d_:
                        pr += f" Extra requirement for the new pattern: {d_}."
                if s["part"] in ("phone_case", "camera_area"):
                    pr = pr.strip() + " " + P.CAM_FRAME_LOCK
                zh_bits.append(f"{len(zh_bits) + 1}. 【{zh}·{_act_zh(s['part'], s.get('action'))}】{pr}")
        prompt = ("Edit this product photo in ONE pass. " + P.render_spec_block(spec) +
                  _ref_header(list(zip(refs, ref_tags)),
                              tail=P.SERIES_TAIL if series else None) +
                  " Apply ALL of the following edits to the FIRST image only "
                  "(do not copy the reference images' layout):\n" + "\n".join(zh_bits) +
                  "\nAll parts NOT mentioned above must remain exactly unchanged. " + P.TAIL)
        if progress:
            progress("整图融合出图中（一次生成，约 30~90 秒）…")
        uris = [aigen.data_uri(base)] + [aigen.data_uri(r) for r in refs]
        n = max(1, min(4, int(data.get("n") or 2)))
        try:
            items, _raw = cl.generate(prompt, uris, OUT, f"{ts}_FUSION", size=size, n=n, timeout=timeout)
        except Exception as ex:
            return _ok(dict(error=f"融合出图失败: {ex}")), 500
        results = []
        for it in items:
            _make_prev(it["path"])
            results.append(_img_info(it["path"]))
        return _ok(dict(mode="fusion", results=results, prompt=prompt,
                        elapsed=round(time.time() - t_all, 1), model=conf.get("model")))

    # ---------- 分层管线：逐部位改 + 保底贴回 ----------
    # 先把「铺图案」拆平：材质步留在手机壳原位，铺图案步（不管单独选还是组合模式）
    # 一律追加为整条管线【最后一步】，所有部位改完才铺图案，铺完再没有任何步骤会碰它。
    flat, pm_pattern = [], None
    for idx, s in enumerate(steps):
        if s.get("part") == "phone_case" and s.get("action") in ("pattern", "pattern_restore"):
            # 单独「换成参考图的图案 / 贴回原图案」：也放最后（与组合模式口径一致）
            refs_ = _parse_refs(s)
            if not any(t == "pattern" for _p, t in refs_):
                return _ok(dict(error=f"第{idx+1}步（手机壳·铺图案）需要先在下方「🎨 铺图案」版块上传图案参考图",
                                done=[])), 400
            # pattern / pattern_restore 模板均没有 {DESC} 占位符，补充描述显式追加（与组合模式一致）
            d_ = (s.get("desc") or "").strip()
            pp = (s.get("prompt") or "").strip() or P.render_prompt("phone_case", s.get("action"), "")
            if d_:
                pp += f" Extra requirement for the new pattern: {d_}."
            pm_pattern = dict(s, prompt=pp)
        elif s.get("part") == "phone_case" and s.get("action") == "pattern_material":
            refs = _parse_refs(s)
            has_m = any(t == "material" for _p, t in refs)
            has_p = any(t == "pattern" for _p, t in refs)
            if not (has_m or has_p):
                return _ok(dict(error=f"第{idx+1}步（手机壳·换图案+材质）需要至少一张参考图（材质或图案）",
                                done=[])), 400
            if has_m:
                # 材质步提示词：用户自己写的优先（系统模板效果不佳时用户可自定义）
                mp_ = (s.get("mat_prompt") or "").strip() or P.render_prompt("phone_case", "material", "")
                flat.append(dict(s, action="material",
                                 refs=[{"path": p_, "tag": "material"} for p_, t in refs if t == "material"],
                                 prompt=mp_))
            if has_p:
                # 图案步提示词：用户自己写的优先（原样发送），留空/不改才走系统模板+补充描述
                pat_custom = (s.get("pat_prompt") or "").strip()
                d_ = (s.get("desc") or "").strip()
                if pat_custom:
                    pp = pat_custom
                else:
                    pp = P.render_prompt("phone_case", "pattern", s.get("desc", ""))
                    if d_:
                        pp += f" Extra requirement for the new pattern: {d_}."
                pm_pattern = dict(s, action="pattern",
                                  refs=[{"path": p_, "tag": "pattern"} for p_, t in refs if t == "pattern"],
                                  prompt=pp)
        else:
            flat.append(s)
    if pm_pattern:
        flat.append(pm_pattern)  # 铺图案放整条管线最后
    steps = flat
    from_index = max(0, int(data.get("from_index") or 0))
    prev_outputs = data.get("prev_outputs") or []
    replay_after = bool(data.get("replay_after"))  # 「只重跑这一步」：之后的步程序重放不花钱
    outs = []
    current = base
    for i, s in enumerate(steps):
        zh = P.PART_MAP.get(s["part"], {}).get("zh", s["part"])
        act_zh = _act_zh(s["part"], s.get("action"))
        # from_index 之前的步：直接复用存档输出，不花钱
        if i < from_index:
            po = _resolve_img(prev_outputs[i]) if i < len(prev_outputs) else None
            if po:
                outs.append(dict(no=i + 1, part=s["part"], zh=zh, action=s.get("action"),
                                 action_zh=act_zh, prompt=s.get("prompt"), kept=True,
                                 out=_img_info(po)))
                current = po
                continue
        if progress:
            progress(f"第 {i + 1}/{len(steps)} 步 · {zh}（{act_zh}）出图中…", outs)
        t0 = time.time()
        refs = _parse_refs(s)
        # 组模式：兄弟锚点图追加为本步附加参考（视觉锁，AI 照已定稿的兄弟图抄）
        if sibling and not any(rp == sibling for rp, _t in refs):
            refs = refs + [(sibling, "sibling")]
        bbox = s.get("bbox") or None
        if bbox and (len(bbox) != 4 or bbox[0] == bbox[2] or bbox[1] == bbox[3]):
            bbox = None
        # 「只重跑这一步」重放：from_index 之后的步不调 AI，用存档输出按原 bbox 程序贴回（免费、秒出）。
        # 存档输出(out)与 AI 原图(raw)在 bbox 区域内像素一致，贴回效果等价。
        # 遇到整图步(无 bbox)/存档缺失/贴回失败 → 自动降级：从这步起转回 AI 正常生成。
        if replay_after and i > from_index:
            po = _resolve_img(prev_outputs[i]) if i < len(prev_outputs) else None
            if po and bbox:
                name = f"{ts}_R{i+1}_{s['part']}_{s.get('action') or 'replay'}"
                fit_fp = os.path.join(OUT, f"{name}_fit.png")
                try:
                    P.composite(current, po, bbox, fit_fp)
                    _make_prev(fit_fp)
                    outs.append(dict(no=i + 1, part=s["part"], zh=zh, action=s.get("action"),
                                     action_zh=act_zh, prompt=s.get("prompt"),
                                     out=_img_info(fit_fp), composited=True, replay=True,
                                     elapsed=round(time.time() - t0, 1)))
                    current = fit_fp
                    if progress:
                        progress(f"第 {i + 1}/{len(steps)} 步 · {zh} 程序重放（不花钱）", outs)
                    continue
                except Exception:
                    pass  # 贴回失败 → 落到下面降级
            replay_after = False  # 整图步/存档缺失/贴回失败 → 降级：从这步起正常 AI 续跑
        try:
            prompt = (s.get("prompt") or P.render_prompt(s["part"], s.get("action"), s.get("desc", ""))).strip()
            if s["part"] in ("phone_case", "camera_area"):
                prompt = prompt + " " + P.CAM_FRAME_LOCK
            # 组模式：规格卡文本锁（逐字注入）+ 参考图说明（组模式换 SERIES_TAIL 兜底句）
            head = P.render_spec_block(spec)
            if refs:
                head += _ref_header(refs, tail=P.SERIES_TAIL if series else None) + "\n"
            full_prompt = head + prompt
            if bbox:
                x1, y1, x2, y2 = bbox
                full_prompt += (f"\n(The area to edit is roughly x {x1/10:.0f}%-{x2/10:.0f}%, "
                                f"y {y1/10:.0f}%-{y2/10:.0f}% of the image. Everything outside this area "
                                "must remain pixel-identical, it will be restored programmatically.)")
            uris = [aigen.data_uri(current)] + [aigen.data_uri(r) for r, _t in refs]
            name = f"{ts}_P{i+1}_{s['part']}_{s.get('action')}"
            items, _raw = cl.generate(full_prompt, uris, OUT, name, size=size, n=1, timeout=timeout)
            gen_fp = items[0]["path"]
            _make_prev(gen_fp)
            # 保底贴回：bbox 外强制用「上一步的图」的像素
            out_fp, composited = gen_fp, False
            if bbox:
                fit_fp = os.path.join(OUT, f"{name}_fit.png")
                try:
                    P.composite(current, gen_fp, bbox, fit_fp)
                    out_fp, composited = fit_fp, True
                    _make_prev(fit_fp)
                except Exception:
                    out_fp, composited = gen_fp, False
        except Exception as ex:
            return _ok(dict(error=f"第{i+1}步（{zh}·{act_zh}）出图失败: {ex}",
                            done=outs)), 500
        outs.append(dict(no=i + 1, part=s["part"], zh=zh, action=s.get("action"),
                         action_zh=act_zh, prompt=full_prompt,
                         refs=[r for r, _t in refs],
                         raw=_img_info(gen_fp), out=_img_info(out_fp),
                         composited=composited, elapsed=round(time.time() - t0, 1)))
        current = out_fp
    return _ok(dict(mode="layered", steps=outs,
                    final=outs[-1]["out"] if outs else None,
                    elapsed=round(time.time() - t_all, 1), model=conf.get("model")))


@app.post("/api/parts/run")
def parts_run():
    """同步执行（保留给单测/调试；页面已改走 run_async 后台任务）"""
    return _parts_pipeline(request.get_json(force=True) or {})


# ================= 生图后台任务：页面关掉也继续 · 落盘记录可随时回看/重跑 =================
JOBS_DIR = os.path.join(HERE, "data", "parts_jobs")
os.makedirs(JOBS_DIR, exist_ok=True)
JOB_SEM = threading.Semaphore(2)  # 并发上限 2：A/B 两个主图可同时跑，第 3 个排队


def _job_path(jid):
    return os.path.join(JOBS_DIR, jid + ".json")


def _job_load(jid):
    try:
        with open(_job_path(jid), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _job_save(job):
    try:
        with open(_job_path(job["id"]), "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False)
    except Exception:
        traceback.print_exc()


def _job_thumb(job):
    """记录列表缩略图：成品优先，还没出图用主图；组任务取第一张已完成的成品"""
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
    # 组任务未出图：用组内第一张主图
    imgs = (job.get("payload") or {}).get("images") or []
    if imgs and isinstance(imgs[0], dict) and imgs[0].get("base_url"):
        return imgs[0]["base_url"]
    return (job.get("payload") or {}).get("base_url") or ""


def _job_summary(job):
    r = job.get("result") or {}
    pl = job.get("payload") or {}
    return dict(id=job["id"], session=job.get("session"), mode=job.get("mode"),
                status=job.get("status"), progress=job.get("progress"),
                note=job.get("note"), created=job.get("created"), elapsed=job.get("elapsed"),
                error=job.get("error"), thumb=_job_thumb(job),
                kind=job.get("kind") or "parts",
                group_id=job.get("group_id") or pl.get("group_id"),
                img_index=job.get("img_index", pl.get("img_index")),
                spec_locked=bool((job.get("spec") or pl.get("spec") or "").strip()),
                n_images=len(job.get("images") or []),
                n_steps=len(r.get("steps") or r.get("results") or
                           (r.get("images") or [])))


def _job_progress(jid, msg=None, steps_so_far=None):
    """管线回调：逐步结果实时落盘——中途失败/服务重启，已出的图也保得住"""
    job = _job_load(jid)
    if not job:
        return
    if msg is not None:
        job["progress"] = msg
    if steps_so_far is not None:
        job["steps"] = steps_so_far
    _job_save(job)


def _result_final_path(result):
    """从管线返回结果里取最终成品路径（layered=final / fusion=results[0]）"""
    if not isinstance(result, dict):
        return None
    fin = result.get("final")
    if fin and fin.get("path"):
        return fin["path"]
    for r in (result.get("results") or []):
        if r.get("path"):
            return r["path"]
    return None


def _group_progress(jid, k, total, name, msg=None, steps=None):
    """组任务进度回调：管线每步落盘到 job.images[k]，进度带「第k/N张」前缀"""
    job = _job_load(jid)
    if not job:
        return
    if msg is not None:
        job["progress"] = f"第 {k + 1}/{total} 张 · {name}：{msg}"
    if steps is not None:
        imgs = job.setdefault("images", [])
        while len(imgs) <= k:
            imgs.append(dict(index=len(imgs) + 1, status="pending"))
        imgs[k]["steps"] = steps
    _job_save(job)


def _group_detect_job(job, jid):
    """批量部位识别（kind=group_detect）：逐张调 detect_parts，每张结果实时落盘"""
    data = job["payload"]
    imgs = [_resolve_img(p) for p in (data.get("images") or [])]
    imgs = [p for p in imgs if p]
    cl, conf = ai_client()
    vm = data.get("vision_model") or conf.get("vision") or aigen.DEFAULT_VISION
    items, t_all = [], time.time()
    for k, im in enumerate(imgs):
        _job_progress(jid, f"第 {k + 1}/{len(imgs)} 张识别中（约 10s/张）…")
        t0 = time.time()
        entry = dict(index=k + 1, image=im, name=os.path.basename(im))
        # 网络瞬断（SSL EOF 等）自动重试，最多 3 次尝试、间隔 3s；识别约 10-30s/次，代价可接受
        res, err = None, None
        for attempt in range(3):
            if attempt:
                _job_progress(jid, f"第 {k + 1}/{len(imgs)} 张识别失败（网络抖动，第 {attempt + 1}/3 次重试中）…")
                time.sleep(3)
            try:
                res = P.detect_parts(cl, im, vm)
                err = None
                break
            except Exception as ex:
                err = str(ex)
        if res is not None:
            entry.update(parts=res["parts"], elapsed=round(time.time() - t0, 1))
        else:
            entry.update(error=err, elapsed=round(time.time() - t0, 1))
        items.append(entry)
        j = _job_load(jid) or job
        j["steps"] = items
        _job_save(j)
    return _ok(dict(kind="group_detect", items=items, vision_model=vm,
                    elapsed=round(time.time() - t_all, 1)))


def _group_run_job(job, jid):
    """组批量生成（kind=group_run）：图1→图N 串行，每张复用 _parts_pipeline。
    链式锚定：图1 先出，图1 成品作为图2~N 的兄弟锚点参考；图1 失败用原始基准图兜底。
    单张失败不中断全组（已出/后出的都保住），前端可只对失败张发起重做。"""
    data = job["payload"]
    groups = []
    for g in (data.get("images") or []):
        bp = _resolve_img(g.get("path"))
        if bp:
            groups.append((bp, g))
    mode = data.get("mode") or "layered"
    spec = (data.get("spec") or "").strip()
    # 组执行前未提取规格卡 → 自动对基准图（图1）提一次（约 10s），失败不阻塞、降级为只用锚点
    if not spec and groups:
        _job_progress(jid, "先提取产品规格卡（约 10s）…")
        try:
            cl, conf = ai_client()
            vm = data.get("vision_model") or conf.get("vision") or aigen.DEFAULT_VISION
            spec = P.extract_spec(cl, groups[0][0], vm)["spec"]
            j = _job_load(jid) or job
            j["spec"] = spec
            j["spec_auto"] = True
            _job_save(j)
        except Exception:
            spec = ""
    # 人工特征标注（出厂档案，权重最高）幂等并入：全组每条提示词 + 后续单张重做都带着走
    # cam_lock 按图1 的实际步骤判定（非手机壳组任务不该被注入摄像头框默认规则）
    _g0_steps = (groups[0][1].get("steps") or []) if groups else []
    _g0_cam = any(s.get("part") in ("phone_case", "camera_area") for s in _g0_steps)
    spec = P.merge_features(spec, data.get("features"), cam_lock=_g0_cam)
    t_all = time.time()
    anchor = None            # 图1 成品路径（图2~N 的锚点）
    anchor_fallback = groups[0][0] if groups else None  # 图1 失败时兜底：原始基准图
    for k, (bp, g) in enumerate(groups):
        name = g.get("name") or os.path.basename(bp)
        entry = dict(index=k + 1, image=bp, name=name, status="running", error=None, result=None)
        j = _job_load(jid) or job
        imgs = j.setdefault("images", [])
        while len(imgs) <= k:
            imgs.append(dict(index=len(imgs) + 1, status="pending"))
        imgs[k] = entry
        _job_save(j)
        _group_progress(jid, k, len(groups), name, "出图中…")
        pl = dict(g)
        pl.update(base_img=bp, base_url=g.get("base_url"), base_name=name,
                  steps=g.get("steps") or [], mode=mode, spec=spec,
                  session=data.get("session"), note=data.get("note"), n=data.get("n"))
        if k >= 1:
            pl["sibling"] = anchor or anchor_fallback  # 视觉锁：兄弟锚点图
        t0 = time.time()
        try:
            rv = _parts_pipeline(
                pl, progress=lambda msg=None, steps=None, _k=k, _n=len(groups), _nm=name:
                    _group_progress(jid, _k, _n, _nm, msg, steps))
            resp = rv[0] if isinstance(rv, tuple) else rv
            result = resp.get_json(silent=True) or {}
        except Exception as ex:
            traceback.print_exc()
            result = dict(error=f"任务异常: {ex}")
        entry["result"] = result
        entry["elapsed"] = round(time.time() - t0, 1)
        if result.get("error"):
            entry["status"] = "error"
            entry["error"] = result["error"]
        else:
            entry["status"] = "done"
            fin = _result_final_path(result)
            if fin and k == 0:
                anchor = fin  # 图1 成品 → 图2~N 的锚点
        j = _job_load(jid) or job
        j.setdefault("images", [])[k] = entry
        _job_save(j)
    j = _job_load(jid) or job
    return _ok(dict(kind="group_run", mode=mode, spec=spec, images=(j.get("images") or []),
                    elapsed=round(time.time() - t_all, 1)))


def _job_worker(jid):
    with JOB_SEM:
        job = _job_load(jid)
        if not job:
            return
        job["status"] = "running"
        job["started"] = time.time()
        _job_save(job)
        try:
            # 管线里的 _ok() 走 jsonify，需要 app context —— 后台线程里必须显式推入
            with app.app_context():
                kind = job.get("kind") or "parts"
                if kind == "group_detect":
                    rv = _group_detect_job(job, jid)
                elif kind == "group_run":
                    rv = _group_run_job(job, jid)
                else:
                    rv = _parts_pipeline(
                        job["payload"],
                        progress=lambda msg=None, steps=None: _job_progress(jid, msg, steps))
            # 成功返回单个 Response，报错返回 (Response, code) 元组 —— 统一解成 dict
            resp = rv[0] if isinstance(rv, tuple) else rv
            result = resp.get_json(silent=True) or {}
            job = _job_load(jid) or job
            job["result"] = result
            if result.get("error"):
                job["status"] = "error"
                job["error"] = result["error"]
            else:
                job["status"] = "done"
            job["progress"] = None
        except Exception as ex:
            traceback.print_exc()
            job["status"] = "error"
            job["error"] = f"任务异常: {ex}"
        job["elapsed"] = round(time.time() - job.get("started", time.time()), 1)
        job["finished"] = time.time()
        _job_save(job)


def _job_prune(keep=120):
    """落盘任务太多时删最老的（只删已结束的，跑着/排队的永远不动）"""
    try:
        files = [(os.path.getmtime(_job_path(f[:-5])), f)
                 for f in os.listdir(JOBS_DIR) if f.endswith(".json")]
        while len(files) > keep:
            files.sort()
            _mt, f = files[0]
            j = _job_load(f[:-5])
            if j and j.get("status") == "running":
                files = files[1:] + [(10 ** 12, f)]  # 跑着的排到最后，绝不删
                continue
            try:
                os.remove(_job_path(f[:-5]))
            except Exception:
                pass
            files = files[1:]
    except Exception:
        pass


def _jobs_recover():
    """服务重启时：磁盘上还标着 running 的任务其实线程已经没了，改成中断"""
    try:
        for f in os.listdir(JOBS_DIR):
            if not f.endswith(".json"):
                continue
            j = _job_load(f[:-5])
            if j and j.get("status") == "running":
                j["status"] = "error"
                j["error"] = "服务重启，任务被中断（已出的图都留在记录里，可从那一步接着重跑）"
                j["progress"] = None
                _job_save(j)
    except Exception:
        traceback.print_exc()   # 启动恢复失败要留痕，否则用户只看到「任务消失」查不到原因


threading.Thread(target=_jobs_recover, daemon=True).start()


@app.post("/api/parts/run_async")
def parts_run_async():
    """后台任务：秒回 job_id，管线在服务端线程跑（页面关掉也继续）。
    data 里可带 session（"A"/"B"）、note（备注）、base_url/base_name（记录缩略图用）。"""
    data = request.get_json(force=True) or {}
    base = _resolve_img(data.get("base_img") or data.get("image"))
    if not base:
        return _ok(dict(error="请先上传主图")), 400
    steps = [s for s in (data.get("steps") or []) if s.get("action") and s["action"] != "none"]
    if not steps:
        return _ok(dict(error="没有要执行的修改：先把某个部位的操作选成非「不动」")), 400
    jid = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    job = dict(id=jid, session=(data.get("session") or ""), note=(data.get("note") or ""),
               mode=(data.get("mode") or "layered"), status="running",
               created=time.time(), progress="排队中…", steps=[], result=None, error=None,
               payload=data)
    _job_save(job)
    threading.Thread(target=_job_worker, args=(jid,), daemon=True).start()
    _job_prune()
    return _ok(dict(job_id=jid))


@app.post("/api/parts/group_detect")
def parts_group_detect():
    """批量识别组内每张主图的部位 bbox（后台任务，约 10s/张，进度落盘）。
    data: {images: [path...], session, note}"""
    data = request.get_json(force=True) or {}
    imgs = [_resolve_img(p) for p in (data.get("images") or [])]
    imgs = [p for p in imgs if p]
    if not imgs:
        return _ok(dict(error="请先上传组内主图")), 400
    jid = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    job = dict(id=jid, kind="group_detect", session=(data.get("session") or "G"),
               note=(data.get("note") or "组批量识别部位"), mode="detect",
               status="running", created=time.time(), progress="排队中…",
               steps=[], result=None, error=None, payload=data)
    _job_save(job)
    threading.Thread(target=_job_worker, args=(jid,), daemon=True).start()
    _job_prune()
    return _ok(dict(job_id=jid))


@app.post("/api/parts/group_run")
def parts_group_run():
    """组批量生成（后台任务）：图1→图N 串行，每张复用分部位管线。
    data: {images: [{path, name, base_url, steps:[…每张自己的 bbox]}],
           mode, spec(规格卡), session, note}
    steps 的操作/参考图/提示词由前端按「全组共享」复制成每张一份（只有 bbox 不同）。
    未传 spec 时任务里先对图1自动提取（提取失败不阻塞）。"""
    data = request.get_json(force=True) or {}
    imgs = [g for g in (data.get("images") or []) if _resolve_img(g.get("path"))]
    if not imgs:
        return _ok(dict(error="请先上传组内主图")), 400
    has_step = any(s.get("action") and s["action"] != "none"
                   for g in imgs for s in (g.get("steps") or []))
    if not has_step:
        return _ok(dict(error="没有要执行的修改：先把某个部位的操作选成非「不动」")), 400
    jid = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    job = dict(id=jid, kind="group_run", session=(data.get("session") or "G"),
               note=(data.get("note") or "对全组执行"), mode=(data.get("mode") or "layered"),
               spec=(data.get("spec") or "").strip(), images=[],
               status="running", created=time.time(), progress="排队中…",
               steps=[], result=None, error=None, payload=data)
    _job_save(job)
    threading.Thread(target=_job_worker, args=(jid,), daemon=True).start()
    _job_prune()
    return _ok(dict(job_id=jid, n_images=len(imgs)))


@app.post("/api/parts/group_redo")
def parts_group_redo():
    """单张重做（后台任务）：同一规格卡 + 同一参考图 + 锚点=组内其他张的成品。
    只重生成这一张，其余张零消耗零影响。data: {base_img, steps, mode, spec,
    sibling(锚点图路径), group_id, img_index, session, note}"""
    data = request.get_json(force=True) or {}
    base = _resolve_img(data.get("base_img") or data.get("image"))
    if not base:
        return _ok(dict(error="请指定要重做的那张主图")), 400
    steps = [s for s in (data.get("steps") or []) if s.get("action") and s["action"] != "none"]
    if not steps:
        return _ok(dict(error="没有要执行的修改：先把某个部位的操作选成非「不动」")), 400
    sib = _resolve_img(data.get("sibling"))
    if not sib:
        return _ok(dict(error="缺少兄弟锚点图（组内其他张的成品路径）")), 400
    data["sibling"] = sib
    jid = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    job = dict(id=jid, session=(data.get("session") or "G"),
               note=(data.get("note") or "单张重做"),
               mode=(data.get("mode") or "layered"),
               group_id=data.get("group_id"), img_index=data.get("img_index"),
               spec=(data.get("spec") or "").strip(),
               status="running", created=time.time(), progress="排队中…",
               steps=[], result=None, error=None, payload=data)
    _job_save(job)
    threading.Thread(target=_job_worker, args=(jid,), daemon=True).start()
    _job_prune()
    return _ok(dict(job_id=jid))


@app.get("/api/parts/jobs")
def parts_jobs():
    limit = max(1, min(200, int(request.args.get("limit") or 50)))
    jobs = []
    try:
        for f in os.listdir(JOBS_DIR):
            if not f.endswith(".json"):
                continue
            j = _job_load(f[:-5])
            if j:
                jobs.append(j)
    except Exception:
        pass
    jobs.sort(key=lambda j: j.get("created") or 0, reverse=True)
    return _ok(dict(jobs=[_job_summary(j) for j in jobs[:limit]]))


@app.get("/api/parts/job/<jid>")
def parts_job(jid):
    j = _job_load(jid)
    if not j:
        return _ok(dict(error="任务不存在或已清理")), 404
    return _ok(j)


# ================= 生图记录删除 + 7 天自动过期（2026-10-12 雪碧需求） =================
IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")
EXPIRE_DAYS = 7          # 与 COS 桶生命周期规则保持一致：图和记录同步活 7 天


def _delete_outputs_names(names):
    """删一批 outputs 文件：COS 对象 + .cosok 标记 + 本地（回收站优先，后台真删）。
    与 /api/delete_output 同一套机制，抽出来供记录删除/过期清理复用。"""
    killed, failed, trashed = [], [], []
    for n in names:
        fp = _out_path(n)
        if not fp:
            failed.append(n)
            continue
        COS.delete("outputs", n)                      # COS 失败只打日志，不阻塞
        if os.path.isfile(fp + ".cosok"):
            _rm(fp + ".cosok")
        if not os.path.isfile(fp):
            killed.append(n)
            continue
        dst = _trash(fp)                              # 改名搬走（毫秒级），页面立刻看不到
        if dst:
            trashed.append(dst)
            killed.append(n)
            continue
        ok, _err = _rm(fp, tries=2, gap=0.03)
        (killed.append(n) if ok else failed.append(n))
    if trashed:
        threading.Thread(target=_rm_later, args=(trashed,), daemon=True).start()
    return killed, failed


def _job_image_names(job):
    """递归扫任务记录里所有字符串，挑出 outputs 目录里真实存在的图片文件名（含 prev_ 缩略图）"""
    names = set()

    def walk(x):
        if isinstance(x, str):
            b = os.path.basename(x)
            if b.lower().endswith(IMG_EXTS):
                names.add(b)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)

    walk(job)
    out = []
    for n in sorted(names):
        if n in ("_base.png",) or n.startswith("prev_"):
            continue                                # prev_ 由主图顺带补，_base 永不删
        if os.path.isfile(os.path.join(OUT, n)):
            out.append(n)
            pv = "prev_" + n
            if os.path.isfile(os.path.join(OUT, pv)):
                out.append(pv)
    return out


def _delete_job(jid, job=None):
    """删一条任务记录 + 该次生成的全部图。返回 (ok, 信息)。"""
    j = job or _job_load(jid)
    if not j:
        return False, "任务不存在或已清理"
    if j.get("status") == "running":
        return False, "任务还在跑，不能删"
    killed, failed = _delete_outputs_names(_job_image_names(j))
    try:
        os.remove(_job_path(jid))
    except Exception as ex:
        return False, "记录删除失败：{0}".format(ex)
    return True, "记录 + {0} 张图已删{1}".format(
        len(killed), "（{0} 个文件稍后后台清）".format(len(failed)) if failed else "")


@app.post("/api/parts/job/<jid>/delete")
def parts_job_delete(jid):
    ok, msg = _delete_job(jid)
    return _ok(dict(ok=ok, msg=msg, error=None if ok else msg)), (200 if ok else 409)


@app.post("/api/parts/jobs/delete")
def parts_jobs_delete():
    """批量删记录：body {ids:[...]} 或 {all:true}。跑着的任务自动跳过。"""
    data = request.get_json(force=True) or {}
    ids = list(data.get("ids") or [])
    if data.get("all"):
        try:
            ids = [f[:-5] for f in os.listdir(JOBS_DIR) if f.endswith(".json")]
        except Exception:
            ids = []
    done, skipped, failed = [], 0, []
    for jid in ids:
        j = _job_load(jid)
        if not j or j.get("status") == "running":
            skipped += 1
            continue
        ok, msg = _delete_job(jid, job=j)
        (done.append(jid) if ok else failed.append(dict(id=jid, error=msg)))
    return _ok(dict(ok=not failed, deleted=len(done), skipped=skipped,
                    failed=failed,
                    msg="已删 {0} 条{1}".format(
                        len(done), "，跳过 {0} 条进行中".format(skipped) if skipped else "")))


def _expire_once():
    """单趟过期清理：outputs 超 7 天的图（本地+COS）+ 超 7 天已结束的任务记录。"""
    cut = time.time() - EXPIRE_DAYS * 86400
    old = []
    for f in os.listdir(OUT):
        if f in ("_base.png", "prev__base.png"):
            continue
        if not (f.startswith("prev_") or f.endswith(".cosok")
                or f.lower().endswith(IMG_EXTS)):
            continue
        fp = os.path.join(OUT, f)
        try:
            if os.path.isfile(fp) and os.path.getmtime(fp) < cut:
                old.append(f)
        except Exception:
            pass
    if old:
        _delete_outputs_names(old)
        print("[expire] 清理超 {0} 天图片 {1} 个".format(EXPIRE_DAYS, len(old)))
    n_job = 0
    for f in os.listdir(JOBS_DIR):
        if not f.endswith(".json"):
            continue
        j = _job_load(f[:-5])
        if not j or j.get("status") == "running":
            continue
        if (j.get("created") or 0) < cut:
            _delete_outputs_names(_job_image_names(j))
            try:
                os.remove(_job_path(f[:-5]))
                n_job += 1
            except Exception:
                pass
    if n_job:
        print("[expire] 清理超 {0} 天任务记录 {1} 条".format(EXPIRE_DAYS, n_job))


def _expire_sweep():
    """每小时清一次（启动 10 分钟后首扫，避开重启抖动与测试进程误伤）。
    与 COS 桶生命周期规则（7 天自动删）保持一致——记录和图同步消失，记录里不会开裂图。"""
    time.sleep(600)
    while True:
        try:
            _expire_once()
        except Exception:
            traceback.print_exc()
        time.sleep(3600)


threading.Thread(target=_expire_sweep, daemon=True).start()


def _sweep_trash():
    """启动时清一次回收站（上次没删完的残留）"""
    for td in _trash_dirs():
        try:
            rest = [f for f in os.listdir(td) if os.path.isfile(os.path.join(td, f))]
        except Exception:
            continue
        for f in rest:
            _rm(os.path.join(td, f), tries=1, gap=0)


threading.Thread(target=_sweep_trash, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8770"))
    print("工作台启动: http://127.0.0.1:%d" % port)
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
