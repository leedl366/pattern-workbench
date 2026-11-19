# -*- coding: utf-8 -*-
"""
分部位编辑引擎（2026-10-05）

职责：
  1. detect_parts  —— 用视觉模型（gpt-5.4）识别部位 bbox（0-1000 归一化坐标）
  2. PROMPT_LIB    —— 部位 × 操作 提示词库 v1（英文模板，gpt-image 系更吃英文）
  3. composite     —— 局部保底贴回：AI 出图后，把 bbox 以外的区域用原图像素贴回
                      （实测 Grok edits 的 mask 只是软约束，gpt-image-2 干脆不走
                       /images/edits；所以「区域外一个像素都不动」必须由程序保证）

设计依据（2026-10-04 实测，见 plan.html）：
  - gpt-5.4 输出 bbox 格式严格可靠，不需要用户手动画框（识别不准可再手动拖）
  - gpt-image-2 只能整图重绘 → 分层管线每步 = 整图重绘 + 程序贴回
"""
import os, re, json
from PIL import Image, ImageDraw, ImageFilter

# ---------------- 部位定义 ----------------
PARTS = [
    dict(id="phone_case", zh="手机壳", en="phone case body"),
    dict(id="lanyard", zh="手机挂绳", en="phone lanyard / wrist strap / charm"),
    dict(id="stand", zh="手机支架", en="phone stand / kickstand / ring holder"),
    dict(id="camera_area", zh="摄像头孔位区", en="camera module area on the phone case"),
]
PART_MAP = {p["id"]: p for p in PARTS}

# 部位 × 可选操作（前端下拉与服务端校验共用）
ACTIONS = {
    "phone_case": [("none", "不动"), ("pattern", "换成参考图的图案"),
                   ("material", "换参考材质（出干净壳）"),
                   ("material_pick", "换材质（自选·出干净壳）"),
                   ("pattern_restore", "贴回原图案（引用原图）"),
                   ("pattern_material", "换图案+材质（两步自动）"),
                   ("custom", "自定义(写描述)")],
    "lanyard":    [("none", "不动"), ("remove", "去掉挂绳"),
                   ("replace", "换成我的挂绳"), ("custom", "自定义(写描述)")],
    "stand":      [("none", "不动"), ("remove", "去掉支架"), ("replace", "换成我的支架"),
                   ("pattern", "改图案"), ("color", "改颜色"), ("custom", "自定义(写描述)")],
    "camera_area": [("none", "不动"), ("clean", "改干净(无图案)"), ("color", "改颜色"),
                    ("custom", "自定义(写描述)")],
}

# ---------------- 提示词库 v1 ----------------
# {DESC} 会被前端的「补充描述」替换；{TAIL} 是保护其他部位的通用尾巴
TAIL = ("Keep every other element of the image EXACTLY unchanged: same product angle, "
        "same lighting, same shadows, same background, same framing. "
        "Do not add text, logo or watermark.")

