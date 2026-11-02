"""Protect serverless migration boundaries, revision evidence and finite slow writes."""

import asyncio
import importlib.util
import json
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
from test_mcp import CONFIG, JWKS, app_module
from test_oauth import CHALLENGE, REDIRECT, SIGNING_KEY, FakeDynamo, register

from mcp_services import oauth_service, storage_service
from mcp_services.oauth_service import CognitoVerifier, create_oauth
from mcp_services.oauth_storage import OAuthStateStore
from mcp_services.validation_service import UploadBatch, load_config

SPEC = importlib.util.spec_from_file_location('mcp_serverless', ROOT / 'scripts/serverless.py')
serverless = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(serverless)


def test_streaming_stack_preserves_existing_identity_and_bounds_ingress() -> None:
    """Use Regional streaming REST with bounded capacity and no recreated identity/data."""
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
    assert resources['PreviewClient']['Condition'] == 'IsPreview'
    assert resources['PreviewBranding']['Condition'] == 'IsPreview'
    assert resources['PreviewDistribution']['Condition'] == 'IsPreview'
    assert resources['Role']['Condition'] == 'IsProduction'
    assert resources['Api']['Properties']['EndpointConfiguration'] == {'Types': ['REGIONAL']}
    for name in ('RootMethod', 'ProxyMethod'):
        integration = resources[name]['Properties']['Integration']
        assert integration['Type'] == 'AWS_PROXY'
        assert integration['ResponseTransferMode'] == 'STREAM'
        assert integration['TimeoutInMillis'] == 180000
    settings = resources['Stage']['Properties']['MethodSettings'][0]
    assert settings['ThrottlingBurstLimit'] == 60
    assert settings['ThrottlingRateLimit'] == 1
    assert settings['DataTraceEnabled'] is False
    assert resources['Function']['Properties']['Timeout'] == 180
    assert resources['Alias']['Properties']['Name'] == 'live'
    assert (
        'response-streaming-invocations'
        in (ROOT / 'aws-infrastructure/serverless.yaml').read_text()
    )
    assert 'AWS_LWA_INVOKE_MODE=response_stream' in (ROOT / 'Dockerfile.lambda').read_text()


def test_acceptance_is_complete_recent_and_revision_bound() -> None:
    """Neither a health check nor stale evidence can approve production cutover."""
    evidence = {
        'release_id': 'release',
        'checks': dict.fromkeys(serverless.CHECKS, True),
        'observed_at': datetime.now(timezone.utc).isoformat(),
        'evidence_path': 'reviewed-evidence.md',
        'preview': {'ReleaseId': 'release', 'PublicBaseUrl': 'https://preview.cloudfront.net'},
    }
    serverless.validate_acceptance(evidence, 'release', evidence['preview'])
    for invalid in (
        evidence | {'release_id': 'other'},
        evidence | {'checks': {'health': True}},
        evidence | {'observed_at': '2020-01-01T00:00:00+00:00'},
        evidence
        | {
            'preview': {
                'ReleaseId': 'release',
                'PublicBaseUrl': 'https://production.cloudfront.net',
            }
        },
    ):
        with pytest.raises(ValueError):
            serverless.validate_acceptance(invalid, 'release', evidence['preview'])


def test_insufficient_concurrency_fails_without_mutation() -> None:
    """The regional minimum unreserved budget must survive the requested reservation."""
    session = Mock()
    client = session.client.return_value
    client.get_account_settings.return_value = {
        'AccountLimit': {'UnreservedConcurrentExecutions': 10}
    }
    client.get_function_concurrency.return_value = {}
    with pytest.raises(ValueError, match='quota'):
        serverless.check_concurrency(session, 2)
    client.create_function.assert_not_called()


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


def test_api_origin_validation_is_literal_and_regional(monkeypatch) -> None:
    """An API origin Host is allowed without admitting foreign hosts or forwarded identity."""
    for key, value in CONFIG.items():
        if key != 'enabled':
            monkeypatch.setenv(key, value)
    monkeypatch.setenv('MCP_ENABLED', 'true')
    monkeypatch.setenv('MCP_ORIGIN_HOST', 'fixture.execute-api.ca-central-1.amazonaws.com')
    assert load_config()['MCP_ORIGIN_HOST'].startswith('fixture.execute-api.')
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


def test_prepare_deploys_isolated_preview_without_mutating_production_edge() -> None:
    """Prepare two configurations with one image while preserving production identity."""
    session = Mock()
    app = {'ClientId': 'productionclient', 'OAuthConfigured': 'true', 'OAuthSecretArn': 'secret'}
    edge = {'PublicBaseUrl': 'https://production.cloudfront.net'}
    image = (
        '249766001220.dkr.ecr.ca-central-1.amazonaws.com/markdown-mcp-service@sha256:' + 'a' * 64
    )
    production = {
        'RuntimeRoleArn': 'arn:aws:iam::249766001220:role/markdown-mcp-service-lambda-role',
        'Origin': 'https://production.execute-api.ca-central-1.amazonaws.com',
    }
    preview = {
        'PublicBaseUrl': 'https://preview.cloudfront.net',
        'ClientId': 'previewclient',
        'Version': '2',
        'ImageUri': image,
        'ReleaseId': 'derived',
        'PreviewDistributionId': 'previewdistribution',
    }
    with (
        patch.object(serverless, 'check_concurrency'),
        patch.object(serverless, 'verify_image_access'),
        patch.object(serverless.deploy, 'stack_state'),
        patch.object(serverless, 'outputs', side_effect=[app, edge]),
        patch.object(serverless.deploy, 'verify_storage_and_owner', return_value='owner'),
        patch.object(serverless, 'apply_stack', side_effect=[production, preview]) as apply,
        patch.object(serverless.deploy, 'verify_endpoint') as verify,
    ):
        result = serverless.deploy_lambda(session, image, 2)
    assert [call.args[1] for call in apply.call_args_list] == [
        serverless.STACK,
        serverless.PREVIEW_STACK,
    ]
    first, second = [call.args[3] for call in apply.call_args_list]
    assert first['DeploymentMode'] == 'production'
    assert second['DeploymentMode'] == 'preview'
    assert first['ImageUri'] == second['ImageUri'] == image
    assert first['ReleaseId'] == second['ReleaseId']
    assert first['ClientId'] == second['ClientId'] == 'productionclient'
    assert second['RuntimeRoleArn'] == production['RuntimeRoleArn']
    assert result['preview']['PublicBaseUrl'] == preview['PublicBaseUrl']
    assert result['production_cutover'] is False
    verify.assert_called_once_with(preview['PublicBaseUrl'])


