import time
import socket
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urlunparse
import warnings

from textwrap import dedent
from tornado import gen
from tornado.concurrent import run_on_executor
from traitlets import Any, Integer, List, Unicode, Bool, default, observe

from marathon import MarathonClient
from marathon.models.app import MarathonApp, MarathonHealthCheck
from marathon.models.container import MarathonContainerPortMapping, \
    MarathonContainer, MarathonContainerVolume, MarathonDockerContainer
from marathon.models.constraint import MarathonConstraint
from marathon.exceptions import NotFoundError, MarathonHttpError
from jupyterhub.spawner import Spawner

from .volumenaming import default_format_volume_name

import jupyterhub
_jupyterhub_xy = '%i.%i' % (jupyterhub.version_info[:2])

class MarathonSpawner(Spawner):

    app_image = Unicode("jupyterhub/singleuser:%s" % _jupyterhub_xy, config=True)

    app_prefix = Unicode(
        "jupyter",
        help=dedent(
            """
            Prefix for app names. The full app name for a particular
            user will be <prefix>/<username>.
            """
        )
    ).tag(config=True)

    marathon_host = Unicode(
        u'',
        help="Hostname of Marathon server").tag(config=True)

    marathon_constraints = List(
        [],
        help='Constraints to be passed through to Marathon').tag(config=True)

    ports = List(
        [8888],
        help='Ports to expose externally'
        ).tag(config=True)

    volumes = List(
        [],
        help=dedent(
            """
            A list in Marathon REST API format for mounting volumes into the docker container.
            [
                {
                    "containerPath": "/foo",
                    "hostPath": "/bar",
                    "mode": "RW"
                }
            ]

            Note that using the template variable {username} in containerPath,
            hostPath or the name variable in case it's an external drive
            it will be replaced with the current user's name.
            """
        )
    ).tag(config=True)

    network_mode = Unicode(
        'BRIDGE',
        help="Enum of BRIDGE or HOST"
        ).tag(config=True)

    hub_ip_connect = Unicode(
        "",
        help="Public IP address of the hub"
        ).tag(config=True)

    force_pull_image = Bool(
        False,
        config=True,
        help="Tell marathon to pull the image on every spawn",
    )

    @observe('hub_ip_connect')
    def _ip_connect_changed(self, change):
        if jupyterhub.version_info >= (0, 8):
            warnings.warn(
                "MarathonSpawner.hub_ip_connect is no longer needed with JupyterHub 0.8."
                "  Use JupyterHub.hub_connect_ip instead.",
                DeprecationWarning,
            )

    hub_port_connect = Integer(
        -1,
        help="Public PORT of the hub"
        ).tag(config=True)

    @observe('hub_port_connect')
    def _port_connect_changed(self, change):
        if jupyterhub.version_info >= (0, 8):
            warnings.warn(
                "MarathonSpawner.hub_port_connect is no longer needed with JupyterHub 0.8."
                "  Use JupyterHub.hub_connect_port instead.",
                DeprecationWarning,
            )

    format_volume_name = Any(
        help="""Any callable that accepts a string template and a Spawner
        instance as parameters in that order and returns a string.
        """
    ).tag(config=True)

    @default('format_volume_name')
    def _get_default_format_volume_name(self):
        return default_format_volume_name

    # fix default port to 8888, used in the container
    @default('port')
    def _port_default(self):
        return 8888

    # default to listening on all-interfaces in the container
    @default('ip')
    def _ip_default(self):
        return '0.0.0.0'

    _executor = None
    @property
    def executor(self):
        cls = self.__class__
        if cls._executor is None:
            cls._executor = ThreadPoolExecutor(1)
        return cls._executor

    def __init__(self, *args, **kwargs):
        super(MarathonSpawner, self).__init__(*args, **kwargs)
        self.marathon = MarathonClient(self.marathon_host)

    @property
    def container_name(self):
        username_sanitized = self.user.name.replace('.','-')
        return '/%s/%s' % (self.app_prefix, username_sanitized)

    def get_state(self):
        state = super(MarathonSpawner, self).get_state()
        state['container_name'] = self.container_name
        if getattr(self, 'limits_name', None):
            state['limits_name'] = self.limits_name
        self.log.info("writing state %s", state)
        return state

    def load_state(self, state):
        super(MarathonSpawner, self).load_state(state)
        self.log.info("loaded state %s", state)
        #if 'container_name' in state:
        #    self.container_name = state['container_name']
        if 'limits_name' in state:
            self.limits_name = state['limits_name']

    def get_health_checks(self):
        health_checks = []
        health_checks.append(MarathonHealthCheck(
            protocol='TCP',
            port_index=0,
            grace_period_seconds=300,
            interval_seconds=30,
            timeout_seconds=20,
            max_consecutive_failures=0
            ))
        return health_checks

    def get_volumes(self):
        volumes = []
        for v in self.volumes:
            mv = MarathonContainerVolume.from_json(v)
            mv.container_path = self.format_volume_name(mv.container_path, self)
            mv.host_path = self.format_volume_name(mv.host_path, self)
            if mv.external and 'name' in mv.external:
                mv.external['name'] = self.format_volume_name(mv.external['name'], self)
            volumes.append(mv)
        return volumes

    def get_port_mappings(self):
        port_mappings = []
        for p in self.ports:
            port_mappings.append(
                MarathonContainerPortMapping(
                    container_port=p,
                    host_port=0,
                    protocol='tcp'
                )
            )
        return port_mappings

    def get_constraints(self):
        constraints = []
        for c in self.marathon_constraints:
            constraints.append(MarathonConstraint.from_json(c))
        return constraints

    @run_on_executor
    def get_deployment(self, deployment_id):
        deployments = self.marathon.list_deployments()
        for d in deployments:
            if d.id == deployment_id:
                return d
        return None

    @run_on_executor
    def get_deployment_for_app(self, app_name):
        deployments = self.marathon.list_deployments()
        for d in deployments:
            if app_name in d.affected_apps:
                return d
        return None

    def get_ip_and_port(self, app_info):
        assert len(app_info.tasks) == 1
        ip = socket.gethostbyname(app_info.tasks[0].host)
        self.log.info("Found app at %s:%s (%s)", ip, app_info.tasks[0].ports[0], app_info.tasks[0].host)
        return (ip, app_info.tasks[0].ports[0])

    @run_on_executor
    def get_app_info(self, app_name):
        try:
            app = self.marathon.get_app(app_name, embed_tasks=True)
        except NotFoundError as e:
            self.log.info("The %s application does not exist", app_name)
            return None
        except Exception as e:
            self.log.error("Got exception at get_app_info!")
            raise e
        else:
            return app

    def _public_hub_api_url(self):
        uri = urlparse(self.hub.api_url)
        port = self.hub_port_connect if self.hub_port_connect > 0 else uri.port
        ip = self.hub_ip_connect if self.hub_ip_connect else uri.hostname
        return urlunparse((
            uri.scheme,
            '%s:%s' % (ip, port),
            uri.path,
            uri.params,
            uri.query,
            uri.fragment
            ))

    def get_args(self):
        args = super().get_args()
        if self.hub_ip_connect:
            # JupyterHub 0.7 specifies --hub-api-url
            # on the command-line, which is hard to update
            for idx, arg in enumerate(list(args)):
                if arg.startswith('--hub-api-url='):
                    args.pop(idx)
                    break
            args.append('--hub-api-url=%s' % self._public_hub_api_url())
        return args

    @gen.coroutine
    def start(self, app_image = None, resource_ram = None, resource_cpu = None, resource_name = None):
        self.log.info("resource_namex: %s" % resource_name)
        if not app_image:
            app_image = self.app_image
        if resource_ram:
            self.mem_limit = resource_ram
        if resource_cpu:
            self.cpu_limit = resource_cpu
        if resource_name:
            self.limits_name = resource_name

        docker_container = MarathonDockerContainer(
            image=app_image,
            network=self.network_mode,
            force_pull_image=self.force_pull_image,
            port_mappings=self.get_port_mappings())

        app_container = MarathonContainer(
            docker=docker_container,
            type='DOCKER',
            volumes=self.get_volumes())

        # the memory request in marathon is in MiB
        if hasattr(self, 'mem_limit') and resource_ram is not None:
            mem_request = self.mem_limit / 1024.0 / 1024.0
        else:
            mem_request = 1024.0

        app_request = MarathonApp(
            id=self.container_name,
            args = self.cmd + self.get_args(),
            env=self.get_env(),
            cpus=self.cpu_limit,
            mem=mem_request,
            container=app_container,
            constraints=self.get_constraints(),
            health_checks=self.get_health_checks(),
            instances=1,
            accepted_resource_roles=['*'],
            )

        self.log.info("Creating App: %s", app_request)
        self.log.info("self.marathon: %s", self.marathon)
        try:
            app = self.marathon.create_app(self.container_name, app_request)
            if app is False or app.deployments is None:
                self.log.error("Failed to create application for %s", self.container_name)
                self.log.error("app: %s", app)
                return None
        except MarathonHttpError as ex:
            if ex.error_message.startswith("An app with id") and \
                    ex.error_message.endswith(" already exists."):
                self.log.info("Attempting to recover %s", self.container_name)
            else:
                raise

        while True:
            app_info = yield self.get_app_info(self.container_name)
            if app_info and app_info.tasks_healthy == 1:
                ip, port = self.get_ip_and_port(app_info)
                break
            yield gen.sleep(1)
        self.log.info("Start completed - %s:%s" % (ip, port))
        nb_url = "http://%s:%s" % (ip, port)
        return nb_url

    @gen.coroutine
    def stop(self, now=False):
        app_info = yield self.get_app_info(self.container_name)
        try:
            status = self.marathon.delete_app(self.container_name, force=True)
        except:
            self.log.error("Could not delete application %s", self.container_name)
            raise
        else:
            if not now:
                while True:
                    deployment = yield self.get_deployment(status['deploymentId'])
                    if deployment is None:
                        break
                    yield gen.sleep(1)

    @gen.coroutine
    def poll(self):
        try:
            deployment = yield self.get_deployment_for_app(self.container_name)
            if deployment:
                for current_action in deployment.current_actions:
                    if current_action.action == 'StopApplication':
                        self.log.error("Application %s is shutting down", self.container_name)
                        return 1
                return None

            app_info = yield self.get_app_info(self.container_name)
            if app_info and app_info.tasks_healthy == 1:
                return None
            return 0
        except MarathonHttpError as e:
            self.log.error("Marathon unreachable in poll(), just saying things are OK")
            self.log.error("Nested exception was: %s" % e)
            return None
