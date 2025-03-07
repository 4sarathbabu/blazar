# -*- coding: utf-8 -*-
#
# Author: François Rossigneux <francois.rossigneux@inria.fr>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import datetime

from novaclient import exceptions as nova_exceptions
from oslo_config import cfg
from oslo_utils import strutils

from blazar import context
from blazar.db import api as db_api
from blazar.db import exceptions as db_ex
from blazar.db import utils as db_utils
from blazar.manager import exceptions as manager_ex
from blazar.plugins import base
from blazar.plugins import monitor
from blazar.plugins import oshosts as plugin
from blazar import status
from blazar.utils.openstack import heat
from blazar.utils.openstack import ironic
from blazar.utils.openstack import nova
from blazar.utils.openstack import placement
from blazar.utils import plugins as plugins_utils
from oslo_log import log as logging
from random import shuffle
from uuid import UUID


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
    cfg.BoolOpt('display_default_resource_properties',
                default=True,
                help='Display default resource_properties if allocation fails '
                     'due to not enough resources'),
    cfg.BoolOpt('retry_allocation_without_defaults',
                default=True,
                help='Whether an allocation should be retried on failure '
                     'without the default properties'),
]

plugin_opts.extend(monitor.monitor_opts)

CONF = cfg.CONF
CONF.register_opts(plugin_opts, group=plugin.RESOURCE_TYPE)
LOG = logging.getLogger(__name__)


before_end_options = ['', 'snapshot', 'default', 'email']
on_start_options = ['', 'default', 'orchestration']

QUERY_TYPE_ALLOCATION = 'allocation'

MONITOR_ARGS = {"resource_type": plugin.RESOURCE_TYPE}


