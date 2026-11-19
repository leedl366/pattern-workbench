# -*- coding: utf-8 -*-
"""需求解析 → 方案生成 → 编排执行（含聊天增量微调）"""
import os, re, json, time, uuid
from . import editor as E
from PIL import Image


# ---------------- 角色识别 ----------------
ROLE_ALIAS = {
    "material": "材质源", "pattern": "图案源", "target": "目标底图",
    "shape": "形态底图", "ref": "参考图", "auto": "自动",
}


def _split_swap(p):
    """「把 A 换成 B」→ head=A(被编辑), tail=B(来源)"""
    m = re.search(r"换成|改成|替换为|换为|改为", p)
    if m:
        return p[:m.start()], p[m.end():]
    return p, ""


def guess_roles(slots, prompt):
    """按中文语序猜角色：被编辑的图在「换成」之前，来源图在之后"""
    p = prompt or ""
    n = len(slots)
    roles = [s.get("role", "auto") for s in slots]
    fixed = [r != "auto" for r in roles]          # 用户手动指定的不动
    head, tail = _split_swap(p)

    def figs(text):
        return [(int(m.group(1)) - 1, m.start()) for m in re.finditer(r"图\s*([1-5])", text or "")]

    def seg(text, pos, a=10, b=18):
        return text[max(0, pos - a):pos + b]

    # 1) 形态底图：「整机/形态/带壳/机身」附近的图号
    for i, pos in figs(p):
        if fixed[i]:
            continue
        if re.search(r"整机|形态|带壳|机身|外形", seg(p, pos)):
            roles[i] = "shape"
    # 2) 材质源：优先「换成」之后且带材质词的图号
    mat = None
    for i, pos in figs(tail):
        if fixed[i]:
            continue
        if re.search(r"材质|质感|颜色|银白|磨砂|金属", seg(tail, pos)):
            mat = i
            break
    if mat is None:                                # 形如「图1的材质」
        for i, pos in figs(p):
            if fixed[i]:
                continue
            if re.match(r"图\s*[1-5](的)?(材质|质感|颜色|银白)", p[pos:pos + 14]):
                mat = i
                break
    if mat is not None:
        roles[mat] = "material"
    # 3) 图案源：「图N的图案/卡通/印花」
    pat = None
    for i, pos in figs(p):
        if fixed[i] or roles[i] != "auto":
            continue
        if re.search(r"图案|卡通|印花|花型", seg(p, pos)):
            pat = i
            break
    if pat is not None:
        roles[pat] = "pattern"
    # 4) 目标底图：「换成」之前的第一个未分配图号
    tgt = None
    for i, pos in figs(head):
        if fixed[i] or roles[i] != "auto":
            continue
        tgt = i
        break
    if tgt is None:
        for i, pos in figs(p):
            if fixed[i] or roles[i] != "auto":
                continue
            tgt = i
            break
    if tgt is not None:
        roles[tgt] = "target"
    # 5) 兜底：有图的第二张当目标、第一张当材质
    if "target" not in roles:
        have = [i for i, s in enumerate(slots) if s.get("path") and not fixed[i]]
        if len(have) >= 2:
            roles[have[1]] = "target"
            if "material" not in roles:
                roles[have[0]] = "material"
        elif have:
            roles[have[0]] = "target"
    # 6) 「图案不变/图案保留」→ 图案源就是目标底图自身
    if roles.count("pattern") == 0 and re.search(r"图案不变|图案保留|保留图案|原图案", p):
        if "target" in roles:
            roles[roles.index("target")] = "target"   # 引擎会自动用 target 自身作图案源
    return roles


