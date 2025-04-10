# THIS FILE IS PART OF THE CYLC WORKFLOW ENGINE.
# Copyright (C) NIWA & British Crown (Met Office) & Contributors.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Log the number and size of each type of object in the data store.

.. note::

   This plugin is for Cylc developers debugging the data store.

If ``matplotlib`` is installed this plugin will plot results as a PDF in
the run directory when the workflow is shut down (cleanly).

"""
import json
from pathlib import Path
import sys
from time import time

from cylc.flow.data_store_mgr import DATA_TEMPLATE
from cylc.flow.main_loop.log_memory import _transpose

from cylc.uiserver.data_store_mgr import DataStoreMgr

try:
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import pyplot as plt
    PLT = True
except ModuleNotFoundError:
    PLT = False

from pympler.asizeof import asizeof, asized, Asizer

from traitlets.config.loader import Config

MIN_SIZE = 1000


def _compute_sizes(obj, min_size=10000):
    """Return the sizes of the attributes of an object."""
    asizer = Asizer()
    asizer.exclude_types(MemoryProfiler, Config)
    size = asizer.asized(
        obj.workflows_mgr,
        obj.data_store_mgr,
        obj.executor,
        obj.handlers,
        obj.resolvers,
        detail=2
    )
    t_asizer = Asizer()
    t_asizer.exclude_types(MemoryProfiler)
    return {
        **{
            item.name.split(':')[0][4:]: item.size
            for ot in size
            for ref in ot.refs
            for item in ref.refs
            if ref.name == '__dict__' and item.size > min_size
        },
        **{'Selected Total': sum(i.size for i in size)},
        **{'Traitlets Config': t_asizer.asizeof(obj.extension_config)},
        **{'Total': t_asizer.asizeof(obj)},
    }


def _dump(data, path):
    json.dump(
        data,
        Path(path, f'{__name__}-uis-attrs.json').open('w+')
    )
    return True


def _plot(fields, times, path, title='Objects'):
    if (
            not PLT
            or len(times) < 2
    ):
        return False

    fig, ax1 = plt.subplots(figsize=(10, 7.5))

    fig.suptitle(title)
    ax1.set_xlabel('Time (s)')
    ax1.set_ylabel('Memory (kb)')

    for key, sizes in fields.items():
        ax1.plot(times, [x / 1000 for x in sizes], label=key)

    ax1.legend(loc=0)

    # start both axis at 0
    ax1.set_xlim(0, ax1.get_xlim()[1])
    ax1.set_ylim(0, ax1.get_ylim()[1])

    plt.savefig(
        Path(path, f'{__name__}-uis-attrs.pdf')
    )
    return True


def _dump_duo(state, path):
    data = {
        'times': state['times'],
        'objects': state['objects'],
        'size': state['size']
    }
    json.dump(
        data,
        Path(path, f'{__name__}-store-workflow-mgrs.json').open('w+')
    )
    return True


def _plot_duo(state, path):
    if (
            not PLT
            or len(state['times']) < 2
    ):
        return False

    times = [tick - state['times'][0] for tick in state['times']]
    fig, ax1 = plt.subplots(figsize=(10, 7.5), layout='constrained')

    ax1.set_xlabel('Time (s)')

    ax1.set_ylabel('Size (kb)')
    for key, sizes in state['size'].items():
        ax1.plot(times, [x / 1000 for x in sizes], label=key)

    ax2 = ax1.twinx()
    ax2.set_ylabel('Objects')
    for objects in state['objects'].values():
        ax2.plot(times, objects, linestyle=':', markevery=2)

    fig.legend(loc='outside lower center', mode='expand', ncols=4)
    ax2.legend(
        (ax1.get_children()[0], ax2.get_children()[0]),
        ('size', 'objects'),
        loc=1
    )

    # start the x-axis at zero
    ax1.set_xlim(0, ax1.get_xlim()[1])

    plt.savefig(
        Path(path, f'{__name__}-store-workflow-mgrs.pdf')
    )
    return True


class MemoryProfiler:
    """Manage the collection and reporting of object sizes."""

    def __init__(self, uiserver, log) -> None:
        self.uiserver = uiserver
        self.log = log

        self.state = {
            'objects': {},
            'size': {},
            'times': [],
            'data': [],
        }

        # Register data-store and other objects of interest.
        for key in DATA_TEMPLATE:
            self.state['objects'][key] = []
            self.state['size'][key] = []

        self.uis_objects = {
            'DSM-Data': self.uiserver.data_store_mgr.data,
            'DSM-WSubs': self.uiserver.data_store_mgr.w_subs,
            'DSM-Delta-Qs': self.uiserver.data_store_mgr.delta_queues,
            'DSM-Executor': self.uiserver.data_store_mgr.executor,
            'WM-Workflows': self.uiserver.workflows_mgr.workflows,
            'WM-Queue': self.uiserver.workflows_mgr._queue,
        }
        for key in self.uis_objects:
            self.state['size'][key] = []

    async def log_object_sizes(self):
        now = time()
        self.state['times'].append(now)
        for aisle in DATA_TEMPLATE:
            cumulative_aisle_lengths = 0
            cumulative_aisle_sizes = 0
            for value in self._iter_data_store(aisle):
                if value and aisle == 'workflow':
                    len_ = 1
                else:
                    len_ = len(value)
                cumulative_aisle_lengths = cumulative_aisle_lengths + len_
                cumulative_aisle_sizes = (
                    cumulative_aisle_sizes + asized(value).size
                )
            self.state['objects'][aisle].append(cumulative_aisle_lengths)
            self.state['size'][aisle].append(cumulative_aisle_sizes)

        for tag, obj in self.uis_objects.items():
            self.state['size'][tag].append(asized(obj, detail=0).size)

        self.state['data'].append((
            now,
            _compute_sizes(self.uiserver, min_size=MIN_SIZE)
        ))

    def _iter_data_store(self, aisle):
        aisle_exists = False
        for flow_store in self.uiserver.data_store_mgr.data.values():
            if aisle in flow_store:
                aisle_exists = True
                yield flow_store[aisle]
        if not aisle_exists:
            yield []

    async def report(self):
        _dump_duo(self.state, Path.cwd())
        _plot_duo(self.state, Path.cwd())
        _dump(self.state['data'], Path.cwd())
        fields, times = _transpose(self.state['data'])
        _plot(
            fields,
            times,
            Path.cwd(),
            f'cylc.uiserver.app.uiserver attrs > {MIN_SIZE / 1000}kb'
        )

