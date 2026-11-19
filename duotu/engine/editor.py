# -*- coding: utf-8 -*-
"""
多图融合编辑引擎（程序化 · 确定性 · 秒级出图）
沉淀自本项目多轮实测：换材质走亮度重映射（tanh 软压缩防崩图），
贴图案走图案层提取 + 纵向平铺 + 摄像头区羽化排除。
"""
import os, io, json, math, re, time, uuid
import numpy as np
from PIL import Image, ImageFilter, ImageDraw, ImageChops

LANCZOS = Image.Resampling.LANCZOS


# ---------------- 基础工具 ----------------
def imread(path):
    # 必须用 with 关闭句柄：Windows 上残留的打开文件会导致后续删除失败
    with Image.open(path) as im:
        if im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            return bg
        return im.convert("RGB")


def A(im):
    return np.asarray(im).astype(np.float32)


def I(a):
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def fields(im):
    a = A(im)
    mx = a.max(axis=2)
    mn = a.min(axis=2)
    return a, mx, mx - mn


def q(v, p):
    return float(np.percentile(v, p))


# ---------------- 分析 / 定位 ----------------
def main_color(im):
    """主色（量化统计）"""
    small = im.resize((160, 160), LANCZOS)
    a = A(small).reshape(-1, 3) // 16 * 16
    keys, cnt = np.unique(a.astype(np.int32), axis=0, return_counts=True)
    top = keys[cnt.argmax()]
    return [int(x) for x in top], float(cnt.max() / cnt.sum())


def neutral_med(im):
    """中性区（低饱和）中位亮度 —— 判定材质是否换成功的核心指标"""
    a, L, sat = fields(im)
    m = (sat < 18) & (L > 40) & (L < 252)
    return float(np.median(L[m])) if m.sum() > 100 else float(np.median(L))


def find_body(im):
    """白底/纯背景上的主体 bbox"""
    a, L, sat = fields(im)
    bg = (L > 245) & (sat < 14)
    h, w = bg.shape
    if bg.mean() > 0.98:
        return (0, 0, w, h)
    rows = (~bg).sum(axis=1)
    cols = (~bg).sum(axis=0)
    ry = np.where(rows > max(2, rows.max() * 0.02))[0]
    rx = np.where(cols > max(2, cols.max() * 0.02))[0]
    if len(ry) == 0 or len(rx) == 0:
        return (0, 0, w, h)
    return (int(rx[0]), int(ry[0]), int(rx[-1]) + 1, int(ry[-1]) + 1)


def find_camera(im, body):
    """定位摄像头模组：在主体上部 40% 找暗色团块，输出合理尺寸的 bbox"""
    bx0, by0, bx1, by1 = body
    bw, bh = bx1 - bx0, by1 - by0
    reg = im.crop((bx0, by0, bx1, by0 + int(bh * 0.40)))
    a, L, sat = fields(reg)
    H, W = L.shape
    # 镜片特征：深色且低饱和（排除彩色卡通图案的干扰）
    dark = (((L < 150) & (sat < 50)) | ((L < 95) & (sat < 90))).astype(np.uint8) * 255
    d = np.asarray(Image.fromarray(dark).filter(
        ImageFilter.GaussianBlur(max(3.0, W / 60.0)))).astype(np.float32) / 255.0
    default_box = (bx0, by0, bx0 + int(bw * 0.46), by0 + int(bh * 0.26))
    # 在低分辨率图上做连通域生长，避免远处散点把框撑大
    GW = 200
    GH = max(60, int(GW * H / max(1, W)))
    ds = np.asarray(Image.fromarray((d * 255).astype(np.uint8)).resize(
        (GW, GH), Image.Resampling.BILINEAR)).astype(np.float32) / 255.0
    cut = max(1, int(GH * 0.62))
    top = ds[:cut, :]
    if top.max() < 0.20:
        return default_box
    cy, cx = np.unravel_index(int(np.argmax(top)), top.shape)
    thr = max(0.25, float(ds[cy, cx]) * 0.55)
    seen = np.zeros((GH, GW), bool)
    stack = [(cy, cx)]
    comp = []
    limit = int(GW * GH * 0.08)
    while stack and len(comp) < limit:
        y, x = stack.pop()
        if y < 0 or y >= GH or x < 0 or x >= GW or seen[y, x] or ds[y, x] < thr:
            continue
        seen[y, x] = True
        comp.append((y, x))
        stack.extend([(y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)])
    if len(comp) < 6:
        return default_box
    ys = np.array([c[0] for c in comp])
    xs = np.array([c[1] for c in comp])
    x0 = float(xs.min()) / GW * W
    x1 = (float(xs.max()) + 1) / GW * W
    y0 = float(ys.min()) / GH * H
    y1 = (float(ys.max()) + 1) / GH * H
    # 尺寸约束：模组宽度机身 12%~42%，高度 8%~30%
    w = x1 - x0
    h = y1 - y0
    w = float(np.clip(w * 1.25, bw * 0.12, bw * 0.42))
    h = float(np.clip(h * 1.35, w * 0.55, min(bh * 0.30, w * 1.1)))
    mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    x0 = int(max(0, mx - w / 2))
    x1 = int(min(W, mx + w / 2))
    y0 = int(max(0, my - h / 2))
    y1 = int(min(H, my + h / 2))
    pad = int(bw * 0.02)
    return (max(bx0, bx0 + x0 - pad), max(by0, by0 + y0 - pad),
            min(bx1, bx0 + x1 + pad), min(by1, by0 + y1 + pad))


