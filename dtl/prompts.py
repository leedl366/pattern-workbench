# -*- coding: utf-8 -*-
"""详情页生成 · 提示词库 —— 1:1 移植 dst.yemanai.cn「image2详情页」。

来源：原站前端 bundle `index-DR2TcHFU.js`，用配对解析器提取模板字面量原文，
只把 JS 的 `${expr}` 占位符换成 Python 的 `{name}`，**文案一字未改**。

三段核心：
  P1    步骤2「分析产品」system prompt（含严格 JSON schema）
  HEAD  步骤3「生成提示词」system prompt 头部（整体设计规范 + 内容规划标题）
  PAGE  步骤3 每页模板（13 个字段，主标题≤13字 / 副标题≤18字）
  EXTRA image2 专属附加参数块（平台/地区/类型/产品名/人群/风格/四色/模特/已选架构）
  P3    步骤4 每页「执行型」生图 prompt（画布锁定 → 文字锁定 → 执行顺序 → …）

渲染用 `render()`：**只替换已知键**，不走 str.format —— 因为 P1 里有 JSON 示例的
裸大括号，format 会炸。这是移植时最容易踩的坑，已避开。
"""
import re

# 步骤2 分析产品 system prompt（原站 Mn 里的 K）
P1 = r'''你是一位顶级的电商视觉总监与产品策划分析师。
你的任务是根据用户提供的产品图（如果有）、产品核心卖点提示（如果有）以及用户的选择参数，分析并输出产品的相关信息，并为用户在电商视觉生成时挑选最佳的生成架构。

用户选择的参数如下：
- 平台选择：{platform}
- 画面语言：{lang}（注意：该语言仅用于控制后续生成的画面中可能出现的文字文案。本JSON结构中的 productName、coreSellingPoints、targetAudience、designStyle、title、subtitle 等所有字段的内容说明，请**务必全部使用中文**输出，品牌专有名词除外）
- 销售地区：{region}
- 尺寸比例：{ratio}
- 清晰度：{res}
- 电商图类型：{kind}
- 生成数量：{count}

可用的生成架构如下（请根据产品图、核心卖点、电商图类型和生成数量，从中选择一组恰当的架构（数量应尽可能与“生成数量”相匹配）。如果没有适合的产品图或所需数量的内建架构，请为多出的数量“生成自定义的架构内容模块”，包含主要内容名称和文字说明，使其保持完整连贯。）：
{arch_list}

请同时根据产品、平台、销售地区、目标人群、电商图类型和核心卖点，生成 3 组不同方向的“设计风格方案”。每组方案必须包含：设计风格名称、风格说明、主色、辅色、点缀色、背景色。三组方案要有明显差异，且都适合电商转化，不要只是颜色不同。

【产品保真铁律 —— 最高优先级，优先于上面所有分析要求】
1. productName、coreSellingPoints、designStyle 中涉及产品本身的描述，**只能写参考图里直接看得到的视觉特征**（外形轮廓、比例、颜色、表面观感、可见部件）。
2. **严禁用行业常识补全图上没有的东西**：不得推断品牌、型号、材质等级或牌号（如“316医用级”“食品级”“航空铝”）、内部结构（内胆/夹层/电路）、功能机构（弹盖/一键开合/按压阀/磁吸）、认证与参数（保温时长/容量/防水等级）。图上看不出来的，一律不写。
3. 产品是扁平插画/矢量图/线稿时，就按插画来描述（色块、描边、高光带），**不要把它说成有真实材质纹理的实物**；是实拍照片时，才描述材质与光影。
4. coreSellingPoints 只能围绕“看得见的外观优点 + 用户明确给出的卖点”展开；用户没给卖点时，宁可写得克制，也不要编造功能。
5. designStyleOptions 只决定**画面氛围**（背景、装饰、字体、色彩、光影），**不得改变产品本身的材质、结构与外观呈现**。

请严格输出一份合法的 JSON 数据，不要包含任何 ```json 等 Markdown 代码块标记。
输出 JSON 结构必须严格如下：
{
  "productName": "推测的或提炼后的产品名称",
  "coreSellingPoints": "优化并梳理出的核心卖点",
  "targetAudience": "推测的目标人群",
  "designStyle": "推荐的高端设计风格与视觉基调",
  "primaryColor": "推荐的主色HEX色值，必须为7位十六进制（如：#00008B）",
  "secondaryColor": "推荐的辅色HEX色值，必须为7位十六进制（如：#ADD8E6）",
  "accentColor": "推荐的点缀色HEX色值，必须为7位十六进制（如：#FFD700）",
  "bgColor": "推荐的背景色HEX色值，必须为7位十六进制（如：#FFFFFF）",
  "designStyleOptions": [
    {
      "id": "style_1",
      "name": "设计风格名称",
      "description": "设计风格说明，需要说明视觉基调、材质氛围、适合的转化表达方式",
      "primaryColor": "主色HEX色值，必须为7位十六进制（如：#00008B）",
      "secondaryColor": "辅色HEX色值，必须为7位十六进制（如：#ADD8E6）",
      "accentColor": "点缀色HEX色值，必须为7位十六进制（如：#FFD700）",
      "bgColor": "背景色HEX色值，必须为7位十六进制（如：#FFFFFF）"
    },
    {
      "id": "style_2",
      "name": "第二套设计风格名称",
      "description": "第二套设计风格说明",
      "primaryColor": "#000000",
      "secondaryColor": "#FFFFFF",
      "accentColor": "#FF0000",
      "bgColor": "#F7F7F7"
    },
    {
      "id": "style_3",
      "name": "第三套设计风格名称",
      "description": "第三套设计风格说明",
      "primaryColor": "#000000",
      "secondaryColor": "#FFFFFF",
      "accentColor": "#FF0000",
      "bgColor": "#F7F7F7"
    }
  ],
  "needsModel": true,
  "modelPrompt": "如果 needsModel 为 true，请根据产品图、核心卖点、目标人群和电商图类型，推荐适合的模特设定，必须包含性别、年龄段、国家/地区或人种气质，并说明适合出现在哪类内容架构画面中；如果 needsModel 为 false，则填写不建议出现模特的原因",
  "selectedArchitectures": [
    {
      "id": "（如果选用已有架构则填其ID，如 white_bg。如是自创的则提供一个下划线的ID，如 custom_scene_1）",
      "title": "（对应架构的主要内容名称）",
      "subtitle": "（对应架构的详细文字说明）"
    }
  ]
}'''

