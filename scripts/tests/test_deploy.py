"""Check deployment guards, additive stack parameters and exact rollout matching."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock, patch

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


def test_provision_preserves_existing_callback_and_uses_caller_role():
    """The additive prerequisite grant is sufficient without a new provisioner role."""
    session = Mock()
    client = session.client.return_value
    client.describe_stacks.return_value = {'Stacks': [{'StackStatus': 'UPDATE_COMPLETE'}]}
    with (
        patch.object(deploy, 'verify_existing_cluster'),
        patch.object(deploy, 'wait_stack', return_value={'ClientId': 'client'}),
    ):
        deploy.provision(session, None)
    request = client.update_stack.call_args.kwargs
    assert request['Parameters'] == [{'ParameterKey': 'CallbackUrl', 'UsePreviousValue': True}]
    assert 'RoleARN' not in request
    assert request['StackName'] == 'markdown-mcp-service'


def test_provision_refuses_concurrent_infrastructure_change():
    """Do not issue another update while the owned stack is changing."""
    session = Mock()
    client = session.client.return_value
    client.describe_stacks.return_value = {'Stacks': [{'StackStatus': 'UPDATE_IN_PROGRESS'}]}
    with patch.object(deploy, 'verify_existing_cluster'):
        with pytest.raises(RuntimeError, match='must finish'):
            deploy.provision(session, None)
    client.update_stack.assert_not_called()


def test_invalid_callback_rejected_before_aws_write():
    """A callback cannot be an insecure or fragment-bearing guessed address."""
    session = Mock()
    with pytest.raises(ValueError, match='HTTPS callback'):
        deploy.provision(session, 'http://example.org/callback')
    session.client.assert_not_called()


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