# ---------------- 意图 → 动作序列 ----------------
def parse_intent(prompt):
    p = prompt or ""
    acts = []
    if re.search(r"换成|改成|整机|形态|带壳整个|整个.*换", p):
        acts.append("set_shape")
    if re.search(r"材质|质感|颜色|银白|磨砂|金属", p):
        acts.append("material_replace")
    # 关键：「图案不变/保留图案」= 材质重映射时保护图案即可，不需要重新贴图
    keep_pat = bool(re.search(r"图案不变|保留.*图案|图案保留|图案不动|图案保持不变", p))
    if re.search(r"图案|卡通|印花|贴回|贴上去|满版|铺满", p) and not keep_pat:
        acts.append("pattern_transfer")
    if re.search(r"底托|原托|托盘|底座不要|不要.*托|去掉.*托", p):
        acts.append("remove_base")
    if re.search(r"摄像头|镜头|模组", p) and re.search(r"不要.*图案|无图案|干净|留白|不要保留|不保留|去掉.*图案", p):
        acts.append("clean_cam")
    if re.search(r"去背景|抠图|白底", p):
        acts.append("clean_bg")
    if not acts:
        acts = ["material_replace", "pattern_transfer"]
    intent = "pattern" if acts == ["pattern_transfer"] else (
        "material" if acts == ["material_replace"] else "mixed")
    return acts, intent


def make_plan(acts, slots, roles, params):
    """生成人可读方案（步骤 + 参数 + 验收标准）"""
    name = {i: f"图{i+1}" for i in range(len(slots))}
    rolemap = {}
    for i, r in enumerate(roles):
        # 角色数可能多于参考图数（例如只传了 1 张图却有 2 个角色），这里兜底，别再抛 KeyError
        rolemap.setdefault(r, []).append(name.get(i, f"图{i+1}"))
    steps = []
    n = 0
    for a in acts:
        n += 1
        if a == "set_shape":
            steps.append(dict(no=n, act=a, title="确定形态底图",
                              detail=f"用 {','.join(rolemap.get('shape', ['主体图']))} 作为最终机身/壳体形态底图，后续所有操作在它上面叠加。",
                              why="程序引擎无法凭空改结构，形态必须由一张真实图提供。"))
        elif a == "material_replace":
            steps.append(dict(no=n, act=a, title="材质重映射",
                              detail=f"提取 {','.join(rolemap.get('material', ['材质源']))} 的中性区中位亮度作为目标，"
                                     f"对底图做 tanh 软压缩亮度重映射（contrast={params['contrast']}, strength={params['strength']}），"
                                     f"图案与暗部结构用羽化蒙版保护。",
                              why="纯 AI 换材质多次失败（主色卡在灰），程序重映射可精确命中 RGB≈224 且不糊层次。"))
        elif a == "pattern_transfer":
            steps.append(dict(no=n, act=a, title="图案层迁移与铺贴",
                              detail=f"从 {','.join(rolemap.get('pattern', ['图案源']))} 提取图案层（彩色+深色墨水线），"
                                     f"按 scale={params['scale']}、density={params['density']} 纵向平铺到机身内，"
                                     f"自动避开摄像头区并做圆角裁切。",
                              why="目标覆盖率 ≈9~11%，摄像头区必须为 0。"))
        elif a == "clean_cam":
            steps.append(dict(no=n, act=a, title="摄像头区清图案",
                              detail="自动定位摄像头模组区域，把区域内的彩色图案抹除并恢复为壳体本色，镜片暗部结构保留。",
                              why="原图摄像头区自带卡通图案，需求要求该区域干净无图案。"))
        elif a == "remove_base":
            steps.append(dict(no=n, act=a, title="去除底托",
                              detail=f"底部 {int(params['base_ratio']*100)}% 区域用背景色羽化填充（或裁切）。",
                              why="小熊之家只保留小熊，底托去掉。"))
        elif a == "clean_bg":
            steps.append(dict(no=n, act=a, title="背景净化",
                              detail="把非白背景统一到纯白，便于电商上架。", why="电商主图规范。"))
    steps.append(dict(no=n + 1, act="verify", title="数据校验与排序",
                      detail="计算机身内彩色占比、摄像头区彩色占比、中性区中位亮度、层次跨度，按综合分排序出版本。",
                      why="用数据代替肉眼判断，避免「看着像换了其实没换」。"))
    return steps


