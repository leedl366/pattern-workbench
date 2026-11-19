# -*- coding: utf-8 -*-
"""详情页复刻版块 —— 纯文本资产（拆解提示词 / 逐屏复刻提示词拼装）。

设计原则（2026-11-18）：
  · **拆解用视觉模型**（分析参考页的版式/配色/层级/元素），**拼装用代码**（确定性、免费、可控）。
  · 逐屏提示词严格套用 gpt-image-2 结构化模板（Use case / Asset type / Input images /
    Composition / Text verbatim / Constraints / Avoid），这套字段名是 2026-11 从
    GitHub 三个开源电商生图工作流（linkpix-detail-page-clone、codexQIYU ecommerce-exact-replication、
    1click-ecom-detailpage）交叉比对后收敛出来的共同骨架，模型对它的遵循度最好。
  · 铁律：**参考图只给版式与视觉结构，产品身份只来自我的产品图**；
    参考页里的品牌/价格/评分/促销标/原商品一律进 Avoid，绝不带进成品。
"""

LANGS = ["中文", "英文", "无文字"]
PLATFORMS = ["淘宝", "京东", "拼多多", "抖音", "亚马逊", "通用"]
RATIOS = ["3:4", "1:1", "4:3", "9:16"]
RESOLUTIONS = ["1K", "2K"]

STRENGTHS = [
    {"id": "layout", "name": "版式锁死",
     "desc": "构图、元素位置、字号层级、留白 1:1 照搬，只换商品与文案（默认，最接近原版）"},
    {"id": "style", "name": "风格统一",
     "desc": "保留配色、图形语言与整体调性，版式允许按我的商品微调"},
    {"id": "creative", "name": "只借灵感",
     "desc": "只借氛围与视觉调性，版式重新设计（差异最大）"},
]
STRENGTH_IDS = [x["id"] for x in STRENGTHS]

# ---------------------------------------------------------------- 步骤2：拆解
ANALYZE_SYSTEM = """你是电商详情页的视觉拆解专家，专门为「照着别人的详情页做一套自己的」服务。
你的输出会被程序直接拼成生图提示词，所以必须**只描述可复用的视觉结构**，绝不搬运品牌资产。

拆解准则：
1. 逐屏拆解：一张参考图 = 一屏。屏内按「从上到下」顺序描述版式块（标题/主图/卖点/参数/对比/评价/场景/结尾）。
2. 只写看得见的东西：位置（顶部/中部/底部/左右分栏）、占比（约屏高百分比）、字号层级（大/中/小、字高约屏高百分比）、
   对齐方式、留白密度、配色（给 #RRGGBB，最多 5 个）、图形语言（圆角矩形/线条图标/渐变/色块/描边）、
   背景（纯色/渐变/场景图/纹理）、光线调性。
3. 原文案照抄到 copy 字段（逐字，含标点），它是版式的一部分；同时给出**改写** my_headline / my_sub / my_labels：
   · 🔴 my_* **不得与任何 copy 原句逐字相同**——相同即判为不合格输出，必须重写。改写方式：换卖点角度、换句式、换修饰词。
   · 参考页与我的商品**同品类时也必须换说法**，不能因为「本来就是同一类货」就照抄原句。
   · 有「我的核心卖点」优先用它；没给就用「我的商品图」里**看得见**的特征（材质、颜色、工艺、结构、配件数量），
     绝对不许编造看不见的参数（型号、认证、材质等级）。
   · 该屏有标题就必须给 my_headline，不许留空（留空等于让模型照抄画面里的原字）。
   · 保留原句式的长短与节奏（字数差控制在 30% 以内），不要变长。
4. brand_marks：列出这一屏里**必须移除**的东西（品牌名、logo、店铺名、价格、划线价、销量、评分、认证标、
   促销角标、二维码、原商品本身、模特脸、原品牌包装）。
5. 不要臆造参考图里没有的模块，不要评价好坏，不要写营销建议。
6. 严格输出 JSON，不要任何解释文字、不要 ``` 围栏。"""

