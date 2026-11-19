"""配置读写：模型引擎 + 水印区域 + 输入/输出路径。

注意：配置文件固定放在本模块同目录（app/wm/config.json），
不与父应用（图案提取工作台）共用，保证两个版块配置物理隔离。
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "engine": "lensapi",
    "lensapi": {
        "base_url": "https://lensapi.cn",
        "api_key": "",          # 留空 = 自动读 app/.apib_key（与图案提取工作台同一把）
        "model": "gpt-image-2.5",
        "prompt_template": "去除图片中的所有水印、文字、logo 和标识，用周围背景自然无缝填充，"
                           "保持其余画面完全不变（构图、元素位置、颜色、大小、清晰度一律不动），输出完整图片。",
    },
    "modelscope": {
        "base_url": "https://api-inference.modelscope.cn",
        "api_key": "",
        "model": "Qwen/Qwen-Image-Edit-2511",
        "reference_image": "",
        "prompt_template": "请去除图片中的水印文字，用周围背景自然无缝填充，其余画面保持不变。",
    },
    "wanx": {
        "base_url": "",
        "api_key": "",
        "model": "wanx2.1-imageedit",
        "function": "remove_watermark",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
        "api_key": "",
        "model": "qwen-image-edit",
        "prompt_template": "请去除图片中的水印文字，用周围背景自然无缝填充，其余画面保持不变。",
    },
    "custom": {
        "base_url": "",
        "api_key": "",
        "model": "",
        "mode": "images_gen",
        "prompt_template": "请去除图片中的水印文字，用周围背景自然无缝填充，其余画面保持不变。",
    },
    "region": {"type": "rect", "x": 0, "y": 0, "w": 0, "h": 0, "points": []},
    "template": {"threshold": 0.65, "brush_width": 24},
    "paths": {"input": "", "output": ""},
}


def _deep_merge(base, override):
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            _deep_merge(cfg, saved)
        except Exception:
            pass
    return cfg


def save_config(cfg):
    # 只落盘关心的键，避免前端回传的临时字段污染配置
    keep = ("engine", "lensapi", "modelscope", "wanx", "qwen", "custom", "region", "template", "paths")
    out = {k: cfg.get(k) for k in keep if k in cfg}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
