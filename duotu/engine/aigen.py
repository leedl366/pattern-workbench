# -*- coding: utf-8 -*-
"""
apib.ai 图像引擎客户端（gpt-image-2 等）

已实测的调用链路（2026-10-03）：
  提交  POST https://api.apib.ai/v1/images/generations
        {"model":"gpt-image-2","prompt":..,"n":1,"size":"1024x1024","image":[data_uri,...]}
        -> {"code":200,"data":[{"status":"submitted","task_id":"task_xxx"}]}
  轮询  GET  https://api.apib.ai/v1/tasks/<task_id>
        -> data.status: processing -> success/completed
        -> data.result.images[0].url[0]  公网图片地址（有有效期）
  注意  gpt-image-2 走 /v1/chat/completions 会报 model_price_error，必须走 images/generations。
"""
import os, io, json, time, base64, urllib.request, urllib.error, traceback
from PIL import Image

DEFAULT_BASE = "https://api.apib.ai/v1"
DEFAULT_MODEL = "gpt-image-2"
DEFAULT_VISION = "gpt-5.4"

IMAGE_HINT = ("image-generation", "image", "vision")


class ApibError(Exception):
    pass


def _http(method, url, body=None, headers=None, key=None, timeout=240):
    req = urllib.request.Request(url, method=method)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def data_uri(path, maxside=1024):
    im = Image.open(path)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im)
        im = bg.convert("RGB")
    else:
        im = im.convert("RGB")
    im.thumbnail((maxside, maxside), Image.Resampling.LANCZOS)
    b = io.BytesIO()
    im.save(b, "PNG")
    return "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()


class Client:
    def __init__(self, api_key, base=DEFAULT_BASE, model=DEFAULT_MODEL, vision=DEFAULT_VISION):
        self.key = api_key
        self.base = base.rstrip("/")
        self.model = model
        self.vision = vision

    # ---------- 模型列表 ----------
    def list_models(self, only_image=True):
        st, raw = _http("GET", f"{self.base}/models", key=self.key, timeout=60)
        if st != 200:
            raise ApibError(f"models http{st}: {raw[:200]!r}")
        d = json.loads(raw.decode("utf-8", "ignore"))
        ids = [m["id"] for m in d.get("data", [])]
        if not only_image:
            return ids
        kw = ("image", "imagen", "flux", "seedream", "seedance", "grok-imagine",
              "midjourney", "qwen-image", "recraft", "ideogram", "z-image", "wan2")
        return [i for i in ids if any(k in i.lower() for k in kw)]

    # ---------- 出图 ----------
    def submit(self, prompt, images=None, size="1024x1024", n=1, quality=None, extra=None):
        body = {"model": self.model, "prompt": prompt, "n": int(n)}
        if size:
            body["size"] = size
        if quality:
            body["quality"] = quality
        if images:
            body["image"] = [images] if isinstance(images, str) else list(images)
        if extra:
            body.update(extra)
        st, raw = _http("POST", f"{self.base}/images/generations", body,
                        {"Content-Type": "application/json"}, key=self.key)
        txt = raw.decode("utf-8", "ignore")
        if st != 200:
            raise ApibError(f"submit http{st}: {txt[:400]}")
        try:
            d = json.loads(txt)
            if isinstance(d.get("data"), list) and d["data"] and d["data"][0].get("task_id"):
                return d["data"][0]["task_id"], d
            if d.get("data") and isinstance(d["data"], dict) and d["data"].get("task_id"):
                return d["data"]["task_id"], d
            # 同步返回（含 b64 或 url）
            return None, d
        except Exception:
            raise ApibError("无法解析提交返回: " + txt[:400])

    def poll(self, task_id, timeout=300, interval=4):
        t0 = time.time()
        last = None
        while time.time() - t0 < timeout:
            st, raw = _http("GET", f"{self.base}/tasks/{task_id}", key=self.key, timeout=60)
            txt = raw.decode("utf-8", "ignore")
            if st != 200:
                raise ApibError(f"poll http{st}: {txt[:300]}")
            try:
                d = json.loads(txt)["data"]
            except Exception:
                raise ApibError("无法解析任务返回: " + txt[:300])
            last = d
            s = (d.get("status") or "").lower()
            if s in ("success", "completed", "succeeded"):
                return d
            if s in ("failed", "error", "cancelled", "canceled"):
                raise ApibError("任务失败: " + json.dumps(d.get("result") or d.get("error") or d,
                                                        ensure_ascii=False)[:400])
            time.sleep(interval)
        raise ApibError(f"任务超时({timeout}s)，最后状态: {json.dumps(last, ensure_ascii=False)[:200]}")

    @staticmethod
    def _urls(task_data):
        res = task_data.get("result") or {}
        out = []
        for im in res.get("images", []) or []:
            u = im.get("url")
            if isinstance(u, list):
                out += u
            elif u:
                out.append(u)
            if im.get("b64_json"):
                out.append(("b64", im["b64_json"]))
        return out

    def generate(self, prompt, images=None, out_dir=".", name="AI", size="1024x1024",
                 n=1, quality=None, save=True, timeout=300):
        """返回 ([{path, url, remote}], task_data)"""
        tid, first = self.submit(prompt, images, size=size, n=n, quality=quality)
        if tid is None:
            data = first
        else:
            data = self.poll(tid, timeout=timeout)
        urls = self._urls(data)
        items = []
        os.makedirs(out_dir, exist_ok=True)
        for i, u in enumerate(urls):
            fn = f"{name}_{i}.png" if len(urls) > 1 else f"{name}.png"
            fp = os.path.join(out_dir, fn) if save else None
            if isinstance(u, tuple) and u[0] == "b64":
                raw = base64.b64decode(u[1])
                if save:
                    open(fp, "wb").write(raw)
                items.append(dict(path=fp, url=None, remote=None, raw=len(raw)))
                continue
            st, raw = _http("GET", u, timeout=180)
            if st != 200:
                raise ApibError(f"下载图片失败 http{st}")
            if save:
                open(fp, "wb").write(raw)
            items.append(dict(path=fp, url=u, remote=u, raw=len(raw)))
        if not items:
            raise ApibError("没有返回图片: " + json.dumps(data, ensure_ascii=False)[:400])
        return items, data

    # ---------- 视觉问答（自检 / 看图） ----------
    def describe(self, image, question, model=None, max_tokens=700):
        """image 可以是本地路径或公网 URL"""
        if os.path.exists(image):
            uri = data_uri(image, 1024)
        else:
            uri = image
        body = {"model": model or self.vision, "stream": False,
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": uri}},
                    {"type": "text", "text": question}]}],
                "max_tokens": max_tokens}
        st, raw = _http("POST", f"{self.base}/chat/completions", body,
                        {"Content-Type": "application/json"}, key=self.key, timeout=180)
        txt = raw.decode("utf-8", "ignore")
        if st != 200:
            return f"[视觉模型 http{st}] {txt[:300]}"
        try:
            d = json.loads(txt)
            return d["choices"][0]["message"]["content"]
        except Exception:
            return txt[:800]


