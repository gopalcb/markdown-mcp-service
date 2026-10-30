"""Provision and deploy only Markdown MCP through the main-branch GitHub role.

CloudFormation owns its image repository, task roles and dedicated OAuth client.
ECS Express owns HTTPS ingress. A health-only first revision obtains its hostname
before the enabled revision binds the exact OAuth audience. OAuth state and key
material persist only in the owned table and secret, never deployment artifacts.
"""

import argparse
import json
import os
import re
import ssl
import time
import urllib.request
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit

import boto3
from botocore.exceptions import ClientError
from botocore.httpsession import get_cert_path

ACCOUNT = '249766001220'
REGION = 'ca-central-1'
STACK = 'markdown-mcp-service'
SERVICE = 'markdown-mcp-service'
CLUSTER = 'softrobots-systems'
POOL = 'ca-central-1_VwnNJYce8'
BUCKET = f'agent-work-history--{ACCOUNT}-{REGION}'
TABLE = 'softrobots-document-api-user-files'
COGNITO_DOMAIN = f'https://softrobots-usernames-{ACCOUNT}.auth.{REGION}.amazoncognito.com'
EDGE_STACK = 'markdown-mcp-service-edge'
SERVICE_ARN = f'arn:aws:ecs:{REGION}:{ACCOUNT}:service/{CLUSTER}/{SERVICE}'
ROOT = Path(__file__).resolve().parents[1]


def github_session() -> boto3.Session:
    """Reject local mutation and unrelated repository, account, branch or identity."""
    session = boto3.Session(region_name=REGION)
    identity = session.client('sts').get_caller_identity()
    if (
        os.environ.get('GITHUB_ACTIONS') != 'true'
        or os.environ.get('GITHUB_REPOSITORY') != 'gopalcb/markdown-mcp-service'
        or os.environ.get('GITHUB_REF') != 'refs/heads/main'
        or identity['Account'] != ACCOUNT
        or not identity['Arn'].startswith(
            f'arn:aws:sts::{ACCOUNT}:assumed-role/markdown-mcp-service-github-role/'
        )
    ):
        raise ValueError('Deployment requires the exact main-branch GitHub CI role')
    return session


def stack_state(client, stack_name: str = STACK) -> dict | None:
    """Read the exact stack, accepting absence only for its explicit missing error."""
    try:
        return client.describe_stacks(StackName=stack_name)['Stacks'][0]
    except ClientError as error:
        if error.response['Error']['Code'] == 'ValidationError' and 'does not exist' in str(error):
            return None
        raise


def wait_stack(client, stack_name: str = STACK) -> dict:
    """Read completion every two minutes; fail on rollback without retrying writes."""
    for _ in range(8):
        time.sleep(120)
        state = stack_state(client, stack_name)
        status = state['StackStatus'] if state else 'MISSING'
        print(f'Infrastructure: {status}', flush=True)
        if status in {'CREATE_COMPLETE', 'UPDATE_COMPLETE'}:
            return {item['OutputKey']: item['OutputValue'] for item in state['Outputs']}
        if not status.endswith('_IN_PROGRESS') or 'ROLLBACK' in status:
            raise RuntimeError(f'Infrastructure failed: {status}; inspect this stack once')
    raise TimeoutError('Infrastructure did not complete; inspect before retrying')


def verify_existing_cluster(session) -> None:
    """Require the existing cluster and never create a replacement cluster."""
    response = session.client('ecs').describe_clusters(clusters=[CLUSTER])
    clusters = response.get('clusters', [])
    if response.get('failures') or len(clusters) != 1:
        raise ValueError('The existing softrobots-systems cluster is required')
    cluster = clusters[0]
    expected = f'arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/{CLUSTER}'
    if cluster.get('clusterArn') != expected or cluster.get('status') != 'ACTIVE':
        raise ValueError('The existing cluster must be ACTIVE in the expected account and region')


def provision(session) -> dict:
    """Apply owned resources and derive the fixed Cognito callback from the edge stack."""
    verify_existing_cluster(session)
    client = session.client('cloudformation')
    state = stack_state(client)
    if state and state['StackStatus'].endswith('_IN_PROGRESS'):
        raise RuntimeError('Existing infrastructure operation must finish before deployment')
    edge = stack_state(client, EDGE_STACK)
    callback = ''
    if edge:
        if edge['StackStatus'] not in {'CREATE_COMPLETE', 'UPDATE_COMPLETE'}:
            raise RuntimeError('Owned CloudFront stack must be complete before OAuth provisioning')
        outputs = {item['OutputKey']: item['OutputValue'] for item in edge['Outputs']}
        base = outputs['PublicBaseUrl']
        if not re.fullmatch(r'https://[a-z0-9]+\.cloudfront\.net', base):
            raise ValueError('Owned edge output must be an exact HTTPS CloudFront origin')
        callback = base + '/auth/callback'
    parameters = [{'ParameterKey': 'CallbackUrl', 'ParameterValue': callback}]
    request = {
        'StackName': STACK,
        'TemplateBody': (ROOT / 'aws-infrastructure/template.yaml').read_text(),
        'Capabilities': ['CAPABILITY_NAMED_IAM'],
        'Parameters': parameters,
        'Tags': [{'Key': 'Project', 'Value': CLUSTER}],
    }
    try:
        if state:
            client.update_stack(**request)
        else:
            client.create_stack(**request)
    except ClientError as error:
        if state and 'No updates are to be performed' in str(error):
            return {item['OutputKey']: item['OutputValue'] for item in state['Outputs']}
        raise
    return wait_stack(client)


