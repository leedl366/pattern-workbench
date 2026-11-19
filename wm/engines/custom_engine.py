"""通用自定义模型引擎：OpenAI 兼容接口。

用户只需在「模型配置」里填 base_url / api_key / model 即可接入，
无需改动任何代码。支持两种主流 OpenAI 兼容接口模式：

- chat（默认）：Chat Completions 多模态格式。
  POST {base_url}/chat/completions，把原图以 data URL 放进 messages，
  靠提示词里的坐标描述让模型去除水印。适用于硅基流动 / GLM-4V /
  OpenRouter / 本地 vLLM / Ollama 等大多数兼容服务。
- images_edits：OpenAI Images Edits 格式（gpt-image-1 等）。
  以 multipart 上传原图 + 水印区域 mask + prompt，让模型只编辑水印区域。
- images_gen：Images Generations 格式（APIMart / nano-banana / gpt-4o-image 等
  按张计费的图像模型）。POST {base_url}/images/generations，原图以 data URL
  放在 image 字段做图生图编辑。APIMart 这类平台为异步任务：响应里只有 task_id
  时自动轮询 GET {base_url}/tasks/{task_id} 直至出图。
  注意：这类图像模型不能走 /chat/completions（其计费模式 image_modalities 与
  聊天通道的 token 预扣费不兼容，会被平台 500 拒绝），必须用本模式。

三种模式都能解析出结果图（支持返回 image URL / data base64 / b64_json）。
"""
import base64
import re
import time

import cv2
import numpy as np
import requests

DEFAULT_PROMPT = (
    "请去除图片中位于矩形区域 [x1,y1] 到 [x2,y2] 内的水印或文字，"
    "用周围背景自然无缝填充，其余画面保持不变。"
)

# images_gen 模式专用：图像编辑模型理解自然语言，不需要坐标
IMAGES_GEN_DEFAULT_PROMPT = (
    "去除图片中的所有水印文字和标识，用周围背景自然无缝填充，"
    "保持其余画面完全不变，输出完整图片。"
)

_TASK_SUCCESS = {"succeeded", "success", "completed", "complete", "done", "finished", "succeed"}
_TASK_FAILED = {"failed", "failure", "error", "canceled", "cancelled", "expired"}


