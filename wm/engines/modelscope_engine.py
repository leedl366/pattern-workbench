"""魔搭 ModelScope「Qwen-Image-Edit-2511」免费改图云引擎。

接口形态：魔搭 API-Inference 异步任务 API（OpenAI 风格路径）
  1. POST {base}/v1/images/generations
     头 X-ModelScope-Async-Mode: true
     body: {model, prompt, image_url: [data-url 或公网url, ...]}
     → 返回 {"task_status":"SUCCEED","task_id":"..."}
  2. GET  {base}/v1/tasks/{task_id}
     头 X-ModelScope-Task-Type: image_generation
     → 轮询至 task_status == SUCCEED，output_images[0] 为结果图 URL
  3. 下载结果图

特点：
  · 免费：每日总2000 次 API 调用额度（生成类模型约 50 张/天，0 点重置，超额报 429）
  · 支持多图输入（image_url 数组，最多 3 张）→ 可做「样例图 + 待处理图」双图参考改图
  · 不需要 mask，整图 + 提示词驱动，复杂背景补全优于本地 LaMa
  · 单张出图约 1~3 分钟（免费通道排队，比百炼慢），批量处理请预留时间

用户只需在「模型配置」里填魔搭访问令牌（ms-...）即可启用；
base_url / 模型名 / 提示词都有默认值，留空即走官方默认。
"""
import base64
import time

import cv2
import numpy as np
import requests

DEFAULT_BASE = "https://api-inference.modelscope.cn/"
DEFAULT_MODEL = "Qwen/Qwen-Image-Edit-2511"
DEFAULT_PROMPT = "请去除图片中的水印文字，用周围背景自然无缝填充，其余画面保持不变。"
SUBMIT_PATH = "v1/images/generations"
TASK_PATH = "v1/tasks/"
#魔搭输入限制：单边不超过 4096 像素、数据量不超过 10MB（超限先等比缩放）
MAX_SIDE = 4096
MAX_BYTES = 9 * 1024 * 1024
MIN_SIDE = 256