# 步骤3 规划 prompt 头部（原站 Dn 里的 Ae 前半）
HEAD = r'''你是一位顶级的电商视觉总监和AI绘画提示词工程师。你的任务是根据用户提供的产品图片、产品卖点以及配置参数，生成一套高端时尚的电商详情页设计提示词。
请注意：除了主标题和副标题的内容需要根据用户选择的“画面语言”来决定语言种类外，【所有其他提示词内容必须全部使用中文】。

配置参数：
- 目标模型：{model}{ratio_line}
- 目标画布比例：{ratio_or_free}（如果选择了具体比例，整套图片每一页都必须按该比例规划；上传图或参考图原始比例不一致时，只能通过重排构图、裁切或补背景适配目标画布，不得输出其它画布比例）
- 画面文字语言：{lang} （如果为“无文字”，则不生成主副标题；如果为中文/繁体中文/英文/日文/泰文/俄文/西班牙语，则主副标题使用对应语言）
- 清晰度：{res}
- 生成数量（详情页页数）：{count}页
- 产品卖点提示：{selling}{extra}

请严格按照以下结构输出最终的提示词：

# 整体设计规范
（请根据产品特性，设定一套统一的高端时尚设计规范）
- **产品保真**：整套画面的产品必须与参考图完全一致（外形、比例、颜色、可见部件、表面观感）；不得添加参考图里没有的结构、按钮、纹理、钢印或 Logo，不得把插画风产品改画成写实实物。
- **色彩规范**：明确主色调、辅助色与背景色。
- **字体规范**：区分标题字体与正文字体，建立清晰的字号层级。字号用**区间 + 相对关系**表达（如：主标题 40-48pt、副标题 22-26pt、正文 16-18pt，主标题约为副标题的 2 倍），不要给绝对像素坐标。必须明确具体字体（如：思源黑体/Helvetica等）。字体颜色需提供两种方案（如：深色背景用白色#FFFFFF，浅色背景用深灰#333333）。
- **视觉语言**：统一装饰元素与图标风格，保持合理留白以突出产品主体（用百分比表达，如留白不低于画面 30%）。装饰只作用于背景与留白区，不得压住产品。
- **摄影风格**：采用高质感电商表现方式（如自然光或柔光），搭配浅景深增强层次。**表现风格必须与参考图一致**：参考图是扁平插画/矢量图，就延续扁平插画的干净色块与描边，只提升排版与光影氛围；参考图是实拍照片，才使用真实材质与镜头语言。
- **风格边界**：上述设计风格只作用于背景、装饰、字体、色彩与光影，**不得改变产品本身的材质、结构与外观呈现**。
- **模特一致性**：如果画面需要出现模特，整套详情页必须保持同一位模特的性别、年龄段、国家/地区或人种气质、发型、肤色、体型和整体穿搭风格一致；不同页面只能调整动作、景别和互动方式。模特动作必须自然，手、脚、腿、手指、关节、肢体比例和面部结构必须符合正常人类生理结构，禁止畸形肢体、额外手指、缺失手指、扭曲关节、反向肢体和不合理姿势。
- **画面质量**：要求高清分辨率（{res}）与强真实感。{ratio_block}

# 详情页内容规划（共 {count} 页）
（请根据上传的产品图和产品卖点，为每一页单独设定内容结构。如果语言不是“无文字”，每页必须包含主标题和副标题。主标题严格控制在13个字符以内，副标题严格控制在18个字符以内。注意：直接输出标题内容，绝对不要在标题后面加上字数说明。）

'''

