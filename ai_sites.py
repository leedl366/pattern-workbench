#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全站生图站点池 —— 同模型跨站点轮询 + 统一错误脱敏（唯一真源）。

雪碧 2026-10-15 需求：
  ① 把 lensapi.cn / grsaiapi.com 两个站点及其模型列表纳入「后台设置」，全站共享；
  ② 轮询规则 = 「原先选用的站点某模型用不了 → 轮用其它站点的**同一个模型**」
     （不是现在的「同站点换模型」）；
  ③ 所有生图版块报错**只露错误码**，不出现模型名 / 站点名 / 欠费等字眼。

本模块由 pt(pipeline) / hd / wm 三个板块统一 import，是站点与模型顺序的唯一真源。

对外接口
--------
load_conf()                -> dict    完整配置（含真实 key）
load_sites()               -> list    启用中的站点（含真实 key）
public_conf()              -> dict    打码后的配置（给 /api/ai_sites 与前端）
save_conf(conf)            -> None    写盘（掩码/空 key 自动保留旧值）
plan_order(models=None)    -> list    [(site, base, key, model), ...] 轮询序列（已过滤熔断中的组合）
remember(site, model)      -> None    记住成功的组合，下次排最前（并解除该组合熔断）
note_fail(site, model)     -> bool    记一次失败；连续 N 次 → 该组合冷却一段时间
breaker_state()            -> dict    诊断：当前冷却中的组合 / 站点
protocol_map()             -> dict    {站点名: 'openai'|'apib'} 协议表（调用方按协议分支）
apib_gen(...)              -> str     apib 私有异步出图（提交+轮询），返回图片 URL/data URI
fetch_bytes(url)           -> bytes   把 URL / data URI 落成字节（hd、wm 用）
error_code(exc)            -> str     '477' / '401' / '408' / '500'
sanitize(exc, prefix)      -> str     '生图失败（错误码 477），请稍后重试。'

