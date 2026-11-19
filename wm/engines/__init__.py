"""模型引擎注册表（仅云端引擎）。

本地 OpenCV / LaMa 引擎已下线：它们依赖「水印样例涂抹定位」，
且 LaMa 需额外 208MB ONNX 模型与本地算力；并入图案提取工作台后
统一走云端整图去水印，线上零新依赖、无需 GPU。
"""

from .wanx_engine import WanxEngine
from .qwen_engine import QwenEngine
from .modelscope_engine import ModelScopeEngine
from .custom_engine import CustomEngine
from .lensapi_engine import LensApiEngine

# 顺序 = 前端下拉顺序；lensapi（图案提取同款）放在首位即默认引擎
ENGINES = {
    "lensapi": LensApiEngine(),
    "modelscope": ModelScopeEngine(),
    "wanx": WanxEngine(),
    "qwen": QwenEngine(),
    "custom": CustomEngine(),
}


# 已下线的本地引擎名（历史前端缓存可能仍会传）→ 统一拒绝并提示
RETIRED = {"lama", "opencv"}


def get_engine(name):
    """取引擎；未知名字兜底为 lensapi（图案提取同款，默认推荐）。"""
    return ENGINES.get(name, ENGINES["lensapi"])


def list_engines():
    return [
        {"name": e.name, "label": e.label, "needs_key": getattr(e, "needs_key", False)}
        for e in ENGINES.values()
    ]