PROMPT_LIB = {
    # —— 挂绳 ——
    "lanyard.remove": (
        "Remove the phone lanyard / wrist strap / charm completely from the product photo. "
        "Naturally reconstruct the phone case surface and the background that were hidden "
        "behind the strap, continuing the existing printed pattern seamlessly. " + TAIL),
    "lanyard.replace": (
        "Replace the phone lanyard / wrist strap in the first image with the lanyard shown "
        "in the reference image. It must be a 1:1 replica: copy its exact colors, materials, "
        "beads, cord texture and hardware without any modification or simplification. "
        "Attach it at the same point on the phone case with natural droop. " + TAIL),
    "lanyard.custom": ("Edit the phone lanyard / wrist strap: {DESC}. " + TAIL),

    # —— 支架 ——
    "stand.remove": (
        "Remove the phone stand / kickstand / ring holder completely from the product photo. "
        "Reconstruct the phone case surface behind it seamlessly. " + TAIL),
    "stand.replace": (
        "Replace the phone stand / kickstand with the stand shown in the reference image. "
        "It must be a 1:1 replica: match its shape, color, material and hinge position "
        "precisely with no modification, attached at the same spot on the case back. " + TAIL),
    "stand.pattern": (
        "Change the pattern / design printed on the phone stand / kickstand to: {DESC}. "
        "Keep the stand's exact shape, size, position and 3D form; only its surface design "
        "changes, following the stand's perspective. " + TAIL),
    "stand.color": (
        "Change the color of the phone stand / kickstand to: {DESC}. Keep its exact shape, "
        "size, position, material finish and lighting. " + TAIL),
    "stand.custom": ("Edit the phone stand / kickstand: {DESC}. " + TAIL),

    # —— 壳体 ——
    "phone_case.pattern": (
        "Replace the printed pattern / artwork on the phone case body with the pattern shown "
        "in the PATTERN reference image. The new pattern must be a 1:1 replica of the "
        "reference — identical motif, layout, proportions and colors; no redesign, "
        "no simplification, no invented elements. Full coverage of the case back, "
        "no distortion. Keep the camera module area clean. " + TAIL),
    "phone_case.material": (
        "Change the material and surface finish of the phone case to match the MATERIAL "
        "reference image — replicate only its base color, gloss and texture 1:1; do NOT "
        "substitute a similar-looking material of your own choice. If the material "
        "reference itself shows any printed pattern, logo or artwork, IGNORE it — its "
        "pattern must NOT be transferred. IMPORTANT: remove the case's existing printed "
        "pattern / artwork COMPLETELY — the output must be a CLEAN BLANK case in the new "
        "material only, with no printed artwork, no leftover colors or ghosts of the old "
        "pattern. Keep the case's exact shape, structure and camera module area intact "
        "and clean. The pattern will be applied in a separate later step. " + TAIL),
    "phone_case.pattern_material": (
        "First change the material and surface finish of the phone case to match the MATERIAL "
        "reference image — replicate only its base color, gloss and texture 1:1; do NOT "
        "substitute a similar-looking material. If the material reference itself shows any "
        "printed pattern, logo or artwork, IGNORE it — its pattern must NOT be transferred. "
        "Then replace the printed pattern / artwork with the pattern "
        "shown in the PATTERN reference image as a 1:1 replica — identical motif, layout and "
        "colors, no redesign and no invented elements — rendered as if printed on the new "
        "material: full coverage of the case back, no distortion. "
        "Keep the camera module area clean. " + TAIL),
    "phone_case.material_pick": (
        "Change the material and surface finish of the phone case body to: {DESC}. "
        "Apply the new material to the entire case body — back panel, edges and bumper — "
        "with physically correct gloss, texture, transparency and light response for that "
        "material. IMPORTANT: remove the case's existing printed pattern / artwork "
        "COMPLETELY — the output must be a CLEAN BLANK case in the new material only, "
        "with no printed artwork, no leftover colors or ghosts of the old pattern. "
        "Keep the case's exact shape, structure and camera module area intact and clean. "
        "The pattern will be applied in a separate later step. " + TAIL),
    "phone_case.pattern_restore": (
        "The base image is a CLEAN BLANK phone case in its FINAL material. Now apply the "
        "original printed pattern / artwork shown in the PATTERN reference image (the "
        "original product photo). The pattern must be a 1:1 replica of the reference — "
        "identical motif, layout, proportions and colors; no redesign, no simplification, "
        "no invented elements — rendered as if printed on the case's CURRENT new material. "
        "CRITICAL: apply ONLY the printed pattern from the reference; do NOT bring back "
        "its old case material, old base color or old finish — the case body must keep "
        "the exact material, color and surface of the base image. Full coverage of the "
        "case back, no distortion. Keep the camera module area clean. " + TAIL),
    "phone_case.custom": ("Edit the phone case body: {DESC}. " + TAIL),

    # —— 摄像头孔位区 ——
    "camera_area.clean": (
        "Make the camera module area of the phone case clean and plain: no printed pattern "
        "over it. Keep the lens holes, lens rings and glass highlights intact. Everything "
        "outside the camera module area must stay exactly unchanged. " + TAIL),
    "camera_area.color": (
        "Change the color / finish of the camera module area on the phone case to: {DESC}. "
        "Keep the lens holes and rings intact. Everything outside the camera module area "
        "must stay exactly unchanged. " + TAIL),
    "camera_area.custom": (
        "Edit ONLY the camera module area of the phone case: {DESC}. "
        "Everything outside the camera module area must stay exactly unchanged. " + TAIL),
}


