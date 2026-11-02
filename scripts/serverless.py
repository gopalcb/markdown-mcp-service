"""Deploy the parallel MCP Lambda through GitHub and switch the existing edge explicitly.

The original application and edge stacks retain Cognito, OAuth state and ECS rollback.
This module never deletes a stack or service. Acceptance evidence and a saved edge
origin are required before a production switch.
"""

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import deploy
from botocore.exceptions import ClientError

STACK = 'markdown-mcp-service-serverless'
PREVIEW_STACK = 'markdown-mcp-service-preview'
FUNCTION = 'markdown-mcp-service'
CHECKS = (
    'adapter_stream_envelope',
    'oauth_cookies_query_origin',
    'private_oauth_refresh_revoke',
    'ready_receipt_readback',
    'slow_storage_uncertain_write',
    'cold_warm_latency',
    'rollback_demonstrated',
)


def outputs(state: dict) -> dict:
    """Extract only nonsecret CloudFormation output values from a complete stack."""
    if not state or state['StackStatus'] not in {'CREATE_COMPLETE', 'UPDATE_COMPLETE'}:
        raise ValueError('Required stack must be complete')
    return {item['OutputKey']: item['OutputValue'] for item in state.get('Outputs', [])}


def apply_stack(session, name: str, template: str, parameters: dict) -> dict:
    """Issue one stack update and use the existing two-minute completion checks."""
    client = session.client('cloudformation')
    state = deploy.stack_state(client, name)
    if state and state['StackStatus'] not in {'CREATE_COMPLETE', 'UPDATE_COMPLETE'}:
        raise ValueError('Existing stack operation must complete before another write')
    request = {
        'StackName': name,
        'TemplateBody': template,
        'Parameters': [
            {'ParameterKey': key, 'ParameterValue': str(value)} for key, value in parameters.items()
        ],
        'Capabilities': ['CAPABILITY_NAMED_IAM'],
        'Tags': [{'Key': 'Project', 'Value': name}],
    }
    try:
        if state:
            client.update_stack(**request)
        else:
            client.create_stack(**request)
    except ClientError as error:
        if state and 'No updates are to be performed' in str(error):
            return outputs(state)
        raise
    return deploy.wait_stack(client, name)


def check_concurrency(session, requested: int) -> None:
    """Fail before mutation if the region cannot reserve the bounded function budget."""
    client = session.client('lambda')
    settings = client.get_account_settings()
    available = settings['AccountLimit']['UnreservedConcurrentExecutions']
    existing = 0
    for function in (FUNCTION, FUNCTION + '-preview'):
        try:
            existing += client.get_function_concurrency(FunctionName=function).get(
                'ReservedConcurrentExecutions', 0
            )
        except client.exceptions.ResourceNotFoundException:
            pass
    if available + existing - requested * 2 < 100:
        raise ValueError(
            'Lambda regional quota cannot reserve concurrency while retaining 100 unreserved; request quota increase through the approved account process'
        )


def verify_image_access(session) -> None:
    """Require pre-applied exact-function ECR access without changing production resources."""
    try:
        policy = json.loads(
            session.client('ecr').get_repository_policy(repositoryName=FUNCTION)['policyText']
        )
    except ClientError as error:
        if error.response['Error']['Code'] == 'RepositoryPolicyNotFoundException':
            raise ValueError(
                'Apply the reviewed ECR Lambda pull policy through the existing infrastructure CI before preparation'
            ) from None
        raise
    required = {
        f'arn:aws:lambda:{deploy.REGION}:{deploy.ACCOUNT}:function:{name}'
        for name in (FUNCTION, FUNCTION + '-preview')
    }
    allowed = set()
    for statement in policy.get('Statement', []):
        if (
            statement.get('Effect') == 'Allow'
            and statement.get('Principal') == {'Service': 'lambda.amazonaws.com'}
            and {'ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer'}
            <= set(statement.get('Action', []))
        ):
            values = statement.get('Condition', {}).get('ArnLike', {}).get('aws:SourceArn', [])
            allowed.update([values] if isinstance(values, str) else values)
    if not required <= allowed:
        raise ValueError(
            'Existing ECR policy must allow the exact production and preview Lambda functions before preparation'
        )