def body_round_mask(size, body, radius_ratio=0.10, feather=10):
    """机身圆角矩形蒙版（0-255）"""
    bx0, by0, bx1, by1 = body
    bw, bh = bx1 - bx0, by1 - by0
    r = int(min(bw, bh) * radius_ratio)
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle([bx0, by0, bx1 - 1, by1 - 1], radius=r, fill=255)
    return np.asarray(m.filter(ImageFilter.GaussianBlur(feather))).astype(np.float32) / 255.0


# ---------------- 动作 1：换材质 ----------------
def material_replace(src, ref=None, target_med=None, contrast=0.85, span=62.0,
                     strength=1.0, region=None, keep_pattern=True, feather=2.2):
    """
    把 src 的中性区亮度重映射到目标亮度（默认取 ref 的中性中位），图案与暗部结构保留。
    tanh 软压缩：高光/暗部不糊、层次不塌（这是防"崩图"的关键）。
    """
    if target_med is None:
        target_med = neutral_med(ref) if ref is not None else 224.0
    a, L, sat = fields(src)
    w_sat = np.clip(1.0 - (sat - 14) / 22.0, 0, 1)
    w_L = np.clip((L - 45) / 25.0, 0, 1) * np.clip((250 - L) / 25.0, 0, 1)
    w = w_sat * w_L
    w = np.asarray(Image.fromarray((w * 255).astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(feather))) / 255.0
    if not keep_pattern:
        w = np.minimum(w, 1.0)
    med0 = float(np.median(L[w > 0.6])) if (w > 0.6).sum() > 100 else float(np.median(L))
    newL = target_med + span * np.tanh((L - med0) * contrast / span)
    newL = np.clip(newL, 30, 253)
    k = newL / np.maximum(L, 1.0)
    k = 1.0 + (k - 1.0) * strength
    out = a * k[..., None] * w[..., None] + a * (1 - w[..., None])
    if region is not None:
        x0, y0, x1, y1 = region
        m = np.zeros(L.shape, np.float32)
        m[y0:y1, x0:x1] = 1
        m = np.asarray(Image.fromarray((m * 255).astype(np.uint8)).filter(
            ImageFilter.GaussianBlur(8))) / 255.0
        out = out * m[..., None] + a * (1 - m[..., None])
    return I(out), dict(target_med=round(target_med, 1), src_med=round(med0, 1),
                        contrast=contrast, strength=strength)


# ---------------- 动作 2：贴图案 ----------------
def pattern_mask(patt, sat_th=34, dark_th=112):
    """提取图案层（彩色 + 深色墨水线）"""
    a, L, sat = fields(patt)
    m = ((sat > sat_th) | (L < dark_th)).astype(np.uint8) * 255
    m = Image.fromarray(m)
    m = m.filter(ImageFilter.MedianFilter(5)).filter(ImageFilter.MaxFilter(3))
    m = m.filter(ImageFilter.GaussianBlur(0.7))
    return np.asarray(m) > 105