# 参考图用途标签 → 提示词里的英文名（server 拼「第N张图是什么」的编号说明行用）
REF_TAGS = {
    "pattern": "the PATTERN reference image",
    "material": "the MATERIAL reference image",
    "look": "the appearance reference image",
    # 组模式：兄弟锚点图（同款产品另一角度的成品，视觉一致性基准）
    "sibling": "the SIBLING photo of the SAME product (binding consistency standard)",
}

# 🔴 严格复刻约束（2026-10-05 用户要求）：提供了参考图就必须 1:1 复刻，禁止 AI 自己联想修改。
# 由 server._ref_header 按每张参考图的编号注入，覆盖所有生成环节（含用户自定义提示词的环节）。
STRICT_REFS = {
    "pattern": (
        "CRITICAL: Image {n} is the PATTERN reference. The new pattern must be a 1:1 replica "
        "of it — identical motif, composition, proportions, line work and colors. "
        "Do NOT redesign, simplify, rearrange, merge or drop any element. "
        "Do NOT invent or imagine a different pattern from your own association."
    ),
    "material": (
        "CRITICAL: Image {n} is the MATERIAL reference. Copy ONLY its surface finish — "
        "identical base color, gloss, matte feel, texture and grain. "
        "If this reference itself shows any printed pattern, logo or artwork, IGNORE it "
        "completely: that pattern must NOT be transferred onto the product. "
        "Do NOT reinterpret it or substitute a similar-looking material of your own choice."
    ),
    "look": (
        "CRITICAL: Image {n} is the appearance reference. Its appearance must be replicated "
        "exactly as shown — identical color, texture and details. "
        "Do NOT modify it with your own ideas or associations."
    ),
}

# 全局兜底句：拼在所有参考图说明之后，再次强调「只准照抄，不准联想」
STRICT_TAIL = (
    "Follow the reference images STRICTLY as binding specifications. "
    "Reproduce what they show exactly; never replace, blend or re-imagine it "
    "with your own association."
)

# ---------------- 产品组一致性约束（2026-10-06，7 图同款关联） ----------------
# 三层锁定：第 1 层文本锁（规格卡）→ 第 2 层视觉锁（兄弟锚点图）→ 第 3 层像素锁（composite 贴回）。

# 组模式兜底句：替代单图场景的 STRICT_TAIL，强调全系列一致
SERIES_TAIL = (
    "Follow the reference images STRICTLY as binding specifications. "
    "Reproduce what they show exactly; never replace, blend or re-imagine it "
    "with your own association. "
    "This photo belongs to a SERIES of the SAME product: keep material, finish, "
    "pattern, colors and details IDENTICAL across the series; only the camera "
    "angle follows the FIRST image. "
    "In particular the CAMERA MODULE FRAME/RING color, the button color and the "
    "edge color must be EXACTLY the same in every photo of the series - "
    "never vary them between photos."
)