def provision_edge(session, origin: str) -> dict:
    """Bind a dedicated uncached CloudFront distribution to the existing Express origin."""
    hostname = urlsplit(origin).hostname
    if (
        not hostname
        or origin != 'https://' + hostname
        or not re.fullmatch(r'[a-z0-9-]+\.ecs\.ca-central-1\.on\.aws', hostname)
    ):
        raise ValueError('Expected the exact regional ECS HTTPS origin')
    stack_name = EDGE_STACK
    client = session.client('cloudformation')
    state = stack_state(client, stack_name)
    if state and state['StackStatus'].endswith('_IN_PROGRESS'):
        raise RuntimeError('Existing CloudFront infrastructure operation must finish')
    request = {
        'StackName': stack_name,
        'TemplateBody': (ROOT / 'aws-infrastructure/edge.yaml').read_text(),
        'Parameters': [{'ParameterKey': 'OriginHostname', 'ParameterValue': hostname}],
        'Tags': [{'Key': 'Project', 'Value': 'markdown-mcp-service-edge'}],
    }
    try:
        if state:
            client.update_stack(**request)
        else:
            client.create_stack(**request)
    except ClientError as error:
        if state and 'No updates are to be performed' in str(error):
            return {item['OutputKey']: item['OutputValue'] for item in state['Outputs']}
        raise
    return wait_stack(client, stack_name)


def verify_storage_and_owner(session) -> str:
    """Check required existing storage protections and resolve only gopalcbala's subject."""
    s3 = session.client('s3')
    rules = s3.get_bucket_encryption(Bucket=BUCKET)['ServerSideEncryptionConfiguration']['Rules']
    if not any(
        rule['ApplyServerSideEncryptionByDefault']['SSEAlgorithm'] == 'AES256' for rule in rules
    ):
        raise ValueError('Expected the existing AES256 bucket encryption contract')
    block = s3.get_public_access_block(Bucket=BUCKET)['PublicAccessBlockConfiguration']
    if not all(
        block.get(key)
        for key in (
            'BlockPublicAcls',
            'IgnorePublicAcls',
            'BlockPublicPolicy',
            'RestrictPublicBuckets',
        )
    ):
        raise ValueError('Existing document bucket must block all public access')
    policy = json.loads(s3.get_bucket_policy(Bucket=BUCKET)['Policy'])
    if not any(
        statement.get('Effect') == 'Deny'
        and statement.get('Principal') == '*'
        and statement.get('Action') in ('s3:*', ['s3:*'])
        and statement.get('Condition', {}).get('Bool', {}).get('aws:SecureTransport')
        in ('false', False)
        and f'arn:aws:s3:::{BUCKET}/*' in statement.get('Resource', [])
        for statement in policy['Statement']
    ):
        raise ValueError('Existing bucket policy must deny insecure object transport')
    user = session.client('cognito-idp').admin_get_user(UserPoolId=POOL, Username='gopalcbala')
    if user['Username'] != 'gopalcbala' or not user['Enabled']:
        raise ValueError('Expected enabled gopalcbala account')
    return next(item['Value'] for item in user['UserAttributes'] if item['Name'] == 'sub')


def public_subnets(session) -> list:
    """Select default public subnets with verified internet routes in two zones."""
    ec2 = session.client('ec2')
    vpcs = ec2.describe_vpcs(Filters=[{'Name': 'is-default', 'Values': ['true']}])['Vpcs']
    if len(vpcs) != 1:
        raise ValueError('Expected one default VPC')
    filters = [{'Name': 'vpc-id', 'Values': [vpcs[0]['VpcId']]}]
    subnets = ec2.describe_subnets(Filters=filters)['Subnets']
    tables = ec2.describe_route_tables(Filters=filters)['RouteTables']
    main = next(
        (table for table in tables if any(a.get('Main') for a in table['Associations'])), None
    )
    selected = []
    zones = set()
    for subnet in subnets:
        if (
            not subnet['DefaultForAz']
            or not subnet['MapPublicIpOnLaunch']
            or subnet['AvailableIpAddressCount'] < 8
        ):
            continue
        table = next(
            (
                table
                for table in tables
                if any(a.get('SubnetId') == subnet['SubnetId'] for a in table['Associations'])
            ),
            main,
        )
        if table and any(
            route.get('DestinationCidrBlock') == '0.0.0.0/0'
            and route.get('GatewayId', '').startswith('igw-')
            and route.get('State') == 'active'
            for route in table['Routes']
        ):
            selected.append(subnet['SubnetId'])
            zones.add(subnet['AvailabilityZone'])
    if len(zones) < 2:
        raise ValueError('Need public default subnets in two availability zones')
    return selected