# ---------------- 执行 ----------------
DEFAULT_PARAMS = dict(
    contrast=0.92, strength=1.0, span=62.0, target_med=None,
    mode="tile", scale=1.18, density=1.0, opacity=0.96, avoid_cam=True,
    base_ratio=0.16, brightness=0.0, saturation=1.0,
)


def _load(slots, roles, role):
    for i, r in enumerate(roles):
        if r == role and slots[i].get("path"):
            return E.imread(slots[i]["path"])
    return None


def _abs_box(im, rel):
    w, h = im.size
    return (int(rel[0] * w), int(rel[1] * h), int(rel[2] * w), int(rel[3] * h))


def pipeline(base_img, mat_img, pat_img, acts, params, out_dir, tag):
    """按动作序列执行，返回 (结果图, 日志, 几何信息)"""
    log = []
    im = base_img
    body = E.find_body(im)
    cam = E.find_camera(im, body)
    if params.get("cam_box"):
        cam = _abs_box(im, params["cam_box"])
    if params.get("body_box"):
        body = _abs_box(im, params["body_box"])
    if "material_replace" in acts and mat_img is not None:
        im, info = E.material_replace(im, ref=mat_img,
                                      target_med=params.get("target_med"),
                                      contrast=params["contrast"],
                                      strength=params["strength"],
                                      span=params["span"])
        log.append(("材质重映射", info))
    if "clean_cam" in acts and cam:
        im, info = E.clean_camera(im, cam)
        log.append(("摄像头区清图案", info))
    if "pattern_transfer" in acts and pat_img is not None:
        im, info = E.pattern_transfer(im, pat_img, mode=params["mode"],
                                      scale=params["scale"], density=params["density"],
                                      opacity=params["opacity"],
                                      avoid_cam=params["avoid_cam"],
                                      body=body, cam=cam)
        log.append(("图案铺贴", {k: v for k, v in info.items() if k != "body"}))
    if "remove_base" in acts:
        im, info = E.remove_base(im, ratio=params["base_ratio"])
        log.append(("去底托", info))
    if "clean_bg" in acts:
        im, info = E.adjust(im, brightness=6, contrast=1.03)
        log.append(("背景净化", info))
    if abs(params.get("brightness", 0)) > 1e-6 or abs(params.get("saturation", 1.0) - 1) > 1e-3:
        im, info = E.adjust(im, brightness=params.get("brightness", 0),
                            saturation=params.get("saturation", 1.0))
        log.append(("整体微调", info))
    return im, log, dict(body=list(body), cam=list(cam) if cam else None)