def pattern_transfer(base, patt, mode="tile", scale=1.0, cover=0.82,
                     avoid_cam=True, opacity=0.96, body=None, cam=None,
                     density=1.0, offset_y=-0.18):
    """把图案层贴到 base 的机身区域；mode: tile 平铺 / stretch 拉伸 / single 单贴"""
    if body is None:
        body = find_body(base)
    if cam is None and avoid_cam:
        cam = find_camera(base, body)
    bx0, by0, bx1, by1 = body
    bw, bh = bx1 - bx0, by1 - by0
    # 图案源 → 带 alpha 的图层（原尺寸）
    pm_bool = pattern_mask(patt)
    tile_full = patt.convert("RGBA")
    tile_full.putalpha(Image.fromarray((pm_bool * 255).astype(np.uint8)))
    # 缩放：让图案宽度 ≈ 机身宽度 * scale
    s = (bw / float(patt.size[0])) * scale
    tw = max(8, int(patt.size[0] * s))
    th = max(8, int(patt.size[1] * s))
    tile_full = tile_full.resize((tw, th), LANCZOS)
    tile_m = tile_full.split()[-1]

    px0 = bx0 + int(bw * 0.012)
    px1 = bx1 - int(bw * 0.012)
    py0 = by0 + int(bh * 0.06)
    py1 = by1 - int(bh * 0.012)
    if cam:
        py0 = max(py0, cam[3] + int(bh * 0.015))

    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    paste = Image.new("L", base.size, 0)

    if mode == "stretch":
        fg = tile_full.resize((max(8, px1 - px0), max(8, py1 - py0)), LANCZOS)
        fm = fg.split()[-1]
        layer.paste(fg, (px0, py0), fm)
        _stamp(paste, fm, (px0, py0))
    else:
        den = max(0.2, float(density))
        step_y = max(8, int(th * 0.98 / den))
        step_x = max(8, int(tw * 0.98 / den))
        y = py0 + int(th * offset_y)
        guard = 0
        while y < py1 and guard < 400:
            x = px0
            while x < px1 and guard < 4000:
                layer.paste(tile_full, (x, y), tile_m)
                _stamp(paste, tile_m, (x, y))
                x += step_x
                guard += 1
            y += step_y
            guard += 1

    rm = body_round_mask(base.size, body, 0.11, 12)
    pm_f = np.asarray(paste.filter(ImageFilter.GaussianBlur(0.6))).astype(np.float32) / 255.0
    cut = np.ones(pm_f.shape, np.float32)
    if cam and avoid_cam:
        cc = Image.new("L", base.size, 255)
        ImageDraw.Draw(cc).rectangle([cam[0], cam[1], cam[2], cam[3]], fill=0)
        cut = np.asarray(cc.filter(ImageFilter.GaussianBlur(14))).astype(np.float32) / 255.0
    alpha = pm_f * rm * cut * opacity
    la = np.asarray(layer).astype(np.float32)
    la[..., 3] = alpha * 255
    layer = Image.fromarray(np.clip(la, 0, 255).astype(np.uint8), "RGBA")
    out = Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")
    # 机身内侧轻微投影，让贴图不"浮"
    b = A(base)
    o = A(out)
    shade = np.clip(b / 255.0, 0.62, 1.0)
    res = o * (0.93 + 0.07 * shade) * alpha[..., None] + b * (1 - alpha[..., None])
    return I(res), dict(mode=mode, scale=scale, density=density,
                        cam_excluded=bool(cam), body=list(body))


def _rgba_with_mask(rgb, m):
    r = rgb.convert("RGBA")
    r.putalpha(m)
    return r


def _stamp(dst_L, tile_m, box):
    """把 tile_m 以 lighter 方式盖到 dst_L 上"""
    tmp = Image.new("L", dst_L.size, 0)
    tmp.paste(tile_m, box)
    dst_L.paste(ImageChops.lighter(dst_L, tmp))


# ---------------- 动作 3：去底托 / 局部清除 ----------------
def remove_base(im, ratio=0.16, mode="fill_bg", side="bottom"):
    """去掉底部（或指定边）的底托：用背景色羽化填充，或裁掉画布"""
    w, h = im.size
    if side == "bottom":
        y0 = int(h * (1 - ratio))
        box = (0, y0, w, h)
        crop_box = (0, 0, w, y0)
    else:
        y0 = int(h * ratio)
        box = (0, 0, w, y0)
        crop_box = (0, y0, w, h)
    if mode == "crop":
        return im.crop(crop_box), dict(mode="crop", ratio=ratio)
    a, L, sat = fields(im)
    corners = np.concatenate([a[0:8, 0:8].reshape(-1, 3), a[0:8, -8:].reshape(-1, 3),
                              a[-8:, 0:8].reshape(-1, 3), a[-8:, -8:].reshape(-1, 3)])
    bgc = np.median(corners, axis=0)
    m = np.zeros((h, w), np.float32)
    m[box[1]:box[3], box[0]:box[2]] = 1.0
    m = np.asarray(Image.fromarray((m * 255).astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(18))) / 255.0
    out = a * (1 - m[..., None]) + bgc * m[..., None]
    return I(out), dict(mode="fill_bg", ratio=ratio, bg=[int(x) for x in bgc])


