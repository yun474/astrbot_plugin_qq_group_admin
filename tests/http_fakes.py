"""Exercise the real SDK response parser without sending network requests."""

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, Mock

from botpy.http import BotHttp


def make_http(*, status=200, data=None, error=None, sandbox=False, empty=False):
    response = NS(
        status=status,
        headers={} if empty else {"content-type": "application/json"},
        request_info=NS(url="https://example.invalid"),
        json=AsyncMock(return_value=data),
        text=AsyncMock(return_value=""),
    )
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=response, side_effect=error)
    manager.__aexit__ = AsyncMock(return_value=False)
    http = BotHttp(timeout=5, is_sandbox=sandbox)
    http.check_session = AsyncMock()
    http._headers = {"Authorization": "test-token"}
    http._session = NS(request=Mock(return_value=manager), closed=True)
    return http
