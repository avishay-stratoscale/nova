# Copyright 2011 OpenStack Foundation  # All Rights Reserved.
#
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
"""
Tests For Scheduler Host Filters.
"""

import mock
from oslo.config import cfg
from oslo.serialization import jsonutils
from oslo.utils import timeutils
import requests
import stubout

from nova import context
from nova import db
from nova import objects
from nova.objects import base as obj_base
from nova.pci import pci_stats
from nova.scheduler import filters
from nova.scheduler.filters import trusted_filter
from nova import servicegroup
from nova import test
from nova.tests import fake_instance
from nova.tests.scheduler import fakes
from nova.virt import hardware

CONF = cfg.CONF


class TestFilter(filters.BaseHostFilter):
    pass


class TestBogusFilter(object):
    """Class that doesn't inherit from BaseHostFilter."""
    pass


class HostFiltersTestCase(test.NoDBTestCase):
    """Test case for host filters."""
    # FIXME(sirp): These tests still require DB access until we can separate
    # the testing of the DB API code from the host-filter code.
    USES_DB = True

    def fake_oat_request(self, *args, **kwargs):
        """Stubs out the response from OAT service."""
        self.oat_attested = True
        self.oat_hosts = args[2]
        return requests.codes.OK, self.oat_data

    def setUp(self):
        super(HostFiltersTestCase, self).setUp()
        self.oat_data = ''
        self.oat_attested = False
        self.stubs = stubout.StubOutForTesting()
        self.stubs.Set(trusted_filter.AttestationService, '_request',
                self.fake_oat_request)
        self.context = context.RequestContext('fake', 'fake')
        filter_handler = filters.HostFilterHandler()
        classes = filter_handler.get_matching_classes(
                ['nova.scheduler.filters.all_filters'])
        self.class_map = {}
        for cls in classes:
            self.class_map[cls.__name__] = cls

    def test_all_filters(self):
        # Double check at least a couple of known filters exist
        self.assertIn('AllHostsFilter', self.class_map)
        self.assertIn('ComputeFilter', self.class_map)

    def test_all_host_filter(self):
        filt_cls = self.class_map['AllHostsFilter']()
        host = fakes.FakeHostState('host1', 'node1', {})
        self.assertTrue(filt_cls.host_passes(host, {}))

    def _stub_service_is_up(self, ret_value):
        def fake_service_is_up(self, service):
                return ret_value
        self.stubs.Set(servicegroup.API, 'service_is_up', fake_service_is_up)

    def test_compute_filter_passes(self):
        self._stub_service_is_up(True)
        filt_cls = self.class_map['ComputeFilter']()
        filter_properties = {'instance_type': {'memory_mb': 1024}}
        service = {'disabled': False}
        host = fakes.FakeHostState('host1', 'node1',
                {'free_ram_mb': 1024, 'service': service})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_type_filter(self):
        self._stub_service_is_up(True)
        filt_cls = self.class_map['TypeAffinityFilter']()

        filter_properties = {'context': self.context,
                             'instance_type': {'id': 1}}
        filter2_properties = {'context': self.context,
                             'instance_type': {'id': 2}}

        service = {'disabled': False}
        host = fakes.FakeHostState('fake_host', 'fake_node',
                {'service': service})
        # True since empty
        self.assertTrue(filt_cls.host_passes(host, filter_properties))
        fakes.FakeInstance(context=self.context,
                           params={'host': 'fake_host', 'instance_type_id': 1})
        # True since same type
        self.assertTrue(filt_cls.host_passes(host, filter_properties))
        # False since different type
        self.assertFalse(filt_cls.host_passes(host, filter2_properties))
        # False since node not homogeneous
        fakes.FakeInstance(context=self.context,
                           params={'host': 'fake_host', 'instance_type_id': 2})
        self.assertFalse(filt_cls.host_passes(host, filter_properties))

    def test_aggregate_type_filter(self):
        self._stub_service_is_up(True)
        filt_cls = self.class_map['AggregateTypeAffinityFilter']()

        filter_properties = {'context': self.context,
                             'instance_type': {'name': 'fake1'}}
        filter2_properties = {'context': self.context,
                             'instance_type': {'name': 'fake2'}}
        service = {'disabled': False}
        host = fakes.FakeHostState('fake_host', 'fake_node',
                {'service': service})
        # True since no aggregates
        self.assertTrue(filt_cls.host_passes(host, filter_properties))
        # True since type matches aggregate, metadata
        self._create_aggregate_with_host(name='fake_aggregate',
                hosts=['fake_host'], metadata={'instance_type': 'fake1'})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))
        # False since type matches aggregate, metadata
        self.assertFalse(filt_cls.host_passes(host, filter2_properties))

    def _test_compute_filter_fails_on_service_disabled(self,
                                                       reason=None):
        self._stub_service_is_up(True)
        filt_cls = self.class_map['ComputeFilter']()
        filter_properties = {'instance_type': {'memory_mb': 1024}}
        service = {'disabled': True}
        if reason:
            service['disabled_reason'] = reason
        host = fakes.FakeHostState('host1', 'node1',
                {'free_ram_mb': 1024, 'service': service})
        self.assertFalse(filt_cls.host_passes(host, filter_properties))

    def test_compute_filter_fails_on_service_disabled_no_reason(self):
        self._test_compute_filter_fails_on_service_disabled()

    def test_compute_filter_fails_on_service_disabled(self):
        self._test_compute_filter_fails_on_service_disabled(reason='Test')

    def test_compute_filter_fails_on_service_down(self):
        self._stub_service_is_up(False)
        filt_cls = self.class_map['ComputeFilter']()
        filter_properties = {'instance_type': {'memory_mb': 1024}}
        service = {'disabled': False}
        host = fakes.FakeHostState('host1', 'node1',
                {'free_ram_mb': 1024, 'service': service})
        self.assertFalse(filt_cls.host_passes(host, filter_properties))

    def _create_aggregate_with_host(self, name='fake_aggregate',
                          metadata=None,
                          hosts=['host1']):
        values = {'name': name}
        if metadata:
            metadata['availability_zone'] = 'fake_avail_zone'
        else:
            metadata = {'availability_zone': 'fake_avail_zone'}
        result = db.aggregate_create(self.context.elevated(), values, metadata)
        for host in hosts:
            db.aggregate_host_add(self.context.elevated(), result['id'], host)
        return result

    def _do_test_isolated_hosts(self, host_in_list, image_in_list,
                            set_flags=True,
                            restrict_isolated_hosts_to_isolated_images=True):
        if set_flags:
            self.flags(isolated_images=['isolated_image'],
                       isolated_hosts=['isolated_host'],
                       restrict_isolated_hosts_to_isolated_images=
                       restrict_isolated_hosts_to_isolated_images)
        host_name = 'isolated_host' if host_in_list else 'free_host'
        image_ref = 'isolated_image' if image_in_list else 'free_image'
        filter_properties = {
            'request_spec': {
                'instance_properties': {'image_ref': image_ref}
            }
        }
        filt_cls = self.class_map['IsolatedHostsFilter']()
        host = fakes.FakeHostState(host_name, 'node', {})
        return filt_cls.host_passes(host, filter_properties)

    def test_isolated_hosts_fails_isolated_on_non_isolated(self):
        self.assertFalse(self._do_test_isolated_hosts(False, True))

    def test_isolated_hosts_fails_non_isolated_on_isolated(self):
        self.assertFalse(self._do_test_isolated_hosts(True, False))

    def test_isolated_hosts_passes_isolated_on_isolated(self):
        self.assertTrue(self._do_test_isolated_hosts(True, True))

    def test_isolated_hosts_passes_non_isolated_on_non_isolated(self):
        self.assertTrue(self._do_test_isolated_hosts(False, False))

    def test_isolated_hosts_no_config(self):
        # If there are no hosts nor isolated images in the config, it should
        # not filter at all. This is the default config.
        self.assertTrue(self._do_test_isolated_hosts(False, True, False))
        self.assertTrue(self._do_test_isolated_hosts(True, False, False))
        self.assertTrue(self._do_test_isolated_hosts(True, True, False))
        self.assertTrue(self._do_test_isolated_hosts(False, False, False))

    def test_isolated_hosts_no_hosts_config(self):
        self.flags(isolated_images=['isolated_image'])
        # If there are no hosts in the config, it should only filter out
        # images that are listed
        self.assertFalse(self._do_test_isolated_hosts(False, True, False))
        self.assertTrue(self._do_test_isolated_hosts(True, False, False))
        self.assertFalse(self._do_test_isolated_hosts(True, True, False))
        self.assertTrue(self._do_test_isolated_hosts(False, False, False))

    def test_isolated_hosts_no_images_config(self):
        self.flags(isolated_hosts=['isolated_host'])
        # If there are no images in the config, it should only filter out
        # isolated_hosts
        self.assertTrue(self._do_test_isolated_hosts(False, True, False))
        self.assertFalse(self._do_test_isolated_hosts(True, False, False))
        self.assertFalse(self._do_test_isolated_hosts(True, True, False))
        self.assertTrue(self._do_test_isolated_hosts(False, False, False))

    def test_isolated_hosts_less_restrictive(self):
        # If there are isolated hosts and non isolated images
        self.assertTrue(self._do_test_isolated_hosts(True, False, True, False))
        # If there are isolated hosts and isolated images
        self.assertTrue(self._do_test_isolated_hosts(True, True, True, False))
        # If there are non isolated hosts and non isolated images
        self.assertTrue(self._do_test_isolated_hosts(False, False, True,
                                                     False))
        # If there are non isolated hosts and isolated images
        self.assertFalse(self._do_test_isolated_hosts(False, True, True,
                                                      False))

    def test_trusted_filter_default_passes(self):
        self._stub_service_is_up(True)
        filt_cls = self.class_map['TrustedFilter']()
        filter_properties = {'context': self.context.elevated(),
                             'instance_type': {'memory_mb': 1024}}
        host = fakes.FakeHostState('host1', 'node1', {})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_trusted_filter_trusted_and_trusted_passes(self):
        self.oat_data = {"hosts": [{"host_name": "node1",
                                   "trust_lvl": "trusted",
                                   "vtime": timeutils.isotime()}]}
        self._stub_service_is_up(True)
        filt_cls = self.class_map['TrustedFilter']()
        extra_specs = {'trust:trusted_host': 'trusted'}
        filter_properties = {'context': self.context.elevated(),
                             'instance_type': {'memory_mb': 1024,
                                               'extra_specs': extra_specs}}
        host = fakes.FakeHostState('host1', 'node1', {})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_trusted_filter_trusted_and_untrusted_fails(self):
        self.oat_data = {"hosts": [{"host_name": "node1",
                                    "trust_lvl": "untrusted",
                                    "vtime": timeutils.isotime()}]}
        self._stub_service_is_up(True)
        filt_cls = self.class_map['TrustedFilter']()
        extra_specs = {'trust:trusted_host': 'trusted'}
        filter_properties = {'context': self.context.elevated(),
                             'instance_type': {'memory_mb': 1024,
                                               'extra_specs': extra_specs}}
        host = fakes.FakeHostState('host1', 'node1', {})
        self.assertFalse(filt_cls.host_passes(host, filter_properties))

    def test_trusted_filter_untrusted_and_trusted_fails(self):
        self.oat_data = {"hosts": [{"host_name": "node",
                                    "trust_lvl": "trusted",
                                    "vtime": timeutils.isotime()}]}
        self._stub_service_is_up(True)
        filt_cls = self.class_map['TrustedFilter']()
        extra_specs = {'trust:trusted_host': 'untrusted'}
        filter_properties = {'context': self.context.elevated(),
                             'instance_type': {'memory_mb': 1024,
                                               'extra_specs': extra_specs}}
        host = fakes.FakeHostState('host1', 'node1', {})
        self.assertFalse(filt_cls.host_passes(host, filter_properties))

    def test_trusted_filter_untrusted_and_untrusted_passes(self):
        self.oat_data = {"hosts": [{"host_name": "node1",
                                    "trust_lvl": "untrusted",
                                    "vtime": timeutils.isotime()}]}
        self._stub_service_is_up(True)
        filt_cls = self.class_map['TrustedFilter']()
        extra_specs = {'trust:trusted_host': 'untrusted'}
        filter_properties = {'context': self.context.elevated(),
                             'instance_type': {'memory_mb': 1024,
                                               'extra_specs': extra_specs}}
        host = fakes.FakeHostState('host1', 'node1', {})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_trusted_filter_update_cache(self):
        self.oat_data = {"hosts": [{"host_name": "node1",
                                    "trust_lvl": "untrusted",
                                    "vtime": timeutils.isotime()}]}

        filt_cls = self.class_map['TrustedFilter']()
        extra_specs = {'trust:trusted_host': 'untrusted'}
        filter_properties = {'context': self.context.elevated(),
                             'instance_type': {'memory_mb': 1024,
                                               'extra_specs': extra_specs}}
        host = fakes.FakeHostState('host1', 'node1', {})

        filt_cls.host_passes(host, filter_properties)     # Fill the caches

        self.oat_attested = False
        filt_cls.host_passes(host, filter_properties)
        self.assertFalse(self.oat_attested)

        self.oat_attested = False

        timeutils.set_time_override(timeutils.utcnow())
        timeutils.advance_time_seconds(
            CONF.trusted_computing.attestation_auth_timeout + 80)
        filt_cls.host_passes(host, filter_properties)
        self.assertTrue(self.oat_attested)

        timeutils.clear_time_override()

    def test_trusted_filter_update_cache_timezone(self):
        self.oat_data = {"hosts": [{"host_name": "node1",
                                    "trust_lvl": "untrusted",
                                    "vtime": "2012-09-09T05:10:40-04:00"}]}

        filt_cls = self.class_map['TrustedFilter']()
        extra_specs = {'trust:trusted_host': 'untrusted'}
        filter_properties = {'context': self.context.elevated(),
                             'instance_type': {'memory_mb': 1024,
                                               'extra_specs': extra_specs}}
        host = fakes.FakeHostState('host1', 'node1', {})

        timeutils.set_time_override(
            timeutils.normalize_time(
                timeutils.parse_isotime("2012-09-09T09:10:40Z")))

        filt_cls.host_passes(host, filter_properties)     # Fill the caches

        self.oat_attested = False
        filt_cls.host_passes(host, filter_properties)
        self.assertFalse(self.oat_attested)

        self.oat_attested = False
        timeutils.advance_time_seconds(
            CONF.trusted_computing.attestation_auth_timeout - 10)
        filt_cls.host_passes(host, filter_properties)
        self.assertFalse(self.oat_attested)

        timeutils.clear_time_override()

    @mock.patch('nova.db.compute_node_get_all')
    def test_trusted_filter_combine_hosts(self, mockdb):
        self.oat_data = {"hosts": [{"host_name": "node1",
                                    "trust_lvl": "untrusted",
                                    "vtime": "2012-09-09T05:10:40-04:00"}]}
        fake_compute_nodes = [
            {'hypervisor_hostname': 'node1',
             'service': {'host': 'host1'},
            },
            {'hypervisor_hostname': 'node2',
             'service': {'host': 'host2'},
            }, ]
        mockdb.return_value = fake_compute_nodes
        filt_cls = self.class_map['TrustedFilter']()
        extra_specs = {'trust:trusted_host': 'trusted'}
        filter_properties = {'context': self.context.elevated(),
                             'instance_type': {'memory_mb': 1024,
                                               'extra_specs': extra_specs}}
        host = fakes.FakeHostState('host1', 'node1', {})

        filt_cls.host_passes(host, filter_properties)     # Fill the caches
        self.assertEqual(set(self.oat_hosts), set(['node1', 'node2']))

    def test_trusted_filter_trusted_and_locale_formated_vtime_passes(self):
        self.oat_data = {"hosts": [{"host_name": "host1",
                                    "trust_lvl": "trusted",
                                    "vtime": timeutils.strtime(fmt="%c")},
                                   {"host_name": "host2",
                                    "trust_lvl": "trusted",
                                    "vtime": timeutils.strtime(fmt="%D")},
                                    # This is just a broken date to ensure that
                                    # we're not just arbitrarily accepting any
                                    # date format.
                        ]}
        self._stub_service_is_up(True)
        filt_cls = self.class_map['TrustedFilter']()
        extra_specs = {'trust:trusted_host': 'trusted'}
        filter_properties = {'context': self.context.elevated(),
                             'instance_type': {'memory_mb': 1024,
                                               'extra_specs': extra_specs}}
        host = fakes.FakeHostState('host1', 'host1', {})
        bad_host = fakes.FakeHostState('host2', 'host2', {})

        self.assertTrue(filt_cls.host_passes(host, filter_properties))
        self.assertFalse(filt_cls.host_passes(bad_host, filter_properties))

    def test_core_filter_passes(self):
        filt_cls = self.class_map['CoreFilter']()
        filter_properties = {'instance_type': {'vcpus': 1}}
        self.flags(cpu_allocation_ratio=2)
        host = fakes.FakeHostState('host1', 'node1',
                {'vcpus_total': 4, 'vcpus_used': 7})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_core_filter_fails_safe(self):
        filt_cls = self.class_map['CoreFilter']()
        filter_properties = {'instance_type': {'vcpus': 1}}
        host = fakes.FakeHostState('host1', 'node1', {})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_core_filter_fails(self):
        filt_cls = self.class_map['CoreFilter']()
        filter_properties = {'instance_type': {'vcpus': 1}}
        self.flags(cpu_allocation_ratio=2)
        host = fakes.FakeHostState('host1', 'node1',
                {'vcpus_total': 4, 'vcpus_used': 8})
        self.assertFalse(filt_cls.host_passes(host, filter_properties))

    def test_aggregate_core_filter_value_error(self):
        filt_cls = self.class_map['AggregateCoreFilter']()
        filter_properties = {'context': self.context,
                             'instance_type': {'vcpus': 1}}
        self.flags(cpu_allocation_ratio=2)
        host = fakes.FakeHostState('host1', 'node1',
                {'vcpus_total': 4, 'vcpus_used': 7})
        self._create_aggregate_with_host(name='fake_aggregate',
                hosts=['host1'],
                metadata={'cpu_allocation_ratio': 'XXX'})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))
        self.assertEqual(4 * 2, host.limits['vcpu'])

    def test_aggregate_core_filter_default_value(self):
        filt_cls = self.class_map['AggregateCoreFilter']()
        filter_properties = {'context': self.context,
                             'instance_type': {'vcpus': 1}}
        self.flags(cpu_allocation_ratio=2)
        host = fakes.FakeHostState('host1', 'node1',
                {'vcpus_total': 4, 'vcpus_used': 8})
        # False: fallback to default flag w/o aggregates
        self.assertFalse(filt_cls.host_passes(host, filter_properties))
        self._create_aggregate_with_host(name='fake_aggregate',
                hosts=['host1'],
                metadata={'cpu_allocation_ratio': '3'})
        # True: use ratio from aggregates
        self.assertTrue(filt_cls.host_passes(host, filter_properties))
        self.assertEqual(4 * 3, host.limits['vcpu'])

    def test_aggregate_core_filter_conflict_values(self):
        filt_cls = self.class_map['AggregateCoreFilter']()
        filter_properties = {'context': self.context,
                             'instance_type': {'vcpus': 1}}
        self.flags(cpu_allocation_ratio=1)
        host = fakes.FakeHostState('host1', 'node1',
                {'vcpus_total': 4, 'vcpus_used': 8})
        self._create_aggregate_with_host(name='fake_aggregate1',
                hosts=['host1'],
                metadata={'cpu_allocation_ratio': '2'})
        self._create_aggregate_with_host(name='fake_aggregate2',
                hosts=['host1'],
                metadata={'cpu_allocation_ratio': '3'})
        # use the minimum ratio from aggregates
        self.assertFalse(filt_cls.host_passes(host, filter_properties))
        self.assertEqual(4 * 2, host.limits['vcpu'])

    @staticmethod
    def _make_zone_request(zone, is_admin=False):
        ctxt = context.RequestContext('fake', 'fake', is_admin=is_admin)
        return {
            'context': ctxt,
            'request_spec': {
                'instance_properties': {
                    'availability_zone': zone
                }
            }
        }

    def test_availability_zone_filter_same(self):
        filt_cls = self.class_map['AvailabilityZoneFilter']()
        service = {'availability_zone': 'nova'}
        request = self._make_zone_request('nova')
        host = fakes.FakeHostState('host1', 'node1',
                                   {'service': service})
        self.assertTrue(filt_cls.host_passes(host, request))

    def test_availability_zone_filter_different(self):
        filt_cls = self.class_map['AvailabilityZoneFilter']()
        service = {'availability_zone': 'nova'}
        request = self._make_zone_request('bad')
        host = fakes.FakeHostState('host1', 'node1',
                                   {'service': service})
        self.assertFalse(filt_cls.host_passes(host, request))

    def test_retry_filter_disabled(self):
        # Test case where retry/re-scheduling is disabled.
        filt_cls = self.class_map['RetryFilter']()
        host = fakes.FakeHostState('host1', 'node1', {})
        filter_properties = {}
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_retry_filter_pass(self):
        # Node not previously tried.
        filt_cls = self.class_map['RetryFilter']()
        host = fakes.FakeHostState('host1', 'nodeX', {})
        retry = dict(num_attempts=2,
                     hosts=[['host1', 'node1'],  # same host, different node
                            ['host2', 'node2'],  # different host and node
                            ])
        filter_properties = dict(retry=retry)
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_retry_filter_fail(self):
        # Node was already tried.
        filt_cls = self.class_map['RetryFilter']()
        host = fakes.FakeHostState('host1', 'node1', {})
        retry = dict(num_attempts=1,
                     hosts=[['host1', 'node1']])
        filter_properties = dict(retry=retry)
        self.assertFalse(filt_cls.host_passes(host, filter_properties))

    def test_filter_num_iops_passes(self):
        self.flags(max_io_ops_per_host=8)
        filt_cls = self.class_map['IoOpsFilter']()
        host = fakes.FakeHostState('host1', 'node1',
                                   {'num_io_ops': 7})
        filter_properties = {}
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_filter_num_iops_fails(self):
        self.flags(max_io_ops_per_host=8)
        filt_cls = self.class_map['IoOpsFilter']()
        host = fakes.FakeHostState('host1', 'node1',
                                   {'num_io_ops': 8})
        filter_properties = {}
        self.assertFalse(filt_cls.host_passes(host, filter_properties))

    def test_filter_num_instances_passes(self):
        self.flags(max_instances_per_host=5)
        filt_cls = self.class_map['NumInstancesFilter']()
        host = fakes.FakeHostState('host1', 'node1',
                                   {'num_instances': 4})
        filter_properties = {}
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_filter_num_instances_fails(self):
        self.flags(max_instances_per_host=5)
        filt_cls = self.class_map['NumInstancesFilter']()
        host = fakes.FakeHostState('host1', 'node1',
                                   {'num_instances': 5})
        filter_properties = {}
        self.assertFalse(filt_cls.host_passes(host, filter_properties))

    def _test_group_anti_affinity_filter_passes(self, cls, policy):
        filt_cls = self.class_map[cls]()
        host = fakes.FakeHostState('host1', 'node1', {})
        filter_properties = {}
        self.assertTrue(filt_cls.host_passes(host, filter_properties))
        filter_properties = {'group_policies': ['affinity']}
        self.assertTrue(filt_cls.host_passes(host, filter_properties))
        filter_properties = {'group_policies': [policy]}
        filter_properties['group_hosts'] = []
        self.assertTrue(filt_cls.host_passes(host, filter_properties))
        filter_properties['group_hosts'] = ['host2']
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_group_anti_affinity_filter_passes(self):
        self._test_group_anti_affinity_filter_passes(
                'ServerGroupAntiAffinityFilter', 'anti-affinity')

    def test_group_anti_affinity_filter_passes_legacy(self):
        self._test_group_anti_affinity_filter_passes(
                'GroupAntiAffinityFilter', 'legacy')

    def _test_group_anti_affinity_filter_fails(self, cls, policy):
        filt_cls = self.class_map[cls]()
        host = fakes.FakeHostState('host1', 'node1', {})
        filter_properties = {'group_policies': [policy],
                             'group_hosts': ['host1']}
        self.assertFalse(filt_cls.host_passes(host, filter_properties))

    def test_group_anti_affinity_filter_fails(self):
        self._test_group_anti_affinity_filter_fails(
                'ServerGroupAntiAffinityFilter', 'anti-affinity')

    def test_group_anti_affinity_filter_fails_legacy(self):
        self._test_group_anti_affinity_filter_fails(
                'GroupAntiAffinityFilter', 'legacy')

    def _test_group_affinity_filter_passes(self, cls, policy):
        filt_cls = self.class_map['ServerGroupAffinityFilter']()
        host = fakes.FakeHostState('host1', 'node1', {})
        filter_properties = {}
        self.assertTrue(filt_cls.host_passes(host, filter_properties))
        filter_properties = {'group_policies': ['anti-affinity']}
        self.assertTrue(filt_cls.host_passes(host, filter_properties))
        filter_properties = {'group_policies': ['affinity'],
                             'group_hosts': ['host1']}
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_group_affinity_filter_passes(self):
        self._test_group_affinity_filter_passes(
                'ServerGroupAffinityFilter', 'affinity')

    def test_group_affinity_filter_passes_legacy(self):
        self._test_group_affinity_filter_passes(
                'GroupAffinityFilter', 'legacy')

    def _test_group_affinity_filter_fails(self, cls, policy):
        filt_cls = self.class_map[cls]()
        host = fakes.FakeHostState('host1', 'node1', {})
        filter_properties = {'group_policies': [policy],
                             'group_hosts': ['host2']}
        self.assertFalse(filt_cls.host_passes(host, filter_properties))

    def test_group_affinity_filter_fails(self):
        self._test_group_affinity_filter_fails(
                'ServerGroupAffinityFilter', 'affinity')

    def test_group_affinity_filter_fails_legacy(self):
        self._test_group_affinity_filter_fails(
                'GroupAffinityFilter', 'legacy')

    def test_aggregate_multi_tenancy_isolation_with_meta_passes(self):
        self._stub_service_is_up(True)
        filt_cls = self.class_map['AggregateMultiTenancyIsolation']()
        aggr_meta = {'filter_tenant_id': 'my_tenantid'}
        self._create_aggregate_with_host(name='fake1', metadata=aggr_meta,
                                         hosts=['host1'])
        filter_properties = {'context': self.context,
                             'request_spec': {
                                 'instance_properties': {
                                     'project_id': 'my_tenantid'}}}
        host = fakes.FakeHostState('host1', 'compute', {})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_aggregate_multi_tenancy_isolation_fails(self):
        self._stub_service_is_up(True)
        filt_cls = self.class_map['AggregateMultiTenancyIsolation']()
        aggr_meta = {'filter_tenant_id': 'other_tenantid'}
        self._create_aggregate_with_host(name='fake1', metadata=aggr_meta,
                                         hosts=['host1'])
        filter_properties = {'context': self.context,
                             'request_spec': {
                                 'instance_properties': {
                                     'project_id': 'my_tenantid'}}}
        host = fakes.FakeHostState('host1', 'compute', {})
        self.assertFalse(filt_cls.host_passes(host, filter_properties))

    def test_aggregate_multi_tenancy_isolation_no_meta_passes(self):
        self._stub_service_is_up(True)
        filt_cls = self.class_map['AggregateMultiTenancyIsolation']()
        aggr_meta = {}
        self._create_aggregate_with_host(name='fake1', metadata=aggr_meta,
                                         hosts=['host1'])
        filter_properties = {'context': self.context,
                             'request_spec': {
                                 'instance_properties': {
                                     'project_id': 'my_tenantid'}}}
        host = fakes.FakeHostState('host1', 'compute', {})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def _fake_pci_support_requests(self, pci_requests):
        self.pci_requests = pci_requests
        return self.pci_request_result

    def test_pci_passthrough_pass(self):
        filt_cls = self.class_map['PciPassthroughFilter']()
        request = objects.InstancePCIRequest(count=1,
            spec=[{'vendor_id': '8086'}])
        requests = objects.InstancePCIRequests(requests=[request])
        filter_properties = {'pci_requests': requests}
        self.stubs.Set(pci_stats.PciDeviceStats, 'support_requests',
                       self._fake_pci_support_requests)
        host = fakes.FakeHostState(
            'host1', 'node1',
            attribute_dict={'pci_stats': pci_stats.PciDeviceStats()})
        self.pci_request_result = True
        self.assertTrue(filt_cls.host_passes(host, filter_properties))
        self.assertEqual(self.pci_requests, requests.requests)

    def test_pci_passthrough_fail(self):
        filt_cls = self.class_map['PciPassthroughFilter']()
        request = objects.InstancePCIRequest(count=1,
            spec=[{'vendor_id': '8086'}])
        requests = objects.InstancePCIRequests(requests=[request])
        filter_properties = {'pci_requests': requests}
        self.stubs.Set(pci_stats.PciDeviceStats, 'support_requests',
                       self._fake_pci_support_requests)
        host = fakes.FakeHostState(
            'host1', 'node1',
            attribute_dict={'pci_stats': pci_stats.PciDeviceStats()})
        self.pci_request_result = False
        self.assertFalse(filt_cls.host_passes(host, filter_properties))
        self.assertEqual(self.pci_requests, requests.requests)

    def test_pci_passthrough_no_pci_request(self):
        filt_cls = self.class_map['PciPassthroughFilter']()
        filter_properties = {}
        host = fakes.FakeHostState('h1', 'n1', {})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_pci_passthrough_comopute_stats(self):
        filt_cls = self.class_map['PciPassthroughFilter']()
        requests = [{'count': 1, 'spec': [{'vendor_id': '8086'}]}]
        filter_properties = {'pci_requests': requests}
        self.stubs.Set(pci_stats.PciDeviceStats, 'support_requests',
                       self._fake_pci_support_requests)
        host = fakes.FakeHostState(
            'host1', 'node1',
            attribute_dict={})
        self.pci_request_result = True
        self.assertRaises(AttributeError, filt_cls.host_passes,
                          host, filter_properties)

    def test_aggregate_image_properties_isolation_passes(self):
        self._stub_service_is_up(True)
        filt_cls = self.class_map['AggregateImagePropertiesIsolation']()
        aggr_meta = {'foo': 'bar'}
        self._create_aggregate_with_host(name='fake1',
                                         metadata=aggr_meta,
                                         hosts=['host1'])
        filter_properties = {'context': self.context,
                             'request_spec': {
                                 'image': {
                                     'properties': {'foo': 'bar'}}}}
        host = fakes.FakeHostState('host1', 'compute', {})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_aggregate_image_properties_isolation_multi_props_passes(self):
        self._stub_service_is_up(True)
        filt_cls = self.class_map['AggregateImagePropertiesIsolation']()
        aggr_meta = {'foo': 'bar', 'foo2': 'bar2'}
        self._create_aggregate_with_host(name='fake1',
                                         metadata=aggr_meta,
                                         hosts=['host1'])
        filter_properties = {'context': self.context,
                             'request_spec': {
                                 'image': {
                                     'properties': {'foo': 'bar',
                                                    'foo2': 'bar2'}}}}
        host = fakes.FakeHostState('host1', 'compute', {})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_aggregate_image_properties_isolation_props_with_meta_passes(self):
        self._stub_service_is_up(True)
        filt_cls = self.class_map['AggregateImagePropertiesIsolation']()
        aggr_meta = {'foo': 'bar'}
        self._create_aggregate_with_host(name='fake1',
                                         metadata=aggr_meta,
                                         hosts=['host1'])
        filter_properties = {'context': self.context,
                             'request_spec': {
                                 'image': {
                                     'properties': {}}}}
        host = fakes.FakeHostState('host1', 'compute', {})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_aggregate_image_properties_isolation_props_imgprops_passes(self):
        self._stub_service_is_up(True)
        filt_cls = self.class_map['AggregateImagePropertiesIsolation']()
        aggr_meta = {}
        self._create_aggregate_with_host(name='fake1',
                                         metadata=aggr_meta,
                                         hosts=['host1'])
        filter_properties = {'context': self.context,
                             'request_spec': {
                                 'image': {
                                     'properties': {'foo': 'bar'}}}}
        host = fakes.FakeHostState('host1', 'compute', {})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_aggregate_image_properties_isolation_props_not_match_fails(self):
        self._stub_service_is_up(True)
        filt_cls = self.class_map['AggregateImagePropertiesIsolation']()
        aggr_meta = {'foo': 'bar'}
        self._create_aggregate_with_host(name='fake1',
                                         metadata=aggr_meta,
                                         hosts=['host1'])
        filter_properties = {'context': self.context,
                             'request_spec': {
                                 'image': {
                                     'properties': {'foo': 'no-bar'}}}}
        host = fakes.FakeHostState('host1', 'compute', {})
        self.assertFalse(filt_cls.host_passes(host, filter_properties))

    def test_aggregate_image_properties_isolation_props_not_match2_fails(self):
        self._stub_service_is_up(True)
        filt_cls = self.class_map['AggregateImagePropertiesIsolation']()
        aggr_meta = {'foo': 'bar', 'foo2': 'bar2'}
        self._create_aggregate_with_host(name='fake1',
                                         metadata=aggr_meta,
                                         hosts=['host1'])
        filter_properties = {'context': self.context,
                             'request_spec': {
                                 'image': {
                                     'properties': {'foo': 'bar',
                                                    'foo2': 'bar3'}}}}
        host = fakes.FakeHostState('host1', 'compute', {})
        self.assertFalse(filt_cls.host_passes(host, filter_properties))

    def test_aggregate_image_properties_isolation_props_namespace(self):
        self._stub_service_is_up(True)
        filt_cls = self.class_map['AggregateImagePropertiesIsolation']()
        self.flags(aggregate_image_properties_isolation_namespace="np")
        aggr_meta = {'np.foo': 'bar', 'foo2': 'bar2'}
        self._create_aggregate_with_host(name='fake1',
                                         metadata=aggr_meta,
                                         hosts=['host1'])
        filter_properties = {'context': self.context,
                             'request_spec': {
                                 'image': {
                                     'properties': {'np.foo': 'bar',
                                                    'foo2': 'bar3'}}}}
        host = fakes.FakeHostState('host1', 'compute', {})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_metrics_filter_pass(self):
        self.flags(weight_setting=['foo=1', 'bar=2'], group='metrics')
        metrics = dict(foo=1, bar=2)
        host = fakes.FakeHostState('host1', 'node1',
                                   attribute_dict={'metrics': metrics})
        filt_cls = self.class_map['MetricsFilter']()
        self.assertTrue(filt_cls.host_passes(host, None))

    def test_metrics_filter_missing_metrics(self):
        self.flags(weight_setting=['foo=1', 'bar=2'], group='metrics')
        metrics = dict(foo=1)
        host = fakes.FakeHostState('host1', 'node1',
                                   attribute_dict={'metrics': metrics})
        filt_cls = self.class_map['MetricsFilter']()
        self.assertFalse(filt_cls.host_passes(host, None))

    def test_aggregate_filter_num_iops_value(self):
        self.flags(max_io_ops_per_host=7)
        filt_cls = self.class_map['AggregateIoOpsFilter']()
        host = fakes.FakeHostState('host1', 'node1',
                                   {'num_io_ops': 7})
        filter_properties = {'context': self.context}
        self.assertFalse(filt_cls.host_passes(host, filter_properties))
        self._create_aggregate_with_host(
            name='fake_aggregate',
            hosts=['host1'],
            metadata={'max_io_ops_per_host': 8})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_aggregate_filter_num_iops_value_error(self):
        self.flags(max_io_ops_per_host=8)
        filt_cls = self.class_map['AggregateIoOpsFilter']()
        host = fakes.FakeHostState('host1', 'node1',
                                   {'num_io_ops': 7})
        self._create_aggregate_with_host(
            name='fake_aggregate',
            hosts=['host1'],
            metadata={'max_io_ops_per_host': 'XXX'})
        filter_properties = {'context': self.context}
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_aggregate_disk_filter_value_error(self):
        self._stub_service_is_up(True)
        filt_cls = self.class_map['AggregateDiskFilter']()
        self.flags(disk_allocation_ratio=1.0)
        filter_properties = {
            'context': self.context,
            'instance_type': {'root_gb': 1,
                              'ephemeral_gb': 1,
                              'swap': 1024}}
        service = {'disabled': False}
        host = fakes.FakeHostState('host1', 'node1',
                                   {'free_disk_mb': 3 * 1024,
                                    'total_usable_disk_gb': 1,
                                   'service': service})
        self._create_aggregate_with_host(name='fake_aggregate',
                hosts=['host1'],
                metadata={'disk_allocation_ratio': 'XXX'})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_aggregate_disk_filter_default_value(self):
        self._stub_service_is_up(True)
        filt_cls = self.class_map['AggregateDiskFilter']()
        self.flags(disk_allocation_ratio=1.0)
        filter_properties = {
            'context': self.context,
            'instance_type': {'root_gb': 2,
                              'ephemeral_gb': 1,
                              'swap': 1024}}
        service = {'disabled': False}
        host = fakes.FakeHostState('host1', 'node1',
                                   {'free_disk_mb': 3 * 1024,
                                    'total_usable_disk_gb': 1,
                                   'service': service})
        # Uses global conf.
        self.assertFalse(filt_cls.host_passes(host, filter_properties))

        # Uses an aggregate with ratio
        self._create_aggregate_with_host(
            name='fake_aggregate',
            hosts=['host1'],
            metadata={'disk_allocation_ratio': '2'})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_filter_aggregate_num_instances_value(self):
        self.flags(max_instances_per_host=4)
        filt_cls = self.class_map['AggregateNumInstancesFilter']()
        host = fakes.FakeHostState('host1', 'node1',
                                   {'num_instances': 5})
        filter_properties = {'context': self.context}
        # No aggregate defined for that host.
        self.assertFalse(filt_cls.host_passes(host, filter_properties))
        self._create_aggregate_with_host(
            name='fake_aggregate',
            hosts=['host1'],
            metadata={'max_instances_per_host': 6})
        # Aggregate defined for that host.
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_filter_aggregate_num_instances_value_error(self):
        self.flags(max_instances_per_host=6)
        filt_cls = self.class_map['AggregateNumInstancesFilter']()
        host = fakes.FakeHostState('host1', 'node1',
                                   {'num_instances': 5})
        filter_properties = {'context': self.context}
        self._create_aggregate_with_host(
            name='fake_aggregate',
            hosts=['host1'],
            metadata={'max_instances_per_host': 'XXX'})
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_numa_topology_filter_pass(self):
        instance_topology = hardware.VirtNUMAInstanceTopology(
            cells=[hardware.VirtNUMATopologyCell(0, set([1]), 512),
                   hardware.VirtNUMATopologyCell(1, set([3]), 512)])
        instance = fake_instance.fake_instance_obj(self.context)
        instance.numa_topology = (
                objects.InstanceNUMATopology.obj_from_topology(
                    instance_topology))
        filter_properties = {
            'request_spec': {
                'instance_properties': jsonutils.to_primitive(
                    obj_base.obj_to_primitive(instance))}}
        host = fakes.FakeHostState('host1', 'node1',
                                   {'numa_topology': fakes.NUMA_TOPOLOGY})
        filt_cls = self.class_map['NUMATopologyFilter']()
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_numa_topology_filter_numa_instance_no_numa_host_fail(self):
        instance_topology = hardware.VirtNUMAInstanceTopology(
            cells=[hardware.VirtNUMATopologyCell(0, set([1]), 512),
                   hardware.VirtNUMATopologyCell(1, set([3]), 512)])
        instance = fake_instance.fake_instance_obj(self.context)
        instance.numa_topology = (
                objects.InstanceNUMATopology.obj_from_topology(
                    instance_topology))

        filter_properties = {
            'request_spec': {
                'instance_properties': jsonutils.to_primitive(
                    obj_base.obj_to_primitive(instance))}}
        host = fakes.FakeHostState('host1', 'node1', {})
        filt_cls = self.class_map['NUMATopologyFilter']()
        self.assertFalse(filt_cls.host_passes(host, filter_properties))

    def test_numa_topology_filter_numa_host_no_numa_instance_pass(self):
        instance = fake_instance.fake_instance_obj(self.context)
        instance.numa_topology = None
        filter_properties = {
            'request_spec': {
                'instance_properties': jsonutils.to_primitive(
                    obj_base.obj_to_primitive(instance))}}
        host = fakes.FakeHostState('host1', 'node1',
                                   {'numa_topology': fakes.NUMA_TOPOLOGY})
        filt_cls = self.class_map['NUMATopologyFilter']()
        self.assertTrue(filt_cls.host_passes(host, filter_properties))

    def test_numa_topology_filter_fail_fit(self):
        instance_topology = hardware.VirtNUMAInstanceTopology(
            cells=[hardware.VirtNUMATopologyCell(0, set([1]), 512),
                   hardware.VirtNUMATopologyCell(1, set([2]), 512),
                   hardware.VirtNUMATopologyCell(2, set([3]), 512)])
        instance = fake_instance.fake_instance_obj(self.context)
        instance.numa_topology = (
                objects.InstanceNUMATopology.obj_from_topology(
                    instance_topology))
        filter_properties = {
            'request_spec': {
                'instance_properties': jsonutils.to_primitive(
                    obj_base.obj_to_primitive(instance))}}
        host = fakes.FakeHostState('host1', 'node1',
                                   {'numa_topology': fakes.NUMA_TOPOLOGY})
        filt_cls = self.class_map['NUMATopologyFilter']()
        self.assertFalse(filt_cls.host_passes(host, filter_properties))

    def test_numa_topology_filter_fail_memory(self):
        self.flags(ram_allocation_ratio=1)

        instance_topology = hardware.VirtNUMAInstanceTopology(
            cells=[hardware.VirtNUMATopologyCell(0, set([1]), 1024),
                   hardware.VirtNUMATopologyCell(1, set([3]), 512)])
        instance = fake_instance.fake_instance_obj(self.context)
        instance.numa_topology = (
                objects.InstanceNUMATopology.obj_from_topology(
                    instance_topology))
        filter_properties = {
            'request_spec': {
                'instance_properties': jsonutils.to_primitive(
                    obj_base.obj_to_primitive(instance))}}
        host = fakes.FakeHostState('host1', 'node1',
                                   {'numa_topology': fakes.NUMA_TOPOLOGY})
        filt_cls = self.class_map['NUMATopologyFilter']()
        self.assertFalse(filt_cls.host_passes(host, filter_properties))

    def test_numa_topology_filter_fail_cpu(self):
        self.flags(cpu_allocation_ratio=1)

        instance_topology = hardware.VirtNUMAInstanceTopology(
            cells=[hardware.VirtNUMATopologyCell(0, set([1]), 512),
                   hardware.VirtNUMATopologyCell(1, set([3, 4, 5]), 512)])
        instance = fake_instance.fake_instance_obj(self.context)
        instance.numa_topology = (
                objects.InstanceNUMATopology.obj_from_topology(
                    instance_topology))
        filter_properties = {
            'request_spec': {
                'instance_properties': jsonutils.to_primitive(
                    obj_base.obj_to_primitive(instance))}}
        host = fakes.FakeHostState('host1', 'node1',
                                   {'numa_topology': fakes.NUMA_TOPOLOGY})
        filt_cls = self.class_map['NUMATopologyFilter']()
        self.assertFalse(filt_cls.host_passes(host, filter_properties))

    def test_numa_topology_filter_pass_set_limit(self):
        self.flags(cpu_allocation_ratio=21)
        self.flags(ram_allocation_ratio=1.3)

        instance_topology = hardware.VirtNUMAInstanceTopology(
            cells=[hardware.VirtNUMATopologyCell(0, set([1]), 512),
                   hardware.VirtNUMATopologyCell(1, set([3]), 512)])
        instance = fake_instance.fake_instance_obj(self.context)
        instance.numa_topology = (
                objects.InstanceNUMATopology.obj_from_topology(
                    instance_topology))
        filter_properties = {
            'request_spec': {
                'instance_properties': jsonutils.to_primitive(
                    obj_base.obj_to_primitive(instance))}}
        host = fakes.FakeHostState('host1', 'node1',
                                   {'numa_topology': fakes.NUMA_TOPOLOGY})
        filt_cls = self.class_map['NUMATopologyFilter']()
        self.assertTrue(filt_cls.host_passes(host, filter_properties))
        limits_topology = hardware.VirtNUMALimitTopology.from_json(
                host.limits['numa_topology'])
        self.assertEqual(limits_topology.cells[0].cpu_limit, 42)
        self.assertEqual(limits_topology.cells[1].cpu_limit, 42)
        self.assertEqual(limits_topology.cells[0].memory_limit, 665)
        self.assertEqual(limits_topology.cells[1].memory_limit, 665)