def test_preview_oauth_state_and_keys_are_isolated_from_production() -> None:
    """Preview registrations, redemption and signing cannot share production sessions."""
    production = OAuthStateStore(Mock(), 'shared')
    preview = OAuthStateStore(Mock(), 'shared', 'preview')
    assert production.item_key('clients', 'same-client')['collection'] == {'S': 'clients'}
    assert preview.item_key('clients', 'same-client')['collection'] == {'S': 'preview:clients'}
    assert production.item_key('redemption-code', 'grant') != preview.item_key(
        'redemption-code', 'grant'
    )
    key = 'fixture-persistent-key-material-' * 2
    with patch.object(oauth_service, 'MarkdownOAuthProxy') as proxy:
        oauth_service.create_oauth(CONFIG, production, key, Mock())
        production_key = proxy.call_args.kwargs['jwt_signing_key']
        oauth_service.create_oauth(
            CONFIG | {'OAUTH_STATE_NAMESPACE': 'preview'}, preview, key, Mock()
        )
        preview_key = proxy.call_args.kwargs['jwt_signing_key']
    assert production_key == key
    assert preview_key != key
    assert len(preview_key) == 64


def test_preview_dcr_and_consent_bind_only_preview_client_and_callback() -> None:
    """Exercise preview consent through HTTP and reject its registration in production."""
    database = FakeDynamo()
    preview_config = CONFIG | {
        'PUBLIC_BASE_URL': 'https://preview.cloudfront.net',
        'MCP_RESOURCE': 'https://preview.cloudfront.net/mcp/',
        'COGNITO_CLIENT_ID': 'previewclient',
        'OAUTH_STATE_NAMESPACE': 'preview',
    }
    preview_oauth = create_oauth(
        preview_config,
        OAuthStateStore(database, 'shared', 'preview'),
        SIGNING_KEY,
        CognitoVerifier(preview_config, JWKS),
    )
    production_oauth = create_oauth(
        CONFIG, OAuthStateStore(database, 'shared'), SIGNING_KEY, CognitoVerifier(CONFIG, JWKS)
    )
    with (
        TestClient(
            app_module.create_app(preview_config, oauth=preview_oauth),
            base_url=preview_config['PUBLIC_BASE_URL'],
        ) as preview_client,
        TestClient(
            app_module.create_app(CONFIG, oauth=production_oauth),
            base_url=CONFIG['PUBLIC_BASE_URL'],
        ) as production_client,
    ):
        registered = register(preview_client)
        params = {
            'response_type': 'code',
            'client_id': registered['client_id'],
            'redirect_uri': REDIRECT,
            'scope': 'markdown-api/write',
            'state': 'fixture',
            'code_challenge': CHALLENGE,
            'code_challenge_method': 'S256',
            'resource': preview_config['MCP_RESOURCE'],
        }
        authorize = preview_client.get('/authorize', params=params, follow_redirects=False)
        assert authorize.status_code == 302
        page = preview_client.get(authorize.headers['location'])
        fields = dict(re.findall(r'name="(txn_id|csrf_token)" value="([^"]+)"', page.text))
        consent = preview_client.post(
            '/consent',
            data=fields | {'action': 'approve', 'submit': 'true'},
            headers={'Origin': preview_config['PUBLIC_BASE_URL']},
            follow_redirects=False,
        )
        assert consent.status_code == 302
        query = parse_qs(urlsplit(consent.headers['location']).query)
        assert query['client_id'] == ['previewclient']
        assert query['redirect_uri'] == ['https://preview.cloudfront.net/auth/callback']
        rejected = production_client.get(
            '/authorize',
            params=params | {'resource': CONFIG['MCP_RESOURCE']},
            follow_redirects=False,
        )
        assert rejected.status_code == 400


def test_ecr_preflight_requires_both_exact_lambda_functions() -> None:
    """Preparation reads its prerequisite without editing the production stack or client."""
    session = Mock()
    statement = {
        'Effect': 'Allow',
        'Principal': {'Service': 'lambda.amazonaws.com'},
        'Action': ['ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer'],
        'Condition': {
            'ArnLike': {
                'aws:SourceArn': [
                    f'arn:aws:lambda:ca-central-1:249766001220:function:{name}'
                    for name in ('markdown-mcp-service', 'markdown-mcp-service-preview')
                ]
            }
        },
    }
    session.client.return_value.get_repository_policy.return_value = {
        'policyText': json.dumps({'Statement': [statement]})
    }
    serverless.verify_image_access(session)
    statement['Condition']['ArnLike']['aws:SourceArn'].pop()
    session.client.return_value.get_repository_policy.return_value = {
        'policyText': json.dumps({'Statement': [statement]})
    }
    with pytest.raises(ValueError, match='production and preview'):
        serverless.verify_image_access(session)
    session.client.return_value.set_repository_policy.assert_not_called()
