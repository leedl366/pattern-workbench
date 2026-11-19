"""内置生图引擎（站点池）：与「图案提取工作台」**同一条生图通道**。

2026-10-24 起从「单站点 + 换模型兜底」升级为**站点池**：
- 站点/模型清单唯一真源在 `app/ai_sites.py`（后台设置页可改）；
- 轮询规则 = **同模型跨站点**：某站点某模型用不了 → 轮用其他站点的同一个模型；
  所有站点都挂，才换下一个模型；
- 密钥默认随站点池走（可在池里配），无需用户在去水印页手填；
- 失败文案统一脱敏为「生图失败（错误码 xxx），请稍后重试。」——
  不暴露站点名/模型名/欠费等细节。

协议（OpenAI 兼容 · 同步返回，与 app/pipeline.py 一致）：
- 带输入图 → POST {base}/v1/images/edits（multipart：model / prompt / size / n / image）
- `size` 必须**显式**给（传 auto 会报 "size is not supported"）
- 响应即成品：data[0].url 就是图片外链；gemini 系回 data[0].b64_json
  ——**没有 task_id、不需要轮询**（单张 29~58s 同步返回）

与 CustomEngine 的区别：CustomEngine 面向「用户自己填 URL + Key」的通用
OpenAI 兼容服务；本引擎是**内置预设**，端点/密钥/模型链都随站点池走。
"""
import base64
import json
import os
import sys
import time

import cv2
import numpy as np
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))            # app/wm/engines
APP_DIR = os.path.dirname(os.path.dirname(BASE_DIR))             # app/
KEY_FILE = os.path.join(APP_DIR, ".apib_key")                    # 与图案提取共用
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
import ai_sites                                                   # noqa: E402 —— 站点池

DEFAULT_BASE = "https://lensapi.cn"
# 2026-10-15：与 pt(pipeline.API_MODEL_CHAIN) 同步 —— 链首补 apib 的 gpt-image-2-official
# （质量优先，apib 已入池置顶）；其后仍是原 gpt-image / gemini 兜底，四条链保持同源。
# 2026-10-16 11:14（方案 A）：official 在该 key 下 403 无权限 → 链首改用有权限的 `gpt-image-2`
# （apib 内层排第一 ⇒ 真·质量优先）；链尾补 apib 站内独有 gemini 两条做兜底。
DEFAULT_MODELS = ("gpt-image-2", "gpt-image-2.5",
                  "gemini-3-pro-image-preview",
                  "gemini-nano-banana-2.1-vip", "gemini-3.1-flash-image-preview",
                  "gemini-nano-banana-2.1", "gemini-3.1-flash-lite-image")
DEFAULT_PROMPT = (
    "去除图片中的所有水印、文字、logo 和标识，用周围背景自然无缝填充，"
    "保持其余画面完全不变（构图、元素位置、颜色、大小、清晰度一律不动），"
    "输出完整图片。"
)


def load_default_key():
    """读取图案提取工作台同一把密钥；文件不存在时返回空串（不抛异常，
    让上层给出「请填 Key」的可读提示）。"""
    try:
        if os.path.exists(KEY_FILE):
            return open(KEY_FILE, encoding="utf-8").read().strip()
    except Exception:
        pass
    return ""


def _endpoint(base, suffix):
    """拼端点：base 可写 `https://lensapi.cn` 或 `https://lensapi.cn/v1`，
    两种写法都能命中 `.../v1/images/edits`，不会出现 /v1/v1。"""
    base = (base or DEFAULT_BASE).strip().rstrip("/")
    if base.endswith(suffix):
        return base
    if suffix.startswith("/v1/") and base.endswith("/v1"):
        return base + suffix[3:]
    return base + suffix


def _fit_size(w, h):
    """请求尺寸：短边固定 1024、长边按比例推（上限 2048），正方形即 1024x1024。
    （返回尺寸与题图不一致也没关系——pipeline.process_image 会把结果缩回原图尺寸）"""
    try:
        w, h = int(w), int(h)
    except Exception:
        return "1024x1024"
    if w <= 0 or h <= 0:
        return "1024x1024"
    short = 1024
    if w <= h:
        long = min(int(round(short * h / float(w) / 2.0) * 2), 2048)
        return "%dx%d" % (short, long)
    long = min(int(round(short * w / float(h) / 2.0) * 2), 2048)
    return "%dx%d" % (long, short)