def deploy_lambda(session, image: str, concurrency: int) -> dict:
    """Deploy an immutable Lambda canary without changing the production edge origin."""
    match = re.fullmatch(
        r'249766001220\.dkr\.ecr\.ca-central-1\.amazonaws\.com/markdown-mcp-service@sha256:([a-f0-9]{64})',
        image,
    )
    if not match:
        raise ValueError('Expected the dedicated repository immutable image digest')
    check_concurrency(session, concurrency)
    verify_image_access(session)
    cf = session.client('cloudformation')
    app = outputs(deploy.stack_state(cf))
    edge = outputs(deploy.stack_state(cf, deploy.EDGE_STACK))
    base = edge['PublicBaseUrl']
    if not re.fullmatch(r'https://[a-z0-9]+\.cloudfront\.net', base):
        raise ValueError('Expected the existing owned CloudFront origin')
    if app.get('OAuthConfigured') != 'true':
        raise ValueError('Existing OAuth callback must already be configured')
    subject = deploy.verify_storage_and_owner(session)
    template = (deploy.ROOT / 'aws-infrastructure/serverless.yaml').read_text()
    # Version and deployment are immutable snapshots. Their logical IDs must change
    # when a new image/configuration is released, not only their descriptions.
    release = hashlib.sha256(
        (image + template + subject + json.dumps(app, sort_keys=True)).encode()
    ).hexdigest()
    for logical in ('Version', 'Deployment'):
        template = re.sub(
            r'(?m)^  ' + logical + r':$', '  ' + logical + release[:24] + ':', template
        )
        template = template.replace(
            '!Ref ' + logical + '\n', '!Ref ' + logical + release[:24] + '\n'
        )
        template = template.replace(
            '!GetAtt ' + logical + '.', '!GetAtt ' + logical + release[:24] + '.'
        )
    parameters = {
        'ImageUri': image,
        'ReleaseId': release,
        'PublicBaseUrl': base,
        'ClientId': app['ClientId'],
        'AllowedSubject': subject,
        'OAuthSecretArn': app['OAuthSecretArn'],
        'ReservedConcurrency': concurrency,
    }
    result = apply_stack(
        session,
        STACK,
        template,
        parameters
        | {
            'DeploymentMode': 'production',
            'RuntimeRoleArn': 'arn:aws:iam::249766001220:role/markdown-mcp-service-lambda-role',
        },
    )
    preview = apply_stack(
        session,
        PREVIEW_STACK,
        template,
        parameters
        | {
            'DeploymentMode': 'preview',
            'RuntimeRoleArn': result['RuntimeRoleArn'],
        },
    )
    if preview['PublicBaseUrl'] == base or preview['ClientId'] == app['ClientId']:
        raise ValueError('Preview must use an isolated CloudFront origin and Cognito client')
    deploy.verify_endpoint(preview['PublicBaseUrl'])
    return result | {
        'image': image,
        'release_id': release,
        'public_base_url': base,
        'production_cutover': False,
        'preview': preview_binding(preview),
    }


def preview_binding(state: dict) -> dict:
    """Bind private preview evidence to the exact URL, client, version and image."""
    return {
        key: state[key]
        for key in (
            'PublicBaseUrl',
            'ClientId',
            'Version',
            'ImageUri',
            'ReleaseId',
            'PreviewDistributionId',
        )
    }


def validate_acceptance(evidence: dict, release: str, preview: dict) -> None:
    """Require complete, recent, revision-bound evidence before an origin switch."""
    if evidence.get('release_id') != release or evidence.get('checks') != dict.fromkeys(
        CHECKS, True
    ):
        raise ValueError('All required acceptance checks must pass for this exact release')
    if evidence.get('preview') != preview or preview.get('ReleaseId') != release:
        raise ValueError('Acceptance must bind the exact isolated preview configuration')
    observed = datetime.fromisoformat(evidence.get('observed_at', ''))
    age = (datetime.now(timezone.utc) - observed).total_seconds()
    if age < 0 or age > 86400 or not evidence.get('evidence_path'):
        raise ValueError('Acceptance requires evidence references observed within 24 hours')


