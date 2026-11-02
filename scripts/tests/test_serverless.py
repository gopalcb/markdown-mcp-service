"""Protect serverless migration boundaries, revision evidence and finite slow writes."""

import asyncio
import importlib.util
import json
import sys
import threading
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
from test_mcp import CONFIG

from mcp_services import storage_service
from mcp_services.validation_service import UploadBatch, load_config

SPEC = importlib.util.spec_from_file_location('mcp_serverless', ROOT / 'scripts/serverless.py')
serverless = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(serverless)


def test_streaming_stack_preserves_existing_identity_and_bounds_ingress() -> None:
    """Use one streaming Function URL with bounded capacity and no recreated identity/data."""
    loader = type('TemplateLoader', (yaml.SafeLoader,), {})
    loader.add_multi_constructor('!', lambda loader, tag, node: None)
    template = yaml.load((ROOT / 'aws-infrastructure/serverless.yaml').read_text(), Loader=loader)
    resources = template['Resources']
    assert not any(
        resource['Type'].startswith(
            (
                'AWS::ECS::',
                'AWS::S3::',
                'AWS::DynamoDB::',
                'AWS::SecretsManager::',
            )
        )
        for resource in resources.values()
    )
    assert sum(resource['Type'] == 'AWS::Lambda::Function' for resource in resources.values()) == 1
    assert not any(name.startswith('Preview') for name in resources)
    assert not any(
        resource['Type'].startswith('AWS::ApiGateway::') for resource in resources.values()
    )
    assert resources['FunctionUrl']['Properties'] == {
        'TargetFunctionArn': None,
        'Qualifier': 'live',
        'AuthType': 'NONE',
        'InvokeMode': 'RESPONSE_STREAM',
    }
    assert resources['FunctionUrlPermission']['Properties']['Action'] == 'lambda:InvokeFunctionUrl'
    assert resources['FunctionInvokePermission']['Properties']['InvokedViaFunctionUrl'] is True
    assert resources['Function']['Properties']['Timeout'] == 180
    assert template['Parameters']['ReservedConcurrency']['MaxValue'] == 10
    assert template['Parameters']['ReservedConcurrency']['MinValue'] == 10
    assert resources['Function']['Properties']['ReservedConcurrentExecutions'] is None
    assert resources['Alias']['Properties']['Name'] == 'live'
    assert 'AWS_LWA_INVOKE_MODE=response_stream' in (ROOT / 'Dockerfile.lambda').read_text()
    assert 'AWS_LWA_REMOVE_BASE_PATH' not in (ROOT / 'Dockerfile.lambda').read_text()


def test_edge_switch_rejects_foreign_host_and_distribution() -> None:
    """Only the exact saved distribution and regional service origins can be used."""
    session = Mock()
    with pytest.raises(ValueError, match='regional'):
        serverless.switch_origin(
            session,
            {
                'OriginHostname': 'attacker.example',
                'OriginPath': '/live',
                'DistributionId': 'existing',
            },
        )
    with patch.object(serverless, 'edge_origin', return_value={'DistributionId': 'existing'}):
        with pytest.raises(ValueError, match='same existing'):
            serverless.switch_origin(
                session,
                {
                    'OriginHostname': 'fixture.execute-api.ca-central-1.amazonaws.com',
                    'OriginPath': '/live',
                    'DistributionId': 'foreign',
                },
            )


@pytest.mark.parametrize('regional,unreserved,existing', [(110, 110, 0), (1100, 100, 10)])
def test_concurrency_selection_never_exceeds_ten(
    regional: int, unreserved: int, existing: int
) -> None:
    """Use an exact reservation only when AWS can retain its required shared pool."""
    session = Mock()
    client = session.client.return_value
    client.get_account_settings.return_value = {
        'AccountLimit': {
            'ConcurrentExecutions': regional,
            'UnreservedConcurrentExecutions': unreserved,
        }
    }
    client.get_function_concurrency.return_value = {'ReservedConcurrentExecutions': existing}
    assert serverless.select_concurrency(session) == 10


@pytest.mark.parametrize('regional,unreserved', [(10, 10), (50, 50), (110, 99)])
def test_concurrency_selection_rejects_an_unenforceable_limit(
    regional: int, unreserved: int
) -> None:
    """Do not deploy until an exact persistent reservation is possible."""
    session = Mock()
    client = session.client.return_value
    client.get_account_settings.return_value = {
        'AccountLimit': {
            'ConcurrentExecutions': regional,
            'UnreservedConcurrentExecutions': unreserved,
        }
    }
    client.get_function_concurrency.return_value = {}
    with pytest.raises(ValueError, match='reserve exactly ten'):
        serverless.select_concurrency(session)


def test_api_origin_validation_is_literal_and_regional(monkeypatch) -> None:
    """An API origin Host is allowed without admitting foreign hosts or forwarded identity."""
    for key, value in CONFIG.items():
        if key != 'enabled':
            monkeypatch.setenv(key, value)
    monkeypatch.setenv('MCP_ENABLED', 'true')
    monkeypatch.setenv('MCP_ORIGIN_HOST', 'fixture.execute-api.ca-central-1.amazonaws.com')
    assert load_config()['MCP_ORIGIN_HOST'].startswith('fixture.execute-api.')
    monkeypatch.setenv('MCP_ORIGIN_HOST', 'fixture.lambda-url.ca-central-1.on.aws')
    assert load_config()['MCP_ORIGIN_HOST'].startswith('fixture.lambda-url.')
    for host in (
        'fixture.execute-api.us-east-1.amazonaws.com',
        'fixture.execute-api.ca-central-1.amazonaws.com.attacker.example',
        'https://fixture.execute-api.ca-central-1.amazonaws.com',
    ):
        monkeypatch.setenv('MCP_ORIGIN_HOST', host)
        with pytest.raises(ValueError, match='regional'):
            load_config()