# 步骤3 每页模板（原站 image2 分支 return 的模板）
PAGE = r'''## 第 {idx} 页：[本页核心主题 - 参考架构：{arch_title} ({arch_sub})]
- **展示方式与视角**：[根据产品复杂程度选择，如正面、45°或特写；视角变化不得改变产品本身的外形与部件]
- **设计目标与核心表达**：[明确本页要传达的信息]
- **产品呈现与占比**：[用百分比设定产品在画面中的位置和大小占比；产品外观必须与参考图一致，不得添加参考图没有的结构、纹理或标识]
- **本页图片尺寸比例**：[必须明确写出本页图片尺寸比例：{ratio_or_free}，并要求构图严格按该比例设计]
- **构图与排版**：[如居中、对称或分布式布局，图文排版方式；用相对关系表达（居中/左对齐/占比/层级），不要给绝对像素坐标]
- **必须显示的画面文字**：
  - 主标题必须显示为：“[根据语言选项生成，13字以内，不带字数说明。若无文字则写“无”]”
  - 副标题必须显示为：“[根据语言选项生成，18字以内，不带字数说明。若无文字则写“无”]”
- **文字位置与排版**：[明确文字放在画面哪个安全区，如左上角/右侧留白区；用相对关系说明层级与对齐（主标题约为副标题 2 倍大、文字区留白不低于画面 20%），给字体与颜色，**文字直接排在背景上，不要加色块底板、渐变底或圆角贴纸**；不要写距边多少 px]
- **视觉重点与卖点**：[突出对应的卖点，只能写参考图可见的外观优点或用户明确给出的卖点]
- **背景与装饰元素**：[搭配适合的背景元素与装饰元素增强画面表现；装饰只放在留白区，不得压住产品；用风格描述而非像素尺寸]
- **氛围、情绪与光影**：[统一整体氛围、情绪关键词及光影效果；光影风格须与参考图一致，插画风产品不要强行加写实材质]
- **模特要求**：[如果本页需要模特，描述同一位模特的出镜方式、动作和与产品互动方式，并强调人体结构正常；如果不需要模特，写“本页不出现模特或人物肢体”]
- **禁止事项**：[列出本页必须避免的问题，如文字乱码、遗漏标题、产品变形、产品被替换成想象的样子、模特肢体畸形、额外手指、背景喧宾夺主等]
- **AI生图Prompt(中文)**：[只写可直接用于AI生图的一段执行型Prompt，必须包含本页图片尺寸比例（{ratio_or_free}）、**产品外观严格照参考图（不得改变外形/颜色/部件/表面观感，不得添加参考图没有的结构与标识）**、构图、背景、光影、必须显示的主标题和副标题、文字位置（相对关系、不加底板）、模特一致性/人体结构要求和禁止事项；不要写分析过程，不要写绝对像素坐标]'''

# image2 专属附加参数块（原站 Dn 里的 at）
EXTRA = r'''
- 平台：{platform}
- 销售地区：{region}
- 电商图类型：{kind}
- 产品名称：{product}
- 目标人群：{audience}
- 设计风格：{style}（仅作用于背景、装饰、字体、色彩与光影，不得改变产品本身的材质、结构与外观呈现）
- 主色：{c1}
- 辅色：{c2}
- 点缀色：{c3}
- 背景色：{c4}
- 是否需要模特：{model_need}
- 已选内容架构：{arch_titles}'''