def edge_origin(session) -> dict:
    """Read the exact origin/path saved in the owned CloudFormation stack parameters."""
    state = deploy.stack_state(session.client('cloudformation'), deploy.EDGE_STACK)
    edge = outputs(state)
    values = {item['ParameterKey']: item['ParameterValue'] for item in state['Parameters']}
    return {
        'OriginHostname': values['OriginHostname'],
        'OriginPath': values.get('OriginPath', ''),
        'DistributionId': edge['DistributionId'],
    }


def switch_origin(session, target: dict) -> dict:
    """Switch only the existing edge stack, accepting exact regional ECS/API targets."""
    host = target['OriginHostname']
    path = target.get('OriginPath', '')
    if not (
        (re.fullmatch(r'[a-z0-9-]+\.ecs\.ca-central-1\.on\.aws', host) and path == '')
        or (
            re.fullmatch(r'[a-z0-9]+\.execute-api\.ca-central-1\.amazonaws\.com', host)
            and path == '/live'
        )
    ):
        raise ValueError('Expected exact regional ECS rollback or REST API origin')
    current = edge_origin(session)
    if current['DistributionId'] != target['DistributionId']:
        raise ValueError('Rollback/cutover must target the same existing distribution')
    result = apply_stack(
        session,
        deploy.EDGE_STACK,
        (deploy.ROOT / 'aws-infrastructure/edge.yaml').read_text(),
        {'OriginHostname': host, 'OriginPath': path},
    )
    deploy.verify_endpoint(result['PublicBaseUrl'])
    return result | {'origin': 'https://' + host, 'origin_path': path}


def main() -> None:
    """Run an explicitly selected CI stage and persist nonsecret revision evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['deploy', 'cutover', 'rollback'])
    parser.add_argument('--image')
    parser.add_argument('--concurrency', type=int, choices=range(1, 11), default=2)
    parser.add_argument('--release', default='/tmp/markdown-mcp-serverless.json')
    parser.add_argument('--acceptance')
    parser.add_argument('--rollback', default='/tmp/markdown-mcp-edge-rollback.json')
    parser.add_argument('--result', default='/tmp/markdown-mcp-serverless-result.json')
    args = parser.parse_args()
    session = deploy.github_session()
    if args.action == 'deploy':
        result = deploy_lambda(session, args.image or '', args.concurrency)
        Path(args.release).write_text(json.dumps(result, indent=2) + '\n')
    elif args.action == 'cutover':
        if not args.acceptance:
            parser.error('--acceptance is required for cutover')
        release = json.loads(Path(args.release).read_text())
        current_release = outputs(deploy.stack_state(session.client('cloudformation'), STACK))
        if any(
            current_release[key] != release[artifact_key]
            for key, artifact_key in (
                ('ReleaseId', 'release_id'),
                ('ImageUri', 'image'),
                ('Origin', 'Origin'),
            )
        ):
            raise ValueError('Saved release no longer matches the deployed canary stack')
        preview = preview_binding(
            outputs(deploy.stack_state(session.client('cloudformation'), PREVIEW_STACK))
        )
        if preview != release['preview'] or preview['ImageUri'] != release['image']:
            raise ValueError(
                'Preview acceptance no longer matches the deployed image and configuration'
            )
        validate_acceptance(
            json.loads(Path(args.acceptance).read_text()), release['release_id'], preview
        )
        current = edge_origin(session)
        Path(args.rollback).write_text(json.dumps(current, indent=2) + '\n')
        result = switch_origin(
            session,
            {
                'OriginHostname': urlsplit(release['Origin']).hostname,
                'OriginPath': '/live',
                'DistributionId': current['DistributionId'],
            },
        )
    else:
        result = switch_origin(session, json.loads(Path(args.rollback).read_text()))
    Path(args.result).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
