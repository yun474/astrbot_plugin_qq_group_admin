from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_plugin_qq_group_admin import main


def mock_legacy_session_managers(test):
    """模拟旧版会话存储，新版环境也能覆盖旧版分支。"""
    service = AsyncMock(return_value=True)
    plugin = AsyncMock(return_value=True)
    mocks = patch.multiple(
        main,
        create=True,
        SessionServiceManager=SimpleNamespace(is_session_enabled=service),
        SessionPluginManager=SimpleNamespace(is_plugin_enabled_for_session=plugin),
    )
    mocks.start()
    test.addCleanup(mocks.stop)
    return service, plugin


def mock_waking_session(stage):
    """为新流水线提供有效配置，为旧流水线保留会话过滤接口。"""
    stage.ctx.plugin_manager = SimpleNamespace(
        context=SimpleNamespace(get_config=lambda umo: stage.ctx.astrbot_config)
    )
    return patch(
        "astrbot.core.pipeline.waking_check.stage.SessionPluginManager",
        create=True,
        new=SimpleNamespace(
            filter_handlers_by_session=AsyncMock(side_effect=lambda event, handlers: handlers)
        ),
    )