# 步骤4 每页执行 prompt（2026-11-18 对齐能影3C /p/dg/ 的实测结构）
# 🔴 关键差异修复：原版执行顺序里没有「产品本体锁」，模型会按文字重绘产品；
#    且原版只把「AI生图Prompt」一段丢给模型，整体设计规范与其余字段全部丢失。
P3 = r'''【最高优先级：画布比例】
{canvas}

【最高优先级：必须显示的画面文字】
{text_lock}

【执行顺序】
1. 先严格锁定画布比例与画布尺寸一致性。
2. 再锁定产品本体：画面中的产品必须百分百来自参考图，不得改变外观、结构、颜色、材质、Logo、数量、比例与关键细节，不得用想象的产品替换；参考图未体现的结构（按钮、弹盖、掀盖、钢印、纹理、标识）一律不得添加；参考图是扁平插画/矢量图时，保持其扁平插画观感，不得改画成写实实物。
3. 再执行整体设计规范与本页画面要求。
4. 最后执行文字、风格、模特与禁止事项。

【整体设计规范】
{global_spec}

【本页画面要求】
{page_brief}

【模特一致性与人体结构】
{model_block}

【禁止事项】
{ban_block}

【清晰度】
{res} 高清，商业级质感，细节锐利。'''


# ---------------- 渲染 ----------------
def render(tpl, **kw):
    """只替换 {已知键}，其余大括号原样保留（P1 的 JSON 示例里有裸 {}）。

    未提供的键**保持原样**而不是报错 —— 与原站 JS 模板「undefined 变字符串」的
    宽容度对齐，但这里保留占位符更容易在自检时发现漏参。
    """
    def rep(m):
        k = m.group(1)
        if k in kw:
            v = kw[k]
            return "" if v is None else str(v)
        return m.group(0)
    return re.sub(r"\{([a-z_][a-z0-9_]*)\}", rep, tpl)


# ---------------- 画布锁定文案（2026-11-18 对齐能影3C） ----------------
def canvas_lock(ratio, obj="图片", size=""):
    """原站 Bl(h,E)：ratio 为空 → 「不限制画布比例。」

    🔴 措辞修复：原版「只能通过重排构图、裁切或补背景适配」等于给模型发了
    「可以动参考图」的许可，与产品保真冲突。现改为「只调整背景与构图留白，
    产品本体不得重排、裁切或重绘」。size 为请求像素（如 1152x1536），写成「约 W×H」。
    """
    r = str(ratio or "").strip()
    if not r or r == "不选择":
        return "不限制画布比例。"
    px = ""
    s = str(size or "").strip().lower().replace("×", "x")
    if "x" in s:
        try:
            a, b = s.split("x")
            px = "（约 %d×%d）" % (int(a), int(b))
        except Exception:                         # noqa: BLE001
            px = ""
    return ("必须严格生成 %s 画布%s，%s必须输出所选尺寸比例对应的画布。"
            "不得输出其它比例，不允许结果更高、更矮、更宽或更窄；"
            "构图、主体占比、文字安全区都必须适配 %s。"
            "产品图原始比例与 %s 不一致时，只能调整背景与构图留白来适配目标画布，"
            "产品本体不得重排、裁切或重绘，不得改变最终画布比例。") % (r, px, obj, r, r)


def canvas_block(ratio, obj="图片", size=""):
    """原站 ar(h,E)：画布比例 + 复核，两段同文案。"""
    t = canvas_lock(ratio, obj, size)
    return "【最高优先级：画布比例】\n%s\n\n【画布比例复核】\n%s" % (t, t)


def banana_prefix(ratio, model):
    """原站 et()：仅 banana 系模型且选了比例时，额外加一段硬约束前缀。"""
    if "banana" not in str(model or "").lower():
        return ""
    r = str(ratio or "").strip()
    if not r or r == "不选择":
        return ""
    return "\n".join([
        "CANVAS_ASPECT_RATIO=%s" % r,
        "最高优先级：最终图片必须严格为 %s 画布比例。" % r,
        "不要输出其他比例，不要因为参考图比例、产品图比例或版式参考改变最终画布比例。",
        "如参考图比例不一致，只重排构图和留白来适配目标比例，最终画布仍必须保持上述比例。",
    ])


