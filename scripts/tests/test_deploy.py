"""Check deployment guards, additive stack parameters and exact rollout matching."""

import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import pytest
import yaml

SPEC = importlib.util.spec_from_file_location('mcp_deploy', Path(__file__).parents[1] / 'deploy.py')
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


def test_local_deployment_is_rejected(monkeypatch):
    """A valid AWS identity alone must never enable local infrastructure writes."""
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    session = Mock()
    session.client.return_value.get_caller_identity.return_value = {
        'Account': deploy.ACCOUNT,
        'Arn': f'arn:aws:sts::{deploy.ACCOUNT}:assumed-role/markdown-mcp-service-github-role/test',
    }
    with patch.object(deploy.boto3, 'Session', return_value=session):
        with pytest.raises(ValueError, match='GitHub CI role'):
            deploy.github_session()


def test_provision_derives_owned_callback_and_uses_caller_role():
    """The fixed upstream callback comes from the owned edge, without a guessed host callback."""
    session = Mock()
    client = session.client.return_value
    client.describe_stacks.side_effect = [
        {'Stacks': [{'StackStatus': 'UPDATE_COMPLETE'}]},
        {
            'Stacks': [
                {
                    'StackStatus': 'UPDATE_COMPLETE',
                    'Outputs': [
                        {
                            'OutputKey': 'PublicBaseUrl',
                            'OutputValue': 'https://fixture.cloudfront.net',
                        }
                    ],
                }
            ]
        },
    ]
    with (
        patch.object(deploy, 'verify_existing_cluster'),
        patch.object(deploy, 'wait_stack', return_value={'ClientId': 'client'}),
    ):
        deploy.provision(session)
    request = client.update_stack.call_args.kwargs
    assert request['Parameters'] == [
        {
            'ParameterKey': 'CallbackUrl',
            'ParameterValue': 'https://fixture.cloudfront.net/auth/callback',
        }
    ]
    assert 'RoleARN' not in request
    assert request['StackName'] == 'markdown-mcp-service'


def test_provision_refuses_concurrent_infrastructure_change():
    """Do not issue another update while the owned stack is changing."""
    session = Mock()
    client = session.client.return_value
    client.describe_stacks.return_value = {'Stacks': [{'StackStatus': 'UPDATE_IN_PROGRESS'}]}
    with patch.object(deploy, 'verify_existing_cluster'):
        with pytest.raises(RuntimeError, match='must finish'):
            deploy.provision(session)
    client.update_stack.assert_not_called()


def test_invalid_owned_edge_rejected_before_aws_write():
    """An invalid edge output must never register an attacker callback."""
    session = Mock()
    client = session.client.return_value
    with (
        patch.object(deploy, 'verify_existing_cluster'),
        patch.object(
            deploy,
            'stack_state',
            side_effect=[
                None,
                {
                    'StackStatus': 'UPDATE_COMPLETE',
                    'Outputs': [
                        {'OutputKey': 'PublicBaseUrl', 'OutputValue': 'https://attacker.example'}
                    ],
                },
            ],
        ),
    ):
        with pytest.raises(ValueError, match='CloudFront origin'):
            deploy.provision(session)
    client.create_stack.assert_not_called()


def test_first_provision_keeps_oauth_disabled_until_edge_exists():
    """A fresh install creates state/key resources without registering a guessed callback."""
    session = Mock()
    with (
        patch.object(deploy, 'verify_existing_cluster'),
        patch.object(deploy, 'stack_state', return_value=None),
        patch.object(deploy, 'wait_stack', return_value={}),
    ):
        deploy.provision(session)
    request = session.client.return_value.create_stack.call_args.kwargs
    assert request['Parameters'] == [{'ParameterKey': 'CallbackUrl', 'ParameterValue': ''}]


def test_rollout_requires_successful_exact_environment():
    """A prior health-only revision with the same image cannot pass activation."""
    ecs = Mock()
    container = {'image': 'image', 'environment': [{'name': 'MCP_ENABLED', 'value': 'true'}]}
    bootstrap = {
        'serviceRevisionArn': 'old',
        'primaryContainer': {
            'image': 'image',
            'environment': [{'name': 'MCP_ENABLED', 'value': 'false'}],
        },
    }
    enabled = {'serviceRevisionArn': 'new', 'primaryContainer': container}
    ecs.describe_express_gateway_service.side_effect = [
        {'service': {'status': {'statusCode': 'ACTIVE'}, 'activeConfigurations': [bootstrap]}},
        {'service': {'status': {'statusCode': 'ACTIVE'}, 'activeConfigurations': [enabled]}},
    ]
    ecs.list_service_deployments.return_value = {
        'serviceDeployments': [
            {'targetServiceRevisionArn': 'old', 'status': 'SUCCESSFUL', 'createdAt': 1},
            {'targetServiceRevisionArn': 'new', 'status': 'SUCCESSFUL', 'createdAt': 2},
        ]
    }
    with patch.object(deploy.time, 'sleep') as sleep:
        result = deploy.wait_service(ecs, container)
    assert result['activeConfigurations'] == [enabled]
    assert sleep.call_count == 2
    assert all(call.args == (120,) for call in sleep.call_args_list)