def _data_url(image_bgr, quality=95):
    """BGR ndarray → data:image/jpeg;base64,...（自动压体积，超大图先等比缩放）。"""
    h, w = image_bgr.shape[:2]
    scale = 1.0
    if max(h, w) > MAX_SIDE:
        scale = MAX_SIDE / float(max(h, w))
    if min(h, w) * scale < MIN_SIDE:
        scale = max(scale, MIN_SIDE / float(min(h, w)))
    img = image_bgr
    if abs(scale - 1.0) > 1e-3:
        nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        img = cv2.resize(image_bgr, (nw, nh),
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    q = quality
    for _ in range(6):
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
        if not ok:
            raise RuntimeError("图像编码失败")
        if len(buf) <= MAX_BYTES or q <= 60:
            break
        q -= 10
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("utf-8")


class ModelScopeEngine:
    name = "modelscope"
    label = "魔搭 ModelScope Qwen-Image-Edit-2511（云端 · 免费额度）"
    needs_key = True

    def inpaint(self, image_bgr, mask_bin, region=None, config=None, **kw):
        config = config or {}
        token = (config.get("api_key") or "").strip()
        base = (config.get("base_url") or "").strip().rstrip("/") or DEFAULT_BASE.rstrip("/")
        model = (config.get("model") or "").strip() or DEFAULT_MODEL
        template = (config.get("prompt_template") or "").strip() or DEFAULT_PROMPT
        # 可选：参考图（水印样例图）URL/本地路径，作为第二张输入图提升一致性
        ref = (config.get("reference_image") or "").strip()

        if not token:
            raise RuntimeError(
                "未配置魔搭访问令牌，请在「模型配置」里填写（ms-...）后再试。"
                "获取路径：modelscope.cn/my/access/token → 新建令牌 →选「写入权限」"
            )

        prompt = self._build_prompt(template, region, image_bgr.shape)
        image_url = [_data_url(image_bgr)]
        if ref:
            image_url.append(self._ref_to_url(ref))

        submit_url = base + "/" + SUBMIT_PATH
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "X-ModelScope-Async-Mode": "true",
        }
        payload = {"model": model, "prompt": prompt, "image_url": image_url}
        try:
            resp = requests.post(submit_url, json=payload, headers=headers, timeout=180)
        except requests.exceptions.Timeout:
            raise RuntimeError("魔搭任务提交超时（网络或平台繁忙），请重试。")
        if resp.status_code != 200:
            raise RuntimeError("魔搭任务提交失败: HTTP %s %s" % (resp.status_code, resp.text[:300]))
        data = resp.json()
        task_id = data[0] if isinstance(data, list) and data else data.get("task_id")
        if not task_id:
            raise RuntimeError("魔搭返回结构异常（未拿到 task_id）: %s" % str(data)[:300])

        # 轮询任务状态：免费通道排队较慢，留足 10 分钟上限
        task_url = base + "/" + TASK_PATH + task_id
        poll_headers = {
            "Authorization": "Bearer " + token,
            "X-ModelScope-Task-Type": "image_generation",
        }
        deadline = time.time() + 600
        last = ""
        while time.time() < deadline:
            time.sleep(4)
            try:
                r = requests.get(task_url, headers=poll_headers, timeout=60)
            except Exception:
                continue
            if r.status_code != 200:
                last = "HTTP %s %s" % (r.status_code, r.text[:150])
                continue
            out = r.json()
            status = out.get("task_status")
            if status == "SUCCEED":
                urls = out.get("output_images") or []
                if not urls:
                    raise RuntimeError("魔搭任务成功但无结果图: %s" % str(out)[:300])
                return self._download(urls[0])
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                raise RuntimeError(
                    "魔搭任务失败: %s %s"
                    % (out.get("code") or status, out.get("message") or out.get("error_msg") or str(out)[:200])
                )
            last = str(status)
        raise RuntimeError("魔搭任务超时（10 分钟未完成）。免费通道高峰排队较久，可稍后重试。%s" % ("| " + last if last else ""))

    # ------------------------------------------------------------------
    @staticmethod
    def _build_prompt(template, region, shape):
        """把圈选区域坐标填进提示词占位符，让模型只改指定位置。"""
        h, w = shape[:2]
        prompt = template
        if not region:
            return prompt
        rw, rh = int(region.get("w", 0)), int(region.get("h", 0))
        if rw <= 0 or rh <= 0:
            return prompt
        x, y = int(region.get("x", 0)), int(region.get("y", 0))
        x1, y1 = max(0, x), max(0, y)
        x2, y2 = min(w, x + rw), min(h, y + rh)
        prompt = (prompt
                  .replace("[x1,y1]", "(%d,%d)" % (x1, y1))
                  .replace("[x2,y2]", "(%d,%d)" % (x2, y2)))
        pts = region.get("points") or []
        if len(pts) >= 3:
            pts_txt = ", ".join("(%d,%d)" % (int(p[0]), int(p[1])) for p in pts)
            if "[poly]" in prompt:
                prompt = prompt.replace("[poly]", pts_txt)
            else:
                prompt += "（该区域为多边形圈选，顶点坐标依次为：%s）" % pts_txt
        else:
            prompt = prompt.replace("[poly]", "（见上方矩形坐标）")
        return prompt

    @staticmethod
    def _ref_to_url(ref):
        """参考图支持公网 URL 直传；本地路径则读成 data-url。"""
        if ref.startswith("http://") or ref.startswith("https://"):
            return ref
        import os
        img = cv2.imdecode(np.fromfile(ref, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("参考图读取失败: %s" % ref)
        return _data_url(img)

    @staticmethod
    def _download(url):
        r = requests.get(url, timeout=240)
        r.raise_for_status()
        arr = np.frombuffer(r.content, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("魔搭结果图下载后解码失败")
        return img