# 规格卡提取提问（gpt-5.4 对基准图跑，产出 4 行 ≤120 词英文规格）
SPEC_Q = (
    "This is the reference photo of a product (photo 1 of a series of the SAME product).\n"
    "Write a compact DESIGN SPEC for it, in English, max 120 words, exactly 4 labeled lines:\n"
    "MATERIAL: base color, gloss/matte level, texture, grain\n"
    "FORM: overall shape, construction and style of the product\n"
    "PATTERN: printed artwork - motif, layout, colors - or write 'none'\n"
    "DETAILS: distinctive details - logo, stitching, edges, hardware, attachment points\n"
    "This spec will be injected into image-generation prompts to keep a whole photo "
    "series of this product perfectly consistent. Be precise and visual; no marketing words."
)

# 规格卡注入格式：拼在每张图每步生成提示词的最前面（文本锁）
SPEC_BLOCK = (
    "[DESIGN SPEC - SAME PRODUCT SERIES, BINDING]\n{spec}\n"
    "This spec is binding for every photo in this series: material, surface finish, "
    "pattern, colors and construction details must stay IDENTICAL across the whole series."
)

# 兄弟锚点约束：图2~7 生成时把图1（或组内最新成品）作为附加参考传入，
# 由 _ref_header 按编号注入这段（视觉锁）
SIBLING_REF = (
    "CRITICAL: Image {n} is a SIBLING photo of the SAME product shown from another angle. "
    "It is the BINDING STANDARD for this product series: your output must reproduce "
    "exactly the same material, surface finish, printed pattern, colors and construction "
    "details as this sibling photo. Pay special attention to the CAMERA MODULE FRAME/RING: "
    "its color, material and finish must be IDENTICAL to the sibling photo (same for the "
    "buttons and the case edges). Do NOT introduce any variation, redesign or "
    "re-interpretation - only adapt the perspective to match the FIRST image's viewpoint."
)

# 注册进 STRICT_REFS：_ref_header 会按参考图编号把这段拼进提示词（与其他 tag 同机制）
STRICT_REFS["sibling"] = SIBLING_REF


def render_spec_block(spec):
    """规格卡文本 → 注入块；spec 为空返回空串（单图模式不受影响）"""
    s = (spec or "").strip()
    if not s:
        return ""
    return SPEC_BLOCK.replace("{spec}", s) + "\n\n"


# 人工特征标注（目标锚定 · 最终产品 ground truth，2026-10-12 语义翻转）：
# 标注的是「要换进去的壳子」长什么样，不是原图现状。并入规格卡文本最前面，
# 冲突时压过 AI 提取的规格卡、参考图印象、以及原图上旧壳的材质颜色。
FEATURES_MARK = "USER-SPECIFIED PRODUCT FEATURES"
FEATURES_BLOCK = (
    "[" + FEATURES_MARK + " - FINAL PRODUCT GROUND TRUTH, HIGHEST PRIORITY]\n{features}\n"
    "These features describe the FINAL product the owner wants to build (the target case "
    "to swap in), NOT the current state of the base photo: they OVERRIDE any conflicting "
    "line in the design spec below, any conflicting impression from the reference images, "
    "and the OLD material, finish or base color of the case shown in the base photo.\n\n"
)


# 🔒 摄像头框一致性锁（2026-10-12）：「同个产品多张图换壳，摄像头框颜色却各不相同」的根因是
# 提示词里没有任何关于框色的确定性规则，AI 每张自由发挥（一张跟 EXTRA 走黑框、一张跟规格卡
# 走的旧白框）。这里给一条默认规则 + 每步硬约束，保证全组逐张一致。
CAM_FRAME_DEFAULT = (
    "CAMERA FRAME COLOR: unless a camera-frame color is explicitly stated above, the raised "
    "frame/ring around the camera module must be the SAME material and color as the case body - "
    "it changes together with the case, in EVERY photo of this series "
    "(never black on one photo and white on another)."
)
CAM_FRAME_LOCK = (
    "Camera module frame/ring: follow the CAMERA FRAME COLOR rule in the features block "
    "EXACTLY; its color and finish must be IDENTICAL across every photo of this series. "
    "Never invent, recolor or vary the frame on your own, and do NOT copy the old rim/frame "
    "color described in the spec card or visible in the original photo."
)