class PhysicalHostPlugin(base.BasePlugin, nova.NovaClientWrapper):
    """Plugin for physical host resource."""
    resource_type = plugin.RESOURCE_TYPE
    title = 'Physical Host Plugin'
    description = 'This plugin starts and shutdowns the hosts.'
    freepool_name = CONF.nova.aggregate_freepool_name
    pool = None
    query_options = {
        QUERY_TYPE_ALLOCATION: ['lease_id', 'reservation_id']
    }

    def __init__(self):
        super(PhysicalHostPlugin, self).__init__()
        self.monitor = PhysicalHostMonitorPlugin(**MONITOR_ARGS)
        self.monitor.register_reallocater(self._reallocate)
        self.placement_client = placement.BlazarPlacementClient()

    def reserve_resource(self, reservation_id, values):
        """Create reservation."""
        ctx = context.current()
        host_ids = self.allocation_candidates(values)

        if not host_ids:
            raise manager_ex.NotEnoughHostsAvailable()

        pool = nova.ReservationPool()
        pool_name = reservation_id
        az_name = "%s%s" % (CONF[self.resource_type].blazar_az_prefix,
                            pool_name)
        pool_instance = pool.create(
            name=pool_name, project_id=ctx.project_id, az=az_name)
        host_rsrv_values = {
            'reservation_id': reservation_id,
            'aggregate_id': pool_instance.id,
            'resource_properties': values['resource_properties'],
            'hypervisor_properties': values['hypervisor_properties'],
            'count_range': values['count_range'],
            'status': 'pending',
            'before_end': values['before_end'],
            'on_start': values['on_start']
        }
        host_reservation = db_api.host_reservation_create(host_rsrv_values)
        for host_id in host_ids:
            db_api.host_allocation_create({'compute_host_id': host_id,
                                          'reservation_id': reservation_id})
        return host_reservation['id']

    def update_reservation(self, reservation_id, values):
        """Update reservation."""
        reservation = db_api.reservation_get(reservation_id)
        lease = db_api.lease_get(reservation['lease_id'])

        if (not [x for x in values.keys() if x in ['min', 'max',
                                                   'hypervisor_properties',
                                                   'resource_properties']]
                and values['start_date'] >= lease['start_date']
                and values['end_date'] <= lease['end_date']):
            # Nothing to update
            return

        dates_before = {'start_date': lease['start_date'],
                        'end_date': lease['end_date']}
        dates_after = {'start_date': values['start_date'],
                       'end_date': values['end_date']}
        host_reservation = db_api.host_reservation_get(
            reservation['resource_id'])
        self._update_allocations(dates_before, dates_after, reservation_id,
                                 reservation['status'], host_reservation,
                                 values)

        updates = {}
        if 'min' in values or 'max' in values:
            count_range = str(values.get(
                'min', host_reservation['count_range'].split('-')[0])
            ) + '-' + str(values.get(
                'max', host_reservation['count_range'].split('-')[1])
            )
            updates['count_range'] = count_range
        if 'hypervisor_properties' in values:
            updates['hypervisor_properties'] = values.get(
                'hypervisor_properties')
        if 'resource_properties' in values:
            updates['resource_properties'] = values.get(
                'resource_properties')
        if updates:
            db_api.host_reservation_update(host_reservation['id'], updates)

    def on_start(self, resource_id, lease=None):
        """Add the hosts in the pool."""
        host_reservation = db_api.host_reservation_get(resource_id)
        pool = nova.ReservationPool()
        hosts = []
        for allocation in db_api.host_allocation_get_all_by_values(
                reservation_id=host_reservation['reservation_id']):
            host = db_api.host_get(allocation['compute_host_id'])
            hosts.append(host['hypervisor_hostname'])
        pool.add_computehost(host_reservation['aggregate_id'], hosts)

        action = host_reservation.get('on_start', 'default')

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
        host_reservation = db_api.host_reservation_get(resource_id)

        action = host_reservation['before_end']
        if action == 'default':
            action = CONF[plugin.RESOURCE_TYPE].before_end

        if action == 'snapshot':
            pool = nova.ReservationPool()
            client = nova.BlazarNovaClient()
            for host in pool.get_computehosts(
                    host_reservation['aggregate_id']):
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
        host_reservation = db_api.host_reservation_get(resource_id)
        db_api.host_reservation_update(host_reservation['id'],
                                       {'status': 'completed'})
        allocations = db_api.host_allocation_get_all_by_values(
            reservation_id=host_reservation['reservation_id'])
        for allocation in allocations:
            db_api.host_allocation_destroy(allocation['id'])
        pool = nova.ReservationPool()
        for host in pool.get_computehosts(host_reservation['aggregate_id']):
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
            pool.delete(host_reservation['aggregate_id'])
        except manager_ex.AggregateNotFound:
            pass

    def _reallocate(self, allocation):
        """Allocate an alternative host.

        :param allocation: allocation to change.
        :return: True if an alternative host was successfully allocated.
        """
        reservation = db_api.reservation_get(allocation['reservation_id'])
        h_reservation = db_api.host_reservation_get(
            reservation['resource_id'])
        lease = db_api.lease_get(reservation['lease_id'])
        pool = nova.ReservationPool()

        # Remove the old host from the aggregate.
        if reservation['status'] == status.reservation.ACTIVE:
            host = db_api.host_get(allocation['compute_host_id'])

            servers = self.nova.servers.list(search_opts={
                "node": host['hypervisor_hostname'], "all_tenants": 1})
            if len(servers) != 0:
                raise manager_ex.HostHavingServers(
                    servers=[s.name for s in servers],
                    host=host['hypervisor_hostname']
                )

            pool.remove_computehost(h_reservation['aggregate_id'],
                                    host['hypervisor_hostname'])

        # Allocate an alternative host.
        start_date = max(datetime.datetime.utcnow(), lease['start_date'])
        new_hostids = self._matching_hosts(
            reservation['hypervisor_properties'],
            reservation['resource_properties'],
            '1-1', start_date, lease['end_date']
        )
        if not new_hostids:
            db_api.host_allocation_destroy(allocation['id'])
            LOG.warn('Could not find alternative host for reservation %s '
                     '(lease: %s).', reservation['id'], lease['name'])
            return False
        else:
            new_hostid = new_hostids.pop()
            db_api.host_allocation_update(allocation['id'],
                                          {'compute_host_id': new_hostid})
            LOG.warn('Resource changed for reservation %s (lease: %s).',
                     reservation['id'], lease['name'])
            if reservation['status'] == status.reservation.ACTIVE:
                # Add the alternative host into the aggregate.
                new_host = db_api.host_get(new_hostid)
                pool.add_computehost(h_reservation['aggregate_id'],
                                     new_host['hypervisor_hostname'])

            return True

    def _get_extra_capabilities(self, host_id):
        extra_capabilities = {}
        raw_extra_capabilities = (
            db_api.host_extra_capability_get_all_per_host(host_id))
        for capability, capability_name in raw_extra_capabilities:
            key = capability_name
            extra_capabilities[key] = capability.capability_value
        return extra_capabilities

    def get(self, host_id):
        return self.get_computehost(host_id)

    def get_computehost(self, host_id):
        host = db_api.host_get(host_id)
        extra_capabilities = self._get_extra_capabilities(host_id)
        if host is not None and extra_capabilities:
            res = host.copy()
            res.update(extra_capabilities)
            return res
        else:
            return host

    def list_computehosts(self, query=None):
        raw_host_list = db_api.host_list()
        host_list = []
        for host in raw_host_list:
            host_list.append(self.get_computehost(host['id']))
        return host_list

    def create_computehost(self, host_values):
        # TODO(sbauza):
        #  - Exception handling for HostNotFound
        host_id = host_values.pop('id', None)
        host_name = host_values.pop('name', None)
        try:
            trust_id = host_values.pop('trust_id')
        except KeyError:
            raise manager_ex.MissingTrustId()

        host_ref = host_id or host_name
        if host_ref is None:
            raise manager_ex.InvalidHost(host=host_values)

        inventory = nova.NovaInventory()
        servers = inventory.get_servers_per_host(host_ref)
        if servers:
            raise manager_ex.HostHavingServers(host=host_ref,
                                               servers=servers)
        host_details = inventory.get_host_details(host_ref)
        # NOTE(sbauza): Only last duplicate name for same extra capability
        # will be stored
        to_store = set(host_values.keys()) - set(host_details.keys())
        extra_capabilities_keys = to_store
        extra_capabilities = dict(
            (key, host_values[key]) for key in extra_capabilities_keys
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
        try:
            if trust_id:
                host_details.update({'trust_id': trust_id})
            host = db_api.host_create(host_details)
        except db_ex.BlazarDBException as e:
            # We need to rollback
            # TODO(sbauza): Investigate use of Taskflow for atomic
            # transactions
            # NOTE(jason): CHAMELEON-ONLY
            # changed from 'service_name' to 'hypervisor_hostname'
            pool.remove_computehost(self.freepool_name,
                                    host_details['hypervisor_hostname'])
            self.placement_client.delete_reservation_provider(
                host_details['hypervisor_hostname'])
            raise e
        for key in extra_capabilities:
            values = {'computehost_id': host['id'],
                      'capability_name': key,
                      'capability_value': extra_capabilities[key],
                      }
            try:
                db_api.host_extra_capability_create(values)
            except db_ex.BlazarDBException:
                cantaddextracapability.append(key)
        if cantaddextracapability:
            raise manager_ex.CantAddExtraCapability(
                keys=cantaddextracapability,
                host=host['id'])
        return self.get_computehost(host['id'])

    def is_updatable_extra_capability(self, capability, capability_name):
        reservations = db_utils.get_reservations_by_host_id(
            capability['computehost_id'], datetime.datetime.utcnow(),
            datetime.date.max)

        for r in reservations:
            plugin_reservation = db_utils.get_plugin_reservation(
                r['resource_type'], r['resource_id'])

            requirements_queries = plugins_utils.convert_requirements(
                plugin_reservation['resource_properties'])

            # TODO(masahito): If all the reservations using the
            # extra_capability can be re-allocated it's okay to update
            # the extra_capability.
            for requirement in requirements_queries:
                # A requirement is of the form "key op value" as string
                if requirement.split(" ")[0] == capability_name:
                    return False
        return True

    def update_computehost(self, host_id, values):
        # nothing to update
        if not values:
            return self.get_computehost(host_id)

        cant_update_extra_capability = []
        previous_capabilities = self._get_extra_capabilities(host_id)
        updated_keys = set(values.keys()) & set(previous_capabilities.keys())
        new_keys = set(values.keys()) - set(previous_capabilities.keys())

        for key in updated_keys:
            raw_capability, cap_name = next(iter(
                db_api.host_extra_capability_get_all_per_name(host_id, key)))
            capability = {'capability_value': values[key]}

            if self.is_updatable_extra_capability(raw_capability, cap_name):
                try:
                    if values[key] is not None:
                        capability = {'capability_value': values[key]}
                        db_api.host_extra_capability_update(
                            raw_capability['id'], capability)
                    else:
                        db_api.host_extra_capability_destroy(
                            raw_capability['id'])
                except (db_ex.BlazarDBException, RuntimeError):
                    cant_update_extra_capability.append(cap_name)
            else:
                LOG.info("Capability %s can't be updated because "
                         "existing reservations require it.",
                         cap_name)
                cant_update_extra_capability.append(cap_name)

        for key in new_keys:
            new_capability = {
                'computehost_id': host_id,
                'capability_name': key,
                'capability_value': values[key],
            }
            try:
                db_api.host_extra_capability_create(new_capability)
            except (db_ex.BlazarDBException, RuntimeError):
                cant_update_extra_capability.append(key)

        if cant_update_extra_capability:
            raise manager_ex.CantAddExtraCapability(
                host=host_id, keys=cant_update_extra_capability)

        LOG.info('Extra capabilities on compute host %s updated with %s',
                 host_id, values)
        return self.get_computehost(host_id)

    def delete_computehost(self, host_id):
        host = db_api.host_get(host_id)
        if not host:
            raise manager_ex.HostNotFound(host=host_id)

        if db_api.host_allocation_get_all_by_values(
                compute_host_id=host_id):
            raise manager_ex.CantDeleteHost(
                host=host_id,
                msg='The host is reserved.'
            )

        try:
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
        except manager_ex.HostNotFound:
            LOG.warning(
                "Host %s not found in Nova and could not be cleaned up. Some "
                "manual cleanup may be required.", host['hypervisor_hostname']
            )

        try:
            # NOTE(sbauza): Extracapabilities will be destroyed thanks to
            #  the DB FK.
            db_api.host_destroy(host_id)
        except db_ex.BlazarDBException as e:
            # Nothing so bad, but we need to alert admins
            # they have to rerun
            raise manager_ex.CantDeleteHost(host=host_id, msg=str(e))

    def list_allocations(self, query, detail=False):
        hosts_id_list = [h['id'] for h in db_api.host_list()]
        options = self.get_query_options(query, QUERY_TYPE_ALLOCATION)
        options['detail'] = detail
        hosts_allocations = self.query_host_allocations(hosts_id_list,
                                                        **options)
        self.add_extra_allocation_info(hosts_allocations)
        return [{"resource_id": host, "reservations": allocs}
                for host, allocs in hosts_allocations.items()]

    def get_allocations(self, host_id, query, detail=False):
        options = self.get_query_options(query, QUERY_TYPE_ALLOCATION)
        options['detail'] = detail
        host_allocations = self.query_host_allocations([host_id], **options)
        allocs = host_allocations.get(host_id, [])
        return {"resource_id": host_id, "reservations": allocs}

    def reallocate_computehost(self, host_id, data):
        allocations = self.get_allocations(host_id, data, detail=True)

        for alloc in allocations['reservations']:
            reservation_flags = {}
            host_allocation = db_api.host_allocation_get_all_by_values(
                compute_host_id=host_id,
                reservation_id=alloc['id'])[0]

            if self._reallocate(host_allocation):
                if alloc['status'] == status.reservation.ACTIVE:
                    reservation_flags.update(dict(resources_changed=True))
                    db_api.lease_update(alloc['lease_id'], dict(degraded=True))
            else:
                reservation_flags.update(dict(missing_resources=True))
                db_api.lease_update(alloc['lease_id'], dict(degraded=True))

            db_api.reservation_update(alloc['id'], reservation_flags)

        return self.get_allocations(host_id, data)

    def query_allocations(self, hosts, detail=None, lease_id=None,
                          reservation_id=None):
        return self.query_host_allocations(hosts, detail=detail,
                                           lease_id=lease_id,
                                           reservation_id=reservation_id)

    def query_host_allocations(self, hosts, detail=None, lease_id=None,
                               reservation_id=None):
        """Return dict of host and its allocations.

        The list element forms
        {
          'host-id': [
                       {
                         'lease_id': lease_id,
                         'id': reservation_id,
                         'start_date': lease_start_date,
                         'end_date': lease_end_date
                       },
                     ]
        }.
        """
        start = datetime.datetime.utcnow()
        end = datetime.date.max

        reservations = db_utils.get_reservation_allocations_by_host_ids(
            hosts, start, end, lease_id, reservation_id)
        host_allocations = {h: [] for h in hosts}

        for reservation in reservations:
            if not detail:
                del reservation['project_id']
                del reservation['lease_name']
                del reservation['status']

            for host_id in reservation['host_ids']:
                if host_id in host_allocations.keys():
                    host_allocations[host_id].append({
                        k: v for k, v in reservation.items()
                        if k != 'host_ids'})

        return host_allocations

    def update_default_parameters(self, values):
        self.add_default_resource_properties(values)

    def allocation_candidates(self, values):
        self._check_params(values)

        host_ids = self._matching_hosts(
            values['hypervisor_properties'],
            values['resource_properties'],
            values['count_range'],
            values['start_date'],
            values['end_date'])

        min_hosts, _ = [int(n) for n in values['count_range'].split('-')]

        if len(host_ids) < min_hosts:
            raise manager_ex.NotEnoughHostsAvailable()

        return host_ids

    def _matching_hosts(self, hypervisor_properties, resource_properties,
                        count_range, start_date, end_date):
        """Return the matching hosts (preferably not allocated)

        """
        count_range = count_range.split('-')
        min_host = count_range[0]
        max_host = count_range[1]
        allocated_host_ids = []
        not_allocated_host_ids = []
        filter_array = []
        start_date_with_margin = start_date - datetime.timedelta(
            minutes=CONF.cleaning_time)
        end_date_with_margin = end_date + datetime.timedelta(
            minutes=CONF.cleaning_time)

        # TODO(frossigneux) support "or" operator
        if hypervisor_properties:
            filter_array = plugins_utils.convert_requirements(
                hypervisor_properties)
        if resource_properties:
            filter_array += plugins_utils.convert_requirements(
                resource_properties)
        for host in db_api.reservable_host_get_all_by_queries(filter_array):
            if not db_api.host_allocation_get_all_by_values(
                    compute_host_id=host['id']):
                not_allocated_host_ids.append(host['id'])
            elif db_utils.get_free_periods(
                host['id'],
                start_date_with_margin,
                end_date_with_margin,
                end_date_with_margin - start_date_with_margin
            ) == [
                (start_date_with_margin, end_date_with_margin),
            ]:
                allocated_host_ids.append(host['id'])
        if len(not_allocated_host_ids) >= int(min_host):
            shuffle(not_allocated_host_ids)
            return not_allocated_host_ids[:int(max_host)]
        all_host_ids = allocated_host_ids + not_allocated_host_ids
        if len(all_host_ids) >= int(min_host):
            shuffle(all_host_ids)
            return all_host_ids[:int(max_host)]
        else:
            return []

    def _convert_int_param(self, param, name):
        """Checks that the parameter is present and can be converted to int."""
        if param is None:
            raise manager_ex.MissingParameter(param=name)
        if strutils.is_int_like(param):
            param = int(param)
        else:
            raise manager_ex.MalformedParameter(param=name)
        return param

    def _check_params(self, values):
        self._validate_min_max_range(values, values.get('min'),
                                     values.get('max'))

        if 'hypervisor_properties' not in values:
            raise manager_ex.MissingParameter(param='hypervisor_properties')
        if 'resource_properties' not in values:
            raise manager_ex.MissingParameter(param='resource_properties')

        if 'before_end' not in values:
            values['before_end'] = 'default'
        if values['before_end'] not in before_end_options:
            raise manager_ex.MalformedParameter(param='before_end')

        if 'on_start' not in values:
            values['on_start'] = 'default'
        if not self._is_valid_on_start_option(values['on_start']):
            raise manager_ex.MalformedParameter(param='on_start')

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

    def _validate_min_max_range(self, values, min_hosts, max_hosts):
        min_hosts = self._convert_int_param(min_hosts, 'min')
        max_hosts = self._convert_int_param(max_hosts, 'max')
        if min_hosts <= 0 or max_hosts <= 0:
            raise manager_ex.MalformedParameter(
                param='min and max (must be greater than or equal to 1)')
        if max_hosts < min_hosts:
            raise manager_ex.InvalidRange()
        values['count_range'] = str(min_hosts) + '-' + str(max_hosts)

    def _update_allocations(self, dates_before, dates_after, reservation_id,
                            reservation_status, host_reservation, values):
        min_hosts = values.get('min', int(
            host_reservation['count_range'].split('-')[0]))
        max_hosts = values.get(
            'max', int(host_reservation['count_range'].split('-')[1]))
        self._validate_min_max_range(values, min_hosts, max_hosts)
        hypervisor_properties = values.get(
            'hypervisor_properties',
            host_reservation['hypervisor_properties'])
        resource_properties = values.get(
            'resource_properties',
            host_reservation['resource_properties'])
        allocs = db_api.host_allocation_get_all_by_values(
            reservation_id=reservation_id)

        allocs_to_remove = self._allocations_to_remove(
            dates_before, dates_after, max_hosts, hypervisor_properties,
            resource_properties, allocs)

        if (allocs_to_remove and
                reservation_status == status.reservation.ACTIVE):
            raise manager_ex.NotEnoughHostsAvailable()

        kept_hosts = len(allocs) - len(allocs_to_remove)
        if kept_hosts < max_hosts:
            min_hosts = min_hosts - kept_hosts \
                if (min_hosts - kept_hosts) > 0 else 0
            max_hosts = max_hosts - kept_hosts
            host_ids = self._matching_hosts(
                hypervisor_properties, resource_properties,
                str(min_hosts) + '-' + str(max_hosts),
                dates_after['start_date'], dates_after['end_date'])
            if len(host_ids) >= min_hosts:
                new_hosts = []
                pool = nova.ReservationPool()
                for host_id in host_ids:
                    db_api.host_allocation_create(
                        {'compute_host_id': host_id,
                         'reservation_id': reservation_id})
                    new_host = db_api.host_get(host_id)
                    new_hosts.append(new_host['hypervisor_hostname'])
                if reservation_status == status.reservation.ACTIVE:
                    # Add new hosts into the aggregate.
                    pool.add_computehost(host_reservation['aggregate_id'],
                                         new_hosts)
            else:
                raise manager_ex.NotEnoughHostsAvailable()

        for allocation in allocs_to_remove:
            db_api.host_allocation_destroy(allocation['id'])

    def _allocations_to_remove(self, dates_before, dates_after, max_hosts,
                               hypervisor_properties, resource_properties,
                               allocs):
        allocs_to_remove = []
        requested_host_ids = [host['id'] for host in
                              self._filter_hosts_by_properties(
                                  hypervisor_properties, resource_properties)]

        for alloc in allocs:
            if alloc['compute_host_id'] not in requested_host_ids:
                allocs_to_remove.append(alloc)
                continue
            if (dates_before['start_date'] > dates_after['start_date'] or
                    dates_before['end_date'] < dates_after['end_date']):
                reserved_periods = db_utils.get_reserved_periods(
                    alloc['compute_host_id'],
                    dates_after['start_date'],
                    dates_after['end_date'],
                    datetime.timedelta(seconds=1))

                max_start = max(dates_before['start_date'],
                                dates_after['start_date'])
                min_end = min(dates_before['end_date'],
                              dates_after['end_date'])

                if not (len(reserved_periods) == 0 or
                        (len(reserved_periods) == 1 and
                         reserved_periods[0][0] == max_start and
                         reserved_periods[0][1] == min_end)):
                    allocs_to_remove.append(alloc)

        kept_hosts = len(allocs) - len(allocs_to_remove)
        if kept_hosts > max_hosts:
            allocs_to_remove.extend(
                [allocation for allocation in allocs
                 if allocation not in allocs_to_remove
                 ][:(kept_hosts - max_hosts)]
            )

        return allocs_to_remove

    def _filter_hosts_by_properties(self, hypervisor_properties,
                                    resource_properties):
        filter = []
        if hypervisor_properties:
            filter += plugins_utils.convert_requirements(hypervisor_properties)
        if resource_properties:
            filter += plugins_utils.convert_requirements(resource_properties)
        if filter:
            return db_api.host_get_all_by_queries(filter)
        else:
            return db_api.host_list()


class PhysicalHostMonitorPlugin(monitor.GeneralMonitorPlugin,
                                nova.NovaClientWrapper):
    """Monitor plugin for physical host resource."""

    def __new__(cls, *args, **kwargs):
        return super(PhysicalHostMonitorPlugin, cls).__new__(cls, *args,
                                                             **kwargs)

    def filter_allocations(self, reservation, host_ids):
        return [alloc for alloc
                in reservation['computehost_allocations']
                if alloc['compute_host_id'] in host_ids]

    def get_reservations_by_resource_ids(self, host_ids,
                                         interval_begin, interval_end):
        return db_utils.get_reservations_by_host_ids(host_ids,
                                                     interval_begin,
                                                     interval_end)

    def get_unreservable_resourses(self):
        return db_api.unreservable_host_get_all_by_queries([])

    def get_notification_event_types(self):
        """Get event types of notification messages to handle."""
        return ['service.update']

    def notification_callback(self, event_type, payload):
        """Handle a notification message.

        It is used as a callback of a notification-based resource monitor.

        :param event_type: an event type of a notification.
        :param payload: a payload of a notification.
        :return: a dictionary of {reservation id: flags to update}
                 e.g. {'de27786d-bd96-46bb-8363-19c13b2c6657':
                       {'missing_resources': True}}
        """
        LOG.trace('Handling a notification...')
        reservation_flags = {}

        data = payload.get('nova_object.data', None)
        if data:
            if data['disabled'] or data['forced_down']:
                failed_hosts = db_api.reservable_host_get_all_by_queries(
                    ['hypervisor_hostname == ' + data['host']])
                if failed_hosts:
                    LOG.warn('%s failed.',
                             failed_hosts[0]['hypervisor_hostname'])
                    for host in failed_hosts:
                        self.set_reservable(host, False)
            else:
                recovered_hosts = db_api.host_get_all_by_queries(
                    ['reservable == 0',
                     'hypervisor_hostname == ' + data['host']])
                if recovered_hosts:
                    db_api.host_update(recovered_hosts[0]['id'],
                                       {'reservable': True})
                    LOG.warn('%s recovered.',
                             recovered_hosts[0]['hypervisor_hostname'])

        return reservation_flags

    def set_reservable(self, resource, is_reservable):
        db_api.host_update(resource["id"], {"reservable": is_reservable})
        LOG.warn('%s %s.', resource["hypervisor_hostname"],
                 "recovered" if is_reservable else "failed")

    def poll_resource_failures(self):
        """Check health of hosts by calling Nova Hypervisors API.

        :return: a list of failed hosts, a list of recovered hosts.
        """
        hosts = db_api.host_get_all_by_filters({})

        ironic_hosts = []
        nova_hosts = []
        for h in hosts:
            if 'hypervisor_type' in h and h['hypervisor_type'] == 'ironic':
                ironic_hosts.append(h)
            else:
                nova_hosts.append(h)

        failed_hosts = []
        recovered_hosts = []
        try:
            if ironic_hosts:
                invalid_power_states = ['error']
                invalid_provision_states = ['error', 'clean failed',
                                            'manageable', 'deploy failed']
                reservable_hosts = [h for h in ironic_hosts
                                    if h['reservable'] is True]
                unreservable_hosts = [h for h in ironic_hosts
                                      if h['reservable'] is False]

                ironic_client = ironic.BlazarIronicClient()
                nodes = ironic_client.ironic.node.list()
                failed_bm_ids = [n.uuid for n in nodes
                                 if n.maintenance
                                 or n.power_state in invalid_power_states
                                 or n.provision_state
                                 in invalid_provision_states]
                failed_hosts.extend([host for host in reservable_hosts
                                     if host['hypervisor_hostname']
                                     in failed_bm_ids])
                active_bm_ids = [n.uuid for n in nodes
                                 if not n.maintenance
                                 and n.provision_state in ['available']]
                recovered_hosts.extend([host for host in unreservable_hosts
                                        if host['hypervisor_hostname']
                                        in active_bm_ids])

            if nova_hosts:
                reservable_hosts = [h for h in nova_hosts
                                    if h['reservable'] is True]
                unreservable_hosts = [h for h in nova_hosts
                                      if h['reservable'] is False]

                hvs = self.nova.hypervisors.list()

                failed_hv_ids = [str(hv.id) for hv in hvs
                                 if hv.state == 'down'
                                 or hv.status == 'disabled']
                failed_hosts.extend([host for host in reservable_hosts
                                     if host['id'] in failed_hv_ids])

                active_hv_ids = [str(hv.id) for hv in hvs
                                 if hv.state == 'up'
                                 and hv.status == 'enabled']
                recovered_hosts.extend([host for host in unreservable_hosts
                                        if host['id'] in active_hv_ids])

        except Exception as e:
            LOG.exception('Skipping health check. %s', str(e))

        return failed_hosts, recovered_hosts
