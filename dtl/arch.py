# -*- coding: utf-8 -*-
"""电商图内容架构常量 —— 1:1 移植 dst.yemanai.cn 前端 bundle。

三套各 12 项，id / title / subtitle / defaultSelected 与原站完全一致。
原站变量名：hj=电商详情页、mj=电商主图、gj=A+图；sl(En) 选表、pj(En) 给标题。
"""

DETAIL = [
    {"id": 'detail_main_visual', "title": '首屏主视觉', "subtitle": '用强视觉和核心利益点建立第一购买印象', "defaultSelected": True},
    {"id": 'detail_selling_overview', "title": '卖点总览', "subtitle": '集中梳理产品核心卖点和差异化优势', "defaultSelected": True},
    {"id": 'detail_pain_solution', "title": '痛点与解决方案', "subtitle": '先指出用户困扰，再说明产品如何解决问题', "defaultSelected": False},
    {"id": 'detail_core_selling', "title": '核心卖点详解', "subtitle": '围绕单个关键卖点做深度视觉表达', "defaultSelected": False},
    {"id": 'detail_material_closeup', "title": '细节材质展示', "subtitle": '突出材质、结构、接口、纹理和设计细节', "defaultSelected": False},
    {"id": 'detail_usage_scene', "title": '使用场景展示', "subtitle": '让用户快速代入真实使用环境和生活方式', "defaultSelected": False},
    {"id": 'detail_effect_compare', "title": '效果对比', "subtitle": '通过前后、优劣或新旧对比强化价值感', "defaultSelected": False},
    {"id": 'detail_dimensions', "title": '尺寸规格', "subtitle": '展示尺寸、重量、容量、比例参照等关键信息', "defaultSelected": False},
    {"id": 'detail_usage_guide', "title": '使用指南', "subtitle": '说明安装、使用、清洁、保养等操作步骤', "defaultSelected": False},
    {"id": 'detail_sku_series', "title": '系列/SKU展示', "subtitle": '展示颜色、规格、套装或多款组合选择', "defaultSelected": False},
    {"id": 'detail_brand_endorsement', "title": '品牌背书', "subtitle": '通过认证、质检、专利、品牌实力建立信任', "defaultSelected": False},
    {"id": 'detail_after_sales', "title": '售后保障', "subtitle": '呈现售后服务、保障承诺、配送或无忧购买信息', "defaultSelected": False},
]

MAIN = [
    {"id": 'main_white_bg', "title": '产品白底主图', "subtitle": '标准平台主图，突出产品本体，干净合规', "defaultSelected": True},
    {"id": 'main_click_scene', "title": '点击率场景主图', "subtitle": '用场景、光影和构图提升点击欲望', "defaultSelected": True},
    {"id": 'main_core_selling', "title": '核心卖点图', "subtitle": '一张图讲清最重要的购买理由', "defaultSelected": False},
    {"id": 'main_function_benefit', "title": '功能利益图', "subtitle": '把产品功能转化为用户可感知的实际好处', "defaultSelected": False},
    {"id": 'main_usage_scene', "title": '使用场景图', "subtitle": '展示产品在真实生活或工作环境中的使用方式', "defaultSelected": False},
    {"id": 'main_detail_closeup', "title": '细节特写图', "subtitle": '放大材质、结构、接口、纹理、工艺等细节', "defaultSelected": False},
    {"id": 'main_dimensions', "title": '尺寸规格图', "subtitle": '展示尺寸、重量、容量和比例参照物', "defaultSelected": False},
    {"id": 'main_multi_angle', "title": '多角度展示图', "subtitle": '展示正面、侧面、背面、俯视等关键角度', "defaultSelected": False},
    {"id": 'main_package_list', "title": '包装清单图', "subtitle": '展示包装内包含的配件和套装内容', "defaultSelected": False},
    {"id": 'main_compare_advantage', "title": '对比优势图', "subtitle": '与普通产品、旧款或竞品形成直观差异', "defaultSelected": False},
    {"id": 'main_applicable_scene', "title": '适用人群/场景图', "subtitle": '说明产品适合哪些人群、场景或需求', "defaultSelected": False},
    {"id": 'main_trust_endorsement', "title": '信任背书图', "subtitle": '展示认证、安全、质检、售后或品牌实力', "defaultSelected": False},
]

APLUS = [
    {"id": 'aplus_brand_visual', "title": '品牌主视觉', "subtitle": '用高质感视觉建立品牌调性和产品第一印象', "defaultSelected": True},
    {"id": 'aplus_value_overview', "title": '产品价值总览', "subtitle": '清晰说明产品核心价值、适用场景和主要优势', "defaultSelected": True},
    {"id": 'aplus_core_features', "title": '核心功能模块', "subtitle": '拆解关键功能，让用户快速理解产品能力', "defaultSelected": False},
    {"id": 'aplus_lifestyle_scene', "title": '生活方式场景', "subtitle": '通过真实使用画面建立代入感和购买想象', "defaultSelected": False},
    {"id": 'aplus_detail_material', "title": '细节与材质', "subtitle": '展示材质、结构、工艺、纹理和品质细节', "defaultSelected": False},
    {"id": 'aplus_dimensions', "title": '尺寸规格', "subtitle": '说明尺寸、容量、重量和适配范围等信息', "defaultSelected": False},
    {"id": 'aplus_comparison_chart', "title": '对比表', "subtitle": '用清晰对比帮助用户理解不同款式或优势差异', "defaultSelected": False},
    {"id": 'aplus_usage_steps', "title": '使用步骤', "subtitle": '展示安装、使用、清洁或维护流程', "defaultSelected": False},
    {"id": 'aplus_package_list', "title": '包装清单', "subtitle": '说明包装内包含的产品、配件和赠品', "defaultSelected": False},
    {"id": 'aplus_fit_unfit', "title": '适用/不适用场景', "subtitle": '降低误购风险，明确适合与不适合的人群或场景', "defaultSelected": False},
    {"id": 'aplus_faq', "title": '常见问题', "subtitle": '提前解答用户在购买前最关心的问题', "defaultSelected": False},
    {"id": 'aplus_brand_trust', "title": '品牌故事/信任背书', "subtitle": '通过品牌故事、认证、质检或服务承诺建立信任', "defaultSelected": False},
]

# 电商图类型（原站 En 的三个 option，顺序一致）
TYPES = ["电商详情页", "A+图", "电商主图"]


def table(kind):
    """原站 sl(En)：按电商图类型取架构表。未知值回落「电商详情页」（与原站三元表达式一致）。"""
    if kind == "电商主图":
        return MAIN
    if kind == "A+图":
        return APLUS
    return DETAIL


def heading(kind):
    """原站 pj(En)：架构区标题。"""
    if kind == "电商主图":
        return "电商主图内容架构"
    if kind == "A+图":
        return "A+图内容架构"
    return "电商详情页内容架构"


def defaults(kind):
    """该类型的默认勾选 id（原站 defaultSelected:true 的两项）。"""
    return [a["id"] for a in table(kind) if a["defaultSelected"]]


def find(kind, arch_id, extra=()):
    """在「内置表 + AI 自创架构」里找一项（原站 [...sl(En),...vi].find）。"""
    for a in list(table(kind)) + list(extra or []):
        if a.get("id") == arch_id:
            return a
    return None


def public(kind):
    """给前端的架构清单（不含内部字段）。"""
    return [{"id": a["id"], "title": a["title"], "subtitle": a["subtitle"],
             "defaultSelected": a["defaultSelected"]} for a in table(kind)]