def test_deploy_requires_dedicated_immutable_image():
    """Tags or another service's digest cannot become this service's revision."""
    session = Mock()
    with pytest.raises(ValueError, match='immutable digest'):
        deploy.deploy(session, 'public-image:latest', {})
    session.client.assert_not_called()


def test_missing_cluster_fails_before_any_creation():
    """The standalone project must never create a cluster when the existing one is missing."""
    session = Mock()
    session.client.return_value.describe_clusters.return_value = {'clusters': [], 'failures': []}
    with pytest.raises(ValueError, match='existing softrobots-systems cluster'):
        deploy.verify_existing_cluster(session)
    session.client.return_value.create_cluster.assert_not_called()


def test_delivery_role_policy_is_complete_and_has_no_self_management():
    """Independent delivery owns only its service and cannot create clusters or edit its identity."""
    root = Path(__file__).resolve().parents[2]
    template = yaml.safe_load((root / 'aws-infrastructure/delivery.yaml').read_text())
    role = template['Resources']['DeploymentRole']['Properties']
    policy = json.loads((root / 'aws-infrastructure/deployer-policy.json').read_text())
    assert role['Policies'][0]['PolicyDocument'] == policy
    assert len(json.dumps(policy, separators=(',', ':'))) < 10240
    trust = role['AssumeRolePolicyDocument']['Statement']
    assert len(trust) == 1
    assert trust[0]['Condition']['StringEquals']['token.actions.githubusercontent.com:sub'] == (
        'repo:gopalcb@13314049/markdown-mcp-service@1439586244:ref:refs/heads/main'
    )
    actions = {
        action
        for statement in policy['Statement']
        for action in (
            [statement['Action']] if isinstance(statement['Action'], str) else statement['Action']
        )
    }
    assert 'ecs:CreateCluster' not in actions
    assert 'iam:CreatePolicyVersion' not in actions
    assert {
        'ecs:DescribeClusters',
        'ecs:CreateExpressGatewayService',
        'ecr:GetAuthorizationToken',
        'ecr:GetLifecyclePolicy',
    } <= actions
    resources = [
        value
        for statement in policy['Statement']
        for value in (
            [statement['Resource']]
            if isinstance(statement['Resource'], str)
            else statement['Resource']
        )
    ]
    assert not any('markdown-mcp-service-github-role' in value for value in resources)
    assert (
        'arn:aws:ecs:ca-central-1:249766001220:service/softrobots-systems/markdown-mcp-service'
        in resources
    )
    assert deploy.CLUSTER == 'softrobots-systems'
    assert deploy.SERVICE == 'markdown-mcp-service'