# ---------------- 拆页 / 抽字段 ----------------
# 🔴 原站正则：r"\s*#*\s*第\s*\d+\s*页[：:]?\s*" —— 不锚行首，于是**正文里的交叉引用**
#    （AI 很爱写「模特与第2页保持一致」）也被当成页标题，3 页被拆成 7 页、多扣 4 张积分。
#    2026-11-18 首次线上真跑就复现了（保温杯案例：3 页 → 7 页）。
#    修法：**只认行首的页标题**（允许 ###/##/#/无标记，允许行首少量空白），
#    行中间出现的「第N页」一律视为正文引用，不参与拆页。
RE_SPLIT = re.compile(r"(?m)^[ \t]*#*[ \t]*第[ \t]*(\d+)[ \t]*页[ \t]*[：:]?[ \t]*")
RE_PAGE_NO = re.compile(r"(?m)^[ \t]*#*[ \t]*第[ \t]*\d+[ \t]*页")
RE_GLOBAL_TITLE = re.compile(r"#+\s*整体设计规范")
RE_RATIO_TOKEN = re.compile(r"\d+\s*:\s*\d+")


def split_pages(prompt):
    """按行首页标题拆页 → (全局规范, [每页正文, ...])。

    与原站语义一致（[0] 去掉「# 整体设计规范」当全局规范），但**不会被正文引用误拆**。
    页序号不连续时按出现顺序排（AI 偶尔跳号，不该因此丢页）。
    """
    txt = prompt or ""
    ms = list(RE_SPLIT.finditer(txt))
    if not ms:
        return RE_GLOBAL_TITLE.sub("", txt).strip(), []
    g = RE_GLOBAL_TITLE.sub("", txt[:ms[0].start()]).strip()
    pages = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(txt)
        pages.append(txt[m.end():end].strip())
    return g, pages


def page_count(prompt):
    """出图张数 = 行首页标题数（与 split_pages 严格同源，避免「预估 3 张实际 7 张」）。"""
    return len(RE_PAGE_NO.findall(prompt or ""))


def field(text, label):
    """原站 Jt(h,E)：抽 `- **标签**：值`（到下一个 - ** 或 ## 或结尾）。"""
    esc = re.escape(label)
    m = re.search(r"- \*\*" + esc + r"\*\*[：:]\s*([\s\S]*?)(?=\n- \*\*|\n## |$)",
                  text or "")
    return (m.group(1).strip() if m else "")


def title_of(text, label):
    """原站 Gr(h,E)：抽「主标题必须显示为：“xxx”」。

    🔴 2026-11-18：AI 常把标题写成「**深蓝简约保温杯**」，星号会跟着进
    画面文字指令与结果卡标题。这里剥掉首尾的 Markdown 装饰（* _ # ` 与空白）。
    前端 pTitle 必须同源处理。
    """
    esc = re.escape(label)
    m = re.search(esc + r"必须显示为[：:]\s*[“\"]([^”\"]+)[”\"]", text or "")
    if not m:
        return ""
    return re.sub(r"^[\s*_#`~]+|[\s*_#`~]+$", "", m.group(1)).strip()


# ================= 步骤2：分析产品 =================
# user 指令（原站 he），两个条件块按原站顺序追加
U_ANALYZE = "请基于上传的产品图及相关要求，直接输出JSON数据。如果在输入中包含了核心卖点，请结合图像对卖点进行优化和扩展。"


def analyze_messages(opt, arch_list):
    """返回 (system, user)。opt 见 app.py 的 _norm_opt。"""
    system = render(P1, platform=opt["platform"], lang=opt["lang"], region=opt["region"],
                    ratio=opt["ratio"], res=opt["res"], kind=opt["kind"],
                    count=opt["count"], arch_list=arch_list)
    user = U_ANALYZE
    if opt.get("selling"):
        user += "\n\n用户提供的核心卖点：" + opt["selling"]
    if opt.get("product"):
        user += "\n用户提供的产品名称：" + opt["product"]
    return system, user


def arch_list_text(kind):
    """原站 B：`- ${title} (ID: ${id}): ${subtitle}` 逐行。"""
    from . import arch as A
    return "\n".join("- %s (ID: %s): %s" % (a["title"], a["id"], a["subtitle"])
                     for a in A.table(kind))


