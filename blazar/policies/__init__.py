#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import itertools

from blazar.policies import base
from blazar.policies import devices
from blazar.policies import floatingips
from blazar.policies import leases
from blazar.policies import networks
from blazar.policies import oshosts


def list_rules():
    return itertools.chain(
        base.list_rules(),
        leases.list_rules(),
        oshosts.list_rules(),
        floatingips.list_rules(),
        networks.list_rules(),
        devices.list_rules()
    )