_ANALYZE_SCHEMA = """{
  "product": {"name": "我的商品名（来自我的产品图与我的文案，没给就按品类推断）",
              "category": "品类", "summary": "一句话卖点（不超过 30 字）"},
  "style_dna": {
    "palette": ["#RRGGBB", "#RRGGBB", "#RRGGBB"],
    "font": "字体层级描述，如「粗黑无衬线大标题 + 细黑正文」",
    "whitespace": "留白节奏，如「上下各留 8% 空、块间距紧」",
    "lighting": "光线调性，如「柔和顶光、浅灰投影」",
    "graphic": "图形语言，如「圆角矩形信息条 + 细线图标」",
    "density": "信息密度，如「低密度、每屏一个重点」"
  },
  "screens": [
    {
      "no": 1,
      "goal": "这一屏要完成的说服任务，如「首屏承诺：让买家 1 秒知道这是什么」",
      "layout": "版式描述（位置+占比+对齐），如「顶部 1/6 主标题居中；中部产品大图占 55% 屏高居中；底部三个图标卖点横排」",
      "blocks": [
        {"type": "主标题", "pos": "顶部居中", "scale": "字高约屏高 6%", "copy": "原文案逐字"},
        {"type": "标签", "pos": "中部左侧", "scale": "字高约屏高 2.5%", "copy": "原文案逐字"}
      ],
      "elements": "这一屏的视觉元素清单（图标/色块/箭头/边框/纹理/道具）",
      "bg": "背景描述（纯色 #RRGGBB / 渐变 / 场景 / 纹理）",
      "brand_marks": ["必须移除：品牌名", "必须移除：价格 ￥199"],
      "my_headline": "改写后的主标题（同长度）",
      "my_sub": "改写后的副标题（可为空）",
      "my_labels": ["改写后的标签1", "改写后的标签2"]
    }
  ],
  "remove_list": ["整页都要移除的元素"],
  "keep_list": ["整页都要保留的结构要素"]
}"""


def analyze_messages(opt, n_ref, n_prod):
    """拆解请求：参考屏在前、我的产品图在后，角色在正文里写死（顺序即角色）。"""
    strength = opt.get("strength") or "layout"
    lines = []
    lines.append("下面一共 %d 张图，请按顺序理解角色：" % (n_ref + n_prod))
    lines.append("· Image 1 ~ Image %d：**参考详情页**，按屏顺序（Image 1 是第 1 屏）。" % n_ref)
    if n_prod:
        lines.append("· Image %d ~ Image %d：**我的商品图**（同一商品的不同角度/细节）。"
                     % (n_ref + 1, n_ref + n_prod))
    lines.append("")
    lines.append("任务：把参考详情页逐屏拆成结构化 JSON，供程序生成「同版式、换商品」的提示词。")
    lines.append("复刻强度 = %s（%s）。" % (strength, {
        "layout": "版式锁死：位置与占比必须精确记录",
        "style": "风格统一：记录配色与图形语言为主，版式记录到块级即可",
        "creative": "只借灵感：重点记录配色、光线、图形语言与调性",
    }.get(strength, "")))
    if opt.get("copy"):
        lines.append("我提供的文案（优先用它改写 my_* 字段，没覆盖到的屏再自己改写）：")
        lines.append(str(opt["copy"])[:2000])
    if opt.get("product"):
        lines.append("我的商品名：%s" % str(opt["product"])[:100])
    if opt.get("selling"):
        lines.append("我的核心卖点：%s" % str(opt["selling"])[:1000])
    lines.append("")
    lines.append("严格按这个 JSON 结构输出（screens 数组长度必须等于 %d，一屏一项，no 从 1 递增）：" % n_ref)
    lines.append(_ANALYZE_SCHEMA)
    return ANALYZE_SYSTEM, "\n".join(lines)