def service_state(ecs) -> dict | None:
    """Read only the owned Express service, preserving real access failures."""
    try:
        return ecs.describe_express_gateway_service(serviceArn=SERVICE_ARN)['service']
    except ClientError as error:
        if error.response['Error']['Code'] in {
            'ServiceNotFoundException',
            'ResourceNotFoundException',
        }:
            return None
        raise


def service_url(service: dict) -> str:
    """Extract the HTTPS hostname provided by Express without guessing its format."""
    for configuration in service.get('activeConfigurations', []):
        for ingress in configuration.get('ingressPaths', []):
            if ingress.get('accessType') == 'PUBLIC' and ingress.get('endpoint'):
                endpoint = ingress['endpoint']
                return endpoint if endpoint.startswith('https://') else f'https://{endpoint}'
    raise ValueError('No public HTTPS endpoint on the completed service')


def wait_service(ecs, container: dict) -> dict:
    """Require the matching image/environment revision's successful deployment."""
    for _ in range(8):
        time.sleep(120)
        state = service_state(ecs)
        if not state or state['status']['statusCode'] in {'DRAINING', 'INACTIVE'}:
            raise RuntimeError('MCP service disappeared or stopped during deployment')
        revisions = {
            item['serviceRevisionArn']
            for item in state.get('activeConfigurations', [])
            if item.get('primaryContainer', {}).get('image') == container['image']
            and {
                entry['name']: entry['value']
                for entry in item.get('primaryContainer', {}).get('environment', [])
            }
            == {entry['name']: entry['value'] for entry in container['environment']}
        }
        deployments = ecs.list_service_deployments(service=SERVICE_ARN, maxResults=20)[
            'serviceDeployments'
        ]
        matching = [
            item for item in deployments if item.get('targetServiceRevisionArn') in revisions
        ]
        if not matching:
            print('MCP deployment: pending requested revision', flush=True)
            continue
        current = max(matching, key=lambda item: item['createdAt'])
        status = current['status']
        print(f'MCP deployment: {status}', flush=True)
        if status == 'SUCCESSFUL':
            return state
        if status.startswith('ROLLBACK') or status in {'STOPPED', 'STOP_REQUESTED'}:
            raise RuntimeError(f'MCP deployment failed: {status}')
    raise TimeoutError('MCP deployment did not complete; inspect before retrying')


def verify_endpoint(base: str) -> None:
    """Check public health, real S256 discovery, resource binding and anonymous denial once."""
    context = ssl.create_default_context(cafile=get_cert_path(True))
    for path in (
        '/health',
        '/.well-known/oauth-protected-resource/mcp',
        '/.well-known/oauth-authorization-server',
    ):
        with urllib.request.urlopen(base + path, timeout=20, context=context) as response:
            payload = json.load(response)
        if path == '/health' and payload.get('status') != 'ok':
            raise ValueError('Unexpected MCP health response')
        if path == '/.well-known/oauth-protected-resource/mcp' and (
            payload.get('resource') != base + '/mcp/'
            or payload.get('authorization_servers') != [base + '/']
        ):
            raise ValueError('Published MCP resource or authorization server does not match')
        if path == '/.well-known/oauth-authorization-server' and (
            payload.get('issuer') != base + '/'
            or 'S256' not in payload.get('code_challenge_methods_supported', [])
            or payload.get('registration_endpoint') != base + '/register'
            or payload.get('authorization_endpoint') != base + '/authorize'
            or payload.get('token_endpoint') != base + '/token'
        ):
            raise ValueError('Published OAuth discovery lacks the expected S256 adapter endpoints')
    try:
        urllib.request.urlopen(base + '/mcp/', timeout=20, context=context).close()
    except HTTPError as error:
        if error.code == 401 and 'resource_metadata=' in error.headers.get('WWW-Authenticate', ''):
            error.close()
            return
        error.close()
        raise ValueError('MCP anonymous denial missing or incorrect') from None
    raise ValueError('MCP unexpectedly accepts anonymous requests')