def run(slots, prompt, out_dir, roles=None, params_override=None, variants=True):
    acts, intent = parse_intent(prompt)
    roles = roles or guess_roles(slots, prompt)
    params = dict(DEFAULT_PARAMS)
    if params_override:
        params.update(params_override)

    shape = _load(slots, roles, "shape")
    mat = _load(slots, roles, "material")
    pat = _load(slots, roles, "pattern")
    tgt = _load(slots, roles, "target")
    base_img = shape or tgt
    if base_img is None:
        for s in slots:
            if s.get("path"):
                base_img = E.imread(s["path"])
                break
    if base_img is None:
        raise ValueError("没有可用图片")

    # 图案源缺省：用目标底图自身（"图案不变"的语义）
    if "pattern_transfer" in acts and pat is None:
        pat = tgt or base_img
    if "material_replace" in acts and mat is None:
        mat = None

    plan = make_plan(acts, slots, roles, params)

    # 变体：A 标准 / B 更亮更密 / C 拉伸铺满 / D 保守
    var_defs = [("A_标准", {})]
    if variants:
        var_defs += [
            ("B_更有层次", dict(contrast=min(1.25, params["contrast"] + 0.18))),
            ("C_更亮银白", dict(target_med=(params.get("target_med") or 224) + 8)),
            ("D_柔和自然", dict(contrast=max(0.5, params["contrast"] - 0.16),
                              target_med=(params.get("target_med") or 224) - 6)),
        ]
    results = []
    os.makedirs(out_dir, exist_ok=True)
    ts = time.strftime("%H%M%S")
    for name, ov in var_defs:
        p = dict(params)
        p.update(ov)
        try:
            im, log, geo = pipeline(base_img.copy(), mat, pat, acts, p, out_dir, name)
        except Exception as ex:
            results.append(dict(name=name, error=str(ex)))
            continue
        m = E.metrics(im, tuple(geo["body"]), tuple(geo["cam"]) if geo["cam"] else None)
        m["body"] = geo["body"]
        m["cam"] = geo["cam"]
        score = E.grade(m, intent)
        fn = f"{ts}_{name}.png"
        fp = os.path.join(out_dir, fn)
        im.save(fp)
        prev = im.copy()
        prev.thumbnail((420, 420), Image.Resampling.LANCZOS)
        prev.save(os.path.join(out_dir, f"prev_{fn}"))
        results.append(dict(name=name, file=fn, url=f"/api/file/outputs/{fn}",
                            preview=f"/api/file/outputs/prev_{fn}",
                            metrics=m, score=score, params=p,
                            log=[dict(k, v) for k, v in []], steps=[l[0] for l in log]))
    results = [r for r in results if "error" not in r]
    results.sort(key=lambda r: -r["score"])
    # 保存原始底图副本，供后续迭代从原图重跑（避免二次累积劣化）
    base_file = os.path.join(out_dir, "_base.png")
    try:
        base_img.save(base_file)
    except Exception:
        base_file = None
    return dict(plan=plan, roles=roles, acts=acts, intent=intent,
                params=params, results=results, base_file=base_file)


# ---------------- 聊天增量微调 ----------------
REFINE_RULES = [
    (r"密|多一点|再多|铺满|更满", lambda p: p.update(density=p["density"] * 1.22)),
    (r"稀|疏一点|少一点|别太满", lambda p: p.update(density=p["density"] * 0.82)),
    (r"大一点|放大|更大", lambda p: p.update(scale=p["scale"] * 1.15)),
    (r"小一点|缩小|更小", lambda p: p.update(scale=p["scale"] * 0.87)),
    (r"亮一点|更亮|提亮|白一点", lambda p: (p.update(target_med=(p.get("target_med") or 224) + 8),
                                       p.update(brightness=p.get("brightness", 0) + 5))),
    (r"暗一点|更暗|压暗|灰一点", lambda p: (p.update(target_med=(p.get("target_med") or 224) - 8),
                                       p.update(brightness=p.get("brightness", 0) - 5))),
    (r"对比|立体|层次", lambda p: p.update(contrast=min(1.2, p["contrast"] + 0.12))),
    (r"平|柔和|别那么立体", lambda p: p.update(contrast=max(0.5, p["contrast"] - 0.12))),
    (r"鲜艳|饱和|更艳", lambda p: p.update(saturation=min(1.35, p["saturation"] + 0.08))),
    (r"淡一点|素一点|降饱和", lambda p: p.update(saturation=max(0.7, p["saturation"] - 0.08))),
    (r"摄像头.*干净|干净.*摄像头|摄像头.*留白|别压摄像头", lambda p: p.update(avoid_cam=True)),
    (r"拉伸|撑满|不留缝", lambda p: p.update(mode="stretch")),
    (r"平铺|重复|原比例", lambda p: p.update(mode="tile")),
]


def refine_params(params, message):
    p = dict(params)
    hits = []
    for rx, fn in REFINE_RULES:
        if re.search(rx, message or ""):
            fn(p)
            hits.append(rx)
    return p, hits