def norm_analysis(d, n_ref, opt):
    """把 AI 的 JSON 收敛成前端可直接编辑、程序可直接拼装的结构（缺项一律兜底，不报错）。"""
    def s(v, lim=4000):
        return str(v or "").strip()[:lim]

    d = d if isinstance(d, dict) else {}
    prod = d.get("product") if isinstance(d.get("product"), dict) else {}
    dna = d.get("style_dna") if isinstance(d.get("style_dna"), dict) else {}
    palette = [s(x, 20) for x in (dna.get("palette") or []) if str(x).strip()][:5]
    if not palette:
        palette = ["#FFFFFF", "#111111", "#888888"]

    screens = []
    raw = d.get("screens") if isinstance(d.get("screens"), list) else []
    for i in range(n_ref):
        src = raw[i] if i < len(raw) and isinstance(raw[i], dict) else {}
        blocks = []
        for b in (src.get("blocks") or [])[:12]:
            if not isinstance(b, dict):
                continue
            blocks.append({"type": s(b.get("type"), 40), "pos": s(b.get("pos"), 120),
                           "scale": s(b.get("scale"), 120), "copy": s(b.get("copy"), 300)})
        labels = [s(x, 60) for x in (src.get("my_labels") or []) if str(x).strip()][:6]
        screens.append({
            "no": i + 1,
            "goal": s(src.get("goal"), 200) or ("第 %d 屏" % (i + 1)),
            "layout": s(src.get("layout"), 1200),
            "blocks": blocks,
            "elements": s(src.get("elements"), 800),
            "bg": s(src.get("bg"), 300),
            "brand_marks": [s(x, 120) for x in (src.get("brand_marks") or []) if str(x).strip()][:10],
            "my_headline": s(src.get("my_headline"), 120),
            "my_sub": s(src.get("my_sub"), 160),
            "my_labels": labels,
            "title": s(src.get("goal"), 60) or ("第 %d 屏" % (i + 1)),
            "prompt": "",           # 步骤3 由代码拼装后回填
            "enabled": True,
        })
    return {
        "product": {"name": s(prod.get("name"), 120) or s(opt.get("product"), 120),
                    "category": s(prod.get("category"), 60),
                    "summary": s(prod.get("summary"), 200) or s(opt.get("selling"), 200)},
        "style_dna": {"palette": palette, "font": s(dna.get("font"), 200),
                      "whitespace": s(dna.get("whitespace"), 200),
                      "lighting": s(dna.get("lighting"), 200),
                      "graphic": s(dna.get("graphic"), 200),
                      "density": s(dna.get("density"), 200)},
        "screens": screens,
        "remove_list": [s(x, 120) for x in (d.get("remove_list") or []) if str(x).strip()][:20],
        "keep_list": [s(x, 120) for x in (d.get("keep_list") or []) if str(x).strip()][:20],
    }


# ---------------------------------------------------------------- 步骤3：逐屏提示词
_DEFAULT_KEEP = ["参考图的版式结构（块位置、占比、对齐）", "字号层级与留白节奏",
                 "配色方案与图形语言", "每屏只讲一个重点的节奏"]
_DEFAULT_REMOVE = ["参考图的品牌名 / logo / 店铺名", "参考图的价格、划线价、销量、评分、认证标",
                   "参考图的促销角标、二维码、水印", "参考图的原商品外观", "参考图的模特脸与纹身等身份特征"]


def _strength_block(strength):
    if strength == "style":
        return ("Primary request: 按参考图的**视觉风格**（配色、图形语言、光线、字重层级）做这一屏，"
                "把主体商品换成我的商品；版式可按我的商品比例微调，但**块的数量与阅读顺序必须一致**。")
    if strength == "creative":
        return ("Primary request: 只借参考图的**氛围与视觉调性**（配色倾向、光线、质感），"
                "重新设计这一屏的版式来完成同样的说服任务；不要照搬参考图的块位置。")
    return ("Primary request: **严格复刻参考图的版式**——块位置、占比、对齐方式、字号层级、留白节奏全部照搬，"
            "把主体商品换成我的商品，文案换成我给的文案；版式误差控制在 5% 以内。")