def test_oauth_resources_have_retention_ttl_and_narrow_permissions() -> None:
    """Persist encrypted OAuth state/key while keeping secret values outside deployment access."""
    loader = type('RuntimeLoader', (yaml.SafeLoader,), {})
    loader.add_multi_constructor('!', lambda loader, tag, node: None)
    template = yaml.load(
        (deploy.ROOT / 'aws-infrastructure/template.yaml').read_text(), Loader=loader
    )
    assert template['Resources']['Client']['Properties']['ExplicitAuthFlows'] == [
        'ALLOW_USER_AUTH',
        'ALLOW_REFRESH_TOKEN_AUTH',
    ]
    table = template['Resources']['OAuthState']
    secret = template['Resources']['OAuthKey']
    assert table['DeletionPolicy'] == secret['DeletionPolicy'] == 'Retain'
    assert table['UpdateReplacePolicy'] == secret['UpdateReplacePolicy'] == 'Retain'
    assert table['Properties']['TableName'] == 'markdown-mcp-service-oauth'
    assert table['Properties']['BillingMode'] == 'PAY_PER_REQUEST'
    assert table['Properties']['TimeToLiveSpecification'] == {
        'AttributeName': 'ttl',
        'Enabled': True,
    }
    assert table['Properties']['SSESpecification']['SSEEnabled'] is True
    assert table['Properties']['KeySchema'] == [
        {'AttributeName': 'collection', 'KeyType': 'HASH'},
        {'AttributeName': 'key', 'KeyType': 'RANGE'},
    ]
    assert secret['Properties']['GenerateSecretString']['PasswordLength'] >= 32
    assert 'SecretString' not in secret['Properties']
    assert set(template['Outputs']) >= {'OAuthStateTable', 'OAuthSecretArn'}
    policy = json.loads((deploy.ROOT / 'aws-infrastructure/deployer-policy.json').read_text())
    actions = {
        action
        for statement in policy['Statement']
        for action in (
            [statement['Action']] if isinstance(statement['Action'], str) else statement['Action']
        )
    }
    secret_reads = [
        item for item in policy['Statement'] if item['Action'] == 'secretsmanager:GetSecretValue'
    ]
    assert len(secret_reads) == 1
    assert secret_reads[0]['Resource'] == (
        f'arn:aws:secretsmanager:{deploy.REGION}:{deploy.ACCOUNT}:'
        'secret:markdown-mcp-service-oauth-key-??????'
    )
    assert secret_reads[0]['Condition'] == {
        'ForAnyValue:StringEquals': {'aws:CalledVia': ['cloudformation.amazonaws.com']}
    }
    assert not any(
        'secretsmanager:GetSecretValue' in item['Action']
        for item in policy['Statement']
        if isinstance(item['Action'], list)
    )
    assert 'dynamodb:GetItem' not in actions
    assert not any(statement['Action'] == 'secretsmanager:*' for statement in policy['Statement'])
    generation = next(item for item in policy['Statement'] if item['Sid'] == 'GenerateMcpOAuthKey')
    assert generation['Action'] == 'secretsmanager:GetRandomPassword'
    assert generation['Resource'] == '*'
    assert generation['Condition']['StringEquals']['aws:RequestedRegion'] == deploy.REGION
    state = next(item for item in policy['Statement'] if item['Sid'] == 'ManageOnlyMcpOAuthState')
    assert state['Resource'] == (
        f'arn:aws:dynamodb:{deploy.REGION}:{deploy.ACCOUNT}:table/markdown-mcp-service-oauth'
    )
    assert {
        'dynamodb:DescribeContinuousBackups',
        'dynamodb:DescribeContributorInsights',
        'dynamodb:DescribeKinesisStreamingDestination',
        'dynamodb:GetResourcePolicy',
    } <= set(state['Action'])


def test_public_verification_requires_real_s256_adapter_discovery() -> None:
    """Successful health alone cannot hide missing OAuth discovery or wrong resource binding."""
    base = 'https://fixture.cloudfront.net'
    metadata = {
        'issuer': base + '/',
        'code_challenge_methods_supported': ['S256'],
        'registration_endpoint': base + '/register',
        'authorization_endpoint': base + '/authorize',
        'token_endpoint': base + '/token',
    }
    responses = [
        io.BytesIO(json.dumps(payload).encode())
        for payload in (
            {'status': 'ok'},
            {'resource': base + '/mcp/', 'authorization_servers': [base + '/']},
            metadata,
        )
    ]
    responses.append(
        HTTPError(
            base + '/mcp/',
            401,
            'Unauthorized',
            {'WWW-Authenticate': 'Bearer resource_metadata="x"'},
            None,
        )
    )
    with patch.object(deploy.urllib.request, 'urlopen', side_effect=responses):
        deploy.verify_endpoint(base)
    responses = [
        io.BytesIO(json.dumps(payload).encode())
        for payload in (
            {'status': 'ok'},
            {'resource': base + '/mcp/', 'authorization_servers': [base + '/']},
            metadata | {'code_challenge_methods_supported': []},
        )
    ]
    with patch.object(deploy.urllib.request, 'urlopen', side_effect=responses):
        with pytest.raises(ValueError, match='S256'):
            deploy.verify_endpoint(base)


