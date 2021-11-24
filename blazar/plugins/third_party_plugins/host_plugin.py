from . import base
from blazar import context
from blazar.db import api as db_api
from blazar.db import exceptions as db_ex
from blazar.db import utils as db_utils
from blazar.manager import exceptions as manager_ex
from blazar.plugins.third_party_plugins import exceptions
from blazar.utils.openstack import heat
from blazar.utils.openstack import placement
from blazar.utils.openstack import nova
from blazar.utils import plugins as plugins_utils
from oslo_log import log as logging
from oslo_config import cfg

LOG = logging.getLogger(__name__)
CONF = cfg.CONF

plugin_opts = [
    cfg.StrOpt('blazar_az_prefix',
               default='blazar_',
               help='Prefix for Availability Zones created by Blazar'),
    cfg.StrOpt('before_end',
               default='',
               help='Actions which we will be taken before the end of '
                    'the lease'),
    cfg.StrOpt('default_resource_properties',
               default='',
               help='Default resource_properties when creating a lease of '
                    'this type.'),
]

before_end_options = ['', 'snapshot', 'default', 'email']
on_start_options = ['', 'default', 'orchestration']

class HostPlugin(base.BasePlugin, nova.NovaClientWrapper):
    freepool_name = CONF.nova.aggregate_freepool_name

    def __init__(self):
        super(HostPlugin, self).__init__()
        self.placement_client = placement.BlazarPlacementClient()
        CONF.register_opts(plugin_opts, group=self.resource_type())

    def resource_type(self):
        return "compute_host"

    def _validate_data(self, data, action_type):
        required_params = [
            "vcpus", "hypervisor_version", "hypervisor_hostname", "memory_mb",
            "local_gb", "service_name", "trust_id"
        ]
        optional_params = ["hypervisor_type", "availability_zone"]
        self.validate_data(data, required_params, optional_params, action_type)

    def validate_create_params(self, data):
        host_id = data.pop('id', None)
        host_name = data.pop('name', None)
        try:
            trust_id = data.pop('trust_id')
        except KeyError:
            raise manager_ex.MissingTrustId()

        host_ref = host_id or host_name
        if host_ref is None:
            raise manager_ex.InvalidHost(host=data)

        inventory = nova.NovaInventory()
        servers = inventory.get_servers_per_host(host_ref)
        if servers:
            raise manager_ex.HostHavingServers(host=host_ref,
                                               servers=servers)
        host_details = inventory.get_host_details(host_ref)
        # NOTE(sbauza): Only last duplicate name for same extra capability
        # will be stored
        to_store = set(data.keys()) - set(host_details.keys())
        extra_capabilities_keys = to_store
        extra_capabilities = dict(
            (key, data[key]) for key in extra_capabilities_keys
        )

        if any([len(key) > 64 for key in extra_capabilities_keys]):
            raise manager_ex.ExtraCapabilityTooLong()

        self.placement_client.create_reservation_provider(
            host_details['hypervisor_hostname'])

        pool = nova.ReservationPool()
        # NOTE(jason): CHAMELEON-ONLY
        # changed from 'service_name' to 'hypervisor_hostname'
        pool.add_computehost(self.freepool_name,
                             host_details['hypervisor_hostname'])

        host = None
        cantaddextracapability = []
        if trust_id:
            host_details.update({'trust_id': trust_id})

        return host_details

    def rollback_create(self, data):
        pool = nova.ReservationPool()
        pool.remove_computehost(self.freepool_name,
                                data['hypervisor_hostname'])
        self.placement_client.delete_reservation_provider(
            data['hypervisor_hostname'])

    def validate_update_params(self, data):
        return data

    def validate_delete(self, resource_id):
        inventory = nova.NovaInventory()
        servers = inventory.get_servers_per_host(
            host['hypervisor_hostname'])
        if servers:
            raise manager_ex.HostHavingServers(
                host=host['hypervisor_hostname'], servers=servers)
        pool = nova.ReservationPool()
        # NOTE(jason): CHAMELEON-ONLY
        # changed from 'service_name' to 'hypervisor_hostname'
        pool.remove_computehost(self.freepool_name,
                                host['hypervisor_hostname'])
        self.placement_client.delete_reservation_provider(
            host['hypervisor_hostname'])

    def _is_valid_on_start_option(self, value):

        if 'orchestration' in value:
            stack = value.split(':')[-1]
            try:
                UUID(stack)
                return True
            except Exception:
                return False
        else:
            return value in on_start_options

    def allocation_candidates(self, values):
        if 'before_end' not in values:
            values['before_end'] = 'default'
        if values['before_end'] not in before_end_options:
            raise manager_ex.MalformedParameter(param='before_end')

        if 'on_start' not in values:
            values['on_start'] = 'default'
        if not self._is_valid_on_start_option(values['on_start']):
            raise manager_ex.MalformedParameter(param='on_start')

        return super(HostPlugin, self).allocation_candidates(values)

    def allocate(self, reservation_id, values):
        self._validate_min_max_range(values, values["min"], values["max"])
        ctx = context.current()
        host_ids = self.allocation_candidates(values)

        if not host_ids:
            raise manager_ex.NotEnoughHostsAvailable()

        pool = nova.ReservationPool()
        pool_name = reservation_id
        az_name = "%s%s" % (CONF[self.resource_type()].blazar_az_prefix,
                            pool_name)
        pool_instance = pool.create(
            name=pool_name, project_id=ctx.project_id, az=az_name)
        rsrv_values = {
            "resource_properties": values["resource_properties"],
            "before_end": values['before_end'],
            "on_start": values['on_start'],
            "aggregate_id": pool_instance.id,
        }
        host_rsrv_values = {
            "reservation_id": reservation_id,
            "values": rsrv_values,
            "status": "pending",
            "count_range": values["count_range"],
            "resource_type": self.resource_type(),
        }
        resource_reservation = db_api.resource_reservation_create(host_rsrv_values)
        for host_id in host_ids:
            db_api.resource_allocation_create({'resource_id': host_id,
                                          'reservation_id': reservation_id})
        return resource_reservation['id']

    def on_start(self, resource_id, lease=None):
        """Add the hosts in the pool."""
        host_reservation = db_api.resource_reservation_get(resource_id)
        pool = nova.ReservationPool()
        hosts = []
        for allocation in db_api.resource_allocation_get_all_by_values(
                reservation_id=host_reservation['reservation_id']):
            host = db_api.resource_get(self.resource_type(), allocation['resource_id'])
            #hosts.append(host['hypervisor_hostname'])
        pool.add_computehost(host_reservation["values"]['aggregate_id'], hosts)

        action = host_reservation["values"].get('on_start', 'default')

        if 'orchestration' in action:
            stack_id = action.split(':')[-1]
            heat_client = heat.BlazarHeatClient()
            heat_client.heat.stacks.update(
                stack_id=stack_id,
                existing=True,
                converge=True,
                parameters=dict(
                    reservation_id=host_reservation['reservation_id']))

    def before_end(self, resource_id, lease=None):
        """Take an action before the end of a lease."""
        host_reservation = db_api.resource_reservation_get(resource_id)

        action = host_reservation["values"]['before_end']
        if action == 'default':
            action = CONF[self.resource_type()].before_end

        if action == 'snapshot':
            pool = nova.ReservationPool()
            client = nova.BlazarNovaClient()
            for host in pool.get_computehosts(
                    host_reservation["values"]['aggregate_id']):
                for server in client.servers.list(
                    search_opts={"node": host, "all_tenants": 1,
                                 "project_id": lease['project_id']}):
                    # TODO(jason): Unclear if this even works! What happens
                    # when you try to createImage on a server not owned by the
                    # authentication context (admin context in this case.) Is
                    # the snapshot owned by the admin, or the original
                    client.servers.create_image(server=server)
        elif action == 'email':
            plugins_utils.send_lease_extension_reminder(
                lease, CONF.os_region_name)

    def on_end(self, resource_id, lease=None):
        """Remove the hosts from the pool."""
        super(HostPlugin, self).on_end(resource_id, lease)
        resource_reservation = db_api.resource_reservation_get(resource_id)
        pool = nova.ReservationPool()
        for host in pool.get_computehosts(resource_reservation["values"]['aggregate_id']):
            for server in self.nova.servers.list(
                    search_opts={"node": host, "all_tenants": 1}):
                try:
                    self.nova.servers.delete(server=server)
                except nova_exceptions.NotFound:
                    LOG.info('Could not find server %s, may have been deleted '
                             'concurrently.', server)
                except Exception as e:
                    LOG.exception('Failed to delete %s: %s.', server, str(e))
        try:
            pool.delete(resource_reservation["values"]['aggregate_id'])
        except manager_ex.AggregateNotFound:
            pass