def rerun(base_file, slots, roles, acts, params, out_dir, label):
    """
    迭代：始终从「原始底图」重跑（不做二次累积），只改参数。
    这样"再密一点/再亮一点"不会在已出图上反复叠加导致劣化。
    """
    base_img = E.imread(base_file)
    mat = _load(slots, roles, "material")
    pat = _load(slots, roles, "pattern")
    if "pattern_transfer" in acts and pat is None:
        pat = _load(slots, roles, "target")
    acts2 = [a for a in acts if a != "set_shape"]
    im, log, geo = pipeline(base_img, mat, pat, acts2, params, out_dir, label)
    m = E.metrics(im, tuple(geo["body"]), tuple(geo["cam"]) if geo["cam"] else None)
    m["body"] = geo["body"]
    m["cam"] = geo["cam"]
    ts = time.strftime("%H%M%S")
    fn = f"{ts}_{label}.png"
    fp = os.path.join(out_dir, fn)
    im.save(fp)
    prev = im.copy()
    prev.thumbnail((420, 420), Image.Resampling.LANCZOS)
    prev.save(os.path.join(out_dir, f"prev_{fn}"))
    return dict(name=label, file=fn, url=f"/api/file/outputs/{fn}",
                preview=f"/api/file/outputs/prev_{fn}", metrics=m,
                score=E.grade(m, "mixed"), params=params,
                steps=[l[0] for l in log])


def iterative(base_file, slots, roles, acts, params, out_dir, label="迭代版", variants=True):
    """
    聊天迭代执行：从「本轮基准图」出发按方案重跑，出 1~3 个版本并打分排序。
    基准图由调用方决定 —— 用户显式选了结果图就用它（在他选的图上叠加），
    没选就退回原始底图 _base.png（避免二次累积劣化）。
    """
    base_img = E.imread(base_file)
    mat = _load(slots, roles, "material")
    pat = _load(slots, roles, "pattern")
    if "pattern_transfer" in acts and pat is None:
        pat = _load(slots, roles, "target") or base_img
    acts2 = [a for a in acts if a != "set_shape"] or list(acts or [])
    if not acts2:
        acts2 = ["material_replace", "pattern_transfer"]
    params = dict(DEFAULT_PARAMS); params.update(dict(params or {}))
    intent = "pattern" if acts2 == ["pattern_transfer"] else (
        "material" if acts2 == ["material_replace"] else "mixed")
    plan = make_plan(acts2, slots, roles, params)

    var_defs = [(label + "_标准", {})]
    if variants:
        var_defs += [
            (label + "_加强", dict(
                density=round(params.get("density", 1.0) * 1.18, 3),
                contrast=min(1.25, round(params.get("contrast", 0.92) + 0.10, 3)))),
            (label + "_保守", dict(
                density=round(params.get("density", 1.0) * 0.86, 3),
                contrast=max(0.5, round(params.get("contrast", 0.92) - 0.10, 3)))),
        ]
    os.makedirs(out_dir, exist_ok=True)
    ts = time.strftime("%H%M%S")
    results = []
    for name, ov in var_defs:
        p = dict(params); p.update(ov)
        try:
            im, log, geo = pipeline(base_img.copy(), mat, pat, acts2, p, out_dir, name)
        except Exception as ex:
            results.append(dict(name=name, error=str(ex)))
            continue
        m = E.metrics(im, tuple(geo["body"]), tuple(geo["cam"]) if geo["cam"] else None)
        m["body"] = geo["body"]; m["cam"] = geo["cam"]
        fn = f"{ts}_{name}.png"
        im.save(os.path.join(out_dir, fn))
        prev = im.copy(); prev.thumbnail((420, 420), Image.Resampling.LANCZOS)
        prev.save(os.path.join(out_dir, f"prev_{fn}"))
        results.append(dict(name=name, file=fn, url=f"/api/file/outputs/{fn}",
                            preview=f"/api/file/outputs/prev_{fn}", metrics=m,
                            score=E.grade(m, intent), params=p,
                            steps=[l[0] for l in log]))
    results = [r for r in results if "error" not in r]
    results.sort(key=lambda r: -r["score"])
    return dict(plan=plan, results=results, params=params, acts=acts2,
                roles=roles, intent=intent, base_file=base_file)
