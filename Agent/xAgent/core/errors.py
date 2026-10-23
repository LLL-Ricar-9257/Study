"""xAgent 的领域异常。

异常只表达“发生了什么”，不包含任何界面文案或终端输出；
由表现层决定如何展示。
"""

from __future__ import annotations


class XAgentError(Exception):
    """项目内所有可预期异常的基类。"""


class ConfigurationError(XAgentError):
    """配置文件、环境变量或启动参数不合法。"""


class WorkspaceError(XAgentError):
    """工作区不可用。"""


class ToolError(XAgentError):
    """工具执行失败，错误信息会原样返回给模型。"""


class WorkspaceBoundaryError(ToolError):
    """请求的路径越过了工作区边界。"""


class AgentBusyError(XAgentError):
    """同一个 Agent 实例正在处理上一轮请求。"""


class ModelCallError(XAgentError):
    """模型调用失败。"""