# ================= 步骤3：生成详情页提示词 =================
# user 指令（原站 Oe 的 image2 分支）
U_PLAN = ("请根据上传的产品图片和提供的产品参数，生成电商图的生图提示词。"
          "如果未上传图片（或图片为空），请【完全依据】配置参数中的产品名称和核心卖点来自由发挥创意设计。"
          "注意：你目前处理的产品是：{product}。核心卖点是：{selling}\n"
          "【产品保真】有上传图时，每页描述的产品外观必须与图片完全一致：只写图上看得到的外形、比例、"
          "颜色与可见部件，不得添加图上没有的结构、按钮、纹理、钢印或 Logo，不得把插画风产品改写成写实实物；"
          "排版一律用相对关系（占比/层级/对齐/留白百分比），不要写绝对像素坐标，文字不要加色块底板。")

# 「是否需要模特」两种文案（原站 at 里的三元，原文照搬）
MODEL_YES_TAIL = ("再结合每个已选内容架构判断该页画面是否需要出现模特，"
                  "需要则把模特自然融入构图，不需要则保持产品或场景为主。")
MODEL_YES_AUTO = ("请先根据产品图、核心卖点、目标人群推荐最适合的模特设定，"
                  "必须明确模特性别、年龄段、国家/地区或人种气质。")
MODEL_NO = "否，所有画面都不要安排模特，避免出现人物主体或肢体。"


def plan_messages(opt, arch_titles, model_name):
    """返回 (system, user)。arch_titles = 已选架构标题串（顿号连接）。"""
    ratio = opt["ratio"]
    free = ratio if ratio and ratio != "不选择" else "不限制"
    # 原站 be：选了比例才有「 - 尺寸比例：X」这一行（注意开头有个空格）
    ratio_line = (" - 尺寸比例：%s" % ratio) if ratio and ratio != "不选择" else ""
    ratio_block = ("\n- **尺寸比例**：%s" % ratio) if ratio_line else ""

    mp = (opt.get("model_prompt") or "").strip()
    if opt.get("needs_model"):
        head = ("是。优先采用用户输入的模特描述：%s。" % mp) if mp else ("是。" + MODEL_YES_AUTO)
        model_need = head + MODEL_YES_TAIL
    else:
        model_need = MODEL_NO

    extra = render(EXTRA, platform=opt["platform"], region=opt["region"], kind=opt["kind"],
                   product=opt.get("product") or "未指定",
                   audience=opt.get("audience") or "未指定",
                   style=opt.get("style") or "未指定",
                   c1=opt.get("c1") or "未指定", c2=opt.get("c2") or "未指定",
                   c3=opt.get("c3") or "未指定", c4=opt.get("c4") or "未指定",
                   model_need=model_need, arch_titles=arch_titles)

    n = max(1, int(opt["count"]))
    pages = []
    ids = opt.get("arch_ids") or []
    from . import arch as A
    for i in range(n):
        aid = ids[i % max(1, len(ids))] if ids else ""
        a = A.find(opt["kind"], aid, opt.get("arch_extra")) if aid else None
        pages.append(render(PAGE, idx=i + 1,
                            arch_title=(a["title"] if a else "未指定"),
                            arch_sub=(a["subtitle"] if a else "未指定"),
                            ratio_or_free=free))

    system = render(HEAD, model=model_name, ratio_line=ratio_line, ratio_or_free=free,
                    lang=opt["lang"], res=opt["res"], count=n,
                    selling=opt.get("selling") or "无特定卖点，请根据图片自行提取核心亮点",
                    extra=extra, ratio_block=ratio_block)
    system += "\n\n".join(pages)
    user = render(U_PLAN, product=opt.get("product") or "未指定",
                  selling=opt.get("selling") or "无")
    return system, user


# ================= 步骤4：每页执行 prompt =================
NO_TEXT = "画面不要出现任何文字、字母、数字、符号、Logo文字或水印。"
TEXT_LOCK_HEAD = ("文字是最高优先级，必须先执行文字要求，再执行构图和风格。"
                  "图片中只能使用当前页文字，不得沿用上一页或其他页面的标题。")
MODEL_PAGE_DEFAULT = ("如本页需要出现模特，动作自然，手、脚、腿、手指、关节、"
                      "肢体比例和面部结构必须符合正常人类生理结构。")
BAN_BASE = ("禁止文字乱码、遗漏主标题、遗漏副标题、错别字、伪文字、产品结构变形、"
            "产品材质失真、额外手指、缺失手指、扭曲关节、反向肢体、肢体比例异常、"
            "面部畸形、背景喧宾夺主。")