def merge_features(spec, features, cam_lock=True):
    """人工特征标注并入规格卡文本（幂等：已含特征块则不重复拼）。features 为空原样返回。

    cam_lock=True（本次任务动到手机壳/摄像头区）时，若用户没显式锚定摄像头框颜色，
    自动补一条「跟壳身同材质同色」的默认值——不留空白给 AI 自由发挥。"""
    f = (features or "").strip()
    s = (spec or "").strip()
    if not f:
        return s
    if FEATURES_MARK in s:
        return s
    if cam_lock and "CAMERA FRAME COLOR" not in f.upper():
        f = f + "\n" + CAM_FRAME_DEFAULT
    return FEATURES_BLOCK.replace("{features}", f) + s


def supersede_old_material(spec):
    """换材质任务专用：AI 提取的规格卡描述的是【原图旧壳】（旧材质/旧框色），
    和目标锚定直接冲突，实测会把换材质拉回旧状态（图1 换完还是亮面白壳）。
    这里把 MATERIAL 行标注作废，末尾再加一句全局声明，只留 FORM/PATTERN/DETAILS 做版式参考。"""
    if not (spec or "").strip():
        return spec
    out = []
    for ln in (spec or "").splitlines():
        if ln.strip().upper().startswith("MATERIAL:"):
            out.append("MATERIAL: (SUPERSEDED - the case material is being CHANGED in this task; "
                       "ignore this OLD description and follow the FINAL PRODUCT GROUND TRUTH above.)")
        else:
            out.append(ln)
    out.append("NOTE: any OLD case material, base color, gloss or camera-frame/rim color described "
               "above is the STATE OF THE ORIGINAL PHOTO and is superseded by the FINAL PRODUCT "
               "GROUND TRUTH when they conflict. This includes a rim/frame color stated anywhere "
               "in the spec card (for example 'white rim' or 'black rim' in FORM/DETAILS) - treat "
               "such a rim color as OLD and follow the FINAL PRODUCT GROUND TRUTH instead.")
    return "\n".join(out)


def extract_spec(client, image_path, vision_model=None):
    """
    对基准图（组内第 1 张）提取产品规格卡。
    返回 {"spec": "MATERIAL: ...\\nFORM: ...\\nPATTERN: ...\\nDETAILS: ...", "raw": 模型原文}
    提取失败/格式不对时抛 RuntimeError。
    """
    raw = client.describe(image_path, SPEC_Q, model=vision_model, max_tokens=400)
    if raw.startswith("[视觉模型"):
        raise RuntimeError(raw)
    txt = raw.strip()
    # 剥可能的 markdown 代码栅栏
    txt = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", txt).strip()
    # 宽松校验：4 个标签行至少要各出现一次
    labels = ["MATERIAL", "FORM", "PATTERN", "DETAILS"]
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    hit = {lb: any(ln.upper().startswith(lb + ":") for ln in lines) for lb in labels}
    missing = [lb for lb, ok in hit.items() if not ok]
    if missing:
        raise RuntimeError("规格卡缺标签行 " + "/".join(missing) + "：" + txt[:300])
    return dict(spec="\n".join(lines), raw=raw)


def render_prompt(part, action, desc=""):
    """从提示词库取模板并替换 {DESC}；没命中的操作退回 custom 模板"""
    tpl = PROMPT_LIB.get(f"{part}.{action}") or PROMPT_LIB.get(f"{part}.custom") or "{DESC}"
    return tpl.replace("{DESC}", (desc or "").strip() or "as described")


