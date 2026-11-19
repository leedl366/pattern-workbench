#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图案提取工作台 · 核心流水线（服务器版）
与 SKILL「商品图案提取生图」scripts 同源逻辑；超分用 cv2 EDSR x2（无GPU服务器友好）。
依赖: PIL numpy cv2 psd_tools（image2psd.py 在同目录）
"""
import base64, io, json, os, subprocess, sys, threading, time, urllib.error, urllib.request, uuid

import ai_sites                      # 站点池：同模型跨站点轮询 + 统一错误脱敏（2026-10-15）

BASE = os.path.dirname(os.path.abspath(__file__))
EDSR_MODEL = os.path.join(BASE, "models", "EDSR_x2.pb")
FSRCNN_MODEL = os.path.join(BASE, "models", "FSRCNN_x2.pb")
# 超分优先级：FSRCNN（轻量，CPU 上比 EDSR 快 10-50 倍）→ EDSR（重，画质更锐但 CPU 上极慢，易把机器拖垮）
SR_MODELS = [("FSRCNN_x2.pb", "fsrcnn"), ("EDSR_x2.pb", "edsr")]
KEY_FILE = os.path.join(BASE, ".apib_key")
TARGET_SHORT = 2048
DPI = 150
# 2026-10-08 用户拍板：整站从 apib.ai（异步 task+poll / gpt-image-2）整体换成 lensapi.cn
# （OpenAI 同步协议 / gpt-image-2.5）。见下方「AI 生图客户端」段的差异说明。
# 2026-10-15 雪碧拍板：站点与模型改为「站点池」统一管理（app/ai_sites.json + ai_sites.py），
# 轮询规则从「同站点换模型」改成「**同模型跨站点**」——gpt-image-2 在本站用不了就轮其它站的
# 同一个模型。下面两个常量保留为「本站的模型优先顺序 + 兜底 base」，实际请求走 ai_sites.plan_order()。
API_BASE = os.environ.get("PATTERN_API_BASE", "https://lensapi.cn")
# 2026-10-15 扩兜底：gpt-image 系上游长期不可用，原链尾只有 1 条 gemini 且**间歇性** 503
# → 整站随时全挂。追加 lensapi 上实测可用的另两条 gemini（见 ai_sites.DEFAULT_MODEL_ORDER 注释）。
# 2026-10-15 雪碧拍板（"apib 的模型要进所有功能" + "排第一位，质量优先"）：
# 链首补 apib 的官方版 gpt-image-2-official —— 对「1:1 复刻原图版式」最强，先用它；
# 失败再按池内轮询落到 lensapi/grsai 的其余模型兜底。apib 走私有异步协议，见 ai_sites.apib_gen。
# 2026-10-16 11:14 修正（雪碧拍板「方案 A」）：`gpt-image-2-official` 在该 key 下 **403 无权限**
# （0.2s 秒拒、每轮白跑一跳），apib 因此从未被真正命中 ⇒ 链首改用**有权限的 `gpt-image-2`**
# （实测 17.8s）。链尾补 apib 站内独有的两条 gemini（nano-banana-2.1 / flash-lite）做全池兜底。
# 与 ai_sites.DEFAULT_MODEL_ORDER / wm.DEFAULT_MODELS 必须同源。
API_MODEL_CHAIN = tuple(m for m in os.environ.get(
    "PATTERN_API_MODELS",
    "gpt-image-2,gpt-image-2.5,gemini-3-pro-image-preview,"
    "gemini-nano-banana-2.1-vip,gemini-3.1-flash-image-preview,"
    "gemini-nano-banana-2.1,gemini-3.1-flash-lite-image").split(",") if m)

# ---------------- 按「参考图张数」分线（2026-11-15 雪碧拍板） ----------------
# 依据 2026-11-14 三站实测：
#   · 多图参考（一次喂 3 张）只有 apib / huahuaapi **稳定真融合**；lensapi 12 次仅 5 次成功
#     （其余只吃中间那张）⇒ 多图线必须把 lensapi 排除。
#   · 单图编辑/满幅重绘/贴回：lensapi **全尺寸同价 ¥0.01（含 4096²）**，最划算 ⇒ 单图线 lensapi 优先。
#   · 雪碧 2026-11-15 明确：**多图主力用 apib**（稳、快 18~29s），huahuaapi 做多图兜底。
#
# ⚠️ 下面四个常量**只是默认值**（真正的单一真源在 ai_sites.DEFAULT_ROUTING）。
#    实际生效顺序**以后台配置 ai_sites.json 的 `routing` 段为准** —— 雪碧要求「不要定死」，
#    在「系统设置 → ② 分线规则」里可以随时改站点顺序、模型顺序、超时，保存即生效、无需重启。
MULTI_CHAIN = tuple(ai_sites.DEFAULT_ROUTING["multi"]["models"])
MULTI_SITES = tuple(ai_sites.DEFAULT_ROUTING["multi"]["sites"])
SINGLE_SITES = tuple(ai_sites.DEFAULT_ROUTING["single"]["sites"])
MULTI_TIMEOUT = ai_sites.DEFAULT_ROUTING["multi"]["timeout"]

STEP1_PROMPT = (
    "图中是同一个手机壳的{imgs}。请完全去掉画面中的手部、水印和文字，"
    "{multi}互相参考，用对方露出的图案补全被手挡住的区域，得到一张完整、干净的手机壳产品图。"
    "图案颜色、样式必须与原图完全一致，禁止添加任何新元素。纯浅色背景。"
)
STEP2_PROMPT = (
    "从这个手机壳上提取完整的表面图案，生成一张图案平铺展示图：把壳上的图案完整提取出来，"
    "平铺展示，正面视角，无透视变形。只提取原图案里已有的元素，禁止添加或发明任何新元素。"
    "被遮挡的部分按图案整体规律补全。纯浅色背景，图案居中占满大部分画面。"
)

# 满幅重绘路线（用户拍板的「两版都出」里的重绘版）——核心是**版式死锁**：
# 允许 AI 提升质感/清洁画面，但一个元素都不许动。
# 2026-10-09 用户拍板「方案 A」修复「AI 把手机壳也画出来」：
#   旧 prompt 反复出现「手机壳产品图 / 铺满整个手机壳表面」，且只禁了「立体阴影与反光」、
#   从未禁止画出壳本身 → 大纯色底壳（如米黄底）会被 AI 当成画面主体，把壳身色铺满整幅，
#   左右还留下对称的黄壳侧边（同批另一任务因图案高饱和才侥幸没翻车）。
#   现改为：**输出对象 = 图案本身**，并硬性列出「不要壳身/侧边/圆角轮廓/开孔/厚度/反光」
#   + 图案外空白一律纯白 #FFFFFF。改 prompt 不动几何链，重跑一次即验证。
#   2026-10-09 实测复验：改后重跑同一任务 → 左右黄壳侧边饱和度 76→11/18、四角背景米黄→纯白
#   (254,254,254)，壳边已消除 ✅。但同时发现上游对「精确复刻输入图」有**概率性内容审核**
#   （旧 prompt 现在也会被拒，约 1/2 概率，码 upstream_text_r）→ 措辞里**避免"印刷/印花/贴纸/
#   素材/图稿"**这类暗示"复制成品"的词（实测带这些词必被拒，去掉即过），并在 redraw_fullbleed
#   加 REDRAW_RETRIES 次自动重试兜概率拒绝。
REDRAW_PROMPT = (
    "这是一张手机壳表面的图案。请把**图案本身**重绘成一张完整、干净的**满幅图案**，"
    "正面视角、无透视变形。\n"
    "【输出对象：只要图案，不要手机壳 · 最高优先级】画布上只允许出现图案元素，"
    "**绝对不要出现手机壳**：\n"
    "· 不要壳身、不要壳体底色或任何壳体色块，不要左右侧边 / 上下边缘 / 圆角轮廓 / 四角弧度；\n"
    "· 不要按键孔、充电孔、摄像头孔、支架孔、镜头保护框、金属圆环等任何壳体开孔与配件；\n"
    "· 不要壳的厚度、立体感、投影、反光、高光；\n"
    "· 图案以外的空白一律填**纯白 #FFFFFF**，不要把壳身的颜色铺满当作底色。\n"
    "【配件处理】原图壳上如果装有**圆支架、镜头保护框、摄像头模组**等配件，请把它们整体去掉，"
    "并按周围图案的风格与疏密把配件占的那块位置补成同风格的图案（不要保留配件本身、"
    "不要保留银环和黑色镜片）。\n"
    "【版式锁定铁律】除了上述配件位置，其余每一个图案元素的**位置、大小、数量、"
    "朝向、相对间距**必须完全不变，像描图一样 1:1 复刻：\n"
    "· 禁止新增任何元素（不许添加原图没有的太阳、彩虹、动物、云朵、**小花、小草、蝴蝶、爱心、"
    "星星、闪光、文字**等任何装饰，哪怕是很小的一点花边也不行）；\n"
    "· 禁止删除任何元素；\n"
    "· 禁止移动、旋转、缩放、重排任何元素；\n"
    "· 禁止改变画面构图、留白与疏密布局。\n"
    "【允许做的】只是把线条画干净、颜色涂均匀、去掉反光/噪点/水印/杂点/灰尘，提升清晰度与质感。\n"
    "输出：整幅画布就是图案本身（不是手机壳），图案以外的空白为**纯白 #FFFFFF**，"
    "不做任何版式改动。"
)
# 整幅底图版（2026-10-14 用户需求）：勾「整幅底图」时用——保留原壳底色铺满画布，
# 而不是图案外空白填白。**版式锁定铁律 / 配件去除 / 禁增删移缩条款与 REDRAW_PROMPT 一字不差**
#（都是踩坑换来的），只改「输出对象」与「背景」两段；同样避开审核敏感词（印刷/印花/贴纸/素材/图稿）。
REDRAW_PROMPT_FULL = (
    "这是一张手机壳表面的图案。请把**图案连同它的底色**重绘成一张完整、干净的**满幅底图**，"
    "正面视角、无透视变形。\n"
    "【输出对象：整幅底图 · 最高优先级】整张画布 = 图案 + 它的底色，铺满边缘到边缘：\n"
    "· 识别原图壳面上的**底色**（背景色），把它作为整幅画的背景色铺满整个画布；\n"
    "· 图案元素直接画在底色上，元素之间露出的部分就是底色本身（不要填白色，除非底色本来就是白色）；\n"
    "· **绝对不要画出手机壳**：不要壳的外轮廓 / 圆角 / 四角弧度 / 左右侧边 / 上下边缘，"
    "不要壳的厚度、立体感、投影、反光、高光；\n"
    "· 不要按键孔、充电孔、摄像头孔、支架孔、镜头保护框、金属圆环等任何壳体开孔与配件。\n"
    "【配件处理】原图壳上如果装有**圆支架、镜头保护框、摄像头模组**等配件，请把它们整体去掉，"
    "并按周围图案的风格与疏密把配件占的那块位置补成同风格的图案（不要保留配件本身、"
    "不要保留银环和黑色镜片）。\n"
    "【版式锁定铁律】除了上述配件位置，其余每一个图案元素的**位置、大小、数量、"
    "朝向、相对间距**必须完全不变，像描图一样 1:1 复刻：\n"
    "· 禁止新增任何元素（不许添加原图没有的太阳、彩虹、动物、云朵、**小花、小草、蝴蝶、爱心、"
    "星星、闪光、文字**等任何装饰，哪怕是很小的一点花边也不行）；\n"
    "· 禁止删除任何元素；\n"
    "· 禁止移动、旋转、缩放、重排任何元素；\n"
    "· 禁止改变画面构图、留白与疏密布局。\n"
    "【允许做的】只是把线条画干净、颜色涂均匀、去掉反光/噪点/水印/杂点/灰尘，提升清晰度与质感。\n"
    "输出：整幅画布 = 底色 + 图案（不是手机壳），背景铺满原壳底色，"
    "不做任何版式改动。"
)
# 满幅重绘的「审核软拒绝」自动重试次数（2026-10-09）：上游对精确复刻类请求有概率性内容审核，
# 报 upstream_text_r「违反与第三方内容相似性的防护限制」；同 prompt 重试可过，故自动重试。
REDRAW_RETRIES = int(os.environ.get("REDRAW_RETRIES", "2"))
GEN_SIZE = {"1:1": "1024x1024", "3:4": "1024x1536", "9:16": "1024x1536"}

# ---------------- 输出尺寸档位（2026-10-21 新增：用户在页面上选输出比例）----------------
# 背景：此前页面上的「输出尺寸」是个空壳（只有 label 没有控件），后端 ratio 字段收下就丢，
#真正决定出图尺寸的一直是 fullbleed_size() 按**壳身实际比例**算 —— 手机壳从 3:4 产品图里
# 裁出来约 2.2:1 的细长条，于是出的一直是细长图，跟用户要的 1:1 / 3:4 / 9:16 对不上。
#
# 设计原则（雪碧拍板 A 方案：**补边不补图**）：
#   目标比例与壳身实际比例不一致时，**先把干净壳等比补边到目标比例再送 AI**，
#   而不是拉伸。理由链：
#     ① 拉伸 = 元素横向压扁，「版式死锁 1:1」当场作废，整条流水线的核心承诺就没了；
#     ② 补边只扩画布不动内容 → 元素位置/大小/相对间距逐像素不变，版式校验照样能过；
#     ③ **不多花一次生图**（对比「AI 延伸补满」方案每张多 20~40s + 一张图的钱）；
#     ④ 上游 apib/lensapi 会把请求尺寸就近吸附到自家分辨率但**长宽比基本保留**，
#        所以只要给对比例即可，具体像素以出图实测为准（server 回读真实尺寸）。
#   边界处理：比例已在 ±2% 以内（等价于同比例）→ **零改动直通**，不碰像素。
RATIO_AUTO = "auto"
RATIO_PRESETS = {
    "1:1": (1, 1), "3:4": (3, 4), "9:16": (9, 16), "2:3": (2, 3),
    "4:3": (4, 3), "3:2": (3, 2), "16:9": (16, 9),
}
RATIO_MIN_SIDE = 256          # 自定义尺寸下限：太小上游会拒 / 图案糊成马赛克
RATIO_MAX_SIDE = 4096         # 上限：与 TARGET_SHORT(2048) 同量级，再大只是白烧带宽
RATIO_DEF_SHORT = 1024        # 预设档位短边像素（gpt-image 系的甜点区）
RATIO_TOL = 0.02              # 比例容差：|cur-tgt|/tgt ≤ 2% 视为同比例，不补边


def parse_ratio(val, short=RATIO_DEF_SHORT):
    """把前端传来的「输出尺寸」解析成 (标签, (宽比, 高比), 请求像素串)。

    支持三种写法（都可混用，非法值一律降级为 auto，绝不抛异常把任务带崩）：
      · 'auto' / '自动' / '' → 自动（按壳身实际比例，= 历史行为）
      · '3:4' 等预置档位   → 取 RATIO_PRESETS
      · '1280x720' / '1280*720' / '1280:720' → 自定义像素（256~4096）
    返回 (label, (rw, rh), 'WxH')；auto 时后两项为 (None, None)。
    """
    s = str(val or "").strip().replace("×", "x").replace("X", "x").replace("*", "x")
    s = s.replace("：", ":").replace("／", "/")   # 中文全角冒号（用户手填常见）
    if (not s) or s.lower() in (RATIO_AUTO, "自动", "0", "none", "null"):
        return RATIO_AUTO, None, ""
    if s in RATIO_PRESETS:
        rw, rh = RATIO_PRESETS[s]
        return s, (rw, rh), _ratio_px(rw, rh, short)
    import re
    m = re.match(r"^(\d{2,5})\s*[x:]\s*(\d{2,5})$", s)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if RATIO_MIN_SIDE <= w <= RATIO_MAX_SIDE and RATIO_MIN_SIDE <= h <= RATIO_MAX_SIDE:
            # 自定义像素：原样用（调用方要的是精确像素，不是比例）
            return s, (w, h), "%dx%d" % (w - w % 2, h - h % 2)
        return RATIO_AUTO, None, ""
    return RATIO_AUTO, None, ""


def _ratio_px(rw, rh, short=RATIO_DEF_SHORT):
    """比例 → 请求像素：短边固定 short，长边按比例，四舍五入到偶数并夹进合法区间。"""
    try:
        rw, rh = max(1, int(rw)), max(1, int(rh))
    except Exception:
        return ""
    if rw >= rh:
        w, h = short, int(round(short * rh / float(rw)))
    else:
        h, w = short, int(round(short * rw / float(rh)))
    w = max(RATIO_MIN_SIDE, min(RATIO_MAX_SIDE, int(round(w / 2.0)) * 2))
    h = max(RATIO_MIN_SIDE, min(RATIO_MAX_SIDE, int(round(h / 2.0)) * 2))
    return "%dx%d" % (w, h)


def _is_custom_px(ratio):
    """判断该ratio 是不是「自定义像素」档（WxH 写法）→ 自定义必须精确落地到用户填的像素。"""
    s = str(ratio or "").strip().lower().replace("×", "x").replace("*", "x").replace("：", ":")
    if not s or s in RATIO_PRESETS or s in (RATIO_AUTO, "自动"):
        return False
    return "x" in s


def fit_to_ratio(src, dst, rw, rh, bg=None, tol=RATIO_TOL, dst_px="", exact=False):
    """把图片**等比补边**到 rw:rh 的画布（不裁切、不拉伸），返回诊断 dict。

    · 比例已在容差内 → 原样复制，零像素改动（`padded=False`）；
    · 否则等比缩放到内接 + 居中补边（`padded=True`），元素版式逐像素不变；
    · 源图带alpha（RGBA）→ 默认补**透明**；不带 alpha → 补纯白（与满幅重绘 prompt
      「图案以外纯白 #FFFFFF」一致；整幅底图模式请传 bg=原壳底色）。
    · `dst_px`（'WxH'，来自 parse_ratio 的第三返回值）：**显式目标像素，原样使用**。
      预置档位它就是「短边1024按比例换算」的结果（与 dst_px 缺省时同值）；
      自定义档位（如 1280x720）必须原样落地 —— 不能被短边规则改写成 1024x576，
      否则「我填多少就出多少」的承诺就废了。
    · `exact=True`（自定义档位专用）：**必须精确等于 dst_px**。
      tol 判定只看比例 → 比例相同就直通，尺寸不同也不会改 —— 所以自定义档位必须
      绕过 tol，强制 contain 缩放 + 补边到精确像素（上游实出会吸附尺寸，如
      请求 1024x1024 实出 1254x1254；用户填 1280x720 就该是 1280x720）。
    返回 {padded, src_size, dst_size, scale, fill}，异常时返回 {error: ...}（调用方须容忍）。
    """
    import shutil
    from PIL import Image
    out = {"padded": False, "src_size": None, "dst_size": None,
           "scale": 1.0, "fill": None}
    try:
        with Image.open(src) as im0:
            im = im0.convert("RGBA")
        W, H = im.size
        out["src_size"] = [W, H]
        tgt = float(rw) / float(rh)
        cur = float(W) / float(H)
        px = str(dst_px or "").strip()
        want = None
        if "x" in px.lower():
            _a, _b = px.lower().split("x", 1)
            want = (int(_a), int(_b))
        same_px = want is not None and want == (W, H)
        if (not exact) and (W <= 0 or H <= 0 or abs(cur - tgt) <= tol * tgt) and not same_px:
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.copyfile(src, dst)
            out["dst_size"] = [W, H]
            return out
        if want is not None:
            tw, th = want
        else:
            tw, th = _ratio_px(rw, rh).split("x")
            tw, th = int(tw), int(th)
        tw = max(1, tw - tw % 2); th = max(1, th - th % 2)
        sc = min(tw / float(W), th / float(H))
        nw, nh = max(1, int(round(W * sc))), max(1, int(round(H * sc)))
        has_a = im.mode == "RGBA" and (im.getchannel("A").getextrema()[0] < 250)
        if bg is None:
            fill = (0, 0, 0, 0) if has_a else (255, 255, 255, 255)
        else:
            b = tuple(bg) if isinstance(bg, (tuple, list)) else (int(bg),) * 3
            fill = (b[0], b[1], b[2], 0 if has_a else 255)
        canvas = Image.new("RGBA", (tw, th), fill)
        canvas.paste(im.resize((nw, nh), Image.LANCZOS), ((tw - nw) // 2, (th - nh) // 2))
        canvas.save(dst)
        out.update({"padded": True, "dst_size": [tw, th],
                    "scale": round(sc, 4), "fill": list(fill)})
        return out
    except Exception as ex:
        out["error"] = str(ex)[:160]
        return out


def fullbleed_size(case_w, case_h, short=1024, lo=1.5, hi=2.6):
    """满幅重绘的请求尺寸：**按壳身实际长宽比**出，避免把竖长的手机壳图案塞进 3:4 画布
    （塞进去 = 元素被横向拉伸，「1:1 锁定」就名存实亡）。

    实测 apib.ai（现为 lensapi.cn）的 gpt-image 系列会把请求尺寸就近吸附到自家可用分辨率，但**长宽比基本保留**
    （1024x2048 → 1344x2688 比例精确；1024x1792 → 941x1672 精确；1152x2496 → 1152x2688 会漂），
    所以这里只要给对比例即可，具体像素以出图实测为准（server 会回读真实尺寸）。"""
    try:
        w, h = float(case_w), float(case_h)
    except Exception:
        return "1024x1536"
    if w <= 0 or h <= 0:
        return "1024x1536"
    r = h / w
    r = min(max(r, lo), hi)                       # 夹在 1.5~2.6 之间，太离谱的比例 API 会乱吸附
    return "%dx%d" % (short, int(round(short * r / 2.0) * 2))


def fullbleed_size_for(case_w, case_h, long_edge=None, short=1024, lo=1.5, hi=2.6):
    """「按壳身实际比例」+ 可指定**长边**的请求尺寸（2026-11-15 新增）。

    为什么需要它：页面上的「输出尺寸」拆成「比例区 + 分辨率区」后，比例选「自动」时
    用户仍可能选 2K/4K —— 此时形状由壳身决定（只有后端裁完壳身才知道比例），
    清晰度由用户指定。fullbleed_size 的 short 参数是**短边（宽）**，
    直接传 2048 会得到 2048×4500 这种「短边 2048」而不是「长边 2048」，所以这里反算。

    · long_edge 为空/非法 → **完全等价于** fullbleed_size(case_w, case_h, short, lo, hi)，
      老调用点一个字节都不会变；
    · 否则先按 short=1024 拿到壳身比例基准，再等比缩放到目标长边（比例严格保持）；
    · 结果强制偶数、单边 ≤ 4096（上游硬上限，超了必失败退费）。
    """
    try:
        le = int(long_edge or 0)
    except Exception:                              # noqa: BLE001
        le = 0
    if le <= 0:
        return fullbleed_size(case_w, case_h, short=short, lo=lo, hi=hi)
    le = max(256, min(4096, le))
    base = fullbleed_size(case_w, case_h, short=1024, lo=lo, hi=hi)
    try:
        bw, bh = [float(v) for v in base.split("x")]
    except Exception:                              # noqa: BLE001
        return fullbleed_size(case_w, case_h, short=short, lo=lo, hi=hi)
    m = max(bw, bh) or 1.0
    s = float(le) / m
    W, H = bw * s, bh * s
    if max(W, H) > 4096:                           # 极端比例兜底：长边压到 4096
        s2 = 4096.0 / max(W, H)
        W, H = W * s2, H * s2
    W = max(2, int(round(W / 2.0) * 2))
    H = max(2, int(round(H / 2.0) * 2))
    return "%dx%d" % (W, H)

# ---------------- AI 生图客户端（lensapi.cn · OpenAI 同步协议） ----------------
# 与旧 apib.ai 协议的三处关键差异（2026-10-08 实测）：
#   ① 带输入图 → POST /v1/images/edits（multipart 上传 image 文件）；纯文生图 → /v1/images/generations（JSON）；
#   ② size 必须**显式**给（传 auto 会报 "size is not supported"）；
#   ③ 响应即成品：data[0].url 就是图片外链，**没有 task_id、不需要轮询**（单张 29–58s 同步返回）。
#   另：上游偶发 500 do_request_failed（约 30s 超时）→ 故加「模型兜底链」逐个换模型重试。
def load_key():
    if not os.path.exists(KEY_FILE):
        raise FileNotFoundError("密钥文件缺失: " + KEY_FILE)
    return open(KEY_FILE, encoding="utf-8").read().strip()

def http_json(url, payload=None, key=None):
    req = urllib.request.Request(url, method="POST" if payload else "GET")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PatternWorkbench/2.0")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(payload).encode("utf-8")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))

def as_str(u):
    if isinstance(u, list):
        u = u[0] if u else None
    if isinstance(u, dict):
        u = u.get("url") or u.get("image_url")
    return u if isinstance(u, str) else None

def _multipart(fields, files):
    """手工拼 multipart/form-data。files=[(field, filename, content_type, raw_bytes)]"""
    bd = "----PatternWB" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                % (bd, k, v)).encode("utf-8")
    for nm, fn, ct, raw in files:
        out += ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                'Content-Type: %s\r\n\r\n' % (bd, nm, fn, ct)).encode("utf-8")
        out += raw + b"\r\n"
    out += ('--%s--\r\n' % bd).encode("utf-8")
    return bd, bytes(out)

def _gen_once(key, model, prompt, size, image_b64, base=None, protocol="openai",
              refs=None, timeout=None):
    """单次生图。**双协议**（2026-10-15）：

      protocol="openai"（lensapi / huahuaapi）：同步 —— 有图走 /v1/images/edits（multipart），
                    无图走 /v1/images/generations（JSON），响应即成品 data[0].url|b64_json。
      protocol="apib"  （api.apib.ai）：私有异步 —— 一律走 generations（参考图放 image 数组），
                    拿 task_id 轮询 /v1/tasks/<id>，见 ai_sites.apib_gen。

    **多图参考（2026-11-15）**：`refs=[b64, b64, ...]` 时
      · openai 协议 → multipart 里挂**多个 `image[]` 字段**（实测 huahua 认这个写法）；
      · apib 协议   → `image` 数组塞多个 data URI（实测 3 张全融合）。
      refs 为空/1 张时退回老的 `image_b64` 单图路径，行为完全不变。

    base 缺省 = 本站 API_BASE；站点池轮询时由调用方传入对应站点的 base。返回图片 URL / data URI。"""
    base = (base or API_BASE).rstrip("/")
    size = size or "1024x1024"
    imgs = [r for r in (refs or []) if r]
    if len(imgs) <= 1:
        imgs = []                                    # 单图/无图 → 走老路径
        if not image_b64 and refs:
            image_b64 = refs[0]
    if protocol == "apib":
        # apib 的 /v1/images/edits 只服务 grok 系，gpt-image 系必须把参考图塞进 generations 的 image 数组。
        return ai_sites.apib_gen(base, key, model, prompt, size,
                                 images=(imgs or ([image_b64] if image_b64 else None)),
                                 timeout=(timeout or 420))
    if imgs:
        files = [("image[]", "in%d.jpg" % i, "image/jpeg", base64.b64decode(r))
                 for i, r in enumerate(imgs)]
        bd, body = _multipart({"model": model, "prompt": prompt, "size": size, "n": "1"}, files)
        req = urllib.request.Request(base + "/v1/images/edits", data=body, method="POST")
        req.add_header("Content-Type", "multipart/form-data; boundary=" + bd)
    elif image_b64:
        raw = base64.b64decode(image_b64)
        bd, body = _multipart({"model": model, "prompt": prompt, "size": size, "n": "1"},
                              [("image", "in.jpg", "image/jpeg", raw)])
        req = urllib.request.Request(base + "/v1/images/edits", data=body, method="POST")
        req.add_header("Content-Type", "multipart/form-data; boundary=" + bd)
    else:
        payload = {"model": model, "prompt": prompt, "size": size, "n": 1}
        req = urllib.request.Request(base + "/v1/images/generations",
                                     data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PatternWorkbench/2.0")
    try:
        with urllib.request.urlopen(req, timeout=(timeout or 420)) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s %s" % (e.code, e.read().decode("utf-8", "ignore")[:200]))
    data = resp.get("data") if isinstance(resp, dict) else None
    d = data[0] if isinstance(data, list) and data else None
    if not isinstance(d, dict):
        raise RuntimeError("生图响应异常: %s" % json.dumps(resp, ensure_ascii=False)[:240])
    u = as_str(d.get("url"))
    if not u:
        # ⚠️ 实测：不同上游返回不一 —— gpt-image-2.5 回外链 data[0].url；
        #    而 gemini-3-pro-image-preview 回 data[0].b64_json（且 url 字段是空串）→ 统一成 data URI。
        b = as_str(d.get("b64_json"))
        if b:
            u = "data:image/png;base64," + b
    if not u:
        raise RuntimeError("生图响应无图片（url/b64_json 均空）: %s" % json.dumps(
            {k: (v[:40] + "…" if isinstance(v, str) and len(v) > 40 else v) for k, v in d.items()},
            ensure_ascii=False)[:200])
    return u

def gen_image(key, prompt, size, image_b64=None, refs=None, models=None):
    """同步生图，成功直接返回**图片 URL / data URI**（由 wait_task 原样透传）。

    **分线（2026-11-19 雪碧拍板：图生图单独配规则、文生图保持不动）**——按参考图张数选三条线：
      · refs ≥ 2 张 → **multi 多图线**：huahuaapi（实测 2/2 真融合；lensapi 477 必须排除）；
      · refs = 1 张 → **i2i 图生图线**：lensapi → huahuaapi，模型按**实测耗时**排序
        （2026-11-19 逐站实测两站各 5 个 gpt-image 模型 10/10 全通，26.0~68.7s）；
      · refs = 0 张 → **t2i 文生图线**：**保持历史行为**（lensapi → huahuaapi → apib，
        模型沿用 model_order），雪碧明确「前面文生图的就不用改」。
      欠费站点（apib 余额不足 / grsai insufficient credits）已从 i2i/multi 摘出，
      不再白跑一跳、也不会把错误码盖成 402 掩盖真因。

    线内仍是「外层模型、内层站点」轮询：同一个模型先跨站点挨个试，全挂才换下一个模型；
    成功的「站点+模型」组合下次排最前。
    全过程失败只回「生图失败（错误码 xxx），请稍后重试。」——不泄漏模型名 / 站点名 / 欠费。

    🔴 单张参考图（2026-11-18 修）：调用方用 `refs=[一张]` 时，本函数会转成 `image_b64`
       传给 _gen_once —— 此前直接把 refs 置 None 导致**参考图被丢弃退化成文生图**，
       批量生图里「1 张图 1 张成品」的场景会出完全无关的图（已实测复现并修复）。

    models（2026-11-18 新增，AI 生图版块用）：**可选**指定模型尝试顺序。
      给 None / 空 → 完全走上面的配置链（老调用点行为一字不变，pt/wm/hd/dtl 都不受影响）；
      给了就用它，并按配置链补足后面的兜底，保证「用户选的先试、失败仍能出图」。
    """
    imgs = [r for r in (refs or []) if r]
    # 单张参考图也可能走位置参数 image_b64（pt/hd/duotu 的老调用点），
    # 选线必须把它算进「参考图张数」，否则会被误判成文生图走错链。
    n_ref = len(imgs) if imgs else (1 if image_b64 else 0)
    multi = n_ref > 1
    # 分线规则**以后台配置为准**（ai_sites.routing()，设置页可改、即时生效）。
    # 2026-11-19：三条线 t2i / i2i / multi，判定统一走 ai_sites.lane_for。
    rt = ai_sites.routing()
    lane_name = ai_sites.lane_for(n_ref)
    lane = rt.get(lane_name) or rt.get("single") or {}
    # 指定模型优先：去重后拼在配置链前面（不删配置链，留兜底）
    want = tuple(m for m in (models or ()) if m)
    if want:
        base_chain = tuple(lane.get("models") or [])
        seen, merged = set(), []
        for m in want + base_chain:
            if m not in seen:
                seen.add(m)
                merged.append(m)
        chain = tuple(merged)
    else:
        chain = tuple(lane.get("models") or [])
    sites = tuple(lane.get("sites") or [])
    tmo = lane.get("timeout") if multi else None
    plan = ai_sites.plan_order(chain, site_order=sites, lane=lane_name)
    _LANE_CN = {"t2i": "文生图", "i2i": "图生图(单图)", "multi": "图生图(多图)"}
    sys.stderr.write("[gen_image] %s线 参考图 %d 张 → 计划 %d 跳：%s\n" % (
        _LANE_CN.get(lane_name, lane_name), n_ref, len(plan),
        ", ".join("%s/%s" % (p[0], p[3]) for p in plan[:6])))
    last_exc = None
    for site, base, skey, model in plan:
        try:
            # 🔴 单图必须显式走 image_b64：下面 refs 在单图时是 None，
            #    若这里也不传 image_b64，_gen_once 会当成**纯文生图**——参考图被静默丢弃。
            #    2026-11-18 实测暴露：批量生图 1 张图的任务出了完全无关的产品图。
            #    （pt/duotu 走位置参数 b64 一直正常，只有用 refs= 的调用点中招。）
            one = (len(imgs) == 1 and not image_b64)
            u = _gen_once(skey or key, model, prompt, size,
                          (imgs[0] if one else image_b64), base=base,
                          protocol=ai_sites.protocol_of(site),
                          refs=(imgs if multi else None), timeout=tmo)
            ai_sites.remember(site, model, lane=lane_name)
            return u
        except Exception as e:                       # noqa: BLE001
            last_exc = e
            cooled = ai_sites.note_fail(site, model)   # 方案 B：连续失败 → 该组合冷却
            sys.stderr.write("[gen_image] %s / %s 失败%s：%s\n" % (
                site, model, "（已熔断）" if cooled else "", str(e)[:140]))
            time.sleep(2)
    raise RuntimeError(ai_sites.sanitize(last_exc))

def wait_task(key, task_id, on_poll=None, timeout=300):
    """lensapi.cn 是**同步**接口：gen_image 返回的已经是图片 URL，
    这里原样透传（保留旧函数名与签名，线上三处调用点无需改动）。"""
    if on_poll:
        try:
            on_poll(0, "SUCCESS")
        except Exception:
            pass
    return task_id

def download(url, path):
    # 兼容两种返回：外链 http(s):// 与内联 data:image/...;base64,
    if isinstance(url, str) and url.startswith("data:"):
        with open(path, "wb") as f:
            f.write(base64.b64decode(url.split(",", 1)[1]))
        return
    with urllib.request.urlopen(url, timeout=300) as r, open(path, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)

# ---------------- 图像工具 ----------------
def img_to_b64(path, max_side=1536):
    from PIL import Image
    im = Image.open(path).convert("RGB")
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode("ascii")

def make_contact_sheet(paths):
    from PIL import Image
    ims = [Image.open(p).convert("RGB") for p in paths]
    h = min(im.height for im in ims)
    ims = [im.resize((int(im.width * h / im.height), h), Image.LANCZOS) for im in ims]
    gap = 40
    W = sum(im.width for im in ims) + gap * (len(ims) - 1)
    sheet = Image.new("RGB", (W, h), "white")
    x = 0
    for im in ims:
        sheet.paste(im, (x, 0)); x += im.width + gap
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode("ascii")

def smart_crop(im, ratio_w, ratio_h):
    import numpy as np
    w, h = im.size
    target = ratio_w / ratio_h
    if abs(w / h - target) < 1e-3:
        return im
    arr = np.asarray(im.convert("RGB"), dtype=int)
    corners = np.concatenate([arr[:40, :40].reshape(-1, 3), arr[:40, -40:].reshape(-1, 3),
                              arr[-40:, :40].reshape(-1, 3), arr[-40:, -40:].reshape(-1, 3)])
    bg = np.median(corners, axis=0)
    sig = np.abs(arr - bg).sum(axis=2)
    sig[sig < 60] = 0
    ys, xs = np.nonzero(sig)
    cx, cy = (int(np.median(xs)), int(np.median(ys))) if len(xs) > 500 else (w // 2, h // 2)
    if w / h > target:
        nw = int(h * target)
        x0 = max(0, min(w - nw, cx - nw // 2))
        return im.crop((x0, 0, x0 + nw, h))
    else:
        nh = int(w / target)
        y0 = max(0, min(h - nh, cy - nh // 2))
        return im.crop((0, y0, w, y0 + nh))

def fit_short(size):
    w, h = size
    if min(w, h) <= TARGET_SHORT:
        return size
    r = TARGET_SHORT / min(w, h)
    return (round(w * r), round(h * r))

# ---------------- 超分（EDSR x2 → 短边2048）----------------
def upscale_short(src, dst, on_log=None):
    """超分到短边 TARGET_SHORT。返回 (dst, warn)。
    透明底（RGBA）保留 alpha：RGB 走模型，alpha 走 LANCZOS。
    模型优先级 FSRCNN → EDSR → LANCZOS 兜底。"""
    from PIL import Image
    im0 = Image.open(src)
    has_a = im0.mode in ("RGBA", "LA") or (im0.mode == "P" and "transparency" in im0.info)
    im = im0.convert("RGBA") if has_a else im0.convert("RGB")
    scale = max(1, TARGET_SHORT // min(im.size))
    if scale == 1:
        im.resize(fit_short(im.size), Image.LANCZOS).save(dst)
        return dst, None
    import cv2, numpy as np
    arr = cv2.imdecode(np.fromfile(src, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    alpha = None
    if arr.ndim == 3 and arr.shape[2] == 4:
        alpha = arr[:, :, 3]
        bgr = arr[:, :, :3]
    elif arr.ndim == 2:
        bgr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    else:
        bgr = arr[:, :, :3]
    try:
        cv2.setNumThreads(max(2, os.cpu_count() or 2))
    except Exception:
        pass
    last = None
    for fn, nm in SR_MODELS:
        mp = os.path.join(BASE, "models", fn)
        if not os.path.exists(mp):
            continue
        try:
            from cv2 import dnn_superres
            if on_log:
                on_log("超分模型 %s（CPU）· 输入 %dx%d" % (nm, bgr.shape[1], bgr.shape[0]))
            sr = dnn_superres.DnnSuperResImpl_create()
            sr.readModel(mp)
            sr.setModel(nm, 2)
            up = sr.upsample(bgr)
            rgb = cv2.cvtColor(up, cv2.COLOR_BGR2RGB)
            out = Image.fromarray(rgb).resize(fit_short((rgb.shape[1], rgb.shape[0])), Image.LANCZOS)
            if alpha is not None:
                ai = Image.fromarray(alpha, "L").resize(out.size, Image.LANCZOS)
                out = out.convert("RGBA")
                out.putalpha(ai)
            out.save(dst)
            return dst, None
        except Exception as e:
            last = e
    # 全部模型不可用 → LANCZOS 放大兜底（质量降级但可用）
    big = im.resize((im.width * scale, im.height * scale), Image.LANCZOS)
    big.resize(fit_short(big.size), Image.LANCZOS).save(dst)
    return dst, "超分模型均不可用，已降级 LANCZOS 放大: %s" % last

# ---------------- 类型判别 ----------------
def classify(img_path):
    import cv2, numpy as np
    arr = cv2.imdecode(np.fromfile(img_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    h, w = arr.shape[:2]
    m = max(8, h // 20)
    border = np.concatenate([arr[:m].reshape(-1, 3), arr[-m:].reshape(-1, 3),
                             arr[:, :m].reshape(-1, 3), arr[:, -m:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    near_bg = (np.abs(border.astype(int) - bg).sum(axis=1) < 45).mean()
    if near_bg > 0.80:
        return "white_bg", "白底元素型（边框%.0f%%为单一底色 RGB(%d,%d,%d)）" % (near_bg * 100, *bg[::-1])
    return "full_bleed", "满幅图案型（边框仅%.0f%%接近单色）" % (near_bg * 100)

# ---------------- 程序化图案提取（版式锁定，不做 AI 重绘） ----------------
def extract_pattern_from_shell(shell_png, out_png, inset_frac=0.075, cam_w=0.97, cam_h=0.36):
    """从「干净壳」程序化提取图案：严格锁定原图版式，逐像素复刻，绝不重排。
    返回 meta = {canvas, missing_zone, ents, ppm}"""
    import cv2, numpy as np
    from PIL import Image
    arr = cv2.imdecode(np.fromfile(shell_png, dtype=np.uint8), cv2.IMREAD_COLOR)
    h, w = arr.shape[:2]
    # 1) 四角泛洪 → 壳体最大连通域 → bbox
    mask = np.zeros((h + 2, w + 2), np.uint8)
    flags = 4 | cv2.FLOODFILL_MASK_ONLY | cv2.FLOODFILL_FIXED_RANGE | (255 << 8)
    work = arr.copy()
    for seed in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        cv2.floodFill(work, mask, seed, 0, (12, 12, 12), (12, 12, 12), flags)
    fg = (~(mask[1:-1, 1:-1] == 255)).astype(np.uint8)
    n, lb, st, _ = cv2.connectedComponentsWithStats(fg, 8)
    if n <= 1:
        raise RuntimeError("未能识别手机壳轮廓，请确认原图是干净的产品图")
    i = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
    x, y, bw, bh, _ = st[i]
    # 2) 内缩去壳边框（银边/高光），裁出背板图案区
    ins = int(round(min(bw, bh) * inset_frac))
    crop = arr[y + ins:y + bh - ins, x + ins:x + bw - ins].copy()
    ch, cw = crop.shape[:2]
    H = ch * cw
    mx = crop.max(-1).astype(int); mn = crop.min(-1).astype(int)
    sat = mx - mn; gray = crop.mean(-1)
    # 3) 大面积「浅色低饱和」= 底（白背板/磁吸圈）→ 透明；小块（白肚/白脸）保留
    bgish = ((sat < 32) & (gray > 195)).astype(np.uint8)
    n2, lbl, stats, _ = cv2.connectedComponentsWithStats(bgish, 8)
    alpha = np.full((ch, cw), 255, np.uint8)
    for k in range(1, n2):
        if stats[k, cv2.CC_STAT_AREA] > H * 0.008:
            alpha[lbl == k] = 0
    # 4) 摄像头模组区（原图被遮蔽、图案缺失）→ 挖空，交给 AI 局部补全
    zw, zh = int(cw * cam_w), int(ch * cam_h)
    alpha[:zh, :zw] = 0
    # 5) 贴边孤立的银边残留清理（仅低饱和，不伤图案）
    edge = int(round(min(cw, ch) * 0.025))
    band = np.zeros((ch, cw), bool)
    band[:edge, :] = band[-edge:, :] = band[:, :edge] = band[:, -edge:] = True
    alpha[band & (sat < 40)] = 0
    rgba = np.dstack([crop[:, :, 2], crop[:, :, 1], crop[:, :, 0], alpha])
    Image.fromarray(rgba, "RGBA").save(out_png)
    n3, _, st3, _ = cv2.connectedComponentsWithStats((alpha > 0).astype(np.uint8), 8)
    return {"canvas": [cw, ch], "missing_zone": [0, 0, zw, zh],
            "ents": int(n3 - 1), "ppm": round((alpha > 0).mean() * 100, 1)}

# ---------------- 部件分离（大镜头框 / 支架 / 壳身） ----------------
PART_BODY = "壳身"
PART_LENS = "大镜头框"
PART_STAND = "支架"
PART_ORDER = (PART_BODY, PART_LENS, PART_STAND)


def save_bgr(path, bgr):
    """按路径存 BGR 图（兼容中文路径）。"""
    import cv2, numpy as np
    ok, buf = cv2.imencode(os.path.splitext(path)[1] or ".png", bgr)
    if not ok:
        raise RuntimeError("图片编码失败: " + path)
    buf.tofile(path)


def grab_shell_bbox(arr, inset=0.05, iters=5, min_cov=0.10, seed=42, work_short=480):
    """【实拍原图专用】GrabCut 分割壳体 → 非壳体填白 → 行/列覆盖率裁框。

    方案 A（支架 / 摄像头任务直接用原图定位）的核心：实拍原图多为深色渐变背景，
    四角泛洪（容差 12/12）扩不出去、会把整图当成前景，故改用 GrabCut 按矩形先验
    分割主体，再剔掉顶部投影 / 悬臂等稀疏连带区域。

    · 固定 RNG 种子 → 结果确定（避免 GrabCut 的 GMM/kmeans 初始化漂移）；
    · 先在「短边 work_short」的小图上做 GrabCut 再上采样掩码：75 万像素原尺寸要
      ~5s，缩到短边 480 只要 ~1.2s，而 bbox 仅偏移数个像素（实测 ≤6px，
      对部件圆心影响 <3px）——形态学仍在大图上做，避免核尺寸换算误差。
    返回 (mask_uint8 0/1, [x,y,bw,bh])；分割失败返回 (None, None)。
    """
    import cv2, numpy as np
    cv2.setRNGSeed(seed)
    h, w = arr.shape[:2]
    sc = min(1.0, float(work_short) / max(1, min(h, w)))
    if sc < 0.999:
        small = cv2.resize(arr, (max(16, int(round(w * sc))), max(16, int(round(h * sc)))),
                           interpolation=cv2.INTER_AREA)
    else:
        small = arr
    sh, sw = small.shape[:2]
    rect = (int(sw * inset), int(sh * inset),
            max(2, int(sw * (1 - 2 * inset))), max(2, int(sh * (1 - 2 * inset))))
    m = np.zeros((sh, sw), np.uint8)
    bgd = np.zeros((1, 65), np.float64)
    fgd = np.zeros((1, 65), np.float64)
    cv2.grabCut(small, m, rect, bgd, fgd, iters, cv2.GC_INIT_WITH_RECT)
    fg = np.where((m == cv2.GC_FGD) | (m == cv2.GC_PR_FGD), 1, 0).astype(np.uint8)
    if (sh, sw) != (h, w):
        fg = cv2.resize(fg, (w, h), interpolation=cv2.INTER_NEAREST)
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    n, lb, st, _ = cv2.connectedComponentsWithStats(fg, 8)
    if n <= 1:
        return None, None
    i = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
    mk = (lb == i).astype(np.uint8)
    if mk.mean() < 0.05:
        return None, None

    # 填内部孔洞：深色镜头模组 / 黑镜片与深色背景颜色相近，GrabCut 会把模组内部
    # 误判成背景 —— 填空时就会把模组整片抹成白色（实测镜头框区域约 1/3 像素被误填，
    # 抠出来的图案颜色随之漂移）。这些区域被壳体完全包围，是真「孔洞」：
    # 从四角对背景泛洪，泛洪不到的背景像素即孔洞 → 补回前景。
    ff = mk.copy()
    fm = np.zeros((mk.shape[0] + 2, mk.shape[1] + 2), np.uint8)
    _mh, _mw = mk.shape
    for _s in ((0, 0), (_mw - 1, 0), (0, _mh - 1), (_mw - 1, _mh - 1)):
        if ff[_s[1], _s[0]] == 0:
            cv2.floodFill(ff, fm, _s, 1)
    mk = ((mk == 1) | (ff == 0)).astype(np.uint8)

    rf, cf = mk.mean(1), mk.mean(0)

    def _bounds(f):
        n0 = len(f); i0 = 0; i1 = n0 - 1
        while i0 < i1 and f[i0] < min_cov:
            i0 += 1
        while i1 > i0 and f[i1] < min_cov:
            i1 -= 1
        return i0, i1

    y0, y1 = _bounds(rf); x0, x1 = _bounds(cf)
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    if bw < w * 0.20 or bh < h * 0.20:
        return None, None
    return mk, [int(x0), int(y0), int(bw), int(bh)]


_CROP_CACHE = {}   # (绝对路径, mtime, size, inset) -> (crop, meta) 复用，避免重复 GrabCut


def shell_crop_meta(shell_png, inset_frac=0.075):
    """【自适应】裁出壳体背板区，返回 (crop_bgr, meta)。

    ① 首选「四角泛洪去背景」（干净壳 / 白底 / 纯色底图：快、准、稳，原逻辑不变）；
    ② 泛洪失效（实拍深色渐变背景：泛洪扩不出去、四边几乎没被填）→ 自动退到
       GrabCut 分割壳体 + 非壳体填白 + 行/列覆盖率裁框（方案 A 实拍原图路径）。
       填白后 detect_parts 的「白底 / 暗部」判据才成立，且抠图时背景本就要透明。

    meta = {bbox:[x,y,bw,bh], inset:ins, mode:"flood"|"grabcut", off:[x+ins,y+ins]}
    其中 off 是 crop 左上角在原图中的偏移（前端/外部需要回映射时可用）。
    结果按（路径+mtime+尺寸+inset）进程内缓存：同一任务「定位」与「抠图」两次调用
    只跑一次 GrabCut。"""
    import cv2, numpy as np
    ck = None
    try:
        _s = os.stat(shell_png)
        ck = (os.path.abspath(shell_png), int(_s.st_mtime), int(_s.st_size), round(float(inset_frac), 4))
        hit = _CROP_CACHE.get(ck)
        if hit is not None:
            return hit[0].copy(), dict(hit[1])
    except OSError:
        ck = None
    arr = cv2.imdecode(np.fromfile(shell_png, dtype=np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        raise RuntimeError("读取原图失败: " + str(shell_png))
    h, w = arr.shape[:2]
    mode = "flood"
    x = y = bw = bh = None
    # ---- ① 四角泛洪 ----
    mask = np.zeros((h + 2, w + 2), np.uint8)
    flags = 4 | cv2.FLOODFILL_MASK_ONLY | cv2.FLOODFILL_FIXED_RANGE | (255 << 8)
    work = arr.copy()
    for seed in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        cv2.floodFill(work, mask, seed, 0, (12, 12, 12), (12, 12, 12), flags)
    fg = (~(mask[1:-1, 1:-1] == 255)).astype(np.uint8)
    # 泛洪是否真的把背景分离出去了？看「四条边中段」的背景占比：
    #   干净壳/白底图 → 四边几乎全是背景（≈1.0）；
    #   实拍深色渐变背景 → 泛洪扩不出去，四边仍是"前景"（≈0）→ 判定失效。
    edge = np.concatenate([mask[1, 1:-1], mask[-2, 1:-1], mask[1:-1, 1], mask[1:-1, -2]])
    edge_bg = float((edge == 255).mean())
    n, lb, st, _ = cv2.connectedComponentsWithStats(fg, 8)
    if n > 1 and edge_bg >= 0.80:
        i = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
        bx, by, bw0, bh0, _ = st[i]
        x, y, bw, bh = int(bx), int(by), int(bw0), int(bh0)
    # ---- ② 泛洪失效 → GrabCut 兜底 ----
    if x is None:
        mk, bb = grab_shell_bbox(arr)
        if mk is None:
            raise RuntimeError("未能识别产品轮廓（泛洪与 GrabCut 均失败），请换更清晰的原图")
        mode = "grabcut"
        x, y, bw, bh = bb
        arr = arr.copy()
        arr[mk == 0] = (255, 255, 255)   # 非壳体填白：统一底色，供检测与抠图
    ins = int(round(min(bw, bh) * inset_frac))
    crop = arr[y + ins:y + bh - ins, x + ins:x + bw - ins].copy()
    if crop.size == 0:
        raise RuntimeError("壳体区域过小，无法裁切（bbox=%s）" % [x, y, bw, bh])
    meta = {"bbox": [int(x), int(y), int(bw), int(bh)], "inset": int(ins),
            "mode": mode, "off": [int(x + ins), int(y + ins)]}
    if ck is not None:
        if len(_CROP_CACHE) > 32:
            _CROP_CACHE.clear()
        _CROP_CACHE[ck] = (crop, dict(meta))
    return crop, meta


def detect_parts(crop):
    """自动检测独立部件。返回 list，元素：
      {"kind":"大镜头框","shape":"rect",  "box":[x,y,w,h], "detected":True, "conf":0.0-1.0}
      {"kind":"支架",    "shape":"circle","center":[cx,cy],"r":int, "detected":True, "conf":...}
    判据：① 镜头框=暗镜片密度法（并集外扩，且只取上半部，过滤下方图案误检）
          ② 支架=Hough 圆 + 双判据（圆内白底占比<30% 且环带银灰占比>30%）"""
    import cv2, numpy as np
    ch, cw = crop.shape[:2]
    short = min(ch, cw)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    S = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)[..., 1]
    out = []
    # --- ① 大镜头框 ---
    dark = ((gray < 130) & (S < 100)).astype(np.uint8)
    dens = cv2.blur(dark.astype(np.float32), (61, 61))
    m = (dens > 0.28).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE,
                         cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (25, 25)))
    n, lb, st, _ = cv2.connectedComponentsWithStats(m, 8)
    boxes = [(int(st[j][0]), int(st[j][1]), int(st[j][2]), int(st[j][3]))
             for j in range(1, n)
             if st[j][4] > 3500 and min(st[j][2], st[j][3]) >= 0.15 * cw
             and (st[j][1] + st[j][3] / 2) < ch * 0.35]
    if boxes:
        x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
        x1 = max(b[0] + b[2] for b in boxes); y1 = max(b[1] + b[3] for b in boxes)
        # 并入「同属该模组的其他暗结构」：镜片密度法只抓到黑镜片，横向大模组右侧的
        # 闪光灯/激光雷达（同样是黑圆）抓不到，会在壳身里留下残留。
        # 做法：在镜片簇的水平带内取所有「块状暗域」并入。
        # 注意：不要用「银灰结构并集」——实测闭运算会把整片上半部背板连成一体，
        # 框被拉成横贯全宽的大横带（踩过）。
        py_band = int((y1 - y0) * 0.22)
        band = np.zeros((ch, cw), np.uint8)
        band[max(0, y0 - py_band):min(ch, y1 + py_band), :] = 1
        dk = ((gray < 130) & (S < 100) & (band > 0)).astype(np.uint8)
        dk = cv2.morphologyEx(dk, cv2.MORPH_CLOSE,
                              cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)))
        n3, lb3, st3, _ = cv2.connectedComponentsWithStats(dk, 8)
        for j in range(1, n3):
            ax, ay, aw, ah, aa = (int(st3[j][0]), int(st3[j][1]),
                                  int(st3[j][2]), int(st3[j][3]), int(st3[j][4]))
            if aa >= 300 and min(aw, ah) >= 12 and aa / float(max(1, aw * ah)) > 0.30:
                # 排除「壳体左右侧边暗阴影条」：实拍图上壳身两侧因曲面落暗，会在
                # 整条横带里被扫成「薄且贴裁切框左右边缘」的暗条；它们不属于镜头
                # 模组，一旦并入就把镜头框拉到裁切框全宽（实测 bx0=0、bx1=cw），
                # 于是壳身左右侧边的彩色图案也被抠进「大镜头框」层里（真值框仅占
                # 产出 opaque 像素的 44%）。
                thin = aw < 0.25 * cw
                at_margin = (ax < 0.10 * cw) or (ax + aw > 0.90 * cw)
                if thin and at_margin:
                    continue
                x0 = min(x0, ax); y0 = min(y0, ay)
                x1 = max(x1, ax + aw); y1 = max(y1, ay + ah)
        px = int((x1 - x0) * 0.10); py = int((y1 - y0) * 0.14)
        bx0, by0 = max(0, x0 - px), max(0, y0 - py)
        bx1, by1 = min(cw, x1 + px), min(ch, y1 + py)
        out.append({"kind": PART_LENS, "shape": "rect", "detected": True, "conf": 0.9,
                    "box": [int(bx0), int(by0), int(bx1 - bx0), int(by1 - by0)]})
    # --- ② 圆支架 ---
    # 判据（实测标定，干净壳 + 实拍图两组候选全量验算都只有真支架通过）：
    #   ① 圆完整：圆心到四边距离 ≥ 0.92r（排除落在壳边缘上的银边"假圆"）
    #   ② 环带（r~0.90r）银灰占比 ≥ 0.75（真支架 0.87~0.90）
    #   ③ 圆内白底亮色占比 ≤ 0.70（真支架 0.12~0.45；排除镜头框区/大面积白底上的假圆）
    #   ④ r ≥ 0.20×短边 且 圆心 y 在 0.18~0.85 高度
    #   ⑤ 多个候选都通过时，取「银环得分最高」者（不是半径最大者，见下方注释）
    g = cv2.medianBlur(gray, 5)
    circ = cv2.HoughCircles(g, cv2.HOUGH_GRADIENT, dp=1.5, minDist=int(short * 0.45),
                            param1=120, param2=55,
                            minRadius=int(short * 0.15), maxRadius=int(short * 0.42))
    best = None
    if circ is not None:
        for cx, cy, r in np.round(circ[0]).astype(int):
            cx, cy, r = int(cx), int(cy), int(r)
            if not (ch * 0.18 < cy < ch * 0.85):
                continue
            if r < 0.20 * short:
                continue
            if min(cx, cw - cx, cy, ch - cy) < r * 0.92:   # ① 圆必须完整落在画布内
                continue
            ringm = np.zeros((ch, cw), np.uint8)
            cv2.circle(ringm, (cx, cy), r, 1, -1)
            cv2.circle(ringm, (cx, cy), int(r * 0.90), 0, -1)
            mr = ringm > 0
            silver = float(((S[mr] < 60) & (gray[mr] > 120)).mean()) if mr.any() else 0.0
            if silver < 0.75:                              # ② 环带必须是银灰
                continue
            inner = np.zeros((ch, cw), np.uint8)
            cv2.circle(inner, (cx, cy), int(r * 0.75), 1, -1)
            mi = inner > 0
            inner_bg = float(((S[mi] < 30) & (gray[mi] > 215)).mean()) if mi.any() else 1.0
            if inner_bg > 0.70:                            # ③ 圆内不能是整片白底
                continue
            # ④ 选「银环得分最高」的候选，**不是半径最大的**。
            #    踩过：干净壳上 Hough 同时给出真支架 (257,671,r163,silver0.89) 和一个
            #    覆盖整段壳身的大假圆 (259,419,r216,silver0.80)，按「最大 r」会选中假圆，
            #    结果挖空圈把「BEST FRIEND」整行都挖掉、壳身中段全空。
            if (best is None or silver > best[3]
                    or (abs(silver - best[3]) < 0.02 and r > best[2])):
                best = (cx, cy, r, silver)
    if best:
        cx, cy, r = best[0], best[1], best[2]
        # Hough 检出的往往是银环内外缘之间的某一圈，真正的银环外径更大，改用
        # **自适应向外扫描**。⚠️ 判据必须是「**上半弧**灰度中位数」而不是「整环银灰占比」：
        # 整环会被壳身左右侧边（灰~180-210）和底部铰链支架污染，实测 r 从真实的 215
        # 一直虚涨到 233，挖空圈连壳身中段的「BEST FRIEND」都挖掉。
        # 上半弧（-160°~-20°）不经过侧边和铰链，金属环→背景的跃变干净。
        yy, xx = np.mgrid[0:ch, 0:cw]
        dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        silver_px = ((S < 60) & (gray > 120) & (gray < 215) & (dist > 0))
        ang = np.deg2rad(np.arange(-160, -20, 2.0))
        ca, sa = np.cos(ang), np.sin(ang)

        ks = list(range(int(r), max(int(r) + 6, int(min(ch, cw) * 0.46)), 3))
        prof = []
        for k in ks:
            xs = np.clip((cx + k * ca).astype(int), 0, cw - 1)
            ys = np.clip((cy + k * sa).astype(int), 0, ch - 1)
            prof.append(float(np.median(gray[ys, xs])))
        # 3 点中值平滑，滤掉孤立跳变（实测 r=221 处有一个亮斑把外径拖大）
        sm = list(prof)
        for i in range(1, len(prof) - 1):
            sm[i] = float(np.median([prof[i - 1], prof[i], prof[i + 1]]))
        ring_med = float(np.median(prof[:max(1, len(prof) // 6)]))
        bg_med = float(np.median(prof[-6:])) if len(prof) >= 6 else 255.0
        if ring_med < 220 and bg_med > ring_med + 8:
            thr = bg_med - 6.0                      # 金属环→背景的跃变阈值（自适应）
            r_final = ks[-1]
            for i, k in enumerate(ks):
                if sm[i] >= thr:
                    r_final = max(int(r), k - 2)
                    break
        else:
            rr = int(r)
            miss = 0
            for k in range(int(r), int(min(ch, cw) * 0.46), 3):
                band = (dist >= k) & (dist < k + 3)
                if int(band.sum()) == 0:
                    break
                if silver_px[band].mean() > 0.30:
                    rr = k + 3
                    miss = 0
                else:
                    miss += 1
                    if miss >= 4:
                        break
            r_final = int(min(rr + 3, min(ch, cw) * 0.46))
        r_final = int(min(r_final, min(ch, cw) * 0.46))
        # 支架图案只印在圆内的白盘上，银环不印图案 → 再找「白盘半径」r_inner：
        # 从里向外第一圈「银灰占比 > 0.25」的位置即银环起点（实测白盘≈145、银环 155~220）。
        r_inner = r
        for k in range(int(r * 0.55), int(min(ch, cw) * 0.48), 2):
            band = (dist >= k) & (dist < k + 2)
            cnt = int(band.sum())
            if cnt == 0:
                break
            if silver_px[band].mean() > 0.25:
                r_inner = max(8, k - 4)
                break
        r_inner = int(min(r_inner, r_final))
        out.append({"kind": PART_STAND, "shape": "circle", "detected": True, "conf": 0.85,
                    "center": [cx, cy], "r": r_final, "r_inner": r_inner})
    return out


def _strokes_to_mask(shape, strokes):
    """涂抹笔画（图像像素坐标）→ 布尔掩码。

    strokes 结构：[{"w": 笔刷直径(图像px), "pts": [[x,y], ...]}, ...]
    画法与画笔一致：折线用 thickness=w 描边，再在**每个采样点**补一个半径 w/2 的实心圆
    （否则折点处会缺角、单点点击画不出东西）。返回 (h, w) 的 bool 掩码。
    """
    import cv2, numpy as np
    h, w = shape[:2]
    mm = np.zeros((h, w), np.uint8)
    for s in strokes or []:
        if not isinstance(s, dict):
            continue
        pts = []
        for v in (s.get("pts") or []):
            if not isinstance(v, (list, tuple)) or len(v) < 2:
                continue
            try:
                pts.append([int(round(float(v[0]))), int(round(float(v[1])))])
            except (TypeError, ValueError):
                continue
        if not pts:
            continue
        try:
            bw = int(round(float(s.get("w") or 0)))
        except (TypeError, ValueError):
            bw = 0
        bw = max(2, min(600, bw or 40))
        if len(pts) >= 2:
            cv2.polylines(mm, [np.array(pts, np.int32)], False, 1, thickness=bw,
                          lineType=cv2.LINE_8)
        rad = max(1, bw // 2)
        for p in pts:
            cv2.circle(mm, (p[0], p[1]), rad, 1, -1)
    return mm > 0


def parts_to_masks(shape, parts):
    """部件列表 → 与 crop 同尺寸的布尔掩码。返回 (lens_mask, stand_mask)。

    ⚠️ 坐标系是 **crop 坐标**（shell_crop_meta 裁出的壳身），不是原图坐标。
       parts_detected / 前端「定位视图.png」也都在这套坐标里，三者必须一致。
    """
    import cv2, numpy as np
    h, w = shape[:2]
    lens = np.zeros((h, w), bool)
    stand = np.zeros((h, w), bool)
    for p in parts:
        if p.get("shape") == "rect":
            x, y, bw, bh = [int(v) for v in p["box"]]
            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(w, x + bw), min(h, y + bh)
            if x1 > x0 and y1 > y0:
                lens[y0:y1, x0:x1] = True
        elif p.get("shape") == "circle":
            cx, cy = [int(v) for v in p["center"]]
            mm = np.zeros((h, w), np.uint8)
            cv2.circle(mm, (cx, cy), int(p["r"]), 1, -1)
            stand |= (mm > 0)
        elif p.get("shape") == "mask":
            # 涂抹抓取：异形/多边形部件（涂到哪算哪）→ 归入「占位区」，
            # 让壳身把这块挖空（与圆形支架同一语义，只是形状自由）。
            stand |= _strokes_to_mask((h, w), p.get("strokes"))
    return lens, stand


def _cutout_core(crop, hole_mask=None, big_frac=0.008, edge_frac=0.025):
    """从 crop(BGR) 抠图案：大面积浅色低饱和→透明；hole_mask→透明；贴边银边清理。
    返回 RGBA（与 crop 同尺寸，未透明区保留原始像素）。"""
    import cv2, numpy as np
    ch, cw = crop.shape[:2]
    H = ch * cw
    mx = crop.max(-1).astype(int); mn = crop.min(-1).astype(int)
    sat = mx - mn; gray = crop.mean(-1)
    bgish = ((sat < 32) & (gray > 195)).astype(np.uint8)
    n2, lbl, stats, _ = cv2.connectedComponentsWithStats(bgish, 8)
    alpha = np.full((ch, cw), 255, np.uint8)
    for k in range(1, n2):
        if stats[k, cv2.CC_STAT_AREA] > H * big_frac:
            alpha[lbl == k] = 0
    if hole_mask is not None:
        alpha[hole_mask] = 0
    edge = int(round(min(cw, ch) * edge_frac))
    band = np.zeros((ch, cw), bool)
    band[:edge, :] = band[-edge:, :] = band[:, :edge] = band[:, -edge:] = True
    alpha[band & (sat < 40)] = 0
    return np.dstack([crop[:, :, 2], crop[:, :, 1], crop[:, :, 0], alpha])


def _part_alpha(sub, sat_min=32, thick=5, min_px=160, fill_min_px=300, fill_thr=0.35,
                ratio_max=6.0):
    """部件区内的图案掩码：**以「有厚度的高饱和色块」为种子**，孔洞填充回填内部细节。
    为什么不用「低饱和连通域整体清除」：图案与银环/底板像素相邻，会被并成一个
    大连通域一起清掉（实测把棕熊身体整块清没了）。
    为什么不用「与底板中位色差」：镜头框底板是暖米色且带纹理，色差法会把整块底板留下。
    为什么不用「闭运算回填」：闭运算会向外扩张，把紧邻图案的银环白边带进来（实测熊右侧
    拖出一条银弧）。改用**孔洞填充**——只填种子内部的空洞，绝不外扩。
    判据：
      ① seed = sat ≥ 32（米色底 sat≈13 / 银环 sat≈3 → 天然排除；棕熊 sat≈59 能进来）
      ② 去细长：腐蚀 5×5 后整个连通域消失的 → 丢弃（银环上的彩色细弧反光、压缩噪点）
      ③ 孔洞填充回填图案内部低饱和细节（白肚皮、高光）
      ④ 连通域过滤：面积<min_px（噪点）/ 长宽比>6 且 ≥300px（银环粗弧）/ 
         细长环状（fill<0.35 且 ≥300px）→ 清"""
    import cv2, numpy as np
    mx = sub.max(-1).astype(int); mn = sub.min(-1).astype(int)
    sat = mx - mn
    seed = (sat >= sat_min).astype(np.uint8)
    # ② 腐蚀存活过滤（整块判断，不会误伤图案轮廓）
    kt = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (thick, thick))
    er = cv2.erode(seed, kt)
    n0, lbl0, _, _ = cv2.connectedComponentsWithStats(seed, 8)
    alive = [int(v) for v in np.unique(lbl0[er > 0]) if v > 0]
    seed2 = np.isin(lbl0, alive).astype(np.uint8) if alive else np.zeros_like(seed)
    # ③ 孔洞填充（不外扩）
    #    ⚠️ 不能只从 (0,0) 单点泛洪：当部件框左上角恰好压在彩色图案上时
    #    （实测镜头框贴 crop 左边缘、框角是图案像素），泛洪会**顺着前景蔓延**，
    #    把所有背景像素都留成「泛洪不到的孔洞」→ 整个框被当成图案保留
    #    （实拍图表现为大片被填白的背景被当不透明像素输出，保真度掉到 91%）。
    #    改为先给掩码补 1 圈背景边框，从补后的 (0,0) 泛洪 —— 恒从背景出发，
    #    只有真正被前景完全包围的封闭区才判为孔洞。
    inv = (1 - seed2).astype(np.uint8)
    pad = cv2.copyMakeBorder(inv, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=1)
    ff = pad.copy()
    m2 = np.zeros((pad.shape[0] + 2, pad.shape[1] + 2), np.uint8)
    cv2.floodFill(ff, m2, (0, 0), 2)
    holes = (ff[1:-1, 1:-1] == 1)
    keep = ((seed2 > 0) | holes).astype(np.uint8)
    # ④ 连通域过滤
    h, w = sub.shape[:2]
    n, lbl, st, _ = cv2.connectedComponentsWithStats(keep, 8)
    out = np.zeros((h, w), np.uint8)
    for j in range(1, n):
        ax, ay, aw, ah, aa = st[j]
        if aa < min_px:
            continue
        fill = aa / float(max(1, aw * ah))
        ratio = max(aw, ah) / float(max(1, min(aw, ah)))
        if aa >= fill_min_px and (fill < fill_thr or ratio > ratio_max):
            continue
        out[lbl == j] = 255
    return out


def _stand_alpha(sub, bg_sat=32, bg_gray=210, big_frac=0.005, min_px=60):
    """支架圆内图案掩码（方案 A：白盘背景剥离，与整幅抓同构）。
    为什么弃用 _part_alpha 的高饱和种子：种子法「宁可少留、只留确定的图案」，
    会把浅色/细线/小元素误删（支架图案抓不全）。
    改为「宁可多留、只删确定的白盘背景」：
      白盘 = 低饱和(sat<bg_sat) 且 亮(gray>bg_gray) 的**大片连通域**(>big_frac)；
      图案无论深浅一律保留。银环/圆外由调用方用圆形掩码清掉。
    实测标定（支架 1008_175439，白盘 gray 中位数≈242、sat≈8）：
      bg_sat=32 / bg_gray=210 / big_frac=0.005 下，前景 25894px(vs 旧法 20382px)，
      白盘背景 ~43k px 被完整清除，主体连通域 25019px，浅色图案被连回主体。
    """
    import cv2, numpy as np
    mx = sub.max(-1).astype(int); mn = sub.min(-1).astype(int)
    sat = mx - mn; gray = sub.mean(-1)
    h, w = sub.shape[:2]; H = h * w
    bgish = ((sat < bg_sat) & (gray > bg_gray)).astype(np.uint8)
    n, lbl, st, _ = cv2.connectedComponentsWithStats(bgish, 8)
    out = np.full((h, w), 255, np.uint8)
    for k in range(1, n):
        if st[k, cv2.CC_STAT_AREA] > H * big_frac:
            out[lbl == k] = 0
    # 去噪：极小孤立块（<min_px）清掉，避免白盘残渣被当图案
    n2, lbl2, st2, _ = cv2.connectedComponentsWithStats((out > 0).astype(np.uint8), 8)
    for k in range(1, n2):
        if st2[k, cv2.CC_STAT_AREA] < min_px:
            out[lbl2 == k] = 0
    return out


def _part_item(kind, box, fp, note=""):
    import cv2, numpy as np
    from PIL import Image
    a = np.array(Image.open(fp))[..., 3]
    n, _, _, _ = cv2.connectedComponentsWithStats((a > 0).astype(np.uint8), 8)
    return {"kind": kind, "box": [int(v) for v in box], "png": os.path.basename(fp),
            "ppm": round(float((a > 0).mean() * 100), 1), "ents": int(n - 1),
            "px": int((a > 0).sum()), "mb": round(os.path.getsize(fp) / 1e6, 2), "note": note}


def extract_parts(shell_png, out_dir, parts=None, on_log=None, only=None):
    """按部件分区提取图案（版式锁定，逐像素复刻，不重排、不 AI 重绘）：
      壳身   = 全画布 − 镜头框区 − 支架圆（部件占位处留透明，供后续按需补全）
      镜头框 = 框内彩色图案（镜片/银环/灰底板 → 透明）
      支架   = 圆内彩色图案（银环/白盘 → 透明）
      涂抹   = 笔迹区内彩色图案（异形/多边形支架；区外 + 白盘 → 透明）
    输出均为**全画布尺寸**（便于合并 PSD 叠在同一坐标系）。

    parts 里每个部件的 shape 支持 "rect"（box）/"circle"（center+r_inner）/"mask"（strokes）。

    only : 只输出指定部件（独立任务模式：支架任务只出「支架」，摄像头任务只出「大镜头框」）。
           为 None 时输出全部。
    返回 meta = {canvas, crop_bbox, inset, parts:[…], detected:[…]}"""
    import cv2, numpy as np
    from PIL import Image
    os.makedirs(out_dir, exist_ok=True)
    crop, cmeta = shell_crop_meta(shell_png)
    ch, cw = crop.shape[:2]
    det = [dict(p) for p in parts] if parts else detect_parts(crop)
    lens_m, stand_m = parts_to_masks((ch, cw), det)
    hole = lens_m | stand_m
    items = []

    # ① 壳身：挖掉部件占位区
    if only in (None, PART_BODY):
        body = _cutout_core(crop, hole_mask=hole)
        fp_body = os.path.join(out_dir, "图案_%s.png" % PART_BODY)
        Image.fromarray(body, "RGBA").save(fp_body)
        items.append(_part_item(PART_BODY, [0, 0, cw, ch], fp_body,
                                "背板图案；镜头框/支架占位处已留透明"))

    # ②③ 各独立部件
    for p in det:
        if only and p.get("kind", PART_LENS) != only:
            continue
        shape = p.get("shape")
        use_stand = False     # 用「白盘背景剥离」还是「高饱和种子」
        sub_mask = None       # 部件区内的像素级裁剪掩码（None = 整个 box 都算）
        if shape == "rect":
            x, y, bw, bh = [int(v) for v in p["box"]]
        elif shape == "mask":
            # 涂抹抓取：由笔迹掩码定 bbox，再按掩码裁到笔迹本身（异形/多边形可用）
            sm = _strokes_to_mask((ch, cw), p.get("strokes"))
            ys, xs = np.where(sm)
            if not len(xs):
                continue
            pad = max(8, int(round(min(cw, ch) * 0.012)))
            x = max(0, int(xs.min()) - pad); y = max(0, int(ys.min()) - pad)
            bw = min(cw, int(xs.max()) + 1 + pad) - x
            bh = min(ch, int(ys.max()) + 1 + pad) - y
            sub_mask = sm[y:y + bh, x:x + bw].copy()
            # 支架（白盘底）用背景剥离；其余部件（镜头框等有纹理底板）用高饱和种子
            use_stand = (p.get("kind") == PART_STAND)
        else:
            cx, cy = int(p["center"][0]), int(p["center"][1])
            # 支架图案区用「白盘半径」r_inner（银环不印图案）；
            # 而壳身挖空用外径 r（见 parts_to_masks），否则壳身会留一圈银环。
            r = int(p.get("r_inner") or round(int(p["r"]) * 0.9))
            x, y, bw, bh = max(0, cx - r), max(0, cy - r), 2 * r, 2 * r
            use_stand = True
        x = max(0, min(x, cw - 1)); y = max(0, min(y, ch - 1))
        bw = max(1, min(bw, cw - x)); bh = max(1, min(bh, ch - y))
        sub = crop[y:y + bh, x:x + bw]
        # 支架圆内改「白盘背景剥离」（方案 A）：抓全浅色/细/小图案，与整幅抓同构；
        # 镜头框仍用 _part_alpha（框内底板有纹理，背景剥离会把整块底板留下）。
        a = _stand_alpha(sub) if use_stand else _part_alpha(sub)
        if shape == "circle":
            # 圆外一律透明（box 是外接正方形，四角不属于支架）
            cm = np.zeros((bh, bw), np.uint8)
            cv2.circle(cm, (cx - x, cy - y), r, 1, -1)
            a = np.where(cm > 0, a, 0).astype(np.uint8)
        elif sub_mask is not None:
            # 涂抹区外一律透明（bbox 是笔迹外接框，框内没涂到的地方不属于本次抓取）
            if sub_mask.shape != a.shape:
                sub_mask = sub_mask[:a.shape[0], :a.shape[1]]
            a = np.where(sub_mask, a, 0).astype(np.uint8)
        rgba = np.zeros((ch, cw, 4), np.uint8)
        rgba[y:y + bh, x:x + bw, :3] = sub[:, :, ::-1]
        rgba[y:y + bh, x:x + bw, 3] = a
        kind = p.get("kind", PART_LENS)
        fp = os.path.join(out_dir, "图案_%s.png" % kind)
        Image.fromarray(rgba, "RGBA").save(fp)
        if shape == "mask":
            note = "涂抹区内图案；白盘/银边已透明（自定义区域，异形可用）"
        else:
            note = "框内图案；镜片/银环/灰底板已透明" if kind == PART_LENS else "圆内图案；银环/白盘已透明"
        items.append(_part_item(kind, [x, y, bw, bh], fp, note))

    # 占位区掩码（= 壳身被挖空的部分），供局部续接精确定位「被切断的图案」；
    # 用掩码而不是外接矩形：圆支架的占位区是圆，外接方框的四角不属于占位区。
    hole_png = os.path.join(out_dir, "_占位区.png")
    Image.fromarray((hole.astype(np.uint8) * 255), "L").save(hole_png)

    meta = {"canvas": [cw, ch], "crop_bbox": cmeta["bbox"], "inset": cmeta["inset"],
            "parts": items, "detected": det, "hole_png": os.path.basename(hole_png)}
    if on_log:
        on_log("部件分离完成：%s" % "、".join("%s(%dx%d)" % (it["kind"], it["box"][2], it["box"][3])
                                           for it in items))
    return meta


def inpaint_regions(pattern_png, key=None, hole_png=None, regions=None, mode="edge",
                    on_log=None, band_frac=0.045, touch_px=4, max_blocks=3,
                    drop_white=True, fill_all=False):
    """部件占位区的处理：**把被部件边界切断的图案顺着断口续接完整**。

    绝不是「把占位区填满」——那是踩过的大坑：把区域内所有透明像素当缺失区，
    AI 会在整块区域里凭空发明原图没有的元素（太阳/彩虹/兔子/Good Day），
    直接违反「只提取原图已有元素」的铁律。

    先算出「被切断的区域」`need`（四条判据缺一不可）：
      ① 占位区**内部**（内缩 8px，避开边角）
      ② 是透明像素（alpha==0）
      ③ **紧邻「被切断的彩色图案」**：占位区边界外侧 2*touch_px 内存在 sat>=32 的彩色像素
         —— 只认彩色图案：壳身灰色包边/银环 sat<32 天然排除，否则会沿灰边糊出一条黑带
      ④ 邻近半径 = band（画布短边的 4.5%），再远就是自由发挥，不补

    然后按 mode 处理：
      · "none" —— 不补，占位区保持透明（**默认，最安全**：支架/镜头框装配后会盖住这里）
      · "edge" —— **边缘延续**：对 need 每个像素取「最近可见像素」的颜色（cv2 距离变换 +
        查表，等价于最近邻外推）。确定性、瞬时、零发明，最忠实于被切断元素的颜色与笔触。
      · "ai"   —— AI 续接：按连通块逐块裁剪送 gpt-image-2，方形填充避免长宽比畸变。
        质量不稳（实测会画黑斑）且慢（每块约 1 分钟），仅在需要时用。

    hole_png: 占位区掩码 PNG（255=被部件遮挡）；regions: 兼容用的矩形列表 [(x,y,w,h)]。
    返回 (是否补过, 说明)。"""
    import cv2, numpy as np
    from PIL import Image
    im = Image.open(pattern_png).convert("RGBA")
    W, H = im.size
    arr = np.array(im)
    if str(mode).lower() in ("none", "0", "off", "false", ""):
        return False, "未启用续接，占位区保持透明"
    hol = np.zeros((H, W), bool)
    if hole_png and os.path.isfile(hole_png):
        hol = np.array(Image.open(hole_png).convert("L")) > 127
    if regions:
        for x, y, w, h in regions:
            x0, y0 = max(0, int(x)), max(0, int(y))
            x1, y1 = min(W, int(x) + int(w)), min(H, int(y) + int(h))
            if x1 > x0 and y1 > y0:
                hol[y0:y1, x0:x1] = True
    if not hol.any():
        return False, "无占位区，跳过"
    rgb = arr[..., :3].astype(int)
    sat = rgb.max(-1) - rgb.min(-1)
    trans = (arr[..., 3] == 0)
    inner = cv2.erode(hol.astype(np.uint8),
                      cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))) > 0   # ①
    band = max(10, int(min(H, W) * band_frac))          # ④
    if fill_all:
        # 「同风格补全」：占位区内**所有透明像素**都补（满幅图案任务专用；用户拍板选此档）。
        # 与「只续接被切断图案」的区别：这里允许 AI 在遮挡区内按周围风格补出新笔触，
        # 目的是得到一张**完整满幅**图案；仍严令禁止引入与四周风格无关的元素。
        need = inner & trans
        if not need.any():
            return False, "占位区内已无透明像素，无需补全"
    else:
        pat = (~trans) & (sat >= 32)                    # ③ 只认彩色图案
        kb_touch = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (touch_px * 2 + 1,) * 2)
        border = hol & (~(cv2.erode(hol.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0))
        cut = (pat & (cv2.dilate(border.astype(np.uint8), kb_touch) > 0)).astype(np.uint8)
        if not cut.any():
            return False, "占位区边界没有切断任何彩色图案，无需续接（背景保持透明）"
        kb_band = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (band * 2 + 1,) * 2)
        need = inner & trans & (cv2.dilate(cut, kb_band) > 0)
        if not need.any():
            return False, "占位区内没有被切断的图案可续接（背景保持透明）"

    # ---------------- edge：最近可见像素的颜色外推 ----------------
    if str(mode).lower() == "edge":
        ref = ~trans                                    # 参考 = 所有可见像素
        n_ref = int(ref.sum())
        if n_ref == 0:
            return False, "无可见像素可参考"
        flat_idx = np.zeros((H, W), np.int32)
        flat_idx[ref] = np.arange(1, n_ref + 1)
        dist, lbl = cv2.distanceTransformWithLabels(
            (~ref).astype(np.uint8), cv2.DIST_L2, 3, labelType=cv2.DIST_LABEL_PIXEL)
        colors = arr[..., :3][ref]                      # 按行优先 = 与 label 顺序一致
        fill = colors[np.clip(lbl - 1, 0, n_ref - 1)]
        out = arr.copy()
        out[..., :3] = np.where(need[..., None], fill, arr[..., :3])
        out[..., 3] = np.where(need, 255, arr[..., 3])
        arr = out
        Image.fromarray(arr, "RGBA").save(pattern_png)
        if on_log:
            on_log("边缘延续 %d px（最近邻颜色外推）" % int(need.sum()))
        return True, "边缘延续 %d px 被切断图案" % int(need.sum())

    # ---------------- ai：逐连通块送 AI 续接 ----------------
    n, lbl2 = cv2.connectedComponents(need.astype(np.uint8), 8)
    blocks = sorted([j for j in range(1, n) if (lbl2 == j).sum() >= 40],
                    key=lambda j: -int((lbl2 == j).sum()))[:max_blocks]
    if not blocks:
        return False, "被切断区域过小（<40px），跳过"
    total = 0
    for bi, j in enumerate(blocks):
        m = (lbl2 == j)
        ys, xs = np.where(m)
        ry0, ry1 = int(ys.min()), int(ys.max()) + 1
        rx0, rx1 = int(xs.min()), int(xs.max()) + 1
        ctx = max(28, band * 3)
        y0b, y1b = max(0, ry0 - ctx), min(H, ry1 + ctx * 2)
        x0b, x1b = max(0, rx0 - ctx), min(W, rx1 + ctx)
        sub = arr[y0b:y1b, x0b:x1b].copy()
        sub_rgb = sub[..., :3].copy()
        sub_rgb[sub[..., 3] == 0] = 255
        sh, sw = sub_rgb.shape[:2]
        side = max(sh, sw)
        sq = np.full((side, side, 3), 255, np.uint8)
        oy, ox = (side - sh) // 2, (side - sw) // 2
        sq[oy:oy + sh, ox:ox + sw] = sub_rgb
        if on_log:
            on_log("AI 续接被切断图案 %d/%d（%dx%d）" % (bi + 1, len(blocks), sw, sh))
        b64 = _pil_to_b64(Image.fromarray(sq))
        if fill_all:
            pr = ("这是一张手机壳满铺图案的局部截图：中间有一块空白（原图中该处被摄像头/支架遮挡）。"
                  "请参考四周已有图案的颜色、元素风格（卡通动物、云朵、星星、小圆点、花草等）"
                  "与疏密分布规律，把这块空白**补成完全同风格**的图案，与四周自然衔接、看不出拼接痕迹。"
                  "严禁写文字、严禁画与四周风格无关的东西（不要加照片、渐变、几何抽象块）。"
                  "空白处若原本应当没有图案，就保持纯白。绝不要改动四周已有的图案。")
        else:
            pr = ("这是一张手机壳满铺图案的局部截图：中间一块空白（原图该处被摄像头/支架遮挡），"
                  "空白边缘有几处**被切断的图案**（例如某个图案只露出一半）。"
                  "请只把这些被切断的图案顺着断口延续补完整，颜色、笔触、风格与断口处必须完全一致。"
                  "绝对不要添加任何新的元素（不要新增太阳、彩虹、动物、文字等你无法确认原图存在的元素）。"
                  "原本没有图案的地方保持纯白空白，绝不要改动已有图案。")
        tid = gen_image(key, pr, "1024x1024", b64)
        url = wait_task(key, tid)
        tmp = pattern_png + ".ai%d.png" % bi
        download(url, tmp)
        gen = Image.open(tmp).convert("RGB").resize((side, side), Image.LANCZOS)
        g = np.array(gen)[oy:oy + sh, ox:ox + sw]
        miss = m[y0b:y1b, x0b:x1b] & (sub[..., 3] == 0)
        if drop_white:
            gi = g.astype(int)
            whiteish = ((gi.max(-1) - gi.min(-1) < 22) & (gi.mean(-1) > 205))
            miss = miss & (~whiteish)
        try:
            os.remove(tmp)
        except OSError:
            pass
        if not miss.any():
            continue
        out = sub.copy()
        out[..., :3] = np.where(miss[..., None], g, sub[..., :3])
        out[..., 3] = np.where(miss, 255, sub[..., 3])
        er = cv2.morphologyEx(miss.astype(np.uint8), cv2.MORPH_GRADIENT,
                              np.ones((3, 3), np.uint8)) > 0
        aa = out[..., 3].astype(float)
        aa[er] = 190
        out[..., 3] = aa.astype(np.uint8)
        arr[y0b:y1b, x0b:x1b] = out
        total += int(miss.sum())
    if total == 0:
        return False, "AI 输出全为背景色，未填入（保持透明）"
    Image.fromarray(arr, "RGBA").save(pattern_png)
    return True, "AI 续接 %d px 被切断图案（%d 块）" % (total, len(blocks))


def _pil_to_b64(im, max_side=1536):
    import io, base64
    if max(im.size) > max_side:
        r = max_side / max(im.size)
        im = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))), 3)
    buf = io.BytesIO(); im.convert("RGB").save(buf, "JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode("ascii")

def inpaint_missing(pattern_png, key, zone_frac=(0.97, 0.36), on_log=None):
    """AI 局部补全：只填「缺失区矩形」（原图被摄像头遮挡处）内的透明像素，
    其余像素一律不动 —— AI 无法影响已有图案的位置。"""
    import cv2, numpy as np
    from PIL import Image
    im = Image.open(pattern_png).convert("RGBA")
    W, H = im.size
    arr = np.array(im)
    zw = min(W, int(W * zone_frac[0])); zh = min(H, int(H * zone_frac[1]))
    if (arr[:zh, :zw, 3] > 0).mean() > 0.5:
        return False, "缺失区已基本填满，跳过"
    # 裁剪：缺失区 + 下方上下文（供 AI 参考风格）
    pad = max(int(zh * 0.85), 60)
    y0, y1 = 0, min(H, zh + pad)
    x0, x1 = 0, W
    sub = arr[y0:y1, x0:x1].copy()
    rgb = sub[..., :3].copy(); a = sub[..., 3]
    rgb[a == 0] = 255
    if on_log:
        on_log("调用 AI 补全缺失区 %dx%d ..." % (x1 - x0, y1 - y0))
    b64 = _pil_to_b64(Image.fromarray(rgb))
    p = ("这是一张手机壳满铺图案的局部截图：图片上半部有一块空白（原图中该处被手机摄像头遮挡），"
         "下半部是完整的图案。请参考下半部图案的颜色、元素风格（卡通小熊、小黄鸭、云朵、星星、小圆点）"
         "以及疏密分布规律，把上半部的空白区域补全成完全同风格的图案，与下半部自然衔接、看不出拼接痕迹。"
         "只补全空白区域，绝对不要改动下半部已有的图案。")
    tid = gen_image(key, p, "1024x1024", b64)
    url = wait_task(key, tid)
    tmp = pattern_png + ".ai.png"
    download(url, tmp)
    gen = Image.open(tmp).convert("RGB").resize((x1 - x0, y1 - y0), Image.LANCZOS)
    g = np.array(gen)
    region = arr[y0:y1, x0:x1]
    # 只填「缺失区矩形内」的透明像素：底色透明让所有空白连成一片，
    # 若按连通域取缺失区，AI 会把元素间隙也画满 = 重排整个上半部，故用位置矩形界定。
    miss = np.zeros(region.shape[:2], bool)
    miss[:zh, :zw] = True
    miss &= (region[..., 3] == 0)
    if not miss.any():
        return False, "缺失区为空"
    out = region.copy()
    out[..., :3] = np.where(miss[..., None], g, region[..., :3])
    out[..., 3] = np.where(miss, 255, region[..., 3])
    # 边缘 2px 羽化，消除拼接硬边
    er = cv2.morphologyEx(miss.astype(np.uint8), cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)) > 0
    aa = out[..., 3].astype(float)
    aa[er] = 190
    out[..., 3] = aa.astype(np.uint8)
    arr[y0:y1, x0:x1] = out
    Image.fromarray(arr, "RGBA").save(pattern_png)
    try:
        os.remove(tmp)
    except OSError:
        pass
    return True, "已补全 %d px" % int(miss.sum())

# ---------------- 满幅图案交付（「手机壳图案」任务用） ----------------
def extract_shell_full(shell_png, out_png, parts=None, cam_frac=None,
                       hole_dir=None, on_log=None):
    """整壳「满幅」图案提取（= 两版里的「锁定版」）：壳身图案整张取出、逐像素复刻。

    挖空范围 = **部件外接框矩形（镜头框）∪ 支架圆**，框内像素一律去掉 ——
    ⚠️ 试过「只挖模组实体、保留框内彩色图案」，结果把壳的银色包边、品牌标（ZEISS）
    全留下来了（它们是低饱和结构而不是图案）。整框挖空最干净；框内原本就有的图案
    （如模组旁边的气球）由后续 `inpaint_regions(fill_all=True)` 同风格补回来。

    同一份掩码既用于挖空、也存盘为 `_补全区.png` 供补全使用。
    返回 meta = {canvas, crop_bbox, inset, ents, ppm, hole_png, detected}"""
    import cv2, numpy as np
    from PIL import Image
    crop, cmeta = shell_crop_meta(shell_png)
    ch, cw = crop.shape[:2]
    det = [dict(p) for p in (parts or [])]
    fill = np.zeros((ch, cw), bool)
    for p in det:
        if p.get("shape") == "rect":
            x, y, w, h = [int(v) for v in p["box"]]
            x0, y0, x1, y1 = max(0, x), max(0, y), min(cw, x + w), min(ch, y + h)
            if x1 > x0 and y1 > y0:
                fill[y0:y1, x0:x1] = True
        else:
            cx, cy = int(p["center"][0]), int(p["center"][1])
            r = int(p.get("r") or 0)
            mm = np.zeros((ch, cw), np.uint8)
            cv2.circle(mm, (cx, cy), max(1, r), 1, -1)
            fill |= (mm > 0)
    if not det:
        # 一个部件都没检出 → 兜底处理左上摄像头模组区（免得黑镜片被当图案留下）
        if cam_frac is None:
            hh, ww = max(1, int(ch * 0.36)), max(1, int(cw * 0.97))
        else:
            hh, ww = max(1, int(ch * cam_frac[1])), max(1, int(cw * cam_frac[0]))
        fill[:hh, :ww] = True
    hole = fill
    rgba = _cutout_core(crop, hole_mask=hole)
    Image.fromarray(rgba, "RGBA").save(out_png)
    n, _, _, _ = cv2.connectedComponentsWithStats((rgba[..., 3] > 0).astype(np.uint8), 8)
    meta = {"canvas": [cw, ch], "crop_bbox": cmeta["bbox"], "inset": cmeta["inset"],
            "ents": int(n - 1), "ppm": round(float((rgba[..., 3] > 0).mean() * 100), 1),
            "hole_px": int(hole.sum()), "fill_px": int(fill.sum()), "detected": det}
    if hole_dir:
        os.makedirs(hole_dir, exist_ok=True)
        fp = os.path.join(hole_dir, "_补全区.png")
        Image.fromarray((fill.astype(np.uint8) * 255), "L").save(fp)
        meta["hole_png"] = os.path.basename(fp)
    if on_log:
        on_log("整壳满幅提取：画布 %dx%d，图案占比 %.1f%%，待补全 %d px"
               % (cw, ch, meta["ppm"], meta["fill_px"]))
    return meta


def redraw_fullbleed(clean_png, out_png, key, size=None, on_log=None, full_bg=False,
                     ratio=None, long_edge=None):
    """AI 满幅重绘（= 两版里的「重绘版」）：把干净壳重绘成一张完整满幅图案，
    prompt 死锁元素版式（1:1 不重排，禁止增/删/移/转/缩）。

    size=None 时**按壳身实际长宽比**自动定尺寸（见 fullbleed_size），
    避免默认 3:4 把竖长图案横向拉伸。返回 (ok, msg)。
    full_bg=True → 用「整幅底图」版提示词（保留原壳底色铺满画布，2026-10-14）。

    ratio（2026-10-21 新增，用户在页面选的输出尺寸）：'auto'/空 → 历史行为不变；
    否则按该比例请求尺寸，**出图后再补边校正到精确目标比例**（上游会把尺寸就近吸附到
    自家分辨率，只保比例不保像素，故必须后处理补边才能给出准确尺寸）。

    long_edge（2026-11-15 新增）：比例=「自动」时用户选的**长边像素**（2K=2048 / 4K=4096 /
    自定义）。只有 ratio 没给出确定像素时才生效 —— 因为「自动」的形状由壳身真实比例决定，
    前端算不出来。传 0/None → 与改造前逐像素一致。"""
    tgt = None
    if ratio:
        _lbl, wh, px = parse_ratio(ratio)
        if wh and px:
            tgt = (wh, px)
            size = px
    try:
        _le = int(long_edge or 0)
    except Exception:                              # noqa: BLE001
        _le = 0
    # 比例=自动 + 指定长边 → 必须**覆盖**调用方按 1024 预算出来的 size，否则档位不生效
    if (not tgt) and _le > 0:
        size = None
    if not size:
        try:
            # ⚠️ 必须用 shell_crop_meta 返回的 **crop 尺寸**（= 壳身内缩后的背板区），
            #    不是 cmeta["bbox"] —— bbox 是 [x, y, w, h] 四元组，当成 x0/y0/x1/y1 去算
            #    会得到离谱的 1:4.4「壳身」，再把比例夹到 2.6 上限，重绘版被纵向拉长。
            crop, _cm = shell_crop_meta(clean_png)
            size = fullbleed_size_for(crop.shape[1], crop.shape[0], long_edge=_le)
        except Exception:
            size = fullbleed_size_for(1024, 1536, long_edge=_le)
    if on_log:
        on_log("AI 满幅重绘（版式死锁 prompt，请求尺寸 %s%s%s）…"
               % (size, " · 目标比例 %d:%d" % tgt[0] if tgt else "",
                  " · 长边 %d" % _le if (_le and not tgt) else ""))
    b64 = img_to_b64(clean_png)
    _prompt = REDRAW_PROMPT_FULL if full_bg else REDRAW_PROMPT
    # 2026-10-09：上游对「精确复刻输入图」有**概率性**内容审核（旧 prompt 也会被拒，约 1/2 概率，
    # 报 upstream_text_r / "违反与第三方内容相似性的防护限制"）→ 对这类**软拒绝自动重试**。
    for attempt in range(REDRAW_RETRIES + 1):
        try:
            tid = gen_image(key, _prompt, size, b64)
            # 满幅重绘尺寸大（1:2 竖长），实测晚间排队会超过 5 分钟 → 超时给 600s
            url = wait_task(key, tid, timeout=600)
            download(url, out_png)
            if not os.path.isfile(out_png) or os.path.getsize(out_png) < 1024:
                raise RuntimeError("满幅重绘输出异常（文件缺失或过小）")
            break
        except Exception as e:
            m = str(e)
            soft = ("相似性" in m) or ("违反" in m) or ("upstream_text_r" in m)
            if attempt < REDRAW_RETRIES and soft:
                if on_log:
                    on_log("上游内容审核软拒绝（第 %d 次），%d 秒后自动重试…" % (attempt + 1, 4))
                time.sleep(4)
                continue
            raise
    # 比例校正：上游只保比例不保像素（实测请求 1024x1024 → 出 1254x1254）→
    # 预设档位只要求**比例**对上（像素以出图实测为准）；自定义档位必须**精确落地**
    # 用户填的像素（1280x720 就得是 1280x720），用 contain 等比缩放 + 补边，不裁不拉。
    if tgt:
        wh, px = tgt
        rw, rh = wh
        exact = _is_custom_px(ratio)
        try:
            info = fit_to_ratio(out_png, out_png + ".fit.png", rw, rh, tol=0.0,
                                dst_px=px, exact=exact)
            if not info.get("error") and info.get("padded"):
                if os.path.isfile(out_png + ".fit.png"):
                    os.replace(out_png + ".fit.png", out_png)
                    if on_log:
                        on_log("按目标比例校正：%s → %dx%d（元素未变形）"
                               % ("%dx%d" % tuple(info["src_size"]),
                                  info["dst_size"][0], info["dst_size"][1]))
        except Exception as ex:
            if on_log:
                on_log("比例校正跳过：%s" % str(ex)[:120])
            try:
                if os.path.isfile(out_png + ".fit.png"):
                    os.remove(out_png + ".fit.png")
            except Exception:
                pass
    try:
        from PIL import Image as _I
        with _I.open(out_png) as _im:
            real = "%dx%d" % _im.size
    except Exception:
        real = size
    if on_log:
        on_log("AI 满幅重绘完成：请求 %s → 实出 %s" % (size, real))
    return True, "AI 满幅重绘完成（%s）" % real


# ---------------- 部件（支架 / 镜头）· AI 完全重绘档（2026-10-10 新增） ----------------
# 用户诉求原话：「支架扣完之后，怎么就是直接生成 PSD 了？没有完全重绘的功能？」
# 与 shell 满幅重绘（redraw_fullbleed）的区别：这里的输入不是整张壳，而是 extract_parts
# 抠出来的**透明底图案**；输出也只要这一片图案（同画布、同位置、透明底），
# 可直接顶替「锁定版」进分层 PSD，前端两版并排挑。
# 价值：方案 A 直抠 = 原图像素，保真 100%，但清晰度锁死在原图（支架圆盘常只有 150~300px），
# 且手指 / 反光 / 水印 / 白盘边缘会一起被抠进来；AI 完全重绘把这些脏东西去掉、
# 把线条颜色画干净、清晰度拉高，同时**死锁图案内部版式**（不增不删不移不转不缩）。
PART_REDRAW_PROMPT = (
    "这是一张从商品图里抠出来的**图案贴图**，图案之外是空白。\n"
    "请把这片图案**重绘成一张干净、清晰、完整的图案**，正面视角、无透视变形、无立体感。\n"
    "【输出对象】只允许出现这片图案本身的图形元素，**绝对不要出现**：\n"
    "· 手机壳的壳身 / 侧边 / 边框 / 圆角 / 厚度 / 投影 / 反光；\n"
    "· 圆支架的金属银环、白色托盘、卡扣等结构件，镜头保护框、镜片、摄像头模组；\n"
    "· 抠图残留的白边、灰边、锯齿、圆盘外轮廓、水印、手指、阴影。\n"
    "【版式锁定铁律】图案内部每个元素的**位置、大小、数量、朝向、相对间距**必须完全不变，"
    "像描图一样 1:1 复刻：\n"
    "· 禁止新增任何元素（不许添加原图没有的花草、蝴蝶、星星、闪光、文字，哪怕一点点也不行）；\n"
    "· 禁止删除任何元素；\n"
    "· 禁止移动、旋转、缩放、重排任何元素，禁止改变构图、留白与疏密；\n"
    "· **严禁改变图案的外形轮廓**：原图是圆形的就画成圆形、是方形就画方形、是有机形状就保持同样的边界；"
    "**绝不允许**把圆形 / 异形图案画成填满画布的方形或矩形，也绝不允许把图案铺满整张画布。\n"
    "【颜色锁定铁律】颜色与原图**逐项完全一致**，这是硬性要求，没有任何发挥空间：\n"
    "· **色相必须一模一样**：原图是红色就画红色、蓝色就画蓝色。**严禁换色**——"
    "严禁改成邻近色系（红→橙/粉、蓝→青/紫、紫→绿 统统不行），"
    "严禁整体调色、复古化、暗化、降饱和、换配色方案；\n"
    "· 逐块照抄：每块区域的颜色直接照搬原图对应位置的颜色，明度与饱和度也对齐原图，"
    "**严禁**把原图没有的鲜艳颜色涂到任何区域；\n"
    "· 禁止添加渐变、光晕、发光、金属 / 霓虹 / 赛博 等风格化配色，禁止整体叠色罩；\n"
    "· 唯一例外：原图被遮挡 / 反光 / 噪点糊掉的区域，用**紧邻的同一颜色**补回，不许另配新色。\n"
    "【允许做的】只把线条画干净、把同一种颜色的区域涂均匀，把模糊的细节画清晰。\n"
    "输出：图案以外的空白为**纯白 #FFFFFF**，不做任何版式改动。"
)


def part_redraw_size(w, h, square=1024):
    """部件重绘的请求尺寸。

    不能复用 fullbleed_size：那个把比例夹在 1.5~2.6（给竖长手机壳用的），
    支架圆盘≈正方形会被硬拉成 1:1.5 竖长 → 圆盘变椭圆。这里按「近方 / 竖 / 横」三档给。"""
    try:
        w, h = float(w), float(h)
    except Exception:
        return "%dx%d" % (square, square)
    if w <= 0 or h <= 0:
        return "%dx%d" % (square, square)
    r = h / w
    if r > 1.25:
        return "%dx%d" % (square, square * 3 // 2)
    if r < 0.8:
        return "%dx%d" % (square * 3 // 2, square)
    return "%dx%d" % (square, square)


def _alpha_from_white(rgb, soft=6.0, full=26.0, min_px=24):
    """从「白底图」提**软 alpha**：近白→0，有色/灰→255，soft~full 之间线性过渡（自带抗锯齿）。

    为什么不用「二值 + 外轮廓填充」：
      · 3×3 MORPH_OPEN 会吃掉 1-2px 的细线条 → 线稿类图案细节成片丢失；
      · 外轮廓填充会把图案内部的留白也填成不透明，语义与「锁定版」（白=透明）不一致。
    去噪改用「面积过小的连通域清零」，既保细节又清散点。"""
    import cv2, numpy as np
    mx = rgb.max(-1).astype(np.float32)
    mn = rgb.min(-1).astype(np.float32)
    gray = rgb.mean(-1).astype(np.float32)
    d = np.maximum(mx - mn, 255.0 - gray)                 # 0 = 纯白背景；越大越"有内容"
    a = np.clip((d - float(soft)) / max(1e-6, float(full) - float(soft)), 0.0, 1.0)
    a = (a * 255.0).round().astype(np.uint8)
    m = (a > 40).astype(np.uint8)
    if m.any():
        n, lbl, stats, _ = cv2.connectedComponentsWithStats(m, 8)
        keep = np.zeros_like(m)
        for k in range(1, n):
            if stats[k, cv2.CC_STAT_AREA] >= min_px:
                keep |= (lbl == k)
        a[keep == 0] = 0
    return a


def _as_rgb_u8(x):
    """把任意 dtype 的 RGB 图规整成 **uint8**（cv2.cvtColor 的硬性要求）。

    ⚠️ 血泪坑（2026-10-10 集成测试抓到）：`cv2.cvtColor` 对 **float32 图像期望值域 [0,1]**，
    传 0~255 的 float32 不会报错，而是**静默**产出一堆垃圾 HSV —— 表现就是「色度锁定后
    图案几乎全被去饱和」（鲜艳像素从 ~20 万暴跌到 17）。`redraw_part` 里的 `white` 正好是
    float32(0~255)（白底合成 `sub*rgb*al + 255*(1-al)`），于是方案 A 在整链路里等于没生效。
    所以这里**统一在函数入口归一**，杜绝调用方 dtype 传错。"""
    import numpy as np
    a = np.asarray(x)
    if a.dtype == np.uint8:
        return np.ascontiguousarray(a)
    if a.dtype == np.uint16:
        return np.ascontiguousarray((a.astype(np.float32) / 257.0).round().astype(np.uint8))
    f = a.astype(np.float32)
    if f.size and float(f.max()) <= 1.5:      # 已是 [0,1] 的归一化浮点 → 还原到 0~255
        f = f * 255.0
    return np.ascontiguousarray(np.clip(f, 0.0, 255.0).round().astype(np.uint8))


def _lock_hue_sat(ai_rgb, ori_rgb, ori_alpha, hue_mix=1.0, sat_mix=1.0, value_lift=0.40):
    """【方案 A · 色度锁原图】把 AI 重绘图的**色相 / 饱和度硬搬回原图**，只保留 AI 的明暗与线条。

    为什么必须硬锁：AI 重绘走 `/v1/images/edits`，**没有「参考强度」这个旋钮**，prompt 又只是
    软约束 —— 实测把「红 + 紫」的支架图案整片画成「绿 + 青」（平均 ΔE 54+，鲜艳像素还翻了 10 倍）。
    所以这里做程序兜底，保证交付图的颜色与原图同色系，「红线永远是红线」。

    实现要点（三条都有坑，别改回去）：
      · **色相 H 是 cv2 的 0~179 环形量**，必须按「最短弧」搬差：直接相减会绕圈
        （红 0 与紫 178 实际只差 2，硬减会得到 -178 → 颜色反着跑）；
      · 饱和度 S 线性换成原图的；**明度 V 完全保留 AI** —— 线条干净度与明暗关系是 AI 的功劳，一点不碰；
      · `ori_alpha` 是原图 alpha：**只在原图真有内容处锁定**，AI 补画的原图被遮挡 / 透明区
        保留 AI 自己的配色（那里本来就没有原图颜色可抄，硬涂白反而抹掉 AI 的补全）；
      · `value_lift`：AI 若把原图的亮色区整块画暗（V 掉太多），锁了色相也看不出颜色 →
        按原图亮度给个温和保底；原图本来就暗的地方（线条）lift≈0 → 不会把线条糊亮。

    参数尺寸可不同：ori_rgb / ori_alpha 会按最近邻域插值缩放到 ai_rgb 的尺寸。
    **入参 dtype 不限** —— `_as_rgb_u8` 会在入口统一归一到 uint8（float32 传 0~255 也不会出错）。
    返回与 ai_rgb 同尺寸的 RGB uint8。"""
    import cv2, numpy as np
    # ⚠️ 必须先在入口归一成 uint8：cv2.cvtColor 对 float32 期望 [0,1]，传 0~255 会静默出错。
    ai = _as_rgb_u8(ai_rgb)
    orr = _as_rgb_u8(ori_rgb)
    if ai.ndim == 2:
        ai = cv2.cvtColor(ai, cv2.COLOR_GRAY2RGB)
    if orr.ndim == 2:
        orr = cv2.cvtColor(orr, cv2.COLOR_GRAY2RGB)
    if ai.shape[:2] != orr.shape[:2]:
        # 先缩 RGB 再转 HSV：**绝不能直接对 H 通道做插值** —— 环形量插值会在色块边界
        # 生成不存在的中间色（红 0 与紫 178 插出绿 89）。
        orr = cv2.resize(orr, (ai.shape[1], ai.shape[0]),
                         interpolation=cv2.INTER_AREA if orr.shape[0] > ai.shape[0] else cv2.INTER_CUBIC)
    if ori_alpha is None:
        w = np.ones(ai.shape[:2], np.float32)
    else:
        w = np.asarray(ori_alpha, np.float32)
        if w.ndim == 3:
            w = w[..., 0]
        if w.size and float(w.max()) > 1.5:   # 0~255 → 0~1；已是 [0,1] 的原样用
            w = w / 255.0
        if w.shape[:2] != ai.shape[:2]:
            w = cv2.resize(w, (ai.shape[1], ai.shape[0]), interpolation=cv2.INTER_LINEAR)
    w = np.clip(w, 0.0, 1.0)
    ah = cv2.cvtColor(ai, cv2.COLOR_RGB2HSV).astype(np.float32)
    oh = cv2.cvtColor(orr, cv2.COLOR_RGB2HSV).astype(np.float32)
    dh = ((oh[..., 0] - ah[..., 0] + 90.0) % 180.0) - 90.0      # 最短弧差（H 周期 = 180）
    h = (ah[..., 0] + dh * w * float(hue_mix)) % 180.0
    s = ah[..., 1] * (1.0 - w * float(sat_mix)) + oh[..., 1] * (w * float(sat_mix))
    v = ah[..., 2]
    if value_lift > 0:
        lift = np.clip(oh[..., 2] - v, 0.0, 255.0) * w * float(value_lift)
        v = np.minimum(255.0, v + lift)
    out = np.stack([h, np.clip(s, 0.0, 255.0), np.clip(v, 0.0, 255.0)], -1)
    return cv2.cvtColor(out.round().astype(np.uint8), cv2.COLOR_HSV2RGB)


def _bbox_of_mask(m):
    import numpy as np
    ys, xs = np.where(m)
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _fill_ratio(a):
    """内容 alpha 在**自身内容外接框内**的填充率（0~1）。

    最硬的形状指纹：正方形≈1.00、正圆≈0.785、细长条更低。
    圆形被画成方形时，这个数会从 ~0.785 跳到 ~1.00。"""
    import cv2, numpy as np
    a = np.asarray(a)
    if a.ndim == 3:
        a = a[..., 0]
    a = a.astype(np.float32)
    if a.size and float(a.max()) <= 1.5:
        a = a * 255.0
    m = a > 127.0
    ys, xs = np.where(m)
    if not len(xs):
        return 0.0
    bh = int(ys.max() - ys.min() + 1)
    bw = int(xs.max() - xs.min() + 1)
    return float(m.sum()) / float(max(1, bh * bw))


def _shape_deviation(src_a, ai_a, flat_thr=0.90, gap_thr=0.10):
    """判「AI 重绘把图案外形画走样」—— 最典型就是把**圆形圆盘画成满幅方形**。

    背景（2026-10-10 现场）：线上支架任务 AI 出图把圆形图案画成 1024×1024 满幅方形，
    程序却**完全信任 AI 的 alpha 轮廓**、没有任何形状校验 → 交付图变成方图。

    判据：原图内容填充率 sf 与 AI 内容填充率 af 对比。
      · 只有原图**本身不是满矩形**（sf < flat_thr，如圆形 0.785）时才有"走样"可言；
      · 且 AI 比原图明显更满（af - sf > gap_thr，圆形→方形差 ~0.215）。
    返回 (走样?, 原图填充率, AI 填充率)。原图本就是方形（sf≈1.0）时恒不判走样。"""
    sf = _fill_ratio(src_a)
    af = _fill_ratio(ai_a)
    bad = (sf < float(flat_thr)) and ((af - sf) > float(gap_thr))
    return bool(bad), round(sf, 4), round(af, 4)


def _restrict_alpha_to_src(ai_a, src_a, pad_px=2, sigma=0.8):
    """兜底：AI 外形走样时，**用原图轮廓把 AI 的 alpha 裁回原形状**（AI 只贡献颜色 / 笔触）。

    原理：原图 alpha 缩到 AI 内容尺寸 → 轻微膨胀（容忍 1~2px 对齐误差）→ 高斯软化（补抗锯齿），
    再与 AI alpha **取 min** → 圆形永远是圆形，方形边角被裁掉；圆内的颜色 / 线条仍来自 AI。"""
    import cv2, numpy as np
    ah, aw = ai_a.shape[:2]
    s = np.asarray(src_a)
    if s.ndim == 3:
        s = s[..., 0]
    s = s.astype(np.float32)
    if s.size and float(s.max()) <= 1.5:
        s = s * 255.0
    s = cv2.resize(s, (aw, ah), interpolation=cv2.INTER_LINEAR)
    m = (s > 127).astype(np.uint8)
    if pad_px > 0 and m.any():
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * int(pad_px) + 1, 2 * int(pad_px) + 1))
        m = cv2.dilate(m, k)
    mask = (m * 255).astype(np.uint8)
    if sigma > 0:
        mask = cv2.GaussianBlur(mask, (0, 0), float(sigma))
    out = np.minimum(ai_a.astype(np.float32), mask.astype(np.float32))
    return out.round().astype(np.uint8)


def redraw_part(pattern_png, out_png, key=None, size=None, on_log=None,
                white_thr=244, keep_scale=True, max_scale=8.0, lock_color=True):
    """【支架 / 部件 · AI 完全重绘】把抠好的透明底图案交给 AI 重绘成干净清晰的一版。

    pattern_png : extract_parts 产出的**全画布 RGBA**（透明底）——锁定版文件本身。
    out_png     : 输出 RGBA PNG（透明底），可直接顶替锁定版进分层 PSD。
    keep_scale  : **默认 True —— 保住 AI 的原生分辨率**。这是清晰度的关键：
                  锁定版的内容只有原图那么点大（支架圆盘常 150~300px），若把 AI 出的
                  ~1000px 缩回原尺寸 → 细节全丢、比原图还糊；改为按 AI 内容尺寸**等比放大整张画布**
                  （内容放大约 5×、画布同比放大、内容仍贴回原相对位置），
                  下游超分只需 ~2× 插值，清晰度实打实提升，且**相对版式零变化**。
    max_scale   : 放大倍数上限，防异常出图把画布撑爆。
    lock_color  : **方案 A · 色度锁原图**（默认开）。AI 有概率整片换色（实测红紫→绿青，
                  ΔE 54+），prompt 只是软约束 → 出图后把**色相 / 饱和度硬搬回原图**，
                  明暗仍用 AI 的（见 _lock_hue_sat）。失败不阻断，只保留 AI 原生配色。
    外形校验     : AI 另有低概率把**圆形图案画成满幅方形**（现场实测 1 次，圆 0.785→方 1.000）——
                  出图后按「内容填充率」判走样，走样则自动重试；仍走样则用**原图轮廓硬锁 alpha**
                  （AI 只贡献颜色 / 笔触），保证交付图的外形与原图一致。

    版式对齐原理：不按画布对齐、**按内容 bbox 对齐** ——
      ① 原图内容 bbox (cx0,cy0,cx1,cy1)，尺寸 (cw,ch)；
      ② AI 结果再算一次内容 bbox（原生 (acw,ach)）；
      ③ scale = acw/cw；输出画布 = 原画布 × scale；AI 内容**按 (cw,ch) 的原始长宽比**缩到
         (cw,ch)×scale，贴在 (cx0,cy0)×scale —— 位置与相对大小逐像素不变，只有笔触/清晰度变了。
    返回 (ok, msg, info)；info 带 scale / 输出画布 / 实出内容尺寸 / 色度锁定状态，供下游裁切换算。
    """
    import cv2, numpy as np, tempfile
    from PIL import Image
    key = key or load_key()
    im = Image.open(pattern_png).convert("RGBA")
    W, H = im.size
    arr = np.array(im)
    bb = _bbox_of_mask(arr[..., 3] > 8)
    if not bb:
        raise RuntimeError("待重绘的图案是透明的（alpha 全空），没有内容可重绘")
    cx0, cy0, cx1, cy1 = bb
    cw, ch = cx1 - cx0, cy1 - cy0
    if cw < 8 or ch < 8:
        raise RuntimeError("待重绘的图案太小（%dx%d），不适合 AI 重绘" % (cw, ch))
    if not size:
        size = part_redraw_size(cw, ch)
    # 透明底 → 纯白底（避免上游把透明区当黑/棋盘背景，也避免 alpha 干扰重绘）
    sub = arr[cy0:cy1, cx0:cx1].astype(np.float32)
    al = (sub[..., 3:4] / 255.0)
    white = sub[..., :3] * al + 255.0 * (1.0 - al)
    tmp = os.path.join(tempfile.gettempdir(), "part_redraw_in_%d.png" % time.time_ns())
    Image.fromarray(white.round().astype(np.uint8), "RGB").save(tmp)
    if on_log:
        on_log("AI 完全重绘（版式死锁 prompt，请求尺寸 %s，原内容 %dx%d）…" % (size, cw, ch))
    ai_raw = out_png + ".ai.png"
    try:
        b64 = img_to_b64(tmp)
        src_a_content = sub[..., 3]              # 原图内容 alpha（sub 已是内容 bbox 裁剪）
        g = None
        a_full = None
        abb = None
        shape_bad = False
        s_fill = a_fill = 0.0
        # 上游对「精确复刻输入图」有概率性内容审核（upstream_text_r）→ 软拒绝自动重试；
        # AI 也有低概率把图案外形画走样（圆形→满幅方形）→ 一并重试。
        for attempt in range(REDRAW_RETRIES + 1):
            try:
                tid = gen_image(key, PART_REDRAW_PROMPT, size, b64)
                url = wait_task(key, tid, timeout=600)
                download(url, ai_raw)
                if not os.path.isfile(ai_raw) or os.path.getsize(ai_raw) < 1024:
                    raise RuntimeError("AI 重绘输出异常（文件缺失或过小）")
                g = np.array(Image.open(ai_raw).convert("RGB"))
                a_full = _alpha_from_white(g)
                abb = _bbox_of_mask(a_full > 32)
                if not abb:
                    raise RuntimeError("AI 重绘结果里找不到图案（整幅近白）")
                shape_bad, s_fill, a_fill = _shape_deviation(
                    src_a_content, a_full[abb[1]:abb[3], abb[0]:abb[2]])
                if shape_bad and attempt < REDRAW_RETRIES:
                    if on_log:
                        on_log("AI 出图外形走样（填充率 %.3f → %.3f，疑似圆形被画成方形），自动重试…"
                               % (s_fill, a_fill))
                    time.sleep(3)
                    continue
                break
            except Exception as e:
                m = str(e)
                soft = ("相似性" in m) or ("违反" in m) or ("upstream_text_r" in m)
                if attempt < REDRAW_RETRIES and soft:
                    if on_log:
                        on_log("上游内容审核软拒绝（第 %d 次），4 秒后自动重试…" % (attempt + 1))
                    time.sleep(4)
                    continue
                raise
        if g is None or abb is None:
            raise RuntimeError("AI 重绘未产出可用图像")
        ax0, ay0, ax1, ay1 = abb
        acw, ach = ax1 - ax0, ay1 - ay0
        crop_rgb = g[ay0:ay1, ax0:ax1]
        crop_a = a_full[ay0:ay1, ax0:ax1]
        # 全部重试仍走样 → 用原图轮廓把 AI 的 alpha 裁回原形状（AI 只贡献颜色 / 笔触），
        # 保证交付图的外形与原图一致（圆形永远是圆形），不再把走样图交给下游。
        shape_msg = "正常"
        if shape_bad:
            crop_a = _restrict_alpha_to_src(crop_a, src_a_content)
            shape_msg = "走样→已锁原图轮廓（原图填充率 %.3f / AI %.3f）" % (s_fill, a_fill)
            if on_log:
                on_log("AI 出图外形走样，已用原图轮廓锁回形状（原图填充率 %.3f → AI %.3f）" % (s_fill, a_fill))
        # 【方案 A · 色度锁原图】AI 有概率整片换色（实测红紫 → 绿青，ΔE 54+），
        # prompt 只能软约束 → 出图后把色相/饱和度硬搬回原图（比到 AI 内容尺寸后逐像素锁）。
        # white = 原图内容的白底 RGB、sub[...,3] = 原图 alpha：只在原图真有内容处锁，
        # AI 补画的原图被遮挡/透明区保留 AI 自己的配色。失败不阻断。
        lock_msg = "关闭（用 AI 原生配色）"
        if lock_color:
            try:
                # ⚠️ 必须取整成 uint8 再传：white 是 float32(0~255)，cv2.cvtColor 对 float32
                #    期望 [0,1]，直接传会静默产出垃圾 HSV → 锁定等于去饱和（历史 bug）。
                lock_ori = white.round().astype(np.uint8)
                crop_rgb = _lock_hue_sat(crop_rgb, lock_ori, sub[..., 3])
                lock_msg = "已锁原图色相/饱和度"
            except Exception as e:
                lock_msg = "锁定失败，已保留 AI 原生配色：%s" % str(e)[:80]
        scale = 1.0
        if keep_scale:
            scale = min(float(max_scale), max(1.0, acw / float(cw)))
        tw, th = max(1, int(round(cw * scale))), max(1, int(round(ch * scale)))
        interp = cv2.INTER_AREA if acw > tw else cv2.INTER_CUBIC
        rgb_r = cv2.resize(crop_rgb, (tw, th), interpolation=interp)
        a_r = cv2.resize(crop_a, (tw, th),
                         interpolation=cv2.INTER_AREA if acw > tw else cv2.INTER_LINEAR)
        OW, OH = max(1, int(round(W * scale))), max(1, int(round(H * scale)))
        out = np.zeros((OH, OW, 4), np.uint8)
        ox = max(0, min(OW - tw, int(round(cx0 * scale))))
        oy = max(0, min(OH - th, int(round(cy0 * scale))))
        # ⚠️ rgb_r 来自 PIL（已是 RGB 序），`Image.fromarray(out,"RGBA")` 期望的也是 RGB 序
        #    → 这里**绝不能**再 `[:, :, ::-1]`。历史的 `::-1` 是照抄 extract_parts 的写法
        #    （那里 crop 是 cv2 的 BGR，转一次才对），结果让重绘版红蓝互换 ——
        #    实测反转回去后色相才从「绿 28.7% + 青 71.3%」回到与「红 47.1% + 紫 52.9%」同族。
        out[oy:oy + th, ox:ox + tw, :3] = rgb_r
        out[oy:oy + th, ox:ox + tw, 3] = a_r
        Image.fromarray(out, "RGBA").save(out_png)
        info = {"scale": round(scale, 4), "canvas": [OW, OH], "content": [acw, ach],
                "origin_canvas": [W, H], "origin_content": [cw, ch],
                "request": size, "align": [ox, oy, tw, th], "color_lock": lock_msg,
                "shape": shape_msg}
        if on_log:
            on_log("AI 完全重绘完成：请求 %s → 内容 %dx%d；放大 %.2f× → 画布 %d×%d，已对齐回原相对位置；"
                   "色度锁定：%s；外形：%s" % (size, acw, ach, scale, OW, OH, lock_msg, shape_msg))
        return True, ("AI 完全重绘完成（内容 %dx%d 放大 %.2f×，版式对齐回原位；色度%s）"
                      % (acw, ach, scale, lock_msg)), info
    finally:
        for f in (tmp, ai_raw):
            try:
                if os.path.isfile(f):
                    os.remove(f)
            except OSError:
                pass


def _content_bbox(im):
    """内容包围盒 (x0,y0,x1,y1)：RGBA 看 alpha，RGB 看非近白像素。"""
    import cv2, numpy as np
    if im.ndim == 3 and im.shape[2] == 4:
        m = im[..., 3] > 10
    else:
        b, g, r = cv2.split(im[:, :, :3])
        m = ~((b > 235) & (g > 235) & (r > 235))
    ys, xs = np.where(m)
    if len(ys) == 0:
        return (0, 0, im.shape[1] - 1, im.shape[0] - 1)
    return (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))


# 摄像头区域「向下延伸系数」默认值（2026-10-09 用户拍板：0.5）
LENS_DOWN_EXT = float(os.environ.get("LENS_DOWN_EXT", "0.5"))


def lens_region_crop(clean_png, redraw_png, parts=None, down_ext=None,
                     out_png=None, on_log=None):
    """【摄像头/大镜头框】「区域比例抠选」——从满幅重绘版抠出摄像头周边图案。

    为什么这样做：摄像头框在壳身里**横向几乎占满全宽**（占内容宽 ~85%）、**纵向只占
    顶部 ~27%**，所以抠选区「横向取内容全宽」天然正确，唯一要调的是「纵向从摄像头
    框底向下延伸多少」。用对称外扩会得到一个接近方形的区（比例错、还漏下方图案）。

    几何链：
      ① 干净壳内容 bbox ↔ 满幅重绘版内容 bbox 对齐（重绘版是干净壳的 AI 满幅重绘，
         版式 1:1 死锁 → 内容 bbox 比例一致，可作可靠锚点）；
      ② 摄像头框 = detect_parts 在 crop 上检出的「大镜头框」rect（crop 坐标）
         → 干净壳全图坐标（cmeta.off + box）→ 内容 bbox 对齐映射到重绘版坐标；
      ③ 抠选区 = 横向：重绘版内容 bbox 全宽；纵向：内容顶 → 摄像头框底 + down_ext×框高。

    返回 (rgba, meta)；meta 含 cam_redraw / crop_box / down_ext 等诊断字段。"""
    import cv2, numpy as np
    from PIL import Image
    down_ext = LENS_DOWN_EXT if down_ext is None else float(down_ext)
    def _rd(p):
        return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    clean = _rd(clean_png)
    redraw = _rd(redraw_png)
    if clean is None or redraw is None:
        raise RuntimeError("读取干净壳/重绘版失败")

    # ② 定位摄像头框（crop 坐标 → 干净壳全图坐标）
    crop, cmeta = shell_crop_meta(clean_png)
    det = [dict(p) for p in (parts or [])]
    if not det:
        det = [p for p in detect_parts(crop) if p.get("kind") == PART_LENS]
    cam = None
    for p in det:
        if p.get("shape") == "rect":
            x, y, w, h = [int(v) for v in p["box"]]
            cam = (x, y, w, h)
            break
    if cam is None:
        # 兜底：crop 顶部 97% 宽 × 36% 高（摄像头模组通常区域）
        cw, ch = crop.shape[1], crop.shape[0]
        cam = (0, 0, max(1, int(cw * 0.97)), max(1, int(ch * 0.36)))
    offx, offy = cmeta["off"]
    cam_x0 = offx + cam[0]; cam_y0 = offy + cam[1]
    cam_w, cam_h = cam[2], cam[3]

    # ① 内容 bbox 对齐：干净壳 ↔ 重绘版
    lb = _content_bbox(clean)
    rb = _content_bbox(redraw)
    sx = (rb[2] - rb[0]) / float(max(1, lb[2] - lb[0]))
    sy = (rb[3] - rb[1]) / float(max(1, lb[3] - lb[1]))
    cam_rx0 = rb[0] + (cam_x0 - lb[0]) * sx
    cam_ry0 = rb[1] + (cam_y0 - lb[1]) * sy
    cam_rw = cam_w * sx
    cam_rh = cam_h * sy
    cam_rx1 = cam_rx0 + cam_rw
    cam_ry1 = cam_ry0 + cam_rh

    # ③ 区域比例抠选：横向全宽，纵向 内容顶 → 框底 + down_ext×框高
    H, W = redraw.shape[:2]
    x0, x1 = int(round(rb[0])), int(round(rb[2]))
    y0 = int(round(rb[1]))
    y1 = int(round(cam_ry1 + down_ext * cam_rh))
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(W, x1), min(H, y1)
    if x1 <= x0 or y1 <= y0:
        raise RuntimeError("区域比例抠选得到空区（x %d..%d y %d..%d）" % (x0, x1, y0, y1))

    sub = redraw[y0:y1, x0:x1, :3]
    rgba = _cutout_core(sub, hole_mask=None)
    if out_png:
        Image.fromarray(rgba, "RGBA").save(out_png)

    meta = {
        "cam_redraw": [int(cam_rx0), int(cam_ry0), int(cam_rw), int(cam_rh)],
        "crop_box": [x0, y0, x1, y1],
        "down_ext": down_ext,
        "size": [int(rgba.shape[1]), int(rgba.shape[0])],
        "ratio": round(rgba.shape[1] / float(rgba.shape[0]), 3),
        "ppm": round(float((rgba[..., 3] > 0).mean() * 100), 1),
    }
    if on_log:
        on_log("区域比例抠选：%dx%d（比例 %.2f）DOWN_EXT=%.2f，图案占比 %.1f%%"
               % (meta["size"][0], meta["size"][1], meta["ratio"], down_ext, meta["ppm"]))
    return rgba, meta


def layout_deviation(ref_png, test_png, grid=6, max_shift=0.12, shift_tol=0.045,
                     sim_tol=0.72, ref_label=""):
    """版式校验：判断 test 相对 ref 的元素版式有没有被重排。
    ① 边缘图平移搜索（搜索范围 ±max_shift×画布）→ 全局最佳偏移 (dx,dy) 与匹配度；
    ② 在最佳对齐下，把两图 6×6 网格的「边缘密度向量」做余弦相似度 → 构图一致性。
    ⚠️ ref 必须选**不含配件遮挡**的参考（满幅任务用「锁定版」而不是「干净壳」）：
    干净壳上还杵着支架圆环和摄像头模组，而重绘版按设计会把配件去掉，
    拿带配件的原图当基准，好作品也会被算成「被重排」。
    返回 {dx, dy, shift_pct, sim, match, ok, note}"""
    import cv2, numpy as np
    SZ = 512

    def prep(p):
        """取「图案内容」并归一化：先按内容 bbox 裁掉外围空白/背景，再缩到 SZ×SZ。
        为什么必须裁剪：干净壳（原图）与重绘版的画布比例往往不同（壳区细长 vs 重绘 3:4），
        直接整图 resize 会各自形变，正常作品也会被误判成「版式偏移」。"""
        im = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if im is None:
            raise RuntimeError("版式校验读图失败: " + os.path.basename(p))
        if im.ndim == 3 and im.shape[2] == 4:
            mask = im[..., 3] > 16
            bgr = im[..., :3]
        else:
            bgr = im if im.ndim == 3 else cv2.cvtColor(im, cv2.COLOR_GRAY2BGR)
            cs = np.vstack([bgr[:8, :8].reshape(-1, 3), bgr[:8, -8:].reshape(-1, 3),
                            bgr[-8:, :8].reshape(-1, 3), bgr[-8:, -8:].reshape(-1, 3)])
            diff = np.abs(bgr.astype(int) - np.median(cs, axis=0).astype(int)).sum(-1)
            mask = diff > 60
        ys, xs = np.where(mask)
        if len(ys) < 200:                        # 内容太少 → 退回整图
            y0, y1, x0, x1 = 0, bgr.shape[0], 0, bgr.shape[1]
        else:
            y0, y1 = int(ys.min()), int(ys.max()) + 1
            x0, x1 = int(xs.min()), int(xs.max()) + 1
        g = cv2.cvtColor(bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, (SZ, SZ), interpolation=cv2.INTER_AREA)
        e = cv2.Canny(g, 60, 160).astype(np.float32)
        return cv2.GaussianBlur(e, (0, 0), 3)

    ea = prep(ref_png)
    eb = prep(test_png)
    m = int(SZ * max_shift)
    tpl = ea[m:SZ - m, m:SZ - m]
    res = cv2.matchTemplate(eb, tpl, cv2.TM_CCOEFF_NORMED)
    _, match, _, loc = cv2.minMaxLoc(res)
    dx, dy = int(loc[0] - m), int(loc[1] - m)
    M = np.float32([[1, 0, -dx], [0, 1, -dy]])
    ebs = cv2.warpAffine(eb, M, (SZ, SZ))
    gs = SZ // grid                      # 512 未必被 grid 整除 → 先裁到 grid*gs 再分块
    a2 = ea[:grid * gs, :grid * gs]
    b2 = ebs[:grid * gs, :grid * gs]
    va = a2.reshape(grid, gs, grid, gs).mean((1, 3)).ravel()
    vb = b2.reshape(grid, gs, grid, gs).mean((1, 3)).ravel()
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    sim = float((va * vb).sum() / (na * nb + 1e-6)) if na > 0 and nb > 0 else 0.0
    sp = max(abs(dx), abs(dy)) / float(SZ)
    ok = (sp <= shift_tol) and (sim >= sim_tol)
    note = ("对比基准：%s · 全局偏移 %+d/%+d px（画布 %.1f%%）· 构图相似度 %.2f · %s"
            % (ref_label or os.path.basename(ref_png), dx, dy, sp * 100, sim,
               "未检测到元素重排 ✓" if ok else "疑似被重排，建议「重跑 AI 重绘版」再试"))
    return {"dx": dx, "dy": dy, "shift_pct": round(sp * 100, 2), "sim": round(sim, 3),
            "match": round(float(match), 3), "ok": bool(ok), "note": note}


def layers_from_cutout(img_path, out_dir, big_min=1800, max_big=12):
    """对「已是透明底」的图案分层：小图标（波点/小星）合并为底图层，主元素各占一层。"""
    import cv2, numpy as np
    from PIL import Image
    from PIL.Image import Resampling
    im = Image.open(img_path).convert("RGBA")
    arr = np.array(im)
    h, w = arr.shape[:2]
    alpha = arr[..., 3]
    os.makedirs(out_dir, exist_ok=True)
    n, labels, stats, _ = cv2.connectedComponentsWithStats((alpha > 0).astype(np.uint8), 8)
    ids = list(range(1, n))
    big = sorted([i for i in ids if stats[i, cv2.CC_STAT_AREA] >= big_min],
                 key=lambda i: -stats[i, cv2.CC_STAT_AREA])[:max_big]
    bset = set(big)
    small = [i for i in ids if i not in bset]
    layers = []
    if small:
        layers.append(("小图标底图", np.isin(labels, small)))
    for i in big:
        layers.append(("元素%02d" % i, labels == i))
    files = []
    for nm, m in layers:
        lyr = arr.copy()
        lyr[..., 3] = np.where(m, arr[..., 3], 0)
        fp = os.path.join(out_dir, "L_%s.png" % nm)
        Image.fromarray(lyr).save(fp)
        files.append((nm, fp))
    return files, (w, h)

# ---------------- 分层 ----------------
def layers_white_bg(img_path, out_dir):
    import cv2, numpy as np
    from PIL import Image
    arr = cv2.imdecode(np.fromfile(img_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    h, w = arr.shape[:2]
    # 白转透明：从四角分别泛洪（FIXED_RANGE 固定相对白色容差，防止沿 AI 柔边渐变穿透进元素内部），
    # 累积背景掩码
    mask = np.zeros((h + 2, w + 2), np.uint8)
    flags = 4 | cv2.FLOODFILL_MASK_ONLY | cv2.FLOODFILL_FIXED_RANGE | (255 << 8)
    work = arr.copy()
    for seed in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        cv2.floodFill(work, mask, seed, 0, (12, 12, 12), (12, 12, 12), flags)
    bg = mask[1:-1, 1:-1] == 255
    alpha = np.where(bg, 0, 255).astype(np.uint8)
    rgba = np.dstack([arr, alpha])
    edge = cv2.morphologyEx(alpha, cv2.MORPH_DILATE, np.ones((3, 3), np.uint8)) - alpha
    for c in range(3):
        ch = rgba[..., c].astype(int)
        a = alpha.astype(int) / 255.0
        comp = np.clip((ch - 255 * (1 - a)) / np.maximum(a, 1e-3), 0, 255)
        rgba[..., c] = np.where(edge > 0, comp, rgba[..., c]).astype(np.uint8)
    rgba[..., 3] = np.where(edge > 0, np.clip(255 - rgba[..., :3].min(axis=2), 0, 255), alpha)
    os.makedirs(out_dir, exist_ok=True)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(alpha, 8)
    # 所有连通域按面积降序：前10大各一层，其余全部归并「小元素合并」层（不丢弃任何内容）
    comps = sorted(range(1, n), key=lambda i: -stats[i, cv2.CC_STAT_AREA])
    big = comps[:10]
    small_ids = comps[10:]
    layers = []
    if small_ids:
        layers.append(("小元素合并", np.isin(labels, small_ids)))
    for i in big:
        layers.append(("元素%02d" % i, labels == i))
    out_files = []
    for name, m in layers:
        lyr = rgba.copy()
        lyr[..., 3] = np.where(m, rgba[..., 3], 0)
        fp = os.path.join(out_dir, "L_%s.png" % name)
        Image.fromarray(cv2.cvtColor(lyr, cv2.COLOR_BGRA2RGBA)).save(fp)
        out_files.append((name, fp))
    return out_files, (arr.shape[1], arr.shape[0])

def layers_full_bleed(img_path, out_dir, k=12):
    import cv2, numpy as np
    from PIL import Image
    arr = cv2.imdecode(np.fromfile(img_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    h, w = arr.shape[:2]
    sm = cv2.resize(arr, (max(1, w * 512 // max(w, h)), max(1, h * 512 // max(w, h))),
                    interpolation=cv2.INTER_AREA).astype(np.float32)
    pts = sm.reshape(-1, 3)
    idx = np.random.default_rng(42).choice(len(pts), min(50000, len(pts)), replace=False)
    X = pts[idx]
    cent = X[np.random.default_rng(7).choice(len(X), k, replace=False)].copy()
    for _ in range(15):
        d = ((X[:, None, :] - cent[None]) ** 2).sum(-1)
        lab = d.argmin(1)
        for j in range(k):
            sel = X[lab == j]
            if len(sel):
                cent[j] = sel.mean(0)
    cent = cent.round().astype(np.uint8)
    assign = np.empty((h, w), np.uint8)
    BS = 256
    for y in range(0, h, BS):
        for x in range(0, w, BS):
            blk = arr[y:y+BS, x:x+BS].astype(np.int32)
            d = ((blk[..., None, :] - cent[None, None].astype(np.int32)) ** 2).sum(-1)
            assign[y:y+BS, x:x+BS] = d.argmin(-1).astype(np.uint8)
    os.makedirs(out_dir, exist_ok=True)
    files = []
    for j in range(k):
        lyr = np.dstack([arr, ((assign == j) * 255).astype(np.uint8)])
        nm = "色系%02d_%02x%02x%02x" % (j, *cent[j][::-1])
        fp = os.path.join(out_dir, "L_%s.png" % nm)
        Image.fromarray(cv2.cvtColor(lyr, cv2.COLOR_BGRA2RGBA)).save(fp)
        files.append((nm, fp))
    return files, (w, h)

# ---------------- 分层（归并制：底色 / 底纹 / 主体） ----------------
SUBJ_MIN_FRAC = 0.005     # 主体元素最小面积占比（相对画布）
SUBJ_MIN_PX = 1500        # 主体元素最小绝对面积
BG_MIN_FRAC = 0.005       # 判为「底色」的连通区最小面积占比
LAYER_MODES = ("merge2", "merge3", "fine", "connected")


def _dominant_bg(rgb, min_share=0.12):
    """找整幅图的主导背景色（量化到 32 级/通道统计）；照片纹理型找不到返回 None。"""
    import numpy as np
    q = rgb.astype(np.int32) // 32
    key = (q[..., 0] << 10) | (q[..., 1] << 5) | q[..., 2]
    vals, counts = np.unique(key, return_counts=True)
    i = int(counts.argmax())
    if counts[i] / key.size < min_share:
        return None
    return rgb[key == vals[i]].mean(0).round().astype(np.uint8)


def _bg_mask(rgb, color, tol=18, min_frac=BG_MIN_FRAC):
    """与 color 接近 **且成片（连通面积够大）** 的像素 → 底色区。
    只取大片连通域，避免把图案内部的同色小块（白肚/白脸）误判成底色。"""
    import cv2, numpy as np
    h, w = rgb.shape[:2]
    cand = (np.abs(rgb.astype(np.int32) - color.astype(np.int32)).sum(-1) < tol * 3).astype(np.uint8)
    n, lb, st, _ = cv2.connectedComponentsWithStats(cand, 8)
    keep = np.zeros((h, w), bool)
    thr = h * w * min_frac
    for k in range(1, n):
        if st[k, cv2.CC_STAT_AREA] >= thr:
            keep |= (lb == k)
    return keep


def _subject_threshold(h, w):
    return max(int(h * w * SUBJ_MIN_FRAC), SUBJ_MIN_PX)


def batch_subject_dish(rgba):
    """把前景连通域按面积切成 (主体掩码, 底纹掩码)。"""
    import cv2, numpy as np
    h, w = rgba.shape[:2]
    fg = (rgba[..., 3] > 0).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(fg, 8)
    T = _subject_threshold(h, w)
    subj_ids = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= T]
    dish_ids = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] < T]
    subj = np.isin(labels, subj_ids) if subj_ids else np.zeros((h, w), bool)
    dish = np.isin(labels, dish_ids) if dish_ids else np.zeros((h, w), bool)
    return subj, dish, len(subj_ids), len(dish_ids)


def _write_layers(rgba, masks, out_dir):
    """按掩码写层 PNG；不可见像素 RGBA 全清零（RLE/ZIP 压缩率的关键）。"""
    import numpy as np
    from PIL import Image
    os.makedirs(out_dir, exist_ok=True)
    files = []
    for nm, m in masks:
        lyr = rgba.copy()
        lyr[~m] = 0
        fp = os.path.join(out_dir, "L_%s.png" % nm)
        Image.fromarray(lyr).save(fp)
        files.append((nm, fp))
    return files


def build_layers(img_path, out_dir, kind="cutout", mode="merge2"):
    """统一分层入口（底→顶顺序）。

    kind : cutout 透明底图案 / white_bg 白底图 / full_bleed 满幅不透明
    mode : merge2    底纹层(含纯色底+小元素+纹理) + 主体层      ← 用户指定
           merge3    底色层 + 底纹层 + 主体层
           fine      小元素合并层 + 前 12 大元素各一层（旧精细模式）
           connected 连通域精细 · 面积≥阈值的大元素各一层 + 小元素合并（适合支架/镜头等部件）
    返回 (files, (w, h))
    """
    import cv2, numpy as np
    from PIL import Image
    if mode not in LAYER_MODES:
        mode = "merge2"

    if mode == "connected":
        # 连通域精细分层：白/透明底已转透明 → 按 alpha 连通域面积降序，大元素各一层、小元素合并。
        # 复用 layers_from_cutout（默认 big_min=1800 面积阈值、max_big=12 上限），
        # 与用户上一阶段「同样的方式」逐像素保真，且不丢弃任何内容。
        return layers_from_cutout(img_path, out_dir)

    im = Image.open(img_path)
    has_a = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    bg_color = None
    if has_a:
        rgba = np.array(im.convert("RGBA"))
    else:
        arr = cv2.imdecode(np.fromfile(img_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        rgb = cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
        bg_color = np.array([255, 255, 255], np.uint8) if kind == "white_bg" else _dominant_bg(rgb)
        if bg_color is None:
            # 满幅照片纹理：无主导底色，拆不出来 → 退化为 3 个明暗调层
            return _layers_tone(rgb, out_dir)
        base = _bg_mask(rgb, bg_color)
        rgba = np.dstack([rgb, np.where(base, 0, 255).astype(np.uint8)])

    h, w = rgba.shape[:2]
    if mode == "fine":
        masks = _masks_fine(rgba)
        return _write_layers(rgba, masks, out_dir), (w, h)

    subj, dish, n_subj, n_dish = batch_subject_dish(rgba)
    full = np.ones((h, w), bool)
    if mode == "merge3" and bg_color is not None:
        # 三层：底色单独抽出（纯色层，压缩后几乎不占体积）
        base = _bg_mask(rgba[..., :3], bg_color)
        masks = [("底纹层", dish & ~base), ("主体层", subj)]
        files = []
        bimg = np.zeros((h, w, 4), np.uint8)
        bimg[..., :3] = bg_color
        bimg[..., 3] = 255
        os.makedirs(out_dir, exist_ok=True)
        fp = os.path.join(out_dir, "L_底色层.png")
        Image.fromarray(bimg).save(fp)
        files.append(("底色层", fp))
        files += _write_layers(rgba, masks, out_dir)
        return files, (w, h)

    # 两层：底纹层吃下「除主体以外的一切」（含纯色底 / 小元素 / 纹理 / 透明区）
    dish2 = full & ~subj
    masks = [("底纹层", dish2), ("主体层", subj)]
    return _write_layers(rgba, masks, out_dir), (w, h)


def _masks_fine(rgba):
    import cv2, numpy as np
    h, w = rgba.shape[:2]
    n, labels, stats, _ = cv2.connectedComponentsWithStats((rgba[..., 3] > 0).astype(np.uint8), 8)
    areas = sorted(((i, int(stats[i, cv2.CC_STAT_AREA])) for i in range(1, n)), key=lambda t: -t[1])
    big = [i for i, _ in areas[:12]]
    small = [i for i, _ in areas[12:]]
    out = []
    if small:
        out.append(("小图标底图", np.isin(labels, small)))
    for i in big:
        out.append(("元素%02d" % i, labels == i))
    return out


def _layers_tone(rgb, out_dir, k=3):
    """满幅照片纹理兜底：按明度切 3 个调层（深/中/亮），代替原 12 色系层。"""
    import numpy as np
    from PIL import Image
    os.makedirs(out_dir, exist_ok=True)  # 兜底路径也要先建目录（铁律：写文件前必 makedirs）
    h, w = rgb.shape[:2]
    lum = rgb.mean(-1)
    qs = np.quantile(lum, [1.0 / 3, 2.0 / 3])
    idx = np.digitize(lum, qs)
    names = ["暗调层", "中调层", "亮调层"]
    files = []
    for j in range(k):
        m = idx == j
        lyr = np.dstack([rgb, (m * 255).astype(np.uint8)])
        lyr[~m] = 0
        fp = os.path.join(out_dir, "L_%s.png" % names[j])
        Image.fromarray(lyr).save(fp)
        files.append((names[j], fp))
    return files, (w, h)


# ---------------- PSD ----------------
def build_manifest(files, size, out_path):
    m = {"canvas": {"width": size[0], "height": size[1]}, "layers": [
        {"name": nm, "file": fp, "x": 0, "y": 0} for nm, fp in files]}
    json.dump(m, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

def psd_compression():
    """PSD 通道压缩方式：默认 rle（最大 Photoshop 兼容性），可用
    PATTERN_PSD_COMPRESSION=zip 换回体积更小的 ZIP+prediction。"""
    v = (os.environ.get("PATTERN_PSD_COMPRESSION") or "").strip().lower()
    return v if v in ("zip", "rle") else "rle"

def assemble(mf, psd_path, preview, compression=None):
    compression = compression or psd_compression()
    r = subprocess.run([sys.executable, os.path.join(BASE, "image2psd.py"),
                        "assemble", "-m", mf, "-o", psd_path, "--preview", preview,
                        "--compression", compression],
                       capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError("PSD 组装失败: " + (r.stderr or r.stdout)[-400:])

def inject_dpi(psd_path, dpi=DPI):
    import struct
    data = bytearray(open(psd_path, "rb").read())
    ir_len = struct.unpack(">I", data[30:34])[0]
    body = struct.pack(">IhhIhh", dpi << 16, 1, 1, dpi << 16, 1, 1)  # 16字节铁律
    res = b"8BIM" + struct.pack(">H", 1005) + b"\x00\x00" + struct.pack(">I", len(body)) + body
    if len(body) % 2:
        res += b"\x00"
    data[30:34] = struct.pack(">I", ir_len + len(res))
    data[34:34] = res
    open(psd_path, "wb").write(data)

def verify(psd_path, mf):
    import numpy as np
    from PIL import Image
    from psd_tools import PSDImage
    psd = PSDImage.open(psd_path)
    m = json.load(open(mf, encoding="utf-8"))
    bad = 0
    for layer in psd:
        rec = next(r for r in m["layers"] if r["name"] == layer.name)
        full = Image.open(rec["file"]).convert("RGBA")
        sp = layer.topil()
        src = np.array(sp.convert("RGBA") if sp.mode != "RGBA" else sp)
        # 层在 PSD 中按 bbox 紧凑存储：从全画布 PNG 裁出同一区域再比
        if src.shape[:2] == full.size[::-1] and layer.left == 0 and layer.top == 0:
            sub = np.array(full)
        else:
            W, H = full.size
            if layer.left < 0 or layer.top < 0 or layer.left + layer.width > W or layer.top + layer.height > H:
                bad += 1; continue
            sub = np.array(full.crop((layer.left, layer.top, layer.left + layer.width, layer.top + layer.height)))
        if src.shape != sub.shape:
            bad += 1; continue
        vis = sub[..., 3] > 0
        if not vis.any():
            continue
        ok = True
        for c in range(4):
            if np.abs(src[..., c].astype(int) - sub[..., c].astype(int))[vis].max(initial=0) > 0:
                ok = False; break
        if not ok:
            bad += 1
    return len(psd) - bad, len(psd)


# ================= 视觉 / 文本大模型调用（2026-11-18 新增，详情页生成版块用） =================
# 起因：详情页生成需要「看产品图 → 出 JSON 分析」「按规范 → 出逐页提示词」两种**文本**能力，
# 而工作台此前只有生图（gen_image），没有任何 chat 通道。
#
# 🔴 选模型不是拍脑袋，是 2026-11-18 在线上逐个真跑筛出来的：
#     ✅ lensapi   claude-haiku-4-5-20251001   纯文本 OK / **视觉识图 OK**
#     ✅ huahuaapi kimi-k3.1-pro               纯文本 OK / **视觉识图 OK**
#     ✅ lensapi   gpt-6-sol                   纯文本 OK（视觉未验，放链尾兜底）
#     ❌ huahuaapi gemini-4-hydrogen / argon   503 No available Gemini accounts
#     ❌ lensapi   gpt-5.5                     503 No available channel
#     ❌ lensapi   gpt-5.6-terra               502 openai_error
#   → 默认链把「已验证能看图」的两条放前面，纯文本那条兜底。
#
# 与 gen_image 同范式：**外层模型、内层站点**轮询；失败脱敏（不泄漏站点/模型/欠费）。
# 后台可改：ai_sites.json 的 routing.vision（缺失时用下面的默认值）。
VISION_CHAIN = tuple(m for m in os.environ.get(
    "PATTERN_VISION_CHAIN",
    "claude-haiku-4-5-20251001,kimi-k3.1-pro,gpt-6-sol").split(",") if m)
VISION_SITES = tuple(s for s in os.environ.get(
    "PATTERN_VISION_SITES", "lensapi,huahuaapi").split(",") if s)

# 模型 → 哪些站点有它（实测清单；未列的模型视为「所有启用站点都试」）
_VISION_MODEL_SITES = {
    "claude-haiku-4-5-20251001": ("lensapi",),
    "claude-sonnet-4-5-20251001": ("lensapi",),
    "gpt-6-sol": ("lensapi",),
    "kimi-k3.1-pro": ("huahuaapi",),
    "grok-4.7": ("huahuaapi",),
}


def _vision_plan():
    """[(site, base, key, model), ...]：外层模型、内层站点，只保留真支持该模型的站点。"""
    try:
        lane = (ai_sites.routing() or {}).get("vision") or {}
    except Exception:                                # noqa: BLE001
        lane = {}
    models = tuple(lane.get("models") or VISION_CHAIN)
    sites_pref = tuple(lane.get("sites") or VISION_SITES)
    enabled = {s["name"]: s for s in ai_sites.load_sites() if s.get("enabled", True)}
    plan = []
    for m in models:
        allow = _VISION_MODEL_SITES.get(m)
        order = [s for s in sites_pref if s in enabled]
        order += [s for s in enabled if s not in order]      # 未在偏好里的启用站点兜底
        for nm in order:
            if allow and nm not in allow:
                continue
            s = enabled[nm]
            plan.append((nm, s.get("base") or "", s.get("key") or "", m))
    return plan


def chat_vision(system, user, images=None, json_mode=False, timeout=180, max_tokens=None):
    """视觉/文本对话 → 返回 content 字符串。

    system  : 系统提示词（可空）
    user    : 用户指令文本
    images  : [bytes | data URI | base64 串]，逐张作为 image_url 追加（顺序即上传顺序）
    json_mode: True 时带 response_format={"type":"json_object"}，并剥掉 ```json 围栏
    max_tokens: 不给则不传（让上游用默认值；提示词很长，写死容易截断）

    全链失败 → RuntimeError（文案已脱敏）。
    """
    import re as _re                                  # 本文件 re 只在局部导入过，这里自带
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    parts = []
    if user:
        parts.append({"type": "text", "text": user})
    for it in (images or []):
        uri = ai_sites._as_data_uri(it)              # noqa: SLF001 —— 同项目内部工具
        if uri:
            parts.append({"type": "image_url", "image_url": {"url": uri}})
    msgs.append({"role": "user", "content": parts if parts else (user or "")})

    plan = _vision_plan()
    if not plan:
        raise RuntimeError("没有可用的文本模型站点，请在系统设置里检查站点配置。")
    sys.stderr.write("[chat_vision] 计划 %d 跳：%s\n" % (
        len(plan), ", ".join("%s/%s" % (p[0], p[3]) for p in plan[:6])))
    last_exc = None
    for site, base, skey, model in plan:
        url = (base or "").rstrip("/")
        if not url.endswith("/v1"):
            url += "/v1"
        url += "/chat/completions"
        payload = {"model": model, "messages": msgs}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        try:
            raw = ai_sites._http_raw("POST", url, payload, skey, timeout=timeout)  # noqa: SLF001
            j = json.loads(raw.decode("utf-8", "ignore"))
            if isinstance(j, dict) and j.get("error"):
                e = j["error"]
                raise RuntimeError(e if isinstance(e, str) else (e.get("message") or str(e)))
            ch = (j.get("choices") or [{}])[0]
            content = (ch.get("message") or {}).get("content")
            if isinstance(content, list):            # 少数上游回分段数组
                content = "".join(str(x.get("text") or "") if isinstance(x, dict) else str(x)
                                  for x in content)
            content = (content or "").strip()
            if not content:
                raise RuntimeError("上游返回空内容")
            if json_mode:
                content = _re.sub(r"^```json\s*", "", content)
                content = _re.sub(r"^```\s*", "", content)
                content = _re.sub(r"\s*```$", "", content).strip()
            ai_sites.remember(site, model)
            return content
        except Exception as e:                       # noqa: BLE001
            last_exc = e
            ai_sites.note_fail(site, model)
            sys.stderr.write("[chat_vision] %s / %s 失败：%s\n" % (site, model, str(e)[:160]))
            time.sleep(1)
    raise RuntimeError(ai_sites.sanitize(last_exc, prefix="AI 分析失败"))
