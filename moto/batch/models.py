from __future__ import unicode_literals
import boto3
import re
from itertools import cycle
import six
import uuid
from moto.core import BaseBackend, BaseModel
from moto.iam import iam_backends
from moto.ec2 import ec2_backends
from moto.ecs import ecs_backends

from .exceptions import InvalidParameterValueException, InternalFailure
from .utils import make_arn_for_compute_env
from moto.ec2.exceptions import InvalidSubnetIdError
from moto.ec2.models import INSTANCE_TYPES as EC2_INSTANCE_TYPES
from moto.iam.exceptions import IAMNotFoundException


DEFAULT_ACCOUNT_ID = 123456789012
COMPUTE_ENVIRONMENT_NAME_REGEX = re.compile(r'^[A-Za-z0-9_]{1,128}$')


class ComputeEnvironment(BaseModel):
    def __init__(self, compute_environment_name, _type, state, compute_resources, service_role, region_name):
        self.name = compute_environment_name
        self.type = _type
        self.state = state
        self.compute_resources = compute_resources
        self.service_role = service_role
        self.arn = make_arn_for_compute_env(DEFAULT_ACCOUNT_ID, compute_environment_name, region_name)

        self.instances = []
        self.ecs_arn = None

    def add_instance(self, instance):
        self.instances.append(instance)

    def set_ecs_arn(self, arn):
        self.ecs_arn = arn