@pytest.mark.parametrize(
    'repo,role,accepted',
    [
        ('gopalcb/markdown-mcp-service', 'markdown-mcp-service-github-role', True),
        ('gopalcb/gb-systems-and-agents', 'softrobots-gb-cicd-role', False),
        ('gopalcb/markdown-mcp-service', 'softrobots-gb-cicd-role', False),
    ],
)
def test_independent_repository_identity_is_required(monkeypatch, repo, role, accepted):
    """Old Softrobots identity or another repository cannot deploy this standalone service."""
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_REPOSITORY', repo)
    monkeypatch.setenv('GITHUB_REF', 'refs/heads/main')
    session = Mock()
    session.client.return_value.get_caller_identity.return_value = {
        'Account': deploy.ACCOUNT,
        'Arn': f'arn:aws:sts::{deploy.ACCOUNT}:assumed-role/{role}/run',
    }
    with patch.object(deploy.boto3, 'Session', return_value=session):
        if accepted:
            assert deploy.github_session() == session
        else:
            with pytest.raises(ValueError):
                deploy.github_session()


def test_edge_provision_is_bound_to_regional_origin() -> None:
    """Deploy only the owned edge stack with the exact HTTPS Express origin parameter."""
    session = Mock()
    with (
        patch.object(deploy, 'stack_state', return_value=None),
        patch.object(
            deploy, 'wait_stack', return_value={'PublicBaseUrl': 'https://edge.cloudfront.net'}
        ),
    ):
        result = deploy.provision_edge(session, 'https://origin.ecs.ca-central-1.on.aws')
    request = session.client.return_value.create_stack.call_args.kwargs
    assert request['StackName'] == 'markdown-mcp-service-edge'
    assert request['Parameters'] == [
        {'ParameterKey': 'OriginHostname', 'ParameterValue': 'origin.ecs.ca-central-1.on.aws'}
    ]
    assert request['Tags'] == [{'Key': 'Project', 'Value': 'markdown-mcp-service-edge'}]
    assert result['PublicBaseUrl'] == 'https://edge.cloudfront.net'
    for origin in (
        'http://origin.ecs.ca-central-1.on.aws',
        'https://attacker.example',
        'https://origin.ecs.ca-central-1.on.aws/path',
    ):
        with pytest.raises(ValueError):
            deploy.provision_edge(session, origin)


def test_existing_service_switches_to_cloudfront_audience() -> None:
    """Update the existing service once; never publish its origin as the new MCP audience."""
    session = Mock()
    ecs = session.client.return_value
    ecs.list_service_deployments.return_value = {'serviceDeployments': []}
    state = {'status': {'statusCode': 'ACTIVE'}}
    origin = 'https://origin.ecs.ca-central-1.on.aws'
    edge = {'PublicBaseUrl': 'https://fixture.cloudfront.net', 'DistributionId': 'DIST'}
    outputs = {
        'ClientId': 'client',
        'TaskRoleArn': 'task',
        'ExecutionRoleArn': 'execution',
        'OAuthConfigured': 'true',
        'OAuthStateTable': 'oauth-table',
        'OAuthSecretArn': 'secret-arn',
    }
    image = (
        f'{deploy.ACCOUNT}.dkr.ecr.{deploy.REGION}.amazonaws.com/markdown-mcp-service@sha256:'
        + 'a' * 64
    )
    with (
        patch.object(deploy, 'verify_existing_cluster'),
        patch.object(deploy, 'verify_storage_and_owner', return_value='owner'),
        patch.object(deploy, 'public_subnets', return_value=['subnet']),
        patch.object(deploy, 'service_state', return_value=state),
        patch.object(deploy, 'service_url', return_value=origin),
        patch.object(deploy, 'provision_edge', return_value=edge),
        patch.object(deploy, 'wait_service', return_value=state),
        patch.object(deploy, 'verify_endpoint') as verify,
    ):
        result = deploy.deploy(session, image, outputs)
    environment = {
        entry['name']: entry['value']
        for entry in ecs.update_express_gateway_service.call_args.kwargs['primaryContainer'][
            'environment'
        ]
    }
    assert environment['PUBLIC_BASE_URL'] == edge['PublicBaseUrl']
    assert environment['MCP_RESOURCE'] == edge['PublicBaseUrl'] + '/mcp/'
    assert environment['MCP_ORIGIN_HOST'] == 'origin.ecs.ca-central-1.on.aws'
    assert environment['COGNITO_DOMAIN'] == deploy.COGNITO_DOMAIN
    assert environment['OAUTH_STATE_TABLE'] == 'oauth-table'
    assert environment['OAUTH_SECRET_ARN'] == 'secret-arn'
    assert result['endpoint'] == edge['PublicBaseUrl'] + '/mcp/'
    assert result['origin'] == origin
    ecs.update_express_gateway_service.assert_called_once()
    ecs.create_express_gateway_service.assert_not_called()
    verify.assert_called_once_with(edge['PublicBaseUrl'])


