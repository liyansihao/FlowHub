import sys,signal
from pathlib import Path
import httpx
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'vendor/compareBot/src'))
from comparebot.application.services.screen_product import qwen_failure_reason


@pytest.mark.parametrize('status,code,expected',[(400,'Arrearage','qwen_billing_blocked'),(401,'InvalidApiKey','qwen_authentication_blocked'),(429,'Throttling','qwen_rate_limited'),(500,'Unknown','qwen_request_failed')])
def test_qwen_dependency_failure_is_not_a_product_rejection(status,code,expected):
    response=httpx.Response(status,json={'error':{'code':code,'message':'private diagnostic'}},request=httpx.Request('POST','https://example.com'))
    error=httpx.HTTPStatusError('private error',request=response.request,response=response)
    assert qwen_failure_reason(error)==expected


def test_permission_error_cleaning_old_group_does_not_stop_supervisor(monkeypatch):
    from flowhub.control import terminate_group
    def failed(*args):raise PermissionError('old group')
    monkeypatch.setattr('flowhub.control.os.killpg',failed)
    assert terminate_group(123,signal.SIGTERM) is False
