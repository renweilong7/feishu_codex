from __future__ import annotations

from .config import Settings
from .feishu import FeishuClient
from .shell_commands import ShellCommandHandler
from .tasks import TaskManager


settings = Settings.from_env()
settings.validate()

feishu = FeishuClient(settings.feishu_app_id, settings.feishu_app_secret)
tasks = TaskManager(settings, feishu)
shell_commands = ShellCommandHandler(settings)
