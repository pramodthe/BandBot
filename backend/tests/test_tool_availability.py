import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.providers.contracts import ProviderError
from app.providers.models import ProviderBlock
from app.persistence.models import ChannelMember, db
from app.runtime.availability import OPTIONAL_SERVICES, OptionalServicesMiddleware, capability_note
from test_tasks import ScriptedModel, job, run_attempt


@pytest.mark.parametrize('service', list(OPTIONAL_SERVICES))
def test_disabled_optional_services_return_actionable_tool_results(service):
    request = SimpleNamespace(tool_call={'id': 'call-1', 'name': 'optional_tool'})
    handler = AsyncMock(side_effect=ProviderError(service, 'not_configured'))
    result = asyncio.run(OptionalServicesMiddleware().awrap_tool_call(request, handler))
    assert isinstance(result, ToolMessage) and result.status == 'error'
    data = json.loads(result.content)
    assert data['service'] == service and data['code'] == 'not_configured'
    assert data['retryable'] is False and 'Providers & APIs' in data['error']
    assert result.tool_call_id == 'call-1'
    handler.assert_awaited_once_with(request)


@pytest.mark.parametrize('service,code', [
    ('llm', 'not_configured'), ('computer', 'authentication'), ('computer', 'revoked'),
    ('computer', 'unavailable'), ('images', 'uncertain'), ('research', 'quota_or_rate_limit'),
])
def test_required_model_and_configured_service_failures_still_require_recovery(service, code):
    error = ProviderError(service, code)
    handler = AsyncMock(side_effect=error)
    with pytest.raises(ProviderError) as failure:
        asyncio.run(OptionalServicesMiddleware().awrap_tool_call(
            SimpleNamespace(tool_call={'id': 'call-1', 'name': 'tool'}), handler))
    assert failure.value is error
    assert handler.await_count == 1


@pytest.mark.parametrize('helper,tool,args', [
    (None, 'run_python', {'code': 'print(1)'}),
    ('coder', 'run_python', {'code': 'print(1)'}),
    ('image', 'generate_image', {'prompt': 'A requested test illustration'}),
])
def test_real_harness_and_helpers_continue_when_optional_execution_is_disabled(tmp_path, job, helper, tool, args):
    task, _, _ = job
    with db() as session:
        session.add(ChannelMember(channel_id=task.channel_id, member_type='agent', member_id=task.agent_id))
        session.commit()
    tool_call = AIMessage(content='', tool_calls=[{'name': tool, 'args': args, 'id': 'missing-service'}])
    no_export = 'Execution is not configured. I can provide report text, but no PDF has been created.'

    class AvailabilityModel(ScriptedModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if self.index == (2 if helper else 1):
                results = [m for m in messages if isinstance(m, ToolMessage)]
                assert results and json.loads(results[-1].content)['code'] == 'not_configured'
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    responses = [tool_call, AIMessage(content=no_export)]
    if helper:
        responses = [AIMessage(content='', tool_calls=[{'name': 'task', 'id': 'delegate-1',
            'args': {'subagent_type': helper, 'description': 'Use the requested optional service, then report its actual availability.'}}]),
            *responses, AIMessage(content=no_export)]
    saved = run_attempt(tmp_path, task, AvailabilityModel(responses=responses))
    assert saved.status == 'completed' and saved.result == no_export
    with db() as session:
        assert session.get(ProviderBlock, task.id) is None


def test_capability_guidance_does_not_promise_disabled_computers_or_exports():
    note = capability_note({'computer': {'disabled': True}, 'images': {'disabled': True}}, hosted_execution=False)
    assert 'Cloud computers' in note and 'Image generation' in note
    assert 'hosted code execution is not enabled' in note
    assert 'never invent a download link' in note


def test_optional_service_handling_never_suppresses_permission_denials():
    handler = AsyncMock(side_effect=ValueError('The agent no longer has access'))
    with pytest.raises(ValueError, match='no longer has access'):
        asyncio.run(OptionalServicesMiddleware().awrap_tool_call(
            SimpleNamespace(tool_call={'id': 'call-1', 'name': 'generate_image'}), handler))
