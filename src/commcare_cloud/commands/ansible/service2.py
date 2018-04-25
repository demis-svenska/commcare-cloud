from abc import ABCMeta, abstractmethod, abstractproperty
from collections import defaultdict, OrderedDict
from itertools import groupby

import attr
import six
from clint.textui import puts, indent, colored

from commcare_cloud.commands.ansible.helpers import get_django_webworker_name, AnsibleContext, \
    get_formplayer_spring_instance_name, get_formplayer_instance_name
from commcare_cloud.commands.ansible.run_module import run_ansible_module
from commcare_cloud.commands.celery_utils import get_celery_workers
from commcare_cloud.commands.command_base import CommandBase
from commcare_cloud.environment.main import get_environment
from commcare_cloud.fab.exceptions import NoHostsMatch

ACTIONS = ['start', 'stop', 'restart', 'status', 'help']

STATES = {
    'start': 'started',
    'stop': 'stopped',
    'restart': 'restarted',
    'status': 'status'
}

@attr.s
class ServiceOption(object):
    name = attr.ib()
    sub_options = attr.ib(default=None)


class ServiceBase(six.with_metaclass(ABCMeta)):
    @abstractproperty
    def name(self):
        raise NotImplementedError

    @abstractproperty
    def inventory_groups(self):
        raise NotImplementedError

    def __init__(self, environment, ansible_context, check=False):
        self.environment = environment
        self.ansible_context = ansible_context
        self.check = check

    def run(self, action, host_pattern=None, process_pattern=None):
        if action == 'help':
            self.print_help()
        else:
            try:
                self.execute_action(action, host_pattern, process_pattern)
            except NoHostsMatch:
                only = limit = ''
                if process_pattern:
                    only = " '--only={}'".format(process_pattern)
                if host_pattern:
                    limit = " '--limit={}'".format(host_pattern)

                puts(colored.red("No '{}' hosts match{}{}".format(self.name, limit, only)))
                return 1

    def print_help(self):
        puts(colored.green("Additional help for service '{}'".format(self.name)))

        options = self.get_options()
        for name, options in options.items():
            puts("{}:".format(name))
            with indent():
                for option in options:
                    puts(option.name)
                    if option.sub_options:
                        with indent():
                            puts('\n'.join(option.sub_options))

    def get_options(self):
        all_group_options = []
        for group in self.inventory_groups:
            sub_groups = [g.name for g in self.environment.inventory_manager.groups[group].child_groups]
            all_group_options.append(ServiceOption(group, sub_groups))

        options = OrderedDict()
        options["Inventory groups (use with '--limit')"] = all_group_options
        options["Hosts (use with '--limit')"] = [ServiceOption(host) for host in self._all_hosts()]
        return options

    @abstractmethod
    def execute_action(self, action, host_pattern=None, process_pattern=None):
        raise NotImplementedError

    def _run_ansible(self, host_pattern, module, module_args):
        extra_args = []
        if self.check:
            extra_args.append('--check')

        return run_ansible_module(
            self.environment,
            self.ansible_context,
            host_pattern,
            module,
            module_args,
            True,
            None,
            *extra_args
        )

    def _all_hosts(self):
        pattern = ','.join(self.inventory_groups)
        return set([
            host.name for host in self.environment.inventory_manager.get_hosts(pattern)
        ])


class SubServicesMixin(six.with_metaclass(ABCMeta)):
    @abstractmethod
    def get_managed_services(self):
        raise NotImplementedError

    def get_options(self):
        options = super(SubServicesMixin, self).get_options()
        options["Sub-services (use with --only)"] = [ServiceOption(service) for service in self.get_managed_services()]
        return options


class SupervisorService(SubServicesMixin, ServiceBase):
    inventory_groups = ['webworkers', 'celery', 'pillowtop', 'touchforms', 'formplayer', 'proxy']

    def execute_action(self, action, host_pattern=None, process_pattern=None):
        if host_pattern:
            self.environment.inventory_manager.subset(host_pattern)

        process_host_mapping = self._get_processes_by_host(process_pattern)

        exit_status = []
        for hosts, processes in process_host_mapping.items():
            command = 'supervisorctl {} {}'.format(
                action,
                ' '.join(processes)
            )
            exit_status.append(self._run_ansible(
                ','.join(hosts),
                'shell',
                command
            ))
        if not exit_status:
            raise NoHostsMatch
        return max(exit_status)

    @abstractmethod
    def _get_processes_by_host(self, process_pattern=None):
        """
        :param process_pattern: process pattern from the args or None
        :return: dict mapping tuple(hostname1,hostname2,...) -> [process name list]
        """
        raise NotImplemented


class AnsibleService(ServiceBase):
    """Service that is controlled via the ansible 'service' module"""

    @property
    def service_name(self):
        return self.name

    def execute_action(self, action, host_pattern=None, process_pattern=None):
        host_pattern = host_pattern or ','.join(self.inventory_groups)
        service_args = 'name={} state={}'.format(self.service_name, STATES[action])
        self._run_ansible(host_pattern, 'service', service_args)