def screen_prompt(scr, ana, opt, n_ref, n_prod):
    """单屏提示词（gpt-image-2 结构化模板）。确定性拼装，同样的输入永远得到同样的文本。"""
    strength = opt.get("strength") if opt.get("strength") in STRENGTH_IDS else "layout"
    lang = opt.get("lang") or "中文"
    platform = opt.get("platform") or "通用"
    dna = ana.get("style_dna") or {}
    prod = ana.get("product") or {}
    no = scr.get("no") or 1

    keep = [k for k in (ana.get("keep_list") or []) if k] or _DEFAULT_KEEP
    remove = [k for k in (ana.get("remove_list") or []) if k] or _DEFAULT_REMOVE
    remove = remove + [x for x in (scr.get("brand_marks") or []) if x]
    # 去重保序
    seen, rmv = set(), []
    for x in remove:
        k = x.strip()
        if k and k not in seen:
            seen.add(k)
            rmv.append(k)

    img_lines = ["Image 1：**版式与视觉结构参考**（只取构图、网格、字号层级、留白、配色、图形语言；"
                 "其中任何商品、品牌、价格、促销元素都不得出现在成品里）"]
    if n_prod:
        if n_prod == 1:
            img_lines.append("Image 2：**我的商品身份参考**（形状、材质、颜色、logo、包装、配件数量以此为准，"
                             "不得改变外观）")
        else:
            img_lines.append("Image 2 ~ Image %d：**我的商品身份参考**（形状、材质、颜色、logo、包装、配件数量以此为准，"
                             "不得改变外观）" % (1 + n_prod))

    text_lines = []
    head = scr.get("my_headline") or ""
    sub = scr.get("my_sub") or ""
    labels = [x for x in (scr.get("my_labels") or []) if x]
    if lang != "无文字":
        if head:
            text_lines.append("主标题「%s」" % head)
        if sub:
            text_lines.append("副标题「%s」" % sub)
        if labels:
            text_lines.append("标签" + "、".join("「%s」" % x for x in labels))

    blocks_txt = "；".join(
        "%s（%s，%s）" % (b.get("type") or "块", b.get("pos") or "", b.get("scale") or "")
        for b in (scr.get("blocks") or []) if b.get("type") or b.get("pos"))

    out = []
    out.append("Use case: style-transfer")
    out.append("Asset type: 电商详情页第 %d 屏（单张完整画面，竖版）" % no)
    out.append(_strength_block(strength))
    out.append("")
    out.append("Input images:")
    out.extend(img_lines)
    out.append("")
    out.append("Scene/backdrop: %s" % (scr.get("bg") or "与参考图一致的干净背景"))
    out.append("Subject: %s%s，主体占比与摆放位置照参考图" % (
        prod.get("name") or "我的商品",
        ("（%s）" % prod.get("summary")) if prod.get("summary") else ""))
    out.append("Style/medium: 商业电商详情页平面设计，%s 平台风格" % platform)
    out.append("Composition/framing: %s" % (scr.get("layout") or "按参考图版式"))
    if blocks_txt:
        out.append("版式块（从上到下）: %s" % blocks_txt)
    if scr.get("elements"):
        out.append("Visual elements: %s" % scr["elements"])
    out.append("Lighting/mood: %s" % (dna.get("lighting") or "柔和均匀的商业棚拍光"))
    out.append("Color palette: %s（主色不超过 3 个，色值尽量贴近）"
               % "、".join(dna.get("palette") or []))
    if dna.get("font"):
        out.append("Typography: %s" % dna["font"])
    if dna.get("whitespace"):
        out.append("Spacing: %s" % dna["whitespace"])
    if dna.get("graphic"):
        out.append("Graphic language: %s" % dna["graphic"])
    if text_lines:
        out.append('Text (verbatim): %s。文字必须逐字照抄，不得改写、不得多加字、不得出现错别字。'
                   % "；".join(text_lines))
    elif lang == "无文字":
        out.append("Text (verbatim): 画面内不要出现任何文字。")
    out.append("Constraints: 必须保留 %s；商品外观与 Image 2 起的产品图完全一致；"
               "画面只做这一屏，**不要拼图、不要多格、不要长图**；画面饱满无空白条。" % "；".join(keep[:6]))
    out.append("Avoid: %s；未提供的价格/销量/认证/功效等任何信息；多余文字与装饰；变形与拉伸。"
               % "；".join(rmv[:10]))
    return "\n".join(out)


def build_prompts(ana, opt, n_ref, n_prod):
    """把所有屏的提示词一次性拼出来（步骤3）。已手改过的 prompt 不覆盖。"""
    out = []
    for scr in (ana.get("screens") or []):
        item = dict(scr)
        if not str(item.get("prompt") or "").strip():
            item["prompt"] = screen_prompt(item, ana, opt, n_ref, n_prod)
        out.append(item)
    return out
