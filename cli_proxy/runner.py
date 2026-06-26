"""向后兼容层：run_cli() 和 antigravity_model_variant() 的原始入口。

内部实现已迁移到 cli_proxy/providers/；这个文件保留是因为：
  - llm_backends.py 从这里导入 run_cli
  - lesson_sdk/ 里的示例可能直接导入
"""
from __future__ import annotations

from .providers import SUPPORTED, get_provider
from .providers.antigravity import antigravity_model_variant  # noqa: F401（重导出供兼容调用）

__all__ = ["SUPPORTED", "run_cli", "antigravity_model_variant"]


def run_cli(
    backend: str,
    text: str,
    model: str = "",
    effort: str = "",
    *,
    env_override: dict[str, str] | None = None,
) -> str:
    """执行指定 CLI 后端，返回纯文本回答。

    线程安全：env_override 通过 subprocess env 参数注入，不修改 os.environ。
    """
    provider = get_provider(backend)  # 未知 backend 时抛 RuntimeError
    return provider.run(text, model, effort, env_override=env_override)
