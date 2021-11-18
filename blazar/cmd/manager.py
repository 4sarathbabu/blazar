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

import eventlet
eventlet.monkey_patch()

import gettext
import sys
from functools import partial

from oslo_config import cfg
from oslo_service import service
from oslo_log import log as logging

gettext.install('blazar')

from blazar.db import api as db_api
from blazar.manager import service as manager_service
from blazar.notification import notifier
from blazar.utils import service as service_utils

LOG = logging.getLogger(__name__)

class ManagerServiceSingleton:
    _instance = manager_service.ManagerService()
    def __new__(self, resource_type=None):
        if resource_type:
            return partial(ManagerServiceSingleton._instance.call, resource_type)
        return ManagerServiceSingleton._instance

manager_service_instance = None

def main():
    cfg.CONF(project='blazar', prog='blazar-manager')
    service_utils.prepare_service(sys.argv)
    db_api.setup_db()
    LOG.info("set up db")
    notifier.init()
    LOG.info("inited notifier")
    service.launch(
        cfg.CONF,
        ManagerServiceSingleton(),
        restart_method='mutate'
    ).wait()


if __name__ == '__main__':
    main()
