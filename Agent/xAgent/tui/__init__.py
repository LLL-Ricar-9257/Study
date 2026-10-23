"""xAgent TUI 表现层包。

业务能力全部位于 core；这里只负责终端交互。
"""

__all__ = ["FileAgentApp"]


def __getattr__(name: str):
    if name == "FileAgentApp":
        from .app import FileAgentApp

        return FileAgentApp
    raise AttributeError(name)