class BatchBackend(BaseBackend):
    def __init__(self, region_name=None):
        super(BatchBackend, self).__init__()
        self.region_name = region_name

        self._compute_environments = {}

    @property
    def iam_backend(self):
        """
        :return: IAM Backend
        :rtype: moto.iam.models.IAMBackend
        """
        return iam_backends['global']

    @property
    def ec2_backend(self):
        """
        :return: EC2 Backend
        :rtype: moto.ec2.models.EC2Backend
        """
        return ec2_backends[self.region_name]

    @property
    def ecs_backend(self):
        """
        :return: ECS Backend
        :rtype: moto.ecs.models.EC2ContainerServiceBackend
        """
        return ecs_backends[self.region_name]

    def reset(self):
        region_name = self.region_name
        self.__dict__ = {}
        self.__init__(region_name)

    def get_compute_environment(self, arn):
        return self._compute_environments.get(arn)

    def get_compute_environment_by_name(self, name):
        for comp_env in self._compute_environments.values():
            if comp_env.name == name:
                return comp_env
        return None

    def describe_compute_environments(self, environments=None, max_results=None, next_token=None):
        envs = set()
        if environments is not None:
            envs = set(environments)

        result = []
        for arn, environment in self._compute_environments.items():
            # Filter shortcut
            if len(envs) > 0 and arn not in envs and environment.name not in envs:
                continue

            json_part = {
                'computeEnvironmentArn': arn,
                'computeEnvironmentName': environment.name,
                'ecsClusterArn': environment.ecs_arn,
                'serviceRole': environment.service_role,
                'state': environment.state,
                'type': environment.type,
                'status': 'VALID'
            }
            if environment.type == 'MANAGED':
                json_part['computeResources'] = environment.compute_resources

            result.append(json_part)

        return result

    def create_compute_environment(self, compute_environment_name, _type, state, compute_resources, service_role):
        # Validate
        if COMPUTE_ENVIRONMENT_NAME_REGEX.match(compute_environment_name) is None:
            raise InvalidParameterValueException('Compute environment name does not match ^[A-Za-z0-9_]{1,128}$')

        if self.get_compute_environment_by_name(compute_environment_name) is not None:
            raise InvalidParameterValueException('A compute environment already exists with the name {0}'.format(compute_environment_name))

        # Look for IAM role
        try:
            self.iam_backend.get_role_by_arn(service_role)
        except IAMNotFoundException:
            raise InvalidParameterValueException('Could not find IAM role {0}'.format(service_role))

        if _type not in ('MANAGED', 'UNMANAGED'):
            raise InvalidParameterValueException('type {0} must be one of MANAGED | UNMANAGED'.format(service_role))

        if state is not None and state not in ('ENABLED', 'DISABLED'):
            raise InvalidParameterValueException('state {0} must be one of ENABLED | DISABLED'.format(state))

        if compute_resources is None and _type == 'MANAGED':
            raise InvalidParameterValueException('computeResources must be specified when creating a MANAGED environment'.format(state))
        elif compute_resources is not None:
            self._validate_compute_resources(compute_resources)

        # By here, all values except SPOT ones have been validated
        new_comp_env = ComputeEnvironment(
            compute_environment_name, _type, state,
            compute_resources, service_role,
            region_name=self.region_name
        )
        self._compute_environments[new_comp_env.arn] = new_comp_env

        # Ok by this point, everything is legit, so if its Managed then start some instances
        if _type == 'MANAGED':
            cpus = int(compute_resources.get('desiredvCpus', compute_resources['minvCpus']))
            instance_types = compute_resources['instanceTypes']
            needed_instance_types = self.find_min_instances_to_meet_vcpus(instance_types, cpus)
            # Create instances

            # Will loop over and over so we get decent subnet coverage
            subnet_cycle = cycle(compute_resources['subnets'])

            for instance_type in needed_instance_types:
                reservation = self.ec2_backend.add_instances(
                    image_id='ami-ecs-optimised',  # Todo import AMIs
                    count=1,
                    user_data=None,
                    security_group_names=[],
                    instance_type=instance_type,
                    region_name=self.region_name,
                    subnet_id=six.next(subnet_cycle),
                    key_name=compute_resources.get('ec2KeyPair', 'AWS_OWNED'),
                    security_group_ids=compute_resources['securityGroupIds']
                )

                new_comp_env.add_instance(reservation.instances[0])

        # Create ECS cluster
        # Should be of format P2OnDemand_Batch_UUID
        cluster_name = 'OnDemand_Batch_' + str(uuid.uuid4())
        ecs_cluster = self.ecs_backend.create_cluster(cluster_name)
        new_comp_env.set_ecs_arn(ecs_cluster.arn)

        return compute_environment_name, new_comp_env.arn

    def _validate_compute_resources(self, cr):
        """
        Checks contents of sub dictionary for managed clusters

        :param cr: computeResources
        :type cr: dict
        """
        for param in ('instanceRole', 'maxvCpus', 'minvCpus', 'instanceTypes', 'securityGroupIds', 'subnets', 'type'):
            if param not in cr:
                raise InvalidParameterValueException('computeResources must contain {0}'.format(param))

        if self.iam_backend.get_role_by_arn(cr['instanceRole']) is None:
            raise InvalidParameterValueException('could not find instanceRole {0}'.format(cr['instanceRole']))

        if cr['maxvCpus'] < 0:
            raise InvalidParameterValueException('maxVCpus must be positive')
        if cr['minvCpus'] < 0:
            raise InvalidParameterValueException('minVCpus must be positive')
        if cr['maxvCpus'] < cr['minvCpus']:
            raise InvalidParameterValueException('maxVCpus must be greater than minvCpus')

        if len(cr['instanceTypes']) == 0:
            raise InvalidParameterValueException('At least 1 instance type must be provided')
        for instance_type in cr['instanceTypes']:
            if instance_type not in EC2_INSTANCE_TYPES:
                raise InvalidParameterValueException('Instance type {0} does not exist'.format(instance_type))

        for sec_id in cr['securityGroupIds']:
            if self.ec2_backend.get_security_group_from_id(sec_id) is None:
                raise InvalidParameterValueException('security group {0} does not exist'.format(sec_id))
        if len(cr['securityGroupIds']) == 0:
            raise InvalidParameterValueException('At least 1 security group must be provided')

        for subnet_id in cr['subnets']:
            try:
                self.ec2_backend.get_subnet(subnet_id)
            except InvalidSubnetIdError:
                raise InvalidParameterValueException('subnet {0} does not exist'.format(subnet_id))
        if len(cr['subnets']) == 0:
            raise InvalidParameterValueException('At least 1 subnet must be provided')

        if cr['type'] not in ('EC2', 'SPOT'):
            raise InvalidParameterValueException('computeResources.type must be either EC2 | SPOT')

        if cr['type'] == 'SPOT':
            raise InternalFailure('SPOT NOT SUPPORTED YET')

    @staticmethod
    def find_min_instances_to_meet_vcpus(instance_types, target):
        """
        Finds the minimum needed instances to meed a vcpu target

        :param instance_types: Instance types, like ['t2.medium', 't2.small']
        :type instance_types: list of str
        :param target: VCPU target
        :type target: float
        :return: List of instance types
        :rtype: list of str
        """
        # vcpus = [ (vcpus, instance_type), (vcpus, instance_type), ... ]
        instance_vcpus = []
        instances = []

        for instance_type in instance_types:
            instance_vcpus.append(
                (EC2_INSTANCE_TYPES[instance_type]['vcpus'], instance_type)
            )

        instance_vcpus = sorted(instance_vcpus, key=lambda item: item[0], reverse=True)
        # Loop through,
        #   if biggest instance type smaller than target, and len(instance_types)> 1, then use biggest type
        #   if biggest instance type bigger than target, and len(instance_types)> 1, then remove it and move on

        #   if biggest instance type bigger than target and len(instan_types) == 1 then add instance and finish
        #   if biggest instance type smaller than target and len(instan_types) == 1 then loop adding instances until target == 0
        #   ^^ boils down to keep adding last till target vcpus is negative
        #   #Algorithm ;-) ... Could probably be done better with some quality lambdas
        while target > 0:
            current_vcpu, current_instance = instance_vcpus[0]

            if len(instance_vcpus) > 1:
                if current_vcpu <= target:
                    target -= current_vcpu
                    instances.append(current_instance)
                else:
                    # try next biggest instance
                    instance_vcpus.pop(0)
            else:
                # Were on the last instance
                target -= current_vcpu
                instances.append(current_instance)

        return instances


available_regions = boto3.session.Session().get_available_regions("batch")
batch_backends = {region: BatchBackend(region_name=region) for region in available_regions}