def test_cancelled_tool_may_finish_write_and_requires_reconciliation(monkeypatch) -> None:
    """Prove cancellation cannot undo a running storage thread or confirm its receipt."""
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    def slow_write(payload: dict, config: dict) -> list:
        """Hold a representative in-flight storage acknowledgement past cancellation."""
        started.set()
        assert release.wait(3)
        completed.set()
        return [{'file_id': 'stable-file-id', 'status': 'READY'}]

    async def cancel_request() -> None:
        """Cancel the requester while preserving bounded cleanup of its storage worker."""
        task = asyncio.create_task(
            storage_service.save_batch(
                UploadBatch(files=[{'name': 'slow.md', 'content': '# slow'}]), {}
            )
        )
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not completed.is_set()
        finally:
            release.set()
        assert await asyncio.to_thread(completed.wait, 2)

    monkeypatch.setattr(storage_service, 'write_documents', slow_write)
    asyncio.run(cancel_request())


def test_delivery_policy_remains_within_inline_limit() -> None:
    """Keep IAM snapshot parity and the documented aggregate inline-policy size bound."""
    policy = json.loads((ROOT / 'aws-infrastructure/deployer-policy.json').read_text())
    delivery = yaml.safe_load((ROOT / 'aws-infrastructure/delivery.yaml').read_text())
    assert (
        delivery['Resources']['DeploymentRole']['Properties']['Policies'][0]['PolicyDocument']
        == policy
    )
    assert len(json.dumps(policy, separators=(',', ':'))) < 10240
    source = (ROOT / 'scripts/serverless.py').read_text()
    assert 'delete_stack(' not in source
    assert 'delete_express_gateway_service(' not in source


def test_deploys_one_lambda_without_mutating_production_edge() -> None:
    """Deploy one configuration and verify its direct origin before edge cutover."""
    session = Mock()
    app = {'ClientId': 'productionclient', 'OAuthConfigured': 'true', 'OAuthSecretArn': 'secret'}
    edge = {'PublicBaseUrl': 'https://production.cloudfront.net'}
    image = (
        '249766001220.dkr.ecr.ca-central-1.amazonaws.com/markdown-mcp-service@sha256:' + 'a' * 64
    )
    production = {
        'RuntimeRoleArn': 'arn:aws:iam::249766001220:role/markdown-mcp-service-lambda-role',
        'Origin': 'https://production.execute-api.ca-central-1.amazonaws.com',
        'OriginPath': '/live',
    }
    with (
        patch.object(serverless, 'select_concurrency', return_value=10),
        patch.object(serverless, 'verify_image_access'),
        patch.object(serverless.deploy, 'stack_state'),
        patch.object(
            serverless,
            'outputs',
            side_effect=[
                app,
                edge,
                {'Origin': 'https://production.execute-api.ca-central-1.amazonaws.com'},
            ],
        ),
        patch.object(serverless.deploy, 'verify_storage_and_owner', return_value='owner'),
        patch.object(
            serverless,
            'apply_stack',
            side_effect=[
                production
                | {
                    'Origin': 'https://fixture.lambda-url.ca-central-1.on.aws/',
                    'OriginPath': '',
                },
                production
                | {
                    'Origin': 'https://fixture.lambda-url.ca-central-1.on.aws/',
                    'OriginPath': '',
                },
            ],
        ) as apply,
        patch.object(serverless.deploy, 'verify_endpoint') as verify,
    ):
        result = serverless.deploy_lambda(session, image)
    assert apply.call_args.args[1] == serverless.STACK
    assert apply.call_count == 2
    parameters = apply.call_args.args[3]
    assert parameters['ImageUri'] == image
    assert parameters['ClientId'] == 'productionclient'
    assert 'DeploymentMode' not in parameters
    assert parameters['ReservedConcurrency'] == 10
    assert parameters['OriginHostname'] == 'fixture.lambda-url.ca-central-1.on.aws'
    assert result['production_cutover'] is False
    assert result['concurrency_cap'] == 10
    verify.assert_called_once_with(
        'https://fixture.lambda-url.ca-central-1.on.aws',
        'https://production.cloudfront.net',
    )


def test_ecr_preflight_requires_only_the_production_lambda() -> None:
    """Deployment reads one production-function prerequisite without editing it."""
    session = Mock()
    statement = {
        'Effect': 'Allow',
        'Principal': {'Service': 'lambda.amazonaws.com'},
        'Action': ['ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer'],
        'Condition': {
            'ArnLike': {
                'aws:SourceArn': (
                    'arn:aws:lambda:ca-central-1:249766001220:function:markdown-mcp-service'
                )
            }
        },
    }
    session.client.return_value.get_repository_policy.return_value = {
        'policyText': json.dumps({'Statement': [statement]})
    }
    serverless.verify_image_access(session)
    statement['Condition']['ArnLike']['aws:SourceArn'] = (
        'arn:aws:lambda:ca-central-1:249766001220:function:other'
    )
    session.client.return_value.get_repository_policy.return_value = {
        'policyText': json.dumps({'Statement': [statement]})
    }
    with pytest.raises(ValueError, match='production Lambda'):
        serverless.verify_image_access(session)
    session.client.return_value.set_repository_policy.assert_not_called()