def test_edge_keeps_mcp_uncached_and_preserves_authorization_transport() -> None:
    """Pin the security-sensitive CloudFront policies and origin TLS/method configuration."""
    loader = type('EdgeLoader', (yaml.SafeLoader,), {})
    loader.add_multi_constructor('!', lambda loader, tag, node: None)
    template = yaml.load((deploy.ROOT / 'aws-infrastructure/edge.yaml').read_text(), Loader=loader)
    distribution = template['Resources']['Distribution']['Properties']['DistributionConfig']
    behavior = distribution['DefaultCacheBehavior']
    assert behavior['CachePolicyId'] == '4135ea2d-6df8-44a3-9df3-4b5a84be39ad'
    assert behavior['OriginRequestPolicyId'] == 'b689b0a8-53d0-40ab-baf2-68738e2966ac'
    assert set(behavior['AllowedMethods']) == {
        'GET',
        'HEAD',
        'OPTIONS',
        'PUT',
        'PATCH',
        'POST',
        'DELETE',
    }
    assert behavior['ViewerProtocolPolicy'] == 'redirect-to-https'
    origin = distribution['Origins'][0]['CustomOriginConfig']
    assert origin['OriginProtocolPolicy'] == 'https-only'
    assert origin['OriginSSLProtocols'] == ['TLSv1.2']
    assert all(item['ErrorCachingMinTTL'] == 0 for item in distribution['CustomErrorResponses'])
    assert 'Logging' not in distribution
    assert set(template['Resources']) == {'Distribution'}


def test_compact_storage_reads_remain_owner_scoped():
    """Conditional overwrite reads add no bucket listing or cross-owner table access."""
    loader = type('TemplateLoader', (yaml.SafeLoader,), {})
    loader.add_multi_constructor('!', lambda *_: None)
    template = yaml.load(
        (deploy.ROOT / 'aws-infrastructure/template.yaml').read_text(), Loader=loader
    )
    policies = template['Resources']['TaskRole']['Properties']['Policies']
    statements = [
        statement for policy in policies for statement in policy['PolicyDocument']['Statement']
    ]
    storage = [
        statement
        for statement in statements
        if statement['Effect'] == 'Allow'
        and 'agent-work-history--' in str(statement.get('Resource'))
    ]
    assert len(storage) == 1
    assert set(storage[0]['Action']) == {'s3:GetObject', 's3:PutObject'}
    assert (
        storage[0]['Resource']
        == f'arn:aws:s3:::{deploy.BUCKET}/agent-work/raw-markdown/gopalcbala/*'
    )
    index = next(
        statement
        for statement in statements
        if 'softrobots-document-api-user-files' in str(statement.get('Resource'))
    )
    assert set(index['Action']) == {'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'}
    assert index['Condition']['ForAllValues:StringEquals']['dynamodb:LeadingKeys'] == ['gopalcbala']


def test_storage_preflight_does_not_require_versioning():
    """Suspension must not block deployment while encryption, TLS and owner checks remain."""
    session = Mock()
    client = session.client.return_value
    client.get_bucket_encryption.return_value = {
        'ServerSideEncryptionConfiguration': {
            'Rules': [{'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]
        }
    }
    client.get_public_access_block.return_value = {
        'PublicAccessBlockConfiguration': {
            key: True
            for key in (
                'BlockPublicAcls',
                'IgnorePublicAcls',
                'BlockPublicPolicy',
                'RestrictPublicBuckets',
            )
        }
    }
    client.get_bucket_policy.return_value = {
        'Policy': json.dumps(
            {
                'Statement': [
                    {
                        'Effect': 'Deny',
                        'Principal': '*',
                        'Action': 's3:*',
                        'Condition': {'Bool': {'aws:SecureTransport': 'false'}},
                        'Resource': [f'arn:aws:s3:::{deploy.BUCKET}/*'],
                    }
                ]
            }
        )
    }
    client.admin_get_user.return_value = {
        'Username': 'gopalcbala',
        'Enabled': True,
        'UserAttributes': [{'Name': 'sub', 'Value': 'verified-sub'}],
    }
    assert deploy.verify_storage_and_owner(session) == 'verified-sub'
    client.get_bucket_versioning.assert_not_called()
    client.get_bucket_encryption.assert_called_once_with(Bucket=deploy.BUCKET)
    client.get_public_access_block.assert_called_once_with(Bucket=deploy.BUCKET)