def page_exec_prompt(page_text, opt, model_name, global_spec="", size=""):
    """从每页正文抽字段 → 组装执行型 prompt（2026-11-18 对齐能影3C /p/dg/）。

    page_text = split_pages 得到的单页正文（不含「## 第 N 页」标题行）。
    global_spec = split_pages 得到的整体设计规范（原版丢失，现已补传）。
    size = 请求像素串（如 "1152x1536"），写进画布锁做二次约束。

    🔴 与原版三处关键差异：
      ① 执行顺序第 2 条 = 产品本体锁（原版没有 → 产品被重绘）
      ② 整体设计规范 + 本页 8 个字段全部下发（原版只发 AI生图Prompt 一段）
      ③ 画布锁措辞不再允许「重排/裁切参考图」
    """
    ratio = opt["ratio"]
    page_prompt = field(page_text, "AI生图Prompt(中文)") or page_text
    page_ratio = field(page_text, "本页图片尺寸比例")
    texts = field(page_text, "必须显示的画面文字")
    main_t = title_of(texts or page_text, "主标题")
    sub_t = title_of(texts or page_text, "副标题")
    layout = field(page_text, "文字位置与排版")
    model_req = field(page_text, "模特要求")
    ban_extra = field(page_text, "禁止事项")

    canvas = canvas_lock(ratio, "所有详情页图片", size)
    # 本页字段里若写了比例 token，以它为准（只取真正的比例，不吞整句话）
    if page_ratio:
        m = RE_RATIO_TOKEN.search(page_ratio)
        if m:
            canvas = canvas_lock(m.group(0), "所有详情页图片", size)

    if opt["lang"] == "无文字":
        text_lock = NO_TEXT
    else:
        text_lock = (TEXT_LOCK_HEAD + "\n"
                     + "主标题必须精确显示为：“%s”\n" % (main_t or "按本页提示词中的主标题执行")
                     + "副标题必须精确显示为：“%s”\n" % (sub_t or "按本页提示词中的副标题执行")
                     + "不得省略、不得改写、不得替换近义词、不得自己生成新标题、不得乱码，"
                       "文字必须清晰可读并严格使用%s。文字直接排在背景上，"
                       "不要加色块底板、渐变底或圆角贴纸。\n" % opt["lang"]
                     + (("文字位置与排版：%s" % layout) if layout
                        else "文字必须放在清晰留白安全区，并保证与背景有足够对比度。"))

    mp = (opt.get("model_prompt") or "").strip()
    if opt.get("needs_model"):
        model_block = ("整套详情页必须保持同一位模特形象一致。"
                       + (("模特设定优先遵循：%s。" % mp) if mp else "")
                       + (model_req or MODEL_PAGE_DEFAULT))
    else:
        model_block = "本页不要出现模特、人物主体或人物肢体。"

    # 本页画面要求：把对生图有用的字段逐条下发（对齐能影的「本页画面要求」段）
    brief = []
    for label, key in (("本页目标", "设计目标与核心表达"),
                       ("视角与展示方式", "展示方式与视角"),
                       ("产品呈现与占比", "产品呈现与占比"),
                       ("构图与排版", "构图与排版"),
                       ("文字位置", "文字位置与排版"),
                       ("背景与装饰", "背景与装饰元素"),
                       ("氛围与光影", "氛围、情绪与光影"),
                       ("模特", "模特要求")):
        v = field(page_text, key)
        if v:
            brief.append("- %s：%s" % (label, v))
    brief.append("- 本页生图要点：%s" % page_prompt)
    page_brief = "\n".join(brief)

    g = (global_spec or "").strip()
    if len(g) > 2200:
        g = g[:2200].rstrip() + "…"
    if not g:
        g = ("产品外观严格照参考图；色彩、字体与装饰保持整套统一；"
             "文字清晰可读，留白充足，背景不喧宾夺主。")

    ban_block = BAN_BASE + (("\n" + ban_extra) if ban_extra else "")

    body = render(P3, canvas=canvas, text_lock=text_lock, global_spec=g,
                  page_brief=page_brief, model_block=model_block,
                  ban_block=ban_block, res=opt["res"])
    # 原站：banana 系模型额外前缀（et()），拼在最前面，空一行
    pre = banana_prefix(ratio, model_name)
    return (pre + "\n\n" + body) if pre else body