def deploy(session, image: str, outputs: dict) -> dict:
    """Create a health-only bootstrap if needed, then deploy the bound MCP resource."""
    prefix = f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/markdown-mcp-service@sha256:'
    if not image.startswith(prefix) or not re.fullmatch('[0-9a-f]{64}', image.removeprefix(prefix)):
        raise ValueError('Expected an immutable digest in the dedicated MCP repository')
    verify_existing_cluster(session)
    subject = verify_storage_and_owner(session)
    subnets = public_subnets(session)
    ecs = session.client('ecs')
    state = service_state(ecs)
    if state and state['status']['statusCode'] != 'ACTIVE':
        raise RuntimeError('Existing MCP service must be ACTIVE before a new deployment')
    if state:
        active = ecs.list_service_deployments(service=SERVICE_ARN, maxResults=20)[
            'serviceDeployments'
        ]
        if any(
            item['status']
            in {
                'PENDING',
                'IN_PROGRESS',
                'STOP_REQUESTED',
                'ROLLBACK_REQUESTED',
                'ROLLBACK_IN_PROGRESS',
            }
            for item in active
        ):
            raise RuntimeError('Existing MCP deployment must complete before a new write')
    origin = service_url(state) if state else 'https://bootstrap.invalid'
    base = origin
    edge = {}
    for enabled in [True] if state else [False, True]:
        if enabled:
            edge = provision_edge(session, origin)
            base = edge['PublicBaseUrl']
            if outputs['OAuthConfigured'] != 'true':
                # On a first installation the edge did not exist during provision.
                # Enable the fixed upstream callback before activating the adapter.
                outputs = provision(session)
            if outputs['OAuthConfigured'] != 'true':
                raise ValueError('Owned OAuth callback must be configured before activation')
        environment = {
            'AWS_DEFAULT_REGION': REGION,
            'MCP_ENABLED': str(enabled).lower(),
            'PUBLIC_BASE_URL': base,
            'COGNITO_ISSUER': f'https://cognito-idp.{REGION}.amazonaws.com/{POOL}',
            'COGNITO_CLIENT_ID': outputs['ClientId'],
            'COGNITO_DOMAIN': COGNITO_DOMAIN,
            'OAUTH_STATE_TABLE': outputs['OAuthStateTable'],
            'OAUTH_SECRET_ARN': outputs['OAuthSecretArn'],
            'MCP_RESOURCE': base + '/mcp/',
            'MCP_ORIGIN_HOST': urlsplit(origin).hostname,
            'MCP_WRITE_SCOPE': 'markdown-api/write',
            'ALLOWED_SUB': subject,
            'S3_BUCKET': BUCKET,
            'USER_FILES_TABLE': TABLE,
        }
        container = {
            'image': image,
            'containerPort': 8000,
            'awsLogsConfiguration': {
                'logGroup': '/markdown-mcp-service',
                'logStreamPrefix': 'app',
            },
            'environment': [{'name': key, 'value': value} for key, value in environment.items()],
        }
        config = {
            'executionRoleArn': outputs['ExecutionRoleArn'],
            'taskRoleArn': outputs['TaskRoleArn'],
            'healthCheckPath': '/health',
            'primaryContainer': container,
            'cpu': '256',
            'memory': '512',
            'scalingTarget': {'minTaskCount': 1, 'maxTaskCount': 1},
            'networkConfiguration': {'subnets': subnets},
        }
        if state:
            ecs.update_express_gateway_service(serviceArn=SERVICE_ARN, **config)
        else:
            ecs.create_express_gateway_service(
                serviceName=SERVICE,
                cluster=CLUSTER,
                infrastructureRoleArn=f'arn:aws:iam::{ACCOUNT}:role/softrobots-ecs-infrastructure-role',
                tags=[{'key': 'Project', 'value': CLUSTER}],
                **config,
            )
        state = wait_service(ecs, container)
        origin = service_url(state)
    verify_endpoint(base)
    return {
        'endpoint': base + '/mcp/',
        'origin': origin,
        'distribution_id': edge['DistributionId'],
        'image': image,
        'client_id': outputs['ClientId'],
        'oauth_configured': outputs['OAuthConfigured'],
        'authenticated_upload_verified': False,
    }


def main() -> None:
    """Run one guarded infrastructure or container stage and save nonsecret output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['provision', 'deploy'])
    parser.add_argument('--image')
    parser.add_argument('--outputs', default='/tmp/markdown-mcp-stack.json')
    parser.add_argument('--result', default='/tmp/markdown-mcp-deployment.json')
    args = parser.parse_args()
    session = github_session()
    if args.action == 'provision':
        result = provision(session)
        Path(args.outputs).write_text(json.dumps(result, indent=2) + '\n')
    else:
        if not args.image:
            parser.error('--image is required for deploy')
        result = deploy(session, args.image, json.loads(Path(args.outputs).read_text()))
        Path(args.result).write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