# ---------------- 部位识别 ----------------
DETECT_Q = (
    "Look at this product photo (a phone case, possibly with a lanyard / wrist strap "
    "and a phone stand / kickstand). Locate these parts: "
    "phone_case (the case body incl. its edges), "
    "lanyard (the strap / cord / charm attached to the phone), "
    "stand (the kickstand / ring holder / support attached to the case), "
    "camera_area (the camera module cutout region on the case). "
    'Reply with STRICT JSON only, no markdown fence, exactly this shape: '
    '{"parts":[{"id":"phone_case","present":true,"bbox":[x1,y1,x2,y2]},'
    '{"id":"lanyard","present":false,"bbox":[0,0,0,0]},'
    '{"id":"stand","present":false,"bbox":[0,0,0,0]},'
    '{"id":"camera_area","present":true,"bbox":[x1,y1,x2,y2]}]} '
    "where bbox uses 0-1000 coordinates normalized to image width and height, "
    "x1<x2, y1<y2, tightly covering that part. "
    "If a part is not visible in the photo, set present=false and bbox [0,0,0,0]."
)


def _clamp_bbox(b):
    try:
        x1, y1, x2, y2 = [max(0, min(1000, int(round(float(v))))) for v in b[:4]]
    except Exception:
        return None
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None
    return [x1, y1, x2, y2]


def detect_parts(client, image_path, vision_model=None):
    """返回 {"parts":[{"id","zh","present","bbox"}], "raw": 视觉模型原文}"""
    raw = client.describe(image_path, DETECT_Q, model=vision_model, max_tokens=600)
    if raw.startswith("[视觉模型"):
        raise RuntimeError(raw)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise RuntimeError("视觉模型没有返回 JSON：" + raw[:300])
    try:
        d = json.loads(m.group(0))
    except Exception:
        raise RuntimeError("视觉模型返回的 JSON 解析失败：" + m.group(0)[:300])
    got = {}
    for p in d.get("parts", []):
        pid = p.get("id")
        if pid in PART_MAP:
            box = _clamp_bbox(p.get("bbox") or [])
            got[pid] = dict(present=bool(p.get("present")) and box is not None,
                            bbox=box or [0, 0, 0, 0])
    parts = []
    for p in PARTS:
        g = got.get(p["id"], dict(present=False, bbox=[0, 0, 0, 0]))
        parts.append(dict(id=p["id"], zh=p["zh"], en=p["en"],
                          present=g["present"], bbox=g["bbox"]))
    return dict(parts=parts, raw=raw)


# ---------------- 局部保底贴回 ----------------
def composite(base_path, edited_path, bbox, out_path, expand=0.14, feather_ratio=0.018):
    """
    bbox = [x1,y1,x2,y2]（0-1000）。AI 出图重绘整张后：
      区域内 → 用 AI 结果（边缘羽化过渡）
      区域外 → 强制用原图像素（确定性保证「不改的地方一个像素都不动」）
    """
    base = Image.open(base_path).convert("RGB")
    ed = Image.open(edited_path).convert("RGB")
    if ed.size != base.size:
        ed = ed.resize(base.size, Image.Resampling.LANCZOS)
    W, H = base.size
    x1, y1, x2, y2 = bbox
    px1, py1, px2, py2 = x1 / 1000 * W, y1 / 1000 * H, x2 / 1000 * W, y2 / 1000 * H
    bw, bh = max(px2 - px1, 1), max(py2 - py1, 1)
    px1 = max(0, px1 - bw * expand); py1 = max(0, py1 - bh * expand)
    px2 = min(W, px2 + bw * expand); py2 = min(H, py2 + bh * expand)
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rectangle([px1, py1, px2, py2], fill=255)
    r = max(4, int(min(W, H) * feather_ratio))
    mask = mask.filter(ImageFilter.GaussianBlur(r))
    Image.composite(ed, base, mask).save(out_path)
    return out_path


def gen_size_for(base_path):
    """按底图比例挑 gpt-image-2 支持的尺寸，避免竖图被压成正方形"""
    w, h = Image.open(base_path).size
    r = w / max(h, 1)
    if r >= 1.2:
        return "1536x1024"
    if r <= 0.83:
        return "1024x1536"
    return "1024x1024"