站点（2026-10-15 起）：apib.ai（异步·排首位） + lensapi.cn + grsaiapi.com（标准同步）。
各板块调用姿势：`plan = plan_order(链)` → `protos = protocol_map()` →
`for site, base, key, model in plan:` 内按 `protos.get(site)` 分流。
"""
import base64
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CONF_FILE = os.path.join(BASE, "ai_sites.json")

# 站点协议（2026-10-15 雪碧要求把 apib.ai 纳入全站）：
#   "openai" —— OpenAI 兼容**标准同步**：带图 POST {base}/v1/images/edits(multipart)、
#              纯文生图 POST {base}/v1/images/generations(JSON)，响应即成品 data[0].url|b64_json。
#              （lensapi / grsai 两家走这条）
#   "apib"   —— apib.ai(APIMart) **私有异步**：POST {base}/images/generations(JSON) → task_id
#              → 轮询 GET {base}/tasks/<id> → data.result.images[0].url。
#              注意：它的 /v1/images/edits 只支持 grok 系（gpt-image 系走 eds 会 400），
#              参考图必须放进 generations 的 `image:[data_uri,...]` 数组 —— 见 apib_gen()。
APIB_BASE = "https://api.apib.ai/v1"

# ---- 默认站点池（首次运行/配置缺失时使用；雪碧 2026-10-15 提供）----
DEFAULT_SITES = [
    # ---- apib.ai（APIMart）：私有异步协议；**排第一位 = 质量优先**（雪碧 2026-10-15 拍板）----
    # 2026-10-16 11:14 复测修正（雪碧拍板「方案 A」）：
    #   ① 原链首 `gpt-image-2-official` 在这把 key 下 **403 无权限**（提交即拒、0.2s 秒回），
    #      每轮冷启动白跑一跳 —— **该 key 实际可用的只有下面 5 个模型**（`/v1/models` 实测口径），
    #      故 models 收敛为这 5 个，official / gpt-image-1.5 / flux-2-pro / grok-imagine-image
    #      （403）与 gpt-image-1（502）一律移出。
    #   ② 链首改用**有权限**的 `gpt-image-2` ⇒ apib 内层排第一 ⇒ 第 1 跳即 `apib / gpt-image-2`，
    #      「apib 质量优先」才真正落地（原方案因模型权限问题实际未生效）。
    #   ③ 站内 gemini 系（pro / flash-image / flash-lite / nano-banana-2.1）作**站内兜底**，
    #      尺寸要吃「比例串」而非像素 —— 由 apib_size() 统一归一化。
    # 密钥与 app/.apib_key 同一把；此处显式写入便于后台设置页展示与就地改。
    {
        "name": "apib",
        "label": "apib.ai（gpt-image-2 · 异步）",
        "base": "https://api.apib.ai/v1",
        "key": "REPLACE_WITH_YOUR_API_KEY",
        "protocol": "apib",     # 未标注 protocol 的站点一律按 "openai" 标准同步处理
        "models": ["gpt-image-2",
                   "gemini-3-pro-image-preview", "gemini-3.1-flash-image-preview",
                   "gemini-3.1-flash-lite-image", "gemini-nano-banana-2.1"],
        "enabled": True,
    },
    {
        "name": "lensapi",
        "label": "lensapi.cn",
        "base": "https://lensapi.cn",
        "key": "REPLACE_WITH_YOUR_API_KEY",
        "models": ["gpt-image-2", "gpt-image-2.5", "gpt-image-2.5-flare",
                   "gpt-image-2.5-sunburst", "gemini-nano-banana-2.1-vip",
                   "gemini-3-pro-image-preview", "gemini-3.1-flash-image-preview"],
        "enabled": True,
    },
    {
        "name": "grsai",
        "label": "grsaiapi.com",
        "base": "https://grsaiapi.com",
        "key": "REPLACE_WITH_YOUR_API_KEY",
        "models": ["gpt-image-2", "gpt-image-2.5", "nano-banana-2.1", "nano-banana-2"],
        # 2026-11-19 实测 `400 insufficient credits`（1.3~2.2s 秒拒）→ 默认禁用，
        # 免得配置文件丢失时把它排进链里白跑。充值后改 True 即可。
        "enabled": False,
    },
    # 🔴 2026-11-19 补入 DEFAULT_SITES：huahuaapi 此前**只存在于后台 ai_sites.json**，
    #    代码默认表里没有它。后果：配置文件一旦丢失/损坏 → load_conf 回落默认表 →
    #    multi 线的 site_order=['huahuaapi'] 匹配不到任何站 → plan_order 静默回落全站
    #    → 把欠费的 apib/grsai 又排进来。自检 _lane3_check.py 实测复现了这个坑。
    #    实测能力：单图 5/5 全通（30.0~45.9s）、多图 2 张 2/2 成功（39.1s / 127.9s）。
    {
        "name": "huahuaapi",
        "label": "花花API",
        "base": "https://api.huahuaapi.com",
        "key": "REPLACE_WITH_YOUR_API_KEY",
        "protocol": "openai",
        "models": ["gpt-image-2", "gpt-image-2.5", "gpt-image-2.5-flare",
                   "gpt-image-2.5-sunburst", "gpt-image-2.6", "gpt-image-2.6-sunburst"],
        "enabled": True,
    },
]

# 模型优先顺序（外层）：**同一个模型先把所有站点试完**，全挂了才换下一个模型。
# 2026-10-15 扩兜底：gpt-image 双型号上游长期不可用（lensapi 477 / grsai 400），
# 且唯一的 gemini 兜底是**间歇性**可用（实测同一模型 503↔200 交替、耗时 16.8~80.5s）
# → 单一兜底＝整站生图随时全挂。现把 lensapi 上**实测可用**的另两个 gemini 也排进链尾：
#   gemini-nano-banana-2.1-vip（池内已有，此前不在任何链）/ gemini-3.1-flash-image-preview（最快 9.7s）。
# 三条 gemini 互相兜底 + 熔断，才是「生图几乎不失败」。
# 2026-10-15 晚：**apib.ai 入池并置顶**（雪碧拍板「质量优先」）。
# 2026-10-16 11:14 修正（方案 A，见上方 DEFAULT_SITES 注释）：链首 `gpt-image-2-official`
# 在该 key 下 403 无权限 ⇒ 每轮白跑、apib 实际从未被命中。现改为：
#   首跳 = apib / gpt-image-2（**该 key 有权限**，实测 17.8s）⇒ 真·apib 质量优先；
#   挂了才按「同模型跨站点」落到 lensapi / grsai 的 gpt-image-2 与 gpt-image-2.5；
#   再挂才走 gemini 兜底 —— 前三条是 lensapi 的，后两条（nano-banana-2.1 / flash-lite）
#   是 **apib 站内独有**（别站无此模型名）⇒ 全池 gpt-image 全挂时兜住。
# 四条链（本文件 / pipeline.API_MODEL_CHAIN / hd / wm）保持同源，改一处必须四处齐改。
DEFAULT_MODEL_ORDER = ["gpt-image-2", "gpt-image-2.5",
                       "gemini-3-pro-image-preview", "gemini-nano-banana-2.1-vip",
                       "gemini-3.1-flash-image-preview",
                       "gemini-nano-banana-2.1", "gemini-3.1-flash-lite-image"]

# ---------------- 分线规则（2026-11-15） ----------------
# 「按参考图张数选线」的**默认值**。真正的生效值存在 ai_sites.json 的 `routing` 段，
# 后台「系统设置 → ② 分线规则」可直接改，改完即时生效、无需重启（雪碧要求：不要定死）。
# 只有配置里没填时才回落到这里；这里的值又可以被环境变量覆盖，方便临时应急。
DEFAULT_ROUTING = {
    # 多图参考（≥2 张）：2026-11-19 实测 huahuaapi 2/2 成功；lensapi 477 上游失败必须排除；
    # apib 欠费已摘（充值后加回首位即可）。
    "multi": {
        "sites": [s.strip() for s in os.environ.get(
            "PATTERN_MULTI_SITES", "huahuaapi").split(",") if s.strip()],
        "models": [m.strip() for m in os.environ.get(
            "PATTERN_MULTI_MODELS",
            "gpt-image-2,gpt-image-2.5,gpt-image-2.6,"
            "gpt-image-2.6-sunburst").split(",") if m.strip()],
        "timeout": int(os.environ.get("PATTERN_MULTI_TIMEOUT", "600")),
    },
    # 单图参考（1 张）= 图生图主力线。2026-11-19 逐站实测 lensapi 5/5 + huahuaapi 5/5 全通，
    # 按实测耗时排序（快的在前）；apib/grsai 欠费已摘。
    "i2i": {
        "sites": [s.strip() for s in os.environ.get(
            "PATTERN_I2I_SITES", "lensapi,huahuaapi").split(",") if s.strip()],
        "models": [m.strip() for m in os.environ.get(
            "PATTERN_I2I_MODELS",
            "gpt-image-2.5-sunburst,gpt-image-2.5,gpt-image-2,"
            "gpt-image-2.6,gpt-image-2.6-sunburst,gpt-image-2.5-flare").split(",") if m.strip()],
        "timeout": int(os.environ.get("PATTERN_I2I_TIMEOUT", "420")),
    },
    # 纯文生图（0 张参考图）：**保持历史行为不变**（雪碧 2026-11-19 明确「文生图不用改」）。
    # models 留空 = 沿用 model_order；sites 保留原三站顺序。
    "t2i": {
        "sites": [s.strip() for s in os.environ.get(
            "PATTERN_T2I_SITES", "lensapi,huahuaapi,apib").split(",") if s.strip()],
        "models": [],
        "timeout": int(os.environ.get("PATTERN_T2I_TIMEOUT", "420")),
    },
    # single = 老键名，**保留兼容**：老配置/老前端只认 single。
    # 语义上等于「1 张或 0 张」的旧合并线；新代码优先取 i2i / t2i，取不到才回落 single。
    "single": {
        "sites": [s.strip() for s in os.environ.get(
            "PATTERN_SINGLE_SITES", "lensapi,huahuaapi,apib").split(",") if s.strip()],
        "models": [],          # 空 = 沿用「模型优先级」(model_order)，兼容老习惯
        "timeout": int(os.environ.get("PATTERN_SINGLE_TIMEOUT", "420")),
    },
}

_lock = threading.RLock()
_LAST_OK = []          # [(site_name, model), ...] 最近成功的组合（全局，兼容老行为）
# 🔴 2026-11-19：**按线分别记忆**。原来只有一个全局 _LAST_OK，
#    导致 t2i 刚成功的组合会被插到 i2i 队首 —— 而 i2i 的模型链是按实测耗时精心排序的，
#    被跨线记忆一顶就失效（实测：i2i 首跳变成 gpt-image-2 而非最快的 2.5-sunburst，
#    还因此撞上一次 lensapi 477）。分线记忆后各线互不干扰。
_LAST_OK_BY_LANE = {}  # {lane_name: (site_name, model)}

# ---------------- 熔断（方案 B，2026-10-15 雪碧拍板） ----------------
# 现象：上游某站/某模型的故障会**持续数分钟**（中继池过载 477、模型「修复中」400…），
# 期间每次任务都要把这几跳白等一遍（hd 原本还要等 420s 超时）。此处在 plan_order 层熔断：
#   · 组合级：(站点, 模型) 连续失败 BREAK_N 次 → 该组合冷却 BREAK_SEC 秒内不再排入；
#   · 站点级：当**该站在本次请求里的全部组合**都已冷却 → 整站视为跳过（逻辑上＝各组合
#     分别熔断的自然结果；**刻意不做「几个组合挂就封整站」**，否则 lensapi 的 gpt-image
#     挂了会把同站可用的 gemini 一起误伤，逼安全阀放行全部死组合，熔断等于白做）；
#   · 安全阀：过滤后若一条都不剩 → **忽略冷却照常全排**（上游可能已恢复，绝不把自己卡死）；
#   · 任何一次成功（remember）立即清除该组合的失败计数与冷却。
# 两个阈值可用 env 覆盖；键空间仅 站点×模型（十位数级别），无需淘汰。
BREAK_N = max(1, int(os.environ.get("AI_SITE_BREAK_N", "2")))
BREAK_SEC = float(os.environ.get("AI_SITE_BREAK_SEC", "300"))
_pair_fail = {}        # (site_name, model) -> {"n": 连续失败数, "until": 冷却截止时间戳}


def _clean_site(s):
    s = s or {}
    models = [str(m).strip() for m in (s.get("models") or []) if str(m).strip()]
    # 协议白名单：openai（标准同步，默认）/ apib（私有异步）。未知值一律回落 openai，
    # 保证「配置写错也走保守路径」，不会让生图因一个字段拼错而全挂。
    _proto = str(s.get("protocol") or "openai").strip().lower()
    if _proto not in ("openai", "apib"):
        _proto = "openai"
    return {
        "name": str(s.get("name") or "").strip() or "site",
        "label": str(s.get("label") or s.get("name") or "").strip(),
        "base": str(s.get("base") or "").strip().rstrip("/"),
        "key": str(s.get("key") or "").strip(),
        "protocol": _proto,
        "models": models,
        "enabled": bool(s.get("enabled", True)),
    }


def _clean_routing(raw):
    """把任意输入揉成合法 routing：{multi, i2i, t2i, single} 各 {sites,models,timeout}。

    容错优先：字段缺失/类型不对一律回落默认值，**绝不因为一个字段写错就让生图挂掉**。
    `models` 允许为空（t2i/single 空 = 沿用 model_order）。

    2026-11-19 新增 i2i / t2i 两条线（雪碧：图生图单独配规则、文生图保持不动）。
    **老配置只有 multi/single 时**：i2i 与 t2i 都从 single 继承，保证行为一字不变。
    """
    raw = raw if isinstance(raw, dict) else {}

    def _lst(v):
        """容错：字符串按逗号/新行拆，其它非列表一律当空。避免 "abc" 被逐字符拆开。"""
        if isinstance(v, str):
            v = re.split(r"[,\n，]", v)
        if not isinstance(v, (list, tuple)):
            return []
        return [str(x).strip() for x in v if str(x).strip()]

    # 老配置兼容：没显式给 i2i/t2i 时，用 single 的原始值当继承源
    legacy = raw.get("single") if isinstance(raw.get("single"), dict) else {}

    out = {}
    for k in ("multi", "i2i", "t2i", "single"):
        d = DEFAULT_ROUTING[k]
        cur = raw.get(k) if isinstance(raw.get(k), dict) else {}
        if not cur and k in ("i2i", "t2i") and legacy:
            cur = legacy                      # 继承老 single，行为不变
        sites = _lst(cur.get("sites")) or list(d["sites"])
        models = _lst(cur.get("models"))
        try:
            tmo = int(cur.get("timeout") or d["timeout"])
        except Exception:                      # noqa: BLE001
            tmo = d["timeout"]
        # models 为空时：i2i/multi 用各自默认链；t2i/single 保持空（外层会填 model_order）
        if models:
            mm = models
        elif k in ("t2i", "single"):
            mm = []
        else:
            mm = list(d["models"])
        out[k] = {"sites": sites, "models": mm,
                  "timeout": max(60, min(1800, tmo))}
    return out


def default_conf():
    return {"version": 1,
            "model_order": list(DEFAULT_MODEL_ORDER),
            "routing": _clean_routing(DEFAULT_ROUTING),
            "sites": [_clean_site(x) for x in DEFAULT_SITES]}


def load_conf():
    """读配置；文件不存在或损坏 → 回默认（不抛异常，保证生图永远有路可走）。"""
    with _lock:
        conf = default_conf()
        try:
            if os.path.exists(CONF_FILE):
                raw = json.load(open(CONF_FILE, encoding="utf-8"))
                if isinstance(raw, dict):
                    sites = raw.get("sites")
                    if isinstance(sites, list) and sites:
                        conf["sites"] = [_clean_site(x) for x in sites]
                    mo = raw.get("model_order")
                    if isinstance(mo, list) and [str(m).strip() for m in mo if str(m).strip()]:
                        conf["model_order"] = [str(m).strip() for m in mo if str(m).strip()]
                    if isinstance(raw.get("routing"), dict):
                        conf["routing"] = _clean_routing(raw["routing"])
        except Exception:                          # noqa: BLE001
            pass
        return conf


def routing():
    """**实际生效的分线规则**（后台可改）。

    返回 {"multi": {...}, "i2i": {...}, "t2i": {...}, "single": {...}}。
    · multi  = 参考图 ≥ 2 张（2026-11-19 实测只有 huahuaapi 稳定真融合，lensapi 477）；
    · i2i    = 参考图恰好 1 张（**图生图主力线**，lensapi + huahuaapi 各 5 模型实测全通）；
    · t2i    = 0 张参考图的**纯文生图**（雪碧要求保持历史行为，models 空→沿用 model_order）；
    · single = 老键名，保留兼容；新代码不再直接用它。
    · t2i/single.models 留空 → 自动沿用「模型优先级」(model_order)，老配置无需迁移。
    """
    conf = load_conf()
    rt = _clean_routing(conf.get("routing"))
    mo = list(conf.get("model_order") or DEFAULT_MODEL_ORDER)
    for k in ("t2i", "single"):
        if not [m for m in rt[k].get("models", []) if m]:
            rt[k]["models"] = list(mo)
    return rt


def lane_for(n_refs):
    """按参考图张数选线名：0 → t2i，1 → i2i，≥2 → multi。

    单独抽出来是为了**前后端/自检共用同一套判定**，避免各处 if 写歪。
    """
    n = int(n_refs or 0)
    if n >= 2:
        return "multi"
    return "i2i" if n == 1 else "t2i"


def _is_masked(k):
    return (not k) or ("..." in k) or ("…" in k) or (k.count("*") >= 4)


def save_conf(conf):
    """写盘。**掩码或空 key 自动保留旧值**（沿用 duotu save_ai_conf 的保护约定）。"""
    with _lock:
        old = load_conf()
        old_by_name = {s["name"]: s for s in old["sites"]}
        out = {"version": 1,
               "model_order": [str(m).strip() for m in (conf.get("model_order") or []) if str(m).strip()]
                              or list(old["model_order"]),
               # 分线规则：提交里没带 routing 就沿用旧值（老前端保存时不会回传这个字段）
               "routing": _clean_routing(conf.get("routing") or old.get("routing")),
               "sites": []}
        for s in (conf.get("sites") or []):
            cs = _clean_site(s)
            if _is_masked(cs["key"]):
                cs["key"] = old_by_name.get(cs["name"], {}).get("key") or cs["key"]
            # 协议字段的保护：老前端（settings.html）保存时不回传 protocol，
            # 若照 _clean_site 的默认值落盘，apib 会被写成 "openai" → 生图必 400。
            # 故：**提交里没带 protocol 就沿用旧配置的 protocol**（与掩码 key 同一套保护约定）。
            if not str((s or {}).get("protocol") or "").strip():
                cs["protocol"] = old_by_name.get(cs["name"], {}).get("protocol") or cs["protocol"]
            if cs["base"] and cs["name"]:
                out["sites"].append(cs)
        if not out["sites"]:
            out["sites"] = old["sites"]
        tmp = CONF_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONF_FILE)
        return out


def load_sites():
    return [s for s in load_conf()["sites"] if s.get("enabled", True) and s.get("base")]


def _mask(k):
    k = k or ""
    if len(k) <= 10:
        return (k[:2] + "…") if k else ""
    return k[:6] + "…" + k[-4:]


def public_conf():
    """打码版（前端展示用）。"""
    conf = load_conf()
    return {"version": conf.get("version", 1),
            "model_order": list(conf.get("model_order") or []),
            "routing": _clean_routing(conf.get("routing")),
            "sites": [dict(s, key=_mask(s.get("key")), has_key=bool(s.get("key")))
                      for s in conf["sites"]]}


# ---------------- 轮询序列 ----------------
def _supports(site, model):
    """站点模型清单为空 → 视为「都支持」（宽松，保证不漏尝试）。"""
    ms = site.get("models") or []
    if not ms:
        return True
    return model in ms


def plan_order(models=None, site_order=None, lane=None):
    """生成轮询序列：**外层模型顺序，内层站点顺序**。

    [(site_name, base, key, model), ...]
      · 同一个模型先把所有站点挨个试完（＝雪碧要的「同模型跨站点轮询」）；
      · 某模型所有站点都失败，才轮到列表里的下一个模型；
      · 站点不在自己 models 清单里的模型自动跳过；
      · 最近成功的「站点+模型」组合整体提到最前（省掉必然失败的白等）；
      · 熔断中的组合 / 站点不排入（见上方 BREAK_N/BREAK_SEC）；若全被熔断则忽略冷却全排。

    `site_order`（2026-11-15 新增）：**显式指定本轮参与轮询的站点及其顺序**，
      未列出的站点本轮直接不参与（用于「按用途分线」——多图线只走 apib/huahuaapi，
      因为实测 lensapi 的多图参考只有 42% 真融合；单图线才走 lensapi）。
      传 None = 老行为（按配置里的站点顺序全排）。
    """
    conf = load_conf()
    sites = [s for s in conf["sites"] if s.get("enabled", True) and s.get("base")]
    if site_order:
        byname = {s["name"]: s for s in sites}
        picked = [byname[n] for n in site_order if n in byname]
        if picked:
            sites = picked                      # 严格按给定顺序；未列出的不参与本轮
        else:
            # 🔴 2026-11-19：指定了 site_order 却一个都匹配不上（站点被删/改名/全禁用）
            #    **不能静默回落全站** —— 那会把该线刻意排除的站点（欠费 apib/grsai、
            #    多图不支持的 lensapi）又排进来，等于分线失效。
            #    此处返回空链，让调用方明确失败并暴露配置问题，而不是悄悄走错站。
            sys.stderr.write("[ai_sites] site_order=%s 在本站点表里一个都匹配不到"
                             "（可用：%s）→ 返回空链，请检查后台站点配置\n"
                             % (list(site_order), sorted(byname)))
            return []
    mlist = [str(m).strip() for m in (models or conf.get("model_order") or DEFAULT_MODEL_ORDER)
             if str(m).strip()]
    pairs = []
    for m in mlist:
        for s in sites:
            if _supports(s, m):
                pairs.append((s["name"], s["base"], s["key"], m))
    now = time.time()
    with _lock:
        # 🔴 给了 lane → **只认该线自己的记忆**，该线没成功过就不提前（保持配置链原序）。
        #    绝不回落全局记忆：否则 t2i 刚成功的组合会顶掉 i2i 精心排的速度序（已实测）。
        #    没给 lane（老调用点）→ 沿用全局记忆，行为一字不变。
        if lane:
            last = _LAST_OK_BY_LANE.get(str(lane))
        else:
            last = _LAST_OK[-1] if _LAST_OK else None
        cooled_pairs = {k for k, d in _pair_fail.items() if d.get("until", 0.0) > now}
    if cooled_pairs:
        alive = [p for p in pairs if (p[0], p[3]) not in cooled_pairs]
        if alive:
            pairs = alive                                              # 安全阀：empty → 保留原序列
    # 🔴 只有当「上次成功组合」本来就在本线链首附近时才提前，避免跨线污染顺序。
    #    给了 lane 且该线已有记忆 → 正常提前；没给 lane（老调用点）→ 沿用老行为。
    if last:
        for i, p in enumerate(pairs):
            if (p[0], p[3]) == last:
                pairs.insert(0, pairs.pop(i))
                break
    return pairs


def note_fail(site, model):
    """记一次「站点+模型」失败；连续 BREAK_N 次 → 该组合冷却 BREAK_SEC 秒。

    返回 True 表示**本次刚好触发（或已在）冷却**，便于调用方打日志。
    成功路径由 remember() 自动清零，调用方无需配对调用。
    """
    now = time.time()
    key = (str(site), str(model))
    with _lock:
        d = _pair_fail.get(key)
        if d is None:
            d = {"n": 0, "until": 0.0}
        elif d.get("until", 0.0) and now >= d["until"]:   # **冷却已过期**（曾经熔断过）→ 重新起数
            d = {"n": 0, "until": 0.0}
        d["n"] += 1
        if d["n"] >= BREAK_N and d["until"] <= now:   # 冷却期内不重复续期
            d["until"] = now + BREAK_SEC
        _pair_fail[key] = d
        return d["until"] > now


def breaker_state():
    """诊断用：当前冷却中的组合；以及**整站已被跳过**的站点（该站全部组合都在冷却）。"""
    now = time.time()
    conf = load_conf()
    with _lock:
        cooled = {("%s / %s" % k): round(d["until"] - now, 1)
                  for k, d in _pair_fail.items() if d.get("until", 0.0) > now}
    site_pairs, site_cooled = {}, {}
    for s in conf["sites"]:
        if not (s.get("enabled", True) and s.get("base")):
            continue
        for m in (s.get("models") or []):
            site_pairs[s["name"]] = site_pairs.get(s["name"], 0) + 1
            if ("%s / %s" % (s["name"], m)) in cooled:
                site_cooled[s["name"]] = site_cooled.get(s["name"], 0) + 1
    return {"pairs": cooled,
            "sites_cooldown_count": site_cooled,
            "sites_total_count": site_pairs,
            "sites_blocked": [n for n, c in site_cooled.items()
                              if c and c >= site_pairs.get(n, c)],
            "break_n": BREAK_N, "break_sec": BREAK_SEC}


def remember(site, model, lane=None):
    """记一次成功：解除该组合熔断，并按线记住「上次成功组合」。

    lane 给了就只影响该线的排序（推荐）；不给则退回全局记忆（老调用点兼容）。
    """
    with _lock:
        try:
            _LAST_OK.remove((site, model))
        except ValueError:
            pass
        _LAST_OK.append((site, model))
        del _LAST_OK[:-4]
        if lane:
            _LAST_OK_BY_LANE[str(lane)] = (site, model)
        _pair_fail.pop((str(site), str(model)), None)  # 成功 → 解除该组合熔断
        return list(_LAST_OK)


# ---------------- 协议分发（openai 标准同步 / apib 私有异步） ----------------
def protocol_map():
    """{站点名: 协议}。协议见文件头；未配置一律 'openai'。"""
    return {s["name"]: (s.get("protocol") or "openai") for s in load_sites()}


def protocol_of(site_name):
    return protocol_map().get(str(site_name), "openai")


def _http_raw(method, url, payload=None, key=None, timeout=180):
    """极简 JSON HTTP。payload=dict 会以 JSON 提交；非 2xx 抛 RuntimeError("HTTP <code> <body>")。"""
    req = urllib.request.Request(url, method=method)
    if payload is not None:
        req.data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", "Bearer " + str(key))
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PatternWorkbench/2.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s %s" % (e.code, e.read().decode("utf-8", "ignore")[:200]))


def _as_data_uri(item):
    """任意输入 → data URI：bytes/bytearray → PNG；无前缀 str → 视为 base64 JPEG。"""
    if item is None:
        return None
    if isinstance(item, (bytes, bytearray)):
        return "data:image/png;base64," + base64.b64encode(bytes(item)).decode()
    s = str(item).strip()
    if not s:
        return None
    return s if s.startswith("data:") else ("data:image/jpeg;base64," + s)


def _apib_pick_url(node):
    """从 apib 的 data / result 节点取图片地址（url 可能是 str、[str]、{url}，或回 b64_json）。"""
    if not isinstance(node, dict):
        return None
    imgs = node.get("images")
    cand = (imgs[0] if (isinstance(imgs, list) and imgs) else imgs) if imgs is not None else None
    if isinstance(cand, dict):
        u = cand.get("url") or cand.get("image_url")
        if isinstance(u, list):
            u = u[0] if u else None
        if isinstance(u, str) and u.strip():
            return u.strip()
        b = cand.get("b64_json") or cand.get("b64")
        if isinstance(b, str) and b.strip():
            return "data:image/png;base64," + b.split(",", 1)[-1]
    elif isinstance(cand, str) and cand.strip():
        return cand.strip()
    for k in ("url", "image_url", "b64_json"):
        v = node.get(k)
        if isinstance(v, list):
            v = v[0] if v else None
        if isinstance(v, str) and v.strip():
            v = v.strip()
            return v if (v.startswith("http") or v.startswith("data:")) else ("data:image/png;base64," + v)
    return None


# ---- apib 的「尺寸语义」分两套（2026-10-15 实测）→ 统一归一化（2026-10-16 方案 A）----
#   · gpt-image-2 系：吃**像素**（"1024x1024"）；
#   · gemini 系 4 个：只吃**比例字符串**（"1:1"/"3:4"/"9:16"…），传像素直接报
#     `unsupported image aspect ratio "1024:1024"`。
# 调用方（pt/hd/wm）一律按像素算尺寸，故在此按模型名归一化，
# **不改调用方**、也不影响 lensapi/grsai（那两家是 openai 协议，走不到这里）。
_APIB_RATIOS_BASE = ("1:1", "16:9", "21:9", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16")
_APIB_RATIOS_EXT = _APIB_RATIOS_BASE + ("1:4", "4:1", "1:8", "8:1")
# 支持「扩展比例集」的三个模型；其余 gemini（如 pro）只支持 _APIB_RATIOS_BASE。
_APIB_RATIO_EXT_MODELS = ("gemini-3.1-flash-image-preview", "gemini-3.1-flash-lite-image",
                          "gemini-nano-banana-2.1")
_APIB_SIZE_RE = re.compile(r"^\s*(\d+)\s*[xX*×]\s*(\d+)\s*$")


def _apib_is_ratio_model(model):
    m = str(model or "").lower()
    return ("gemini" in m) or ("nano-banana" in m)


def apib_size(model, size):
    """把调用方传来的尺寸归一化成 apib 该模型要的形态。

    · gpt-image-2 系 / 非 gemini 系 → **原样像素**（缺省 1024x1024）；
    · gemini 系 → 像素按**比例最接近**（对数距离）换算成 "a:b"；
      传入的已是比例串 / 关键字则原样透传；解析不出像素时回落 "1:1"。

    公开函数：apib_gen 内部用它，duotu 的独立异步客户端（不走站点池）也复用它。
    """
    s = str(size or "").strip()
    if not _apib_is_ratio_model(model):
        return s or "1024x1024"
    if ":" in s or s.lower() in ("auto", "square", "landscape", "portrait"):
        return s                                        # 已是比例串 / 关键字 → 原样透传
    mch = _APIB_SIZE_RE.match(s)
    if not mch:
        return "1:1"
    w, h = int(mch.group(1)), int(mch.group(2))
    if w <= 0 or h <= 0:
        return "1:1"
    lower = str(model or "").lower()
    pool = _APIB_RATIOS_EXT if any(k in lower for k in _APIB_RATIO_EXT_MODELS) else _APIB_RATIOS_BASE
    tgt = math.log(w / float(h))
    best, best_d = pool[0], None
    for r in pool:
        a, b = r.split(":")
        d = abs(tgt - math.log(int(a) / float(b)))
        if best_d is None or d < best_d:
            best, best_d = r, d
    return best


def apib_gen(base, key, model, prompt, size="1024x1024", images=None,
             timeout=420, poll=3.0):
    """apib.ai（APIMart）**私有异步**出图 —— 返回图片 URL / data URI（与 pipeline._gen_once 同约定）。

    提交  POST {base}/images/generations  JSON{model,prompt,size,n, image?:[data_uri,...]}
          -> {"code":200,"data":[{"status":"submitted","task_id":"task_xxx"}]}
    轮询  GET  {base}/tasks/<task_id>
          -> data.status: submitted|processing → success/completed
          → data.result.images[0].url（str 或 [str]）

    ⚠️ gpt-image 系**不能**走 /v1/images/edits —— apib 只允许 grok 系走那个端点，
       实测 `400 only supports Grok image models`。参考图只能塞进 generations 的 `image` 数组。
    ⚠️ 余额校验在**执行阶段**：余额不足时提交照样返回 task_id，随后任务失败（402）
       —— 故必须以轮询结果判成败，不能以「提交成功」判可用。
    ⚠️ 尺寸语义两套：gpt-image-2 吃像素、gemini 系吃比例串 → 由 apib_size() 自动归一化，
       调用方继续传像素即可（lensapi/grsai 那条同步链不受影响）。
    """
    base = (base or APIB_BASE).strip().rstrip("/")
    payload = {"model": model, "prompt": prompt, "n": 1, "size": apib_size(model, size)}
    uris = [u for u in (_as_data_uri(x) for x in (images or [])) if u]
    if uris:
        payload["image"] = uris

    body = _http_raw("POST", base + "/images/generations", payload, key, timeout=180)
    try:
        j = json.loads(body.decode("utf-8"))
    except Exception:                                  # noqa: BLE001
        raise RuntimeError("apib 响应非 JSON：%s" % body[:200])
    direct = _apib_pick_url(j)                         # 少数模型可能同步直给图
    if direct:
        return direct
    tid = None
    for d in (j.get("data") or []):
        if isinstance(d, dict) and d.get("task_id"):
            tid = d["task_id"]
            break
    if not tid:
        raise RuntimeError("apib 提交未返回 task_id：%s" % json.dumps(j, ensure_ascii=False)[:200])

    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(poll)
        body = _http_raw("GET", base + "/tasks/" + str(tid), None, key, timeout=60)
        try:
            j = json.loads(body.decode("utf-8"))
        except Exception:                              # noqa: BLE001
            continue
        d = j.get("data") if isinstance(j.get("data"), dict) else {}
        status = str(d.get("status") or "").lower()
        if status in ("success", "completed", "succeeded", "done"):
            url = _apib_pick_url(d.get("result") or {}) or _apib_pick_url(d)
            if url:
                return url
            raise RuntimeError("apib 任务成功但取不到图片：%s" % json.dumps(d, ensure_ascii=False)[:200])
        if status in ("failed", "error", "canceled", "cancelled"):
            raise RuntimeError("apib 任务失败：%s" % json.dumps(d, ensure_ascii=False)[:240])
    raise RuntimeError("apib 任务轮询超时（%ds）" % int(timeout))


def fetch_bytes(url, timeout=180):
    """把「外链 / data URI」落成 bytes（hd、wm 需要原始字节）。"""
    if isinstance(url, str) and url.startswith("data:"):
        return base64.b64decode(url.split(",", 1)[1])
    req = urllib.request.Request(str(url), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------- 错误脱敏 ----------------
# 顺序敏感：先长关键词，再短数字，避免 "500" 抢走 "gpt-image-2.5" 里的 5 之类误判
_HINT_CODES = [
    (("image_task_failed", "requires_review", "selection_timeout", "过载", "review"), "477"),
    (("insufficient", "balance", "quota", "exceeded your current quota", "余额",
      "欠费", "额度", "no enough", "billing"), "402"),
    (("invalid api key", "incorrect api key", "invalid token", "unauthorized",
      "authentication", "not authorized"), "401"),
    (("rate limit", "too many requests", "429"), "429"),
    (("forbidden", "permission denied"), "403"),
    (("timed out", "timeout", "超时", "urlopen error", "connection reset",
      "connection refused", "temporary failure in name resolution",
      "max retries exceeded", "ssl"), "408"),
    (("service unavailable", "502 bad gateway", "bad gateway", "upstream"), "502"),
]

_HTTP_RE = re.compile(r"HTTP\s*(\d{3})", re.I)
_BARE_HTTP_RE = re.compile(r"\bhttp\s*(\d{3})\b", re.I)
_ANY_3DIG = re.compile(r"\b(4\d{2}|5\d{2})\b")


def error_code(exc):
    """从任意异常/文本里抽出**错误码**（拿不到就 500）。"""
    m = str(exc if exc is not None else "")
    low = m.lower()
    hit = _HTTP_RE.search(m) or _BARE_HTTP_RE.search(low)
    if hit:
        return hit.group(1)
    for keys, code in _HINT_CODES:
        if any(k in low for k in keys):
            return code
    hit = _ANY_3DIG.search(m)
    if hit:
        return hit.group(1)
    return "500"


def sanitize(exc, prefix="生图失败"):
    """**统一脱敏**：只保留错误码，绝不回传模型名 / 站点名 / 欠费原文。"""
    return "%s（错误码 %s），请稍后重试。" % (prefix, error_code(exc))


if __name__ == "__main__":                          # 自检
    import pprint
    pprint.pprint(public_conf())
    print("--- plan_order ---")
    for p in plan_order():
        print("  ", p[0], p[3])
