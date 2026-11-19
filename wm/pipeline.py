"""核心处理管线：扫描 → 固定坐标 mask → inpaint → 对比图 → 落盘。

全程使用中文路径安全的读写（cv2.imread/imwrite 在 Windows 对非 ASCII 路径有坑，
这里改用 np.fromfile / imencode + tofile 规避）。
"""
import os
import cv2
import numpy as np

IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}


def read_image(path):
    try:
        data = np.fromfile(path, dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        return None


def save_image(path, img):
    ext = os.path.splitext(path)[1].lower()
    if ext not in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
        ext = ".png"
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        return False
    buf.tofile(path)
    return True


def scan_images(input_dir):
    """递归扫描文件夹内所有图片（支持多层子文件夹，ComfyUI 式整树读入）。"""
    if not input_dir or not os.path.isdir(input_dir):
        return []
    files = []
    for root, _dirs, fns in os.walk(input_dir):
        for fn in fns:
            if os.path.splitext(fn)[1].lower() in IMG_EXTS:
                files.append(os.path.join(root, fn))
    files.sort(key=lambda p: os.path.relpath(p, input_dir).replace("\\", "/"))
    return files


def rel_of(path, input_dir):
    """图片相对输入目录的路径（统一 / 分隔），作为多层结构的唯一标识。"""
    return os.path.relpath(path, input_dir).replace("\\", "/")


def safe_rel(p):
    """清洗前端传来的相对路径：去 .. / 盘符 / 绝对路径，防目录穿越。"""
    p = (p or "").replace("\\", "/").lstrip("/")
    parts = [seg for seg in p.split("/") if seg not in ("", ".", "..")]
    rel = "/".join(parts)
    if not rel or (len(rel) >= 2 and rel[1] == ":"):
        return ""
    return rel


def region_bbox(region):
    """返回 region 的外接矩形 (x, y, w, h)。
    rect 模式直接取 x/y/w/h；poly 模式由点集计算外接矩形。"""
    rtype = (region.get("type") or "rect").lower()
    if rtype == "poly":
        pts = region.get("points") or []
        if len(pts) >= 3:
            xs = [int(p[0]) for p in pts]
            ys = [int(p[1]) for p in pts]
            x1, y1 = min(xs), min(ys)
            x2, y2 = max(xs), max(ys)
            return x1, y1, x2 - x1, y2 - y1
        return 0, 0, 0, 0
    return (
        int(region.get("x", 0)),
        int(region.get("y", 0)),
        int(region.get("w", 0)),
        int(region.get("h", 0)),
    )


def build_mask(shape, region):
    """生成水印 mask。
    - rect：固定坐标矩形框
    - poly：自由圈选多边形（fillPoly 填充）
    """
    h, w = shape[:2]
    mask = np.zeros((h, w), np.uint8)
    rtype = (region.get("type") or "rect").lower()

    if rtype == "poly":
        pts = region.get("points") or []
        if len(pts) < 3:
            return mask
        poly = np.array(
            [[max(0, min(w - 1, int(p[0]))), max(0, min(h - 1, int(p[1])))] for p in pts],
            dtype=np.int32,
        )
        cv2.fillPoly(mask, [poly], 255)
        return mask

    x, y, rw, rh = region_bbox(region)
    if rw <= 0 or rh <= 0:
        return mask
    x1 = max(0, x)
    y1 = max(0, y)
    x2 = min(w, x + rw)
    y2 = min(h, y + rh)
    if x2 > x1 and y2 > y1:
        mask[y1:y2, x1:x2] = 255
    return mask


def process_image(image_path, engine, region, engine_config=None, prebuilt_mask=None):
    img = read_image(image_path)
    if img is None:
        raise RuntimeError("无法读取图片: %s" % image_path)
    region = dict(region or {})
    if prebuilt_mask is not None:
        mask = prebuilt_mask
    else:
        # poly 模式：把外接矩形坐标补进 region，供引擎提示词占位符使用
        if (region.get("type") or "rect").lower() == "poly":
            bx, by, bw, bh = region_bbox(region)
            region["x"], region["y"], region["w"], region["h"] = bx, by, bw, bh
        mask = build_mask(img.shape, region)
    if mask.max() == 0:
        # 空区域：
        # - 本地引擎（opencv / lama）必须依赖 mask 才能工作 → 原样返回；
        # - 远程模型引擎（custom / qwen）靠提示词驱动，允许不圈选，
        #   直接把整图交给模型处理（用户可用纯文字描述水印）。
        engine_name = getattr(engine, "name", "")
        if engine_name in ("opencv", "lama"):
            result = img.copy()
        else:
            result = engine.inpaint(img, mask, region=region, config=engine_config)
    else:
        result = engine.inpaint(img, mask, region=region, config=engine_config)
    if result is None or result.size == 0:
        raise RuntimeError("引擎返回空结果")
    if result.shape[:2] != img.shape[:2]:
        result = cv2.resize(result, (img.shape[1], img.shape[0]))
    return img, result


def make_comparison(original, result, sep_width=8):
    h = max(original.shape[0], result.shape[0])
    o = original
    r = result
    if o.shape[0] != h:
        o = cv2.resize(o, (int(o.shape[1] * h / o.shape[0]), h))
    if r.shape[0] != h:
        r = cv2.resize(r, (int(r.shape[1] * h / r.shape[0]), h))
    sep = np.full((h, sep_width, 3), 255, np.uint8)
    return np.hstack([o, sep, r])


# ============================================================
# 水印样例（模板匹配）模式：
# 用户在样图上画笔涂抹出水印外观 → 批量时逐张自动找同款水印
# ============================================================

def strokes_to_mask(shape, strokes):
    """画笔轨迹 → 二值 mask。
    strokes: [{"points": [[x,y],...], "width": 原图像素}, ...]（原图坐标系）
    """
    h, w = shape[:2]
    mask = np.zeros((h, w), np.uint8)
    for st in strokes or []:
        pts = st.get("points") or []
        width = max(1, int(st.get("width", 10)))
        for i in range(1, len(pts)):
            p1 = (max(0, min(w - 1, int(pts[i - 1][0]))), max(0, min(h - 1, int(pts[i - 1][1]))))
            p2 = (max(0, min(w - 1, int(pts[i][0]))), max(0, min(h - 1, int(pts[i][1]))))
            cv2.line(mask, p1, p2, 255, width)
        # 单点也画（点击一下）
        if len(pts) == 1:
            cv2.circle(mask, (int(pts[0][0]), int(pts[0][1])), max(1, width // 2), 255, -1)
    return mask


def extract_template(img, strokes):
    """从样图裁出水印模板 + 涂抹形状 mask（只取涂抹区外接矩形，留 4px 边距）。

    关键——背景均质化：未涂抹区域填充为涂抹区平均色。
    TM_CCOEFF_NORMED 会减去均值，常数背景对分子分母贡献≈0，
    匹配信号只来自涂抹轨迹覆盖的水印本体；否则模板夹带的随机背景
    会稀释真水印分数、并在缩小时退化成「平滑背景处处 ~0.75」的假阳性。
    """
    mask = strokes_to_mask(img.shape, strokes)
    if mask.max() == 0:
        return None, None
    ys, xs = np.where(mask > 0)
    x1, y1 = max(0, xs.min() - 4), max(0, ys.min() - 4)
    x2, y2 = min(img.shape[1], xs.max() + 5), min(img.shape[0], ys.max() + 5)
    template = img[y1:y2, x1:x2].copy()
    tmask = mask[y1:y2, x1:x2].copy()
    sel = tmask > 127
    if sel.any() and not sel.all():
        mean_color = template[sel].mean(axis=0)
        template[~sel] = mean_color
    return template, tmask


def _edge_dilate(gray):
    """Canny 边缘 + 3×3 膨胀（吸收 1px 左右的亚像素错位）。"""
    return cv2.dilate(cv2.Canny(gray, 50, 150), np.ones((3, 3), np.uint8))


def match_scores(img_gray, img_edge, tmpl_gray, tmask=None):
    """单尺度匹配：gray + 边缘双通道 + prod45 乘积组合。

    通道设计（全矩阵实测验证）：
    - gray   ：TM_CCOEFF_NORMED 灰度相关，区分度最好，但对跨尺度
               细字（双重 resize 亚像素相位差）会崩到 ≈0.4；
    - edil3  ：膨胀边缘 CCOEFF，区分度好但信号弱（真区常只有 ~0.47）；
    - eccor  ：膨胀边缘 CCORR_NORMED，信号强（细字真区 0.84）但
               区分度差（平滑噪声区也有 ~0.75-0.79）；
    - prod45 ：eccor × min(1, max(edil3,0)/0.45)——用 edil3 当门控
               压掉 eccor 的平滑区噪声，两者互补；
    - 组合   ：max(gray, edil3, prod)。
    不再用 blur 通道：缩小时模板退化为平滑背景，任何图的平滑区
    都会假阳性 ~0.75（是旧版跨水印误匹配的主因）。
    """
    h, w = tmpl_gray.shape[:2]
    if img_gray.shape[0] < h or img_gray.shape[1] < w:
        return None

    def _clean(s):
        if s is None:
            return None
        return np.where(np.isfinite(s), s, 0.0).astype(np.float32)

    gray = _clean(cv2.matchTemplate(img_gray, tmpl_gray, cv2.TM_CCOEFF_NORMED))
    te = cv2.Canny(tmpl_gray, 50, 150)
    if te.sum() <= 0:
        return gray
    ted = cv2.dilate(te, np.ones((3, 3), np.uint8))
    if img_edge.shape[0] < ted.shape[0] or img_edge.shape[1] < ted.shape[1]:
        return gray
    try:
        edil3 = _clean(cv2.matchTemplate(img_edge, ted, cv2.TM_CCOEFF_NORMED))
        eccor = _clean(cv2.matchTemplate(img_edge, ted, cv2.TM_CCORR_NORMED))
    except cv2.error:
        return gray
    prod = eccor * np.minimum(1.0, np.maximum(0.0, edil3) / 0.45)
    return np.maximum(np.maximum(gray, edil3), prod)


def find_watermark(img, template, tmask, threshold=0.65, scales=None):
    """多尺度模板匹配：在同款水印（可能缩放 0.5~2×）里找最佳位置。
    返回 (score, top_left(x,y), scale)；未命中返回 (0, None, None)。

    尺度步长 0.05：细字水印对尺度偏差极敏感（差 5~10% 相关性即崩），
    旧 12 档粗粒度会漏掉 0.667 这类中间缩放比。
    """
    if scales is None:
        scales = [round(0.5 + 0.05 * i, 2) for i in range(31)]
    img_gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    img_edge = _edge_dilate(img_gray)
    tmpl_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    th, tw = tmpl_gray.shape[:2]

    best = (0.0, None, 1.0)
    candidates = []  # (combined, gray_at_loc, maxloc, sc) 供近平局二次裁决
    for sc in scales:
        nw, nh = int(round(tw * sc)), int(round(th * sc))
        if nw < 12 or nh < 12:
            continue
        if nw >= img_gray.shape[1] or nh >= img_gray.shape[0]:
            continue
        t = cv2.resize(tmpl_gray, (nw, nh), interpolation=cv2.INTER_AREA if sc < 1 else cv2.INTER_LINEAR)
        m = cv2.resize(tmask, (nw, nh), interpolation=cv2.INTER_NEAREST)
        m_bin = (m > 127).astype(np.uint8) * 255

        combined = match_scores(img_gray, img_edge, t, m_bin)
        if combined is None:
            continue
        _, maxv, _, maxloc = cv2.minMaxLoc(combined)
        # 该尺度最高分位置的 gray 通道分（用于近平局裁决）
        gmap = cv2.matchTemplate(img_gray, t, cv2.TM_CCOEFF_NORMED)
        gval = float(gmap[maxloc[1], maxloc[0]])
        candidates.append((float(maxv), gval, maxloc, sc))
        if maxv > best[0]:
            best = (float(maxv), maxloc, sc)

    if best[1] is None or best[0] < threshold:
        return 0.0, None, None

    # 近平局裁决：eccor 通道对小尺度模板有系统性偏置（小模板落进大字内部
    # 也会拿高分），若另一尺度的 combined 与 best 接近（差 <0.08）但 gray 通道
    # 明显更强（高 >0.25），说明那才是真实尺度，改选它——否则会出现
    # 「0.55 尺度 0.812 分压过真实 0.8 尺度 0.759 分」导致 mask 只盖住局部文字。
    best_gray = next(g for v, g, loc, sc in candidates
                     if sc == best[2] and loc == best[1])
    for v, g, loc, sc in candidates:
        if sc == best[2]:
            continue
        if best[0] - v < 0.08 and v >= threshold and g - best_gray > 0.25 and g > 0.45:
            best = (v, loc, sc)
            best_gray = g
    return best


def template_mask_for_image(img_shape, template, tmask, top_left, scale):
    """把涂抹形状 mask 映射到目标图坐标系（模板位置+缩放），生成整图 mask。"""
    h, w = img_shape[:2]
    out = np.zeros((h, w), np.uint8)
    tx, ty = top_left
    th, tw = tmask.shape[:2]
    nw, nh = int(round(tw * scale)), int(round(th * scale))
    m = cv2.resize(tmask, (nw, nh), interpolation=cv2.INTER_NEAREST)
    x1, y1 = max(0, tx), max(0, ty)
    x2, y2 = min(w, tx + nw), min(h, ty + nh)
    if x2 <= x1 or y2 <= y1:
        return out
    out[y1:y2, x1:x2] = m[(y1 - ty):(y2 - ty), (x1 - tx):(x2 - tx)]
    return out