class CustomEngine:
    name = "custom"
    label = "自定义模型（OpenAI 兼容 · 填 URL + Key）"
    needs_key = True

    def inpaint(self, image_bgr, mask_bin, region=None, config=None, **kw):
        config = config or {}
        self._last_download_err = None
        base_url = (config.get("base_url") or "").strip().rstrip("/")
        api_key = (config.get("api_key") or "").strip()
        model = (config.get("model") or "").strip()
        mode = (config.get("mode") or "chat").strip()
        template = config.get("prompt_template") or DEFAULT_PROMPT

        if not api_key:
            raise RuntimeError("未配置 API Key，请在「模型配置」里填写。")
        if not base_url:
            raise RuntimeError("未配置 Base URL，请在「模型配置」里填写。")
        if not model:
            raise RuntimeError("未配置模型名，请在「模型配置」里填写。")

        prompt = template
        if region:
            has_rect = int(region.get("w", 0)) > 0 and int(region.get("h", 0)) > 0
            if has_rect:
                x = int(region.get("x", 0))
                y = int(region.get("y", 0))
                w = int(region.get("w", 0))
                h = int(region.get("h", 0))
                prompt = prompt.replace("[x1,y1]", f"({x},{y})").replace(
                    "[x2,y2]", f"({x + w},{y + h})"
                )
            pts = region.get("points") or []
            if len(pts) >= 3:
                pts_txt = ", ".join(f"({int(p[0])},{int(p[1])})" for p in pts)
                if "[poly]" in prompt:
                    prompt = prompt.replace("[poly]", pts_txt)
                else:
                    prompt += f"（该区域为多边形圈选，顶点坐标依次为：{pts_txt}）"
            else:
                prompt = prompt.replace("[poly]", "（见上方矩形坐标）")

        if mode == "images_edits":
            result = self._images_edits(base_url, api_key, model, image_bgr, mask_bin, prompt)
        elif mode == "images_gen":
            # 图像编辑模型按自然语言工作，坐标模板反而干扰；无坐标信息时换专用提示词
            gen_prompt = prompt
            if "[x1,y1]" in gen_prompt and not region:
                gen_prompt = IMAGES_GEN_DEFAULT_PROMPT
            result = self._images_generations(base_url, api_key, model, image_bgr, gen_prompt)
        else:
            result = self._chat_completions(base_url, api_key, model, image_bgr, prompt)

        if result is None:
            raise RuntimeError("模型返回里没有找到可用的图片。")
        return result

    # ---- 工具 ----
    @staticmethod
    def _encode_b64(image_bgr, ext=".png", params=None):
        ok, buf = cv2.imencode(ext, image_bgr, params or [])
        if not ok:
            raise RuntimeError("图像编码失败")
        return base64.b64encode(buf.tobytes()).decode("utf-8")

    @staticmethod
    def _decode_b64(b64):
        arr = np.frombuffer(base64.b64decode(b64), np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("结果图 base64 解码失败")
        return img

    def _download(self, url, attempts=3):
        """下载结果图。APIMart 系（getapib.org 等）走 CloudFront，刚出图时 CDN
        边缘节点可能短暂回 403 AccessDenied（源站对象尚未同步），需退避重试。
        注意重试总时长受平台网关 60s 上限约束，最多 3 次（5s+10s 退避）。"""
        last = None
        for i in range(attempts):
            try:
                r = requests.get(url, timeout=180)
                if r.status_code in (403, 404, 409) and i < attempts - 1:
                    time.sleep(5 * (i + 1))  # 5s/10s/15s 退避，等 CDN 同步
                    continue
                r.raise_for_status()
                arr = np.frombuffer(r.content, np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is None:
                    raise RuntimeError("结果图下载后解码失败")
                return img
            except RuntimeError:
                raise
            except Exception as e:
                last = e
                if i < attempts - 1:
                    time.sleep(5 * (i + 1))
        raise RuntimeError("结果图下载失败（%s）: %s" % (url[:120], last))

    # ---- Chat Completions 模式 ----
    def _chat_completions(self, base_url, api_key, model, image_bgr, prompt):
        url = self._endpoint(base_url, "/chat/completions")
        data_url = "data:image/png;base64," + self._encode_b64(image_bgr)
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        }
        headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
        resp = requests.post(url, json=payload, headers=headers, timeout=300)
        if resp.status_code != 200:
            raise RuntimeError(
                "自定义模型调用失败: HTTP %s %s" % (resp.status_code, resp.text[:300])
            )
        data = resp.json()
        return self._find_image(data)

    # ---- Images Edits 模式 ----
    def _images_edits(self, base_url, api_key, model, image_bgr, mask_bin, prompt):
        url = self._endpoint(base_url, "/images/edits")
        ok1, buf1 = cv2.imencode(".png", image_bgr)
        if not ok1:
            raise RuntimeError("原图编码失败")
        # OpenAI edits 约定：mask 的「透明区域」表示需要编辑。
        # 我们构造 RGBA：水印区域 alpha=0（透明，需编辑），其余 alpha=255（保留）。
        alpha = 255 - ((mask_bin > 0).astype(np.uint8) * 255)
        rgba = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2BGRA)
        rgba[:, :, 3] = alpha
        ok2, buf2 = cv2.imencode(".png", rgba)
        if not ok2:
            raise RuntimeError("mask 编码失败")

        files = {
            "image": ("image.png", buf1.tobytes(), "image/png"),
            "mask": ("mask.png", buf2.tobytes(), "image/png"),
            "prompt": (None, prompt),
            "model": (None, model),
            "n": (None, "1"),
        }
        headers = {"Authorization": "Bearer " + api_key}
        resp = requests.post(url, headers=headers, files=files, timeout=300)
        if resp.status_code != 200:
            raise RuntimeError(
                "自定义模型调用失败: HTTP %s %s" % (resp.status_code, resp.text[:300])
            )
        data = resp.json()
        return self._find_image(data)

    @staticmethod
    def _endpoint(base_url, suffix):
        if base_url.endswith(suffix):
            return base_url
        return base_url + suffix

    # ---- Images Generations 模式（APIMart / nano-banana 等，支持异步任务轮询）----
    def _images_generations(self, base_url, api_key, model, image_bgr, prompt):
        url = self._endpoint(base_url, "/images/generations")
        # JPEG 编码控制请求体积
        data_url = "data:image/jpeg;base64," + self._encode_b64(
            image_bgr, ext=".jpg", params=[cv2.IMWRITE_JPEG_QUALITY, 95]
        )
        # APIMart（apib.ai/apimart.ai）规范：参考图放 image_urls 数组
        # （URL 与 base64 data URI 可混用，传入即自动切换图生图模式）；
        # 同时带 image 字段兼容其他中转站，多余字段按官方文档会被忽略。
        # 不传 size：图生图时输出分辨率默认与输入图一致（官方文档说明）。
        payload = {
            "model": model,
            "prompt": prompt,
            "image_urls": [data_url],
            "image": data_url,
            "n": 1,
        }
        headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
        resp = requests.post(url, json=payload, headers=headers, timeout=300)
        if resp.status_code != 200:
            raise RuntimeError(
                "自定义模型调用失败: HTTP %s %s" % (resp.status_code, resp.text[:300])
            )
        data = resp.json()

        # 情况1：同步直接出图
        img = self._find_image(data)
        if img is not None:
            return img

        # 情况2：异步任务（APIMart 等）—— 提取 task_id 轮询
        task_id = self._find_task_id(data)
        if not task_id:
            raise RuntimeError(
                "图像接口返回里既没有结果图也没有任务ID: %s" % str(data)[:300]
            )
        return self._poll_task(base_url, api_key, task_id)

    @staticmethod
    def _find_task_id(data):
        """从各种常见结构里找任务ID：task_id / id / request_id（含嵌套 data/output，
        data 可能是数组——APIMart 提交响应为 {"code":200,"data":[{"task_id":...}]}）。"""
        if not isinstance(data, dict):
            return None
        for key in ("task_id", "id", "request_id"):
            v = data.get(key)
            if isinstance(v, str) and v:
                return v
        for nest in ("data", "output", "task"):
            sub = data.get(nest)
            if isinstance(sub, list):
                sub = next((it for it in sub if isinstance(it, dict)), None)
            if isinstance(sub, dict):
                for key in ("task_id", "id", "request_id"):
                    v = sub.get(key)
                    if isinstance(v, str) and v:
                        return v
        return None

    @staticmethod
    def _find_status(data):
        """从响应里找任务状态字段（status / state），返回小写字符串或 None。"""
        def walk(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k.lower() in ("status", "state") and isinstance(v, str):
                        yield v.lower()
                    else:
                        yield from walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    yield from walk(v)
        for s in walk(data):
            return s
        return None

    def _poll_task(self, base_url, api_key, task_id, timeout=300, interval=3):
        url = self._endpoint(base_url, "/tasks/") + task_id
        headers = {"Authorization": "Bearer " + api_key}
        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            try:
                r = requests.get(url, headers=headers, timeout=60)
                if r.status_code == 200:
                    data = r.json()
                    status = self._find_status(data)
                    if status in _TASK_FAILED:
                        raise RuntimeError("图像任务执行失败: %s" % str(data)[:300])
                    img = self._find_image(data)
                    if img is not None:
                        return img
                    if status in _TASK_SUCCESS:
                        dl_hint = ""
                        if getattr(self, "_last_download_err", None):
                            dl_hint = "（结果链接下载失败: %s）" % self._last_download_err
                        raise RuntimeError(
                            "任务显示成功但响应里没有结果图%s: %s" % (dl_hint, str(data)[:300])
                        )
                    # 否则仍在排队/处理中，继续等
                else:
                    last_err = "HTTP %s %s" % (r.status_code, r.text[:200])
            except RuntimeError:
                raise
            except Exception as e:  # 网络抖动等，重试
                last_err = str(e)
            time.sleep(interval)
        raise RuntimeError(
            "图像任务轮询超时（%ds，task_id=%s）%s"
            % (timeout, task_id, ("，最后错误: " + last_err) if last_err else "")
        )

    # ---- 结果图片解析（覆盖多种返回结构）----
    _URL_KEYS = ("url", "urls", "image", "image_url", "img", "imgurl", "img_url")

    def _find_image(self, data):
        data_urls, link_urls, raw_b64 = [], [], []

        def walk(obj, key=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    walk(v, str(k).lower())
            elif isinstance(obj, list):
                for v in obj:
                    walk(v, key)
            elif isinstance(obj, str):
                if re.search(r"data:image/[a-zA-Z0-9.+-]+;base64,", obj):
                    data_urls.append(obj)
                elif key in CustomEngine._URL_KEYS and obj.startswith("http"):
                    # 位于 url 类字段下的链接直接采信——APIMart 结果链接
                    # 形如 https://getapib.org/f/image/<hash>，没有扩展名
                    link_urls.append(obj)
                elif re.search(r"https?://[^\s\"']+?\.(?:png|jpe?g|webp)", obj, re.I):
                    m = re.search(r"https?://[^\s\"']+?\.(?:png|jpe?g|webp)", obj, re.I)
                    link_urls.append(m.group(0))
                else:
                    t = obj.strip()
                    if len(t) > 200 and re.fullmatch(r"[A-Za-z0-9+/=]+", t):
                        raw_b64.append(t)

        walk(data)

        # 1) data URL：data:image/...;base64,xxx
        for s in data_urls:
            m = re.search(r"data:image/[a-zA-Z0-9.+-]+;base64,([A-Za-z0-9+/=]+)", s)
            if m:
                try:
                    return self._decode_b64(m.group(1))
                except Exception:
                    continue
        # 2) 图片直链（url 类字段优先，其次带扩展名的链接）
        for u in link_urls:
            try:
                return self._download(u)
            except Exception as e:
                self._last_download_err = "%s -> %s" % (u[:120], str(e)[:200])
                continue
        # 3) 裸 base64 串（b64_json 等字段，长度较大）
        for t in raw_b64:
            try:
                return self._decode_b64(t)
            except Exception:
                continue
        return None
