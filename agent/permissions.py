"""Permission modes shared by the runtime, tools, and terminal UI."""

from enum import Enum


class PermissionMode(str, Enum):
    ASK = "ask"
    APPROVE = "approve"
    FULL = "full"
