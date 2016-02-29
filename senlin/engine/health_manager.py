# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""
Health Manager class.

Health Manager is responsible for monitoring the health of the clusters and
trigger corresponding actions to recover the clusters based on the pre-defined
health policies.
"""

from oslo_config import cfg
import oslo_messaging
from oslo_service import service
from oslo_service import threadgroup

from senlin.common import consts
from senlin.common import context
from senlin.common import messaging as rpc_messaging
from senlin.db import api as db_api
from senlin.rpc import client as rpc_client


health_mgr_opts = [
    cfg.IntOpt('periodic_interval_max',
               default=60,
               help='Seconds between periodic tasks to be called'),
]

CONF = cfg.CONF
CONF.register_opts(health_mgr_opts)


class HealthManager(service.Service):

    def __init__(self, engine_service, topic, version):
        super(HealthManager, self).__init__()

        self.TG = threadgroup.ThreadGroup()
        self.engine_id = engine_service.engine_id
        self.topic = topic
        self.version = version
        self.ctx = context.get_admin_context()
        self.periodic_interval_max = CONF.periodic_interval_max
        self.rt = {
            'registries': [],
        }

    def _idle_task(self):
        pass

    def _periodic_check(self, cluster_id):
        rpcc = rpc_client.EngineClient()
        rpcc.cluster_check(self.ctx, cluster_id)

    def start_periodic_tasks(self):
        """Tasks to be run at a periodic interval."""
        # TODO(anyone): start timers to check clusters
        # - get clusters that needs health management from DB
        # - get their checking options
        #   * if it is about node status polling, add a timer to trigger its
        #     do_check logic
        #   * if it is about listening to message queue, start a thread to
        #     listen events targeted at that cluster
        self.TG.add_timer(cfg.CONF.periodic_interval, self._idle_task)

        # for registry in self.registries:
        #    if registry.check_type == 'NODE_STATUS_POLLING':
        #        interval = min(registry.interval, self.periodic_interval_max)
        #        self.TG.add_timer(interval,
        #                          self._periodic_check(registry.cluster_id))

    def start(self):
        super(HealthManager, self).start()
        self.target = oslo_messaging.Target(server=self.engine_id,
                                            topic=self.topic,
                                            version=self.version)
        server = rpc_messaging.get_rpc_server(self.target, self)
        server.start()
        # self._load_runtime_registry()
        self.start_periodic_tasks()

    def _load_runtime_registry(self):
        db_registries = db_api.registry_claim(self.ctx, self.engine_id)
        self.rt = {'registries': [r for r in db_registries]}

    @property
    def registries(self):
        return self.rt['registries']

    def stop(self):
        self.TG.stop_timers()
        super(HealthManager, self).stop()

    def listening(self, ctx):
        """Respond to confirm that the rpc service is still alive."""
        return True

    def register_cluster(self, cluster_id, check_type, interval=None,
                         **params):
        """Register cluster for health checking.

        :param cluster_id: The ID of the cluster to be checked.
        :param check_type: A string indicating the type of checks.
        :param interval: An optional integer indicating the length of checking
                         periods in seconds.
        :param \*\*params: Other parameters for the health check.
        :return: None
        """
        registry = db_api.registry_create(self.ctx, cluster_id, check_type,
                                          interval, params, self.engine_id)
        self.rt['registries'].append(registry)

    def unregister_cluster(self, cluster_id):
        """Unregister a cluster from health checking.

        :param cluster_id: The ID of the cluste to be unregistered.
        :return: None
        """
        for registry in self.rt['registries']:
            if registry.cluster_id == cluster_id:
                self.rt['registries'].remove(registry)
        db_api.registry_delete(self.ctx, cluster_id=cluster_id)


def notify(engine_id, method, *args, **kwargs):
    """Send notification to health manager service.

    :param engine_id: dispatcher to notify; broadcast if value is None
    :param method: remote method to call
    """

    timeout = cfg.CONF.engine_life_check_timeout
    client = rpc_messaging.get_rpc_client(version=consts.RPC_API_VERSION)

    if engine_id:
        # Notify specific dispatcher identified by engine_id
        call_context = client.prepare(
            version=consts.RPC_API_VERSION,
            timeout=timeout,
            topic=consts.ENGINE_HEALTH_MGR_TOPIC,
            server=engine_id)
    else:
        # Broadcast to all disptachers
        call_context = client.prepare(
            version=consts.RPC_API_VERSION,
            timeout=timeout,
            topic=consts.ENGINE_HEALTH_MGR_TOPIC)

    ctx = context.get_admin_context()

    try:
        call_context.call(ctx, method, *args, **kwargs)
        return True
    except oslo_messaging.MessagingTimeout:
        return False


def register(cluster_id, engine_id=None, *args, **kwargs):
    return notify(engine_id, 'register_cluster', cluster_id, *args, **kwargs)


def unregister(cluster_id, engine_id=None):
    return notify(engine_id, 'unregister_cluster', cluster_id)


def list_opts():
    yield None, health_mgr_opts
