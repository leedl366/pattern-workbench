"""阿里云百炼 Qwen-Image-Edit 云引擎（通用图像编辑）。

只需在「模型配置」里填百炼 API Key 即可启用；base_url / 模型名 /
提示词都有默认值，留空即走官方默认。默认模型 qwen-image-edit，
同步多模态生成接口，提示词驱动去水印，复杂背景效果优于本地 LaMa。
"""
import base64
import cv2
import numpy as np
import requests

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
DEFAULT_MODEL = "qwen-image-edit"
DEFAULT_PROMPT = "请去除图片中的水印文字，用周围背景自然无缝填充，其余画面保持不变。"


class QwenEngine:
    name = "qwen"
    label = "阿里云百炼 Qwen-Image-Edit（云端 · 需Key）"
    needs_key = True

    def inpaint(self, image_bgr, mask_bin, region=None, config=None, **kw):
        config = config or {}
        base_url = (config.get("base_url") or "").strip() or DEFAULT_BASE_URL
        api_key = (config.get("api_key") or "").strip()
        model = (config.get("model") or "").strip() or DEFAULT_MODEL
        template = (config.get("prompt_template") or "").strip() or DEFAULT_PROMPT

        if not api_key:
            raise RuntimeError("未配置 Qwen API Key，请在「模型配置」里填写后再试。")

        ok, buf = cv2.imencode(".png", image_bgr)
        if not ok:
            raise RuntimeError("图像编码失败")
        b64 = base64.b64encode(buf.tobytes()).decode("utf-8")
        image_data = "data:image/png;base64," + b64

        prompt = template
        if region:
            has_rect = region.get("w", 0) > 0 and region.get("h", 0) > 0
            if has_rect:
                x = int(region.get("x", 0))
                y = int(region.get("y", 0))
                w = int(region.get("w", 0))
                h = int(region.get("h", 0))
                prompt = prompt.replace("[x1,y1]", f"({x},{y})").replace("[x2,y2]", f"({x + w},{y + h})")
            pts = region.get("points") or []
            if len(pts) >= 3:
                pts_txt = ", ".join(f"({int(p[0])},{int(p[1])})" for p in pts)
                if "[poly]" in prompt:
                    prompt = prompt.replace("[poly]", pts_txt)
                else:
                    prompt += f"（该区域为多边形圈选，顶点坐标依次为：{pts_txt}）"
            else:
                prompt = prompt.replace("[poly]", "（见上方矩形坐标）")

        payload = {
            "model": model,
            "input": {
                "messages": [
                    {"role": "user", "content": [{"image": image_data}, {"text": prompt}]}
                ]
            },
            "parameters": {"prompt_extend": True, "watermark": False},
        }
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + api_key}
        resp = requests.post(base_url, json=payload, headers=headers, timeout=180)
        if resp.status_code != 200:
            raise RuntimeError("Qwen API 调用失败: HTTP %s %s" % (resp.status_code, resp.text[:300]))
        data = resp.json()
        try:
            url = data["output"]["choices"][0]["message"]["content"][0]["image"]
        except Exception:
            raise RuntimeError("Qwen 返回结构异常: %s" % str(data)[:300])

        img_resp = requests.get(url, timeout=180)
        img_resp.raise_for_status()
        arr = np.frombuffer(img_resp.content, np.uint8)
        result = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if result is None:
            raise RuntimeError("Qwen 结果图解码失败")
        return result