class LensApiEngine:
    name = "lensapi"
    label = "内置生图通道（推荐 · 开箱即用）"
    needs_key = False      # 默认走站点池内置密钥，无需用户手填

    def inpaint(self, image_bgr, mask_bin, region=None, config=None, **kw):
        config = config or {}
        prompt = (config.get("prompt_template") or DEFAULT_PROMPT).strip()
        if image_bgr is None or image_bgr.size == 0:
            raise RuntimeError("输入图为空")

        # 模型优先级：配置页指定了就用它打头，其余内置模型兜底
        model_cfg = (config.get("model") or "").strip()
        mlist = [m.strip() for m in model_cfg.split(",") if m.strip()] or list(DEFAULT_MODELS)

        # 站点池展开：外层模型、内层站点（同一模型跨站点轮询）
        plan = list(ai_sites.plan_order(mlist))

        # 兼容旧语义：配置页显式填了「池外」自定义网关时，把它插到最前
        cb = (config.get("base_url") or "").strip()
        ck = (config.get("api_key") or "").strip()
        if cb.lower().startswith(("http://", "https://")) and \
                cb.rstrip("/") not in {p[1].rstrip("/") for p in plan}:
            plan.insert(0, ("custom", cb, ck or load_default_key(), mlist[0]))

        if not plan:
            raise RuntimeError("缺少密钥：请填写密钥，或在服务端放置 app/.apib_key（与图案提取工作台共用）")

        last_exc = None
        for site, base, skey, model in plan:
            try:
                img = self._gen_once(base, skey or load_default_key(), model, prompt, image_bgr,
                                     protocol=ai_sites.protocol_of(site))
                if img is not None:
                    ai_sites.remember(site, model)
                    return img
                raise RuntimeError("响应里没有可用图片")
            except Exception as e:                    # noqa: BLE001
                last_exc = e
                ai_sites.note_fail(site, model)       # 方案 B：连续失败 → 该组合冷却
                time.sleep(1.5)
        raise RuntimeError(ai_sites.sanitize(last_exc))

    # ---- 内部 ----
    def _gen_once(self, base_url, api_key, model, prompt, image_bgr, protocol="openai"):
        """双协议取图（2026-10-15）：
        - openai（lensapi / grsai）：同步打 /v1/images/edits（multipart），响应即成品。
        - apib  （api.apib.ai）    ：私有异步 —— gpt-image 系**不能**走 edits（只服务 grok 系），
          输入图必须放进 /v1/images/generations 的 image 数组，再轮询 /v1/tasks/<id> 取图。
        """
        h, w = image_bgr.shape[:2]
        size = _fit_size(w, h)
        ok, buf = cv2.imencode(".jpg", image_bgr, [cv2.IMWRITE_JPEG_QUALITY, 94])
        if not ok:
            raise RuntimeError("输入图编码失败")

        if protocol == "apib":
            link = ai_sites.apib_gen(base_url, api_key, model, prompt, size,
                                     images=[buf.tobytes()])
            raw = ai_sites.fetch_bytes(link)
            return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)

        url = _endpoint(base_url, "/v1/images/edits")
        files = {
            "image": ("in.jpg", buf.tobytes(), "image/jpeg"),
            "model": (None, model),
            "prompt": (None, prompt),
            "size": (None, size),
            "n": (None, "1"),
        }
        headers = {
            "Authorization": "Bearer " + api_key,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PatternWorkbench/2.0",
        }
        resp = requests.post(url, headers=headers, files=files, timeout=420)
        if resp.status_code != 200:
            raise RuntimeError("HTTP %s %s" % (resp.status_code, resp.text[:200]))
        try:
            data = resp.json()
        except Exception:
            raise RuntimeError("响应非 JSON：%s" % resp.text[:200])

        d = None
        if isinstance(data, dict):
            arr = data.get("data")
            if isinstance(arr, list) and arr:
                d = arr[0]
        if not isinstance(d, dict):
            raise RuntimeError("生图响应异常：%s" % json.dumps(
                data, ensure_ascii=False)[:220])

        link = d.get("url")
        if isinstance(link, list):
            link = link[0] if link else None
        if isinstance(link, dict):
            link = link.get("url")
        if isinstance(link, str) and link.startswith("http"):
            r = requests.get(link, timeout=180)
            r.raise_for_status()
            arr = np.frombuffer(r.content, np.uint8)
            return cv2.imdecode(arr, cv2.IMREAD_COLOR)

        b64 = d.get("b64_json")      # gemini-3-pro-image-preview 走这里
        if isinstance(b64, str) and b64.strip():
            if b64.startswith("data:"):
                b64 = b64.split(",", 1)[-1]
            raw = base64.b64decode(b64)
            arr = np.frombuffer(raw, np.uint8)
            return cv2.imdecode(arr, cv2.IMREAD_COLOR)

        raise RuntimeError("生图响应无图片（url / b64_json 均空）：%s" % json.dumps(
            {k: (v[:40] + "…" if isinstance(v, str) and len(v) > 40 else v)
             for k, v in d.items()}, ensure_ascii=False)[:200])
