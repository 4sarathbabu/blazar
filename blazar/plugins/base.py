# Copyright (c) 2013 Mirantis Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import abc
import collections

from blazar.db import api as db_api
from blazar.db import utils as db_utils
from blazar.utils.openstack import keystone
from oslo_config import cfg
from oslo_log import log as logging

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class BasePlugin(object, metaclass=abc.ABCMeta):

    resource_type = 'none'
    title = None
    description = None
    monitor = None
    query_options = None

    def get_plugin_opts(self):
        """Plugin can expose some options that should be specified in conf file

        For example:

            def get_plugin_opts(self):
            return [
                cfg.StrOpt('mandatory-conf', required=True),
                cfg.StrOpt('optional_conf', default="42"),
            ]
        """
        return []

    def setup(self, conf):
        """Plugin initialization

        :param conf: plugin-specific configurations
        """
        pass

    def to_dict(self):
        return {
            'resource_type': self.resource_type,
            'title': self.title,
            'description': self.description,
        }

    @abc.abstractmethod
    def get(self, resource_id):
        """Get resource by id"""
        pass

    @abc.abstractmethod
    def reserve_resource(self, reservation_id, values):
        """Reserve resource."""
        pass

    @abc.abstractmethod
    def list_allocations(self, query, detail=False):
        """List resource allocations."""
        pass

    @abc.abstractmethod
    def query_allocations(self, resource_id_list, lease_id=None,
                          reservation_id=None):
        """List resource allocations."""
        pass

    @abc.abstractmethod
    def allocation_candidates(self, lease_values):
        """Get candidates for reservation allocation."""
        pass

    @abc.abstractmethod
    def update_reservation(self, reservation_id, values):
        """Update reservation."""
        pass

    @abc.abstractmethod
    def on_end(self, resource_id, lease=None):
        """Delete resource."""
        pass

    @abc.abstractmethod
    def on_start(self, resource_id, lease=None):
        """Wake up resource."""
        pass

    def list_resource_properties(self, query):
        detail = False if not query else query.get('detail', False)
        resource_properties = collections.defaultdict(list)

        for name, private, value in db_api.resource_properties_list(
                self.resource_type):

            if not private:
                resource_properties[name].append(value)

        if detail:
            return [
                dict(property=k, private=False, values=v)
                for k, v in resource_properties.items()]
        else:
            return [dict(property=k) for k, v in resource_properties.items()]

    def update_default_parameters(self, values):
        """Update values with any defaults"""
        pass

    def add_default_resource_properties(self, values):
        if not values.get('resource_properties', ''):
            values['resource_properties'] = CONF[
                self.resource_type
            ].default_resource_properties
        return values

    def update_resource_property(self, property_name, values):
        return db_api.resource_property_update(
            self.resource_type, property_name, values)

    def before_end(self, resource_id, lease=None):
        """Take actions before the end of a lease"""
        pass

    def heal_reservations(self, failed_resources, interval_begin,
                          interval_end):
        """Heal reservations which suffer from resource failures.

        :param failed_resources: failed resources
        :param interval_begin: start date of the period to heal.
        :param interval_end: end date of the period to heal.
        :return: a dictionary of {reservation id: flags to update}
                 e.g. {'de27786d-bd96-46bb-8363-19c13b2c6657':
                       {'missing_resources': True}}
        """
        raise NotImplementedError

    def get_query_options(self, params, index_type):
        options = {k: params[k] for k in params
                   if k in self.query_options[index_type]}
        unsupported = set(params) - set(options)
        if unsupported:
            LOG.debug('Unsupported query key is specified in API request: %s',
                      unsupported)
        return options

    def is_project_allowed(self, project_id, resource):
        # If this resource has the extra capability "authorized_projects"
        if "authorized_projects" in resource and \
                isinstance(resource["authorized_projects"], str):
            # Parse the field as a CSV, and check the resulting list
            authorized_projects = resource["authorized_projects"].split(",")
            return project_id in authorized_projects
        return True

    def add_extra_allocation_info(self, resource_allocations):
        """Add extra information to allocations (to show in calendar)"""
        extras = CONF.api.allocation_extras
        for allocs in resource_allocations.values():
            for alloc in allocs:
                alloc["extras"] = {}
        if "user_name" in extras:
            ids = []
            for allocations in resource_allocations.values():
                for alloc in allocations:
                    ids.append(alloc["lease_id"])
            items = db_utils.get_user_ids_for_lease_ids(ids)
            keystoneclient = keystone.BlazarKeystoneClient()
            users = keystoneclient.users.list()
            user_map = {user.id: user for user in users}
            lease_to_name = dict()
            for lease_id, user_id in items:
                user = user_map[user_id]
                lease_to_name[lease_id] = user.name

            for allocations in resource_allocations.values():
                for alloc in allocations:
                    alloc["extras"]["user_name"] = \
                        lease_to_name[alloc["lease_id"]]


class BaseMonitorPlugin(metaclass=abc.ABCMeta):
    """Base class of monitor plugin."""
    @abc.abstractmethod
    def is_notification_enabled(self):
        """Check if the notification monitor is enabled."""
        pass

    @abc.abstractmethod
    def get_notification_event_types(self):
        """Get a list of event types of messages to handle."""
        pass

    @abc.abstractmethod
    def get_notification_topics(self):
        """Get a list of topics of notification to subscribe to."""
        pass

    @abc.abstractmethod
    def notification_callback(self, event_type, payload):
        """Handle a notification message.

        It is used as a callback of a notification based resource monitor.

        :param event_type: an event type of a notification.
        :param payload: a payload of a notification.
        :return: a dictionary of {reservation id: flags to update}
                 e.g. {'de27786d-bd96-46bb-8363-19c13b2c6657':
                       {'missing_resources': True}}
        """
        pass

    @abc.abstractmethod
    def is_polling_enabled(self):
        """Check if the polling monitor is enabled."""
        pass

    @abc.abstractmethod
    def get_polling_interval(self):
        """Get an interval of polling in seconds."""
        pass

    @abc.abstractmethod
    def poll(self):
        """Check health of resources.

        :return: a dictionary of {reservation id: flags to update}
                 e.g. {'de27786d-bd96-46bb-8363-19c13b2c6657':
                       {'missing_resources': True}}
        """
        pass

    @abc.abstractmethod
    def get_healing_interval(self):
        """Get interval of reservation healing in minutes."""
        pass

    @abc.abstractmethod
    def heal(self):
        """Heal suffering reservations.

        :return: a dictionary of {reservation id: flags to update}
        """