# ---------------- 动作 4：整体微调 ----------------
def adjust(im, brightness=0.0, contrast=1.0, saturation=1.0):
    a, L, sat = fields(im)
    out = a * contrast + brightness
    if abs(saturation - 1.0) > 1e-3:
        gray = a.mean(axis=2, keepdims=True)
        out = gray + (out - gray) * saturation
    return I(out), dict(brightness=brightness, contrast=contrast, saturation=saturation)


# ---------------- 动作 5：摄像头区清图案 ----------------
def clean_camera(im, cam, sat_th=26, feather=12):
    """把摄像头模组区域的彩色图案抹掉，恢复为壳体本色（保留镜片暗部结构）"""
    if not cam:
        return im, dict(skipped=True)
    a, L, sat = fields(im)
    x0, y0, x1, y1 = [int(v) for v in cam]
    x0, y0 = max(0, x0), max(0, y0)
    sub_s = sat[y0:y1, x0:x1]
    sub_l = L[y0:y1, x0:x1]
    m = np.zeros(L.shape, np.float32)
    m[y0:y1, x0:x1] = ((sub_s > sat_th) & (sub_l > 118)).astype(np.float32)
    m = np.asarray(Image.fromarray((m * 255).astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(feather))) / 255.0
    # 填充色 = 该区域内非彩色像素的中位色（与壳体质感一致）
    sub_a = a[y0:y1, x0:x1]
    keep = ~((sub_s > sat_th) & (sub_l > 118))
    fill = np.median(sub_a[keep], axis=0) if keep.sum() > 50 else np.array([224, 224, 224])
    out = a * (1 - m[..., None]) + fill[None, None, :] * m[..., None]
    return I(out), dict(cam=list(cam), fill=[int(v) for v in fill],
                        cleaned_ratio=round(float(m.mean()), 4))


# ---------------- 校验指标 ----------------
def metrics(im, body=None, cam=None):
    if body is None:
        body = find_body(im)
    if cam is None:
        cam = find_camera(im, body)
    a, L, sat = fields(im)
    bx0, by0, bx1, by1 = body
    ba = sat[by0:by1, bx0:bx1]
    bl = L[by0:by1, bx0:bx1]
    color_ratio = float((ba > 40).mean()) if ba.size else 0.0
    p25, p75 = q(bl, 25), q(bl, 75)
    layers = p75 - p25
    body_neutral = neutral_med(im.crop(body))
    if cam:
        cw = im.crop(cam)
        _, _, cs = fields(cw)
        cam_color = float((cs > 40).mean())
    else:
        cam_color = 0.0
    white = float((L > 250).mean())
    mc, mcp = main_color(im)
    return dict(
        body_color_ratio=round(color_ratio, 4),
        cam_color_ratio=round(cam_color, 4),
        body_neutral_med=round(body_neutral, 1),
        layers=round(layers, 1),
        white_bg_ratio=round(white, 3),
        main_color=mc, main_color_ratio=round(mcp, 3),
        body=list(body), cam=list(cam) if cam else None,
    )


def grade(m, intent="material"):
    """按意图打分（0-100）：材质命中 45 + 图案覆盖 30 + 层次 15 + 镜头洁净 10"""
    s = 0.0
    cov = float(m.get("body_color_ratio") or 0)
    cam = float(m.get("cam_color_ratio") or 0)
    med = float(m.get("body_neutral_med") or 0)
    lay = float(m.get("layers") or 0)
    if intent in ("material", "mixed"):
        s += 45.0 * max(0.0, 1.0 - abs(med - 224) / 80.0)
    else:
        s += 25.0
    if intent in ("pattern", "mixed"):
        s += 30.0 * max(0.0, 1.0 - abs(cov - 0.11) / 0.11)
    s += 15.0 * min(1.0, lay / 48.0)
    s += 10.0 * max(0.0, 1.0 - cam / 0.03)
    return int(max(0, min(100, s)))