# ---------------- 提示词构建 ----------------
ROLE_CN = {"material": "材质/质感来源", "pattern": "图案来源", "target": "被编辑的目标底图",
           "shape": "最终形态底图", "ref": "仅参考", "auto": "未指定角色"}
ROLE_EN = {"material": "MATERIAL reference (source of color and surface finish)",
           "pattern": "PATTERN source (the artwork to transfer)",
           "target": "TARGET image (the one to be edited)",
           "shape": "SHAPE reference (defines the final silhouette)",
           "ref": "reference only, do not copy its layout",
           "auto": "role unspecified"}


def build_fuse_prompt(user_prompt, slots, roles, acts=None, plan=None, extra=""):
    """把中文需求 + 图位角色 + 方案约束 → 结构化出图提示词（中英双语，gpt-image 系更吃英文）"""
    acts = acts or []
    lines = []
    role_lines = []
    en_role_lines = []
    for i, s in enumerate(slots):
        if not s.get("path"):
            continue
        r = (roles[i] if roles and i < len(roles) else s.get("role", "auto"))
        role_lines.append(f"- 图{i+1} = {ROLE_CN.get(r, '未指定角色')}")
        en_role_lines.append(f"- Image {i+1} = {ROLE_EN.get(r, 'role unspecified')}")
    con = []
    if "material_replace" in acts:
        con.append("- 只改壳体材质/颜色/质感，不改造型、不改构图、不换机型")
    if "pattern_transfer" in acts:
        con.append("- 把图案源的图案完整贴到壳背，图案造型与配色保持原样，不要变形、不要只贴局部")
    else:
        con.append("- 卡通图案的形状、配色、相对位置必须与目标底图完全一致，一笔不改")
    if "clean_cam" in acts:
        con.append("- 摄像头模组区域必须干净，不得有卡通图案覆盖，保留镜头开孔与镜片")
    if "remove_base" in acts:
        con.append("- 去掉底部的托架/底座/托盘，只保留主体本身")
    if "clean_bg" in acts:
        con.append("- 背景纯净（白底或纯色影棚背景）")
    con.append("- 产品摄影质感，边缘清晰，不要文字、不要水印、不要多余物体")

    lines.append("【任务】" + (user_prompt or "").strip())
    if role_lines:
        lines.append("【输入图角色】\n" + "\n".join(role_lines))
    if con:
        lines.append("【硬约束】\n" + "\n".join(con))
    if plan:
        lines.append("【执行步骤】\n" + "\n".join(f"{s['no']}. {s['title']}：{s['detail']}" for s in plan))
    if extra:
        lines.append("【补充】" + extra)

    en = []
    en.append("TASK: " + (user_prompt or "").strip())
    if role_lines:
        en.append("INPUT IMAGES (in order):\n" + "\n".join(en_role_lines))
    en.append("HARD CONSTRAINTS:")
    en.append("- Keep the original product silhouette, camera position and overall composition of the TARGET image.")
    en.append("- Recolor/refinish ONLY the case surface material to match the MATERIAL reference image.")
    if "pattern_transfer" in acts:
        en.append("- Transfer the pattern from the PATTERN image onto the case, full coverage, no distortion.")
    else:
        en.append("- The cartoon artwork must stay pixel-faithful: identical shapes, colors and positions as in the target image.")
    if "clean_cam" in acts:
        en.append("- Camera module area must be clean: no artwork on it, keep lens holes and glass highlights.")
    if "remove_base" in acts:
        en.append("- Remove any stand/base/holder at the bottom; show only the product itself.")
    en.append("- Clean studio product photo, pure white background, sharp edges, no text, no watermark, no extra objects.")
    lines.append("【English restatement for the image model】\n" + "\n".join(en))
    return "\n\n".join(lines)
