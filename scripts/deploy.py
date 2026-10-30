"""Provision and deploy only Markdown MCP through the main-branch GitHub role.

CloudFormation owns its image repository, task roles and dedicated OAuth client.
ECS Express owns HTTPS ingress. A health-only first revision obtains its hostname
before the enabled revision binds the exact OAuth audience. No credentials persist.
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


def stack_state(client) -> dict | None:
    """Read the exact stack, accepting absence only for its explicit missing error."""
    try:
        return client.describe_stacks(StackName=STACK)['Stacks'][0]
    except ClientError as error:
        if error.response['Error']['Code'] == 'ValidationError' and 'does not exist' in str(error):
            return None
        raise


def wait_stack(client) -> dict:
    """Read completion every two minutes; fail on rollback without retrying writes."""
    for _ in range(8):
        time.sleep(120)
        state = stack_state(client)
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


def provision(session, callback: str | None) -> dict:
    """Apply the dedicated stack using the independent GitHub role and existing ECS cluster."""
    if callback is not None and callback and not re.fullmatch(r'https://[^\s#]+', callback):
        raise ValueError('Callback must be the exact HTTPS callback displayed by the MCP host')
    verify_existing_cluster(session)
    client = session.client('cloudformation')
    state = stack_state(client)
    parameters = []
    if callback is not None:
        parameters.append({'ParameterKey': 'CallbackUrl', 'ParameterValue': callback})
    elif state:
        parameters.append({'ParameterKey': 'CallbackUrl', 'UsePreviousValue': True})
    request = {
        'StackName': STACK,
        'TemplateBody': (ROOT / 'aws-infrastructure/template.yaml').read_text(),
        'Capabilities': ['CAPABILITY_NAMED_IAM'],
        'Parameters': parameters,
        'Tags': [{'Key': 'Project', 'Value': CLUSTER}],
    }
    if state and state['StackStatus'].endswith('_IN_PROGRESS'):
        raise RuntimeError('Existing infrastructure operation must finish before deployment')
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


def verify_storage_and_owner(session) -> str:
    """Check required existing storage protections and resolve only gopalcbala's subject."""
    s3 = session.client('s3')
    if s3.get_bucket_versioning(Bucket=BUCKET).get('Status') != 'Enabled':
        raise ValueError('Existing document bucket must have versioning enabled')
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
    """Make one final bounded health, metadata and anonymous-denial check."""
    context = ssl.create_default_context(cafile=get_cert_path(True))
    for path in ('/health', '/.well-known/oauth-protected-resource/mcp'):
        with urllib.request.urlopen(base + path, timeout=20, context=context) as response:
            payload = json.load(response)
        if path == '/health' and payload.get('status') != 'ok':
            raise ValueError('Unexpected MCP health response')
        if path != '/health' and payload.get('resource') != base + '/mcp/':
            raise ValueError('Published MCP resource does not match its actual endpoint')
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
    base = service_url(state) if state else 'https://bootstrap.invalid'
    for enabled in [True] if state else [False, True]:
        environment = {
            'AWS_DEFAULT_REGION': REGION,
            'MCP_ENABLED': str(enabled).lower(),
            'PUBLIC_BASE_URL': base,
            'COGNITO_ISSUER': f'https://cognito-idp.{REGION}.amazonaws.com/{POOL}',
            'COGNITO_CLIENT_ID': outputs['ClientId'],
            'MCP_RESOURCE': base + '/mcp/',
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
        base = service_url(state)
    verify_endpoint(base)
    return {
        'endpoint': base + '/mcp/',
        'image': image,
        'client_id': outputs['ClientId'],
        'oauth_configured': outputs['OAuthConfigured'],
        'authenticated_upload_verified': False,
    }


def main() -> None:
    """Run one guarded infrastructure or container stage and save nonsecret output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['provision', 'deploy'])
    parser.add_argument('--callback')
    parser.add_argument('--image')
    parser.add_argument('--outputs', default='/tmp/markdown-mcp-stack.json')
    parser.add_argument('--result', default='/tmp/markdown-mcp-deployment.json')
    args = parser.parse_args()
    session = github_session()
    if args.action == 'provision':
        result = provision(session, args.callback)
        Path(args.outputs).write_text(json.dumps(result, indent=2) + '\n')
    else:
        if not args.image:
            parser.error('--image is required for deploy')
        result = deploy(session, args.image, json.loads(Path(args.outputs).read_text()))
        Path(args.result).write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
