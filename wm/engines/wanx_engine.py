"""阿里云百炼 · 通义万相「去水印专能」云引擎。

模型：wanx2.1-imageedit（function=remove_watermark），官方专门为
「去除图片水印」训练的能力——不需要 mask、不需要提示词，整图丢进去
自动识别并抹除水印/文字，背景由模型自然补全。对复杂花纹背景的效果
明显优于本地 LaMa，按张计费（约 ¥0.14/张），处理时才调用。

接口形态：DashScope 异步任务 API
  1. POST {base}/api/v1/services/aigc/image2image/image-synthesis
     头 X-DashScope-Async: enable，提交后返回 task_id
  2. GET  {base}/api/v1/tasks/{task_id} 轮询至 SUCCEEDED
  3. 下载 output.results[0].url 的结果图

用户只需在「模型配置」里填百炼 API Key（sk-...）即可启用；
base_url / 模型名 / 专能名都有默认值，留空即走官方默认。
"""
import base64
import time

import cv2
import numpy as np
import requests

DEFAULT_BASE = "https://dashscope.aliyuncs.com"
SUBMIT_SUFFIX = "/api/v1/services/aigc/image2image/image-synthesis"
TASK_SUFFIX = "/api/v1/tasks/"
# 万相输入限制：宽高 [512, 4096] 像素、≤10MB；超出需先等比缩放到合法范围
MIN_SIDE, MAX_SIDE = 512, 4096
DEFAULT_PROMPT = "去除图像中的文字和水印"


def _fit_limits(image_bgr):
    """等比缩放使宽高都落在 [MIN_SIDE, MAX_SIDE] 内（仅送模型用，输出会被 pipeline 拉回原始尺寸）。"""
    h, w = image_bgr.shape[:2]
    scale = 1.0
    if max(h, w) > MAX_SIDE:
        scale = MAX_SIDE / float(max(h, w))
    if min(h, w) * scale < MIN_SIDE:
        scale = max(scale, MIN_SIDE / float(min(h, w)))
    if abs(scale - 1.0) < 1e-3:
        return image_bgr
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    return cv2.resize(image_bgr, (nw, nh), interpolation=interp)


class WanxEngine:
    name = "wanx"
    label = "阿里云百炼 万相去水印专能（云端 · 推荐 · 需Key）"
    needs_key = True

    def inpaint(self, image_bgr, mask_bin, region=None, config=None, **kw):
        config = config or {}
        api_key = (config.get("api_key") or "").strip()
        base_url = (config.get("base_url") or "").strip().rstrip("/") or DEFAULT_BASE
        model = (config.get("model") or "").strip() or "wanx2.1-imageedit"
        function = (config.get("function") or "").strip() or "remove_watermark"
        prompt = (config.get("prompt") or "").strip() or DEFAULT_PROMPT

        if not api_key:
            raise RuntimeError("未配置百炼 API Key，请在「模型配置」里填写（sk-...）后再试。")

        # JPEG 编码控制请求体积（万相支持 jpg/png 的 data URL；宽高需落在 512~4096）
        send_img = _fit_limits(image_bgr)
        ok, buf = cv2.imencode(".jpg", send_img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            raise RuntimeError("图像编码失败")
        image_data = "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("utf-8")

        # 允许用户把 base_url 直接填成完整提交地址
        submit_url = base_url if base_url.endswith(SUBMIT_SUFFIX) else base_url + SUBMIT_SUFFIX
        payload = {
            "model": model,
            "input": {"function": function, "prompt": prompt, "base_image_url": image_data},
            "parameters": {"n": 1},
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
            "X-DashScope-Async": "enable",
        }
        resp = requests.post(submit_url, json=payload, headers=headers, timeout=120)
        if resp.status_code != 200:
            raise RuntimeError(
                "万相任务提交失败: HTTP %s %s" % (resp.status_code, resp.text[:300])
            )
        data = resp.json()
        try:
            task_id = data["output"]["task_id"]
        except Exception:
            raise RuntimeError("万相返回结构异常: %s" % str(data)[:300])

        # 轮询任务状态（去水印通常 5~30 秒，留足 5 分钟上限）
        task_url = base_url + TASK_SUFFIX + task_id
        poll_headers = {"Authorization": "Bearer " + api_key}
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(2)
            r = requests.get(task_url, headers=poll_headers, timeout=60)
            if r.status_code != 200:
                continue
            out = r.json().get("output") or {}
            status = out.get("task_status")
            if status == "SUCCEEDED":
                results = out.get("results") or []
                if not results or not results[0].get("url"):
                    raise RuntimeError("万相任务成功但无结果图: %s" % str(out)[:300])
                return self._download(results[0]["url"])
            if status in ("FAILED", "CANCELED"):
                raise RuntimeError(
                    "万相任务失败: %s %s"
                    % (out.get("code") or status, out.get("message") or str(out)[:200])
                )
        raise RuntimeError("万相任务超时（5 分钟未完成），请重试。")

    @staticmethod
    def _download(url):
        r = requests.get(url, timeout=180)
        r.raise_for_status()
        arr = np.frombuffer(r.content, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("万相结果图下载后解码失败")
        return img