class MultiAnsibleService(SubServicesMixin, AnsibleService):
    """Service that is made up of multiple other services e.g. RiakCS"""

    @abstractmethod
    def get_inventory_group_for_sub_process(self, sub_service):
        """
        :param sub_service: name of a sub-service
        :return: inventory group for that service
        """
        raise NotImplementedError

    def execute_action(self, action, host_pattern=None, process_pattern=None):
        if host_pattern:
            self.environment.inventory_manager.subset(host_pattern)

        if process_pattern:
            assert process_pattern in self.get_managed_services(), (
                "{} does not match available sub-processes".format(process_pattern)
            )

            run_on = self.get_inventory_group_for_sub_process(process_pattern)
            hosts = self.environment.inventory_manager.get_hosts(run_on)
            if not hosts:
                raise NoHostsMatch
            run_on = ','.join([host.name for host in hosts])

            service_args = 'name={} state={}'.format(process_pattern, STATES[action])
            self._run_ansible(run_on, 'service', service_args)
        else:
            for service in self.get_managed_services():
                run_on = self.get_inventory_group_for_sub_process(service)
                hosts = self.environment.inventory_manager.get_hosts(run_on)
                if hosts:
                    service_args = 'name={} state={}'.format(service, STATES[action])
                    self._run_ansible(run_on, 'service', service_args)


class Postgresql(AnsibleService):
    name = 'postgresql'
    inventory_groups = ['postgresql', 'pg_standby']


class Pgbouncer(AnsibleService):
    name = 'pgbouncer'
    inventory_groups = ['postgresql']


class Nginx(AnsibleService):
    name = 'nginx'
    inventory_groups = ['proxy']


class Elasticsearch(AnsibleService):
    name = 'elasticsearch'
    inventory_groups = ['elasticsearch']


class Couchdb(AnsibleService):
    name = 'couchdb'
    inventory_groups = ['couchdb2']


class RabbitMq(AnsibleService):
    name = 'rabbitmq'
    inventory_groups = ['rabbitmq']
    service_name = 'rabbitmq-server'


class Redis(AnsibleService):
    name = 'redis'
    inventory_groups = ['redis']
    service_name = 'redis-server'


class Riakcs(MultiAnsibleService):
    name = 'riakcs'
    inventory_groups = ['riakcs', 'stanchion']

    def get_managed_services(self):
        return [
            'riak', 'riak-cs', 'stanchion'
        ]

    def get_inventory_group_for_sub_process(self, sub_process):
        return {
            'stanchion': 'stanchion'
        }.get(sub_process, 'riakcs')


class Kafka(MultiAnsibleService):
    name = 'kafka'
    inventory_groups = ['kafka', 'zookeeper']

    def get_managed_services(self):
        return [
            'kafka-server', 'zookeeper'
        ]

    def get_inventory_group_for_sub_process(self, sub_process):
        return {
            'stanchion': 'stanchion'
        }.get(sub_process, 'riakcs')


class SingleSupervisorService(SupervisorService):
    @abstractproperty
    def supervisor_process_name(self):
        raise NotImplementedError

    def _get_processes_by_host(self, process_pattern=None):
        return {
            tuple(self._all_hosts()): self.supervisor_process_name()
        }


class Webworker(SingleSupervisorService):
    name = 'webworker'
    inventory_groups = ['webworkers']

    @property
    def supervisor_process_name(self):
        return get_django_webworker_name(self.environment)


class Formplayer(SupervisorService):
    name = 'formplayer'
    inventory_groups = ['formplayer']

    @property
    def supervisor_process_name(self):
        return get_formplayer_spring_instance_name(self.environment)


class Touchforms(SupervisorService):
    name = 'touchforms'
    inventory_groups = ['touchforms']

    @property
    def supervisor_process_name(self):
        return get_formplayer_instance_name(self.environment)


class Celery(SupervisorService):
    name = 'celery'
    inventory_groups = ['celery']

    def _get_processes_by_host(self, process_pattern=None):
        all_hosts = self._all_hosts()

        worker_match = queue_match = None
        if process_pattern:
            if ':' in process_pattern:
                queue_match, worker_match = process_pattern.split(':')
                worker_match = int(worker_match)
            else:
                queue_match = process_pattern

        def matches(item, matcher):
            return matcher is None or matcher == item

        workers = get_celery_workers(self.environment)
        processes_by_host = defaultdict(set)
        for host, queue, worker_num, process_name in workers:
            if host in all_hosts \
                    and matches(queue, queue_match) \
                    and matches(worker_num, worker_match):
                processes_by_host[host].add(process_name)

        processes_by_hosts = {}
        for grouper in groupby(processes_by_host.items(), key=lambda hp: hp[1]):
            hosts = tuple(host_processes[0] for host_processes in grouper[1])
            processes = grouper[0]
            processes_by_hosts[hosts] = processes
        return processes_by_hosts

    def get_managed_services(self):
        workers = get_celery_workers(self.environment)
        return sorted({
            '{}{}'.format(queue, ':{}'.format(worker_num) if worker_num > 1 else '')
            for host, queue, worker_num, process_name in workers
        })


SERVICES = [
    Webworker,
    Celery,
    Postgresql,
    Pgbouncer,
    Riakcs,
    Nginx,
]


class Service2(CommandBase):
    command = 'service2'

    def _service_names(self):
        return sorted([
            service.name for service in SERVICES
        ])

    def make_parser(self):
        self.parser.add_argument(
            'services',
            nargs="+",
            choices=self._service_names(),
            help="The services to run the command on (comma separated list)"
        )
        self.parser.add_argument(
            'action',
            choices=ACTIONS,
            help="What action to take"
        )
        self.parser.add_argument('--limit', help=(
            "Restrict the hosts to run the command on."
        ))
        self.parser.add_argument(
            '--only',
            help=(
                "Sub-process name to limit action to."
            )
        )

    def run(self, args, unknown_args):
        environment = get_environment(args.env_name)

        services_by_name = {
            service.name: service for service in SERVICES
        }

        services = [
            services_by_name[name]
            for name in args.services
        ]

        ansible_context = AnsibleContext(args)
        for service in services:
            service(environment, ansible_context).run(args.action, args.limit, args.only)
