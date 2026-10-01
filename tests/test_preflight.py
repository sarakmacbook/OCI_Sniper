"""Tests for the launch pre-flight check that turns a mystery 404 into a fix.

Run with the project's Python environment:

    python -m unittest tests.test_preflight -v

No network access and no real OCI SDK are needed: ``app.get_oci`` is patched with
a tiny SDK double (mirroring tests/test_bootvolumes.py) and the OCI clients are
in-memory fakes. These tests pin the real-world failure the loop used to hide:
``VM.Standard.E2.1.Micro`` (and other shapes) are not offered in every region,
and OCI reports that with a permanent ``404 NotAuthorizedOrNotFound``. The
pre-flight now detects it *before* any launch attempt is spent.
"""
import os
import types
import unittest
from unittest import mock

os.environ.setdefault('APP_PASSWORD', 'test-password')

import app  # noqa: E402

AD = 'uRBW:AP-KULAI-2-AD-1'

CRED = {
    'user': 'ocid1.user.fake',
    'tenancy': 'ocid1.tenancy.fake',
    'fingerprint': 'aa:bb',
    'region': 'ap-kulai-1',
    'key_content': 'fake-key',
}


class FakeServiceError(Exception):
    """Stand-in for oci.exceptions.ServiceError (code + status + message)."""

    def __init__(self, code='Error', status=500, message='boom'):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


class FakeModel:
    """Stands in for the oci.core.models.* details classes."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


LAUNCH_MODELS = types.SimpleNamespace(
    LaunchInstanceDetails=FakeModel,
    InstanceSourceViaImageDetails=FakeModel,
    CreateVnicDetails=FakeModel,
    LaunchInstanceShapeConfigDetails=FakeModel,
)


def _resp(data):
    return types.SimpleNamespace(data=data)


class FakeCompute:
    def __init__(self, shapes_offered, images=None, instances=None):
        self.shapes_offered = list(shapes_offered)
        self.images = images if images is not None else {}
        self.instances = instances if instances is not None else []
        # Shapes OCI says each image is compatible with. ``None`` means "not
        # modelled": the lookup reports every shape as compatible, so only tests
        # that opt in (by setting a list) exercise the chipset check.
        self.image_shape_compat = None
        self.shape_compat_raises = False
        self.compat_calls = []
        # Display names by image id; the default is an x86 image (no platform
        # marker in the name), so only tests that set this hit the ARM branch.
        self.image_names = {}
        self.launch_calls = []
        self.launch_error = None
        self.shape_lookup_ads = []
        self.list_shapes_raises = False
        self.list_shapes_empty = False
        # If set, `target_shape` starts appearing in list_shapes from this
        # 1-based call number onward — models Oracle adding a shape to a region.
        self.offer_shape_from_call = None
        self.target_shape = 'VM.Standard.E2.1.Micro'
        self._shape_calls = 0

    def get_image(self, image_id=None):
        state = self.images.get(image_id)
        if state is None:
            raise FakeServiceError(
                code='NotAuthorizedOrNotFound', status=404,
                message='Authorization failed or requested resource not found.'
            )
        return _resp(types.SimpleNamespace(
            id=image_id,
            display_name=self.image_names.get(image_id, 'Canonical-Ubuntu-24.04-2025.09.15-0'),
            lifecycle_state=state,
        ))

    def list_shapes(self, compartment_id=None, availability_domain=None):
        self.shape_lookup_ads.append(availability_domain)
        if self.list_shapes_raises:
            raise FakeServiceError(code='InternalError', status=500, message='boom')
        if self.list_shapes_empty:
            return _resp([])
        self._shape_calls += 1
        offered = list(self.shapes_offered)
        if (self.offer_shape_from_call is not None
                and self._shape_calls >= self.offer_shape_from_call
                and self.target_shape and self.target_shape not in offered):
            offered = offered + [self.target_shape]
        return _resp([types.SimpleNamespace(shape=s) for s in offered])

    def list_image_shape_compatibility_entries(self, image_id=None):
        self.compat_calls.append(image_id)
        if self.shape_compat_raises:
            raise FakeServiceError(code='InternalError', status=500, message='boom')
        compatible = self.image_shape_compat
        if compatible is None:
            compatible = ['VM.Standard.E2.1.Micro', 'VM.Standard.A1.Flex',
                          'VM.Standard.E4.Flex', 'VM.Standard.E5.Flex']
        return _resp([types.SimpleNamespace(shape=s) for s in compatible])

    def list_instances(self, compartment_id=None):
        return _resp(self.instances)

    def launch_instance(self, launch_instance_details=None):
        self.launch_calls.append(launch_instance_details)
        if self.launch_error is not None:
            raise self.launch_error
        return _resp(types.SimpleNamespace(id='ocid1.instance.new', display_name='new'))


class FakeNetwork:
    def __init__(self, subnets=None):
        self.subnets = subnets if subnets is not None else {}

    def get_subnet(self, subnet_id=None):
        state = self.subnets.get(subnet_id)
        if state is None:
            raise FakeServiceError(
                code='NotAuthorizedOrNotFound', status=404,
                message='Authorization failed or requested resource not found.'
            )
        return _resp(types.SimpleNamespace(
            id=subnet_id, display_name='sn', lifecycle_state=state,
        ))

    def list_vcns(self, compartment_id=None):
        return _resp([types.SimpleNamespace(id='vcn-1')])

    def list_subnets(self, compartment_id=None, vcn_id=None):
        return _resp([])


class FakeIdentity:
    def __init__(self, ads, user=None):
        self.ads = ads
        self.user = user if user is not None else types.SimpleNamespace(
            name='tester', email='tester@example.com', description=None,
        )

    def list_availability_domains(self, compartment_id=None):
        return _resp(self.ads)

    def get_user(self, user_id=None):
        return _resp(self.user)


class FakeBlock:
    def list_boot_volumes(self, compartment_id=None, availability_domain=None):
        return _resp([])


def make_sdk(compute, network, block, identity):
    clients = {'compute': compute, 'network': network, 'block': block, 'identity': identity}
    sdk = types.SimpleNamespace(
        config=types.SimpleNamespace(validate_config=lambda config: None),
        core=types.SimpleNamespace(
            ComputeClient='compute',
            VirtualNetworkClient='network',
            BlockstorageClient='block',
            models=LAUNCH_MODELS,
        ),
        identity=types.SimpleNamespace(IdentityClient='identity'),
        exceptions=types.SimpleNamespace(ServiceError=FakeServiceError),
    )
    return sdk, clients


def make_account(**overrides):
    account = {
        'shape': 'VM.Standard.E2.1.Micro',
        'image_id': 'ocid1.image.apkulai',
        'subnet_id': 'ocid1.subnet.apkulai',
        'ssh_key': 'ssh-rsa AAAAB3NzaC1yc2EAAAADAQAB test',
        'boot_volume_gb': 50,
        'display_name': 'test-bot',
        'ad_preference': '',
    }
    account.update(overrides)
    return account


class PreflightTestBase(unittest.TestCase):
    def setUp(self):
        self.compute = FakeCompute(
            shapes_offered=['VM.Standard.E2.1.Micro', 'VM.Standard.A1.Flex'],
            images={'ocid1.image.apkulai': 'AVAILABLE'},
        )
        self.network = FakeNetwork(subnets={'ocid1.subnet.apkulai': 'AVAILABLE'})
        self.block = FakeBlock()
        self.identity = FakeIdentity([types.SimpleNamespace(name=AD)])
        self.sdk, self.clients = make_sdk(
            self.compute, self.network, self.block, self.identity
        )
        self.config = dict(CRED)

        self._saved_oci = app.oci
        app.oci = self.sdk
        self.patches = [
            mock.patch.object(app, 'get_oci', return_value=self.sdk),
            mock.patch.object(
                app, 'create_oci_client',
                side_effect=lambda cls, config: self.clients[cls],
            ),
        ]
        for patcher in self.patches:
            patcher.start()

        app.automation_running = False
        app.automation_shape = None
        app.stop_event.clear()
        with app.logs_lock:
            self._log_mark = app.global_log_base + len(app.global_logs)

    def tearDown(self):
        for patcher in self.patches:
            patcher.stop()
        app.oci = self._saved_oci
        app.automation_running = False
        app.automation_shape = None
        app.stop_event.clear()

    def log_text(self):
        with app.logs_lock:
            start = max(0, self._log_mark - app.global_log_base)
            return '\n'.join(app.global_logs[start:])


class ShapeArchitectureTests(unittest.TestCase):
    """The shape -> chipset mapping the UI prompt and the pre-flight share."""

    def test_ampere_shapes_are_arm(self):
        self.assertEqual(app.shape_architecture('VM.Standard.A1.Flex'), 'arm')
        self.assertEqual(app.shape_architecture('VM.Standard.A2.Flex'), 'arm')
        self.assertEqual(app.shape_architecture('BM.Standard.A1.160'), 'arm')

    def test_amd_and_intel_shapes_are_x86(self):
        self.assertEqual(app.shape_architecture('VM.Standard.E2.1.Micro'), 'x86')
        self.assertEqual(app.shape_architecture('VM.Standard.E4.Flex'), 'x86')
        self.assertEqual(app.shape_architecture('VM.Standard3.Flex'), 'x86')
        self.assertEqual(app.shape_architecture('VM.Optimized3.Flex'), 'x86')
        # A GPU shape's 'A10' family is not an Ampere A-series.
        self.assertEqual(app.shape_architecture('VM.GPU.A10.1'), 'x86')

    def test_empty_shape_has_no_architecture(self):
        self.assertIsNone(app.shape_architecture(''))
        self.assertIsNone(app.shape_architecture(None))

    def test_image_architecture_reads_the_display_name(self):
        self.assertEqual(app.image_architecture(
            types.SimpleNamespace(display_name='Canonical-Ubuntu-24.04-aarch64-2025.09.15-0')),
            'arm')
        # An x86 name has no marker: OCI is asked instead of guessing.
        self.assertIsNone(app.image_architecture(
            types.SimpleNamespace(display_name='Canonical-Ubuntu-24.04-2025.09.15-0')))


class ShapeOfferedInAdTests(PreflightTestBase):
    def test_true_when_shape_listed(self):
        self.assertTrue(app._shape_offered_in_ad(self.compute, 't', AD, 'VM.Standard.E2.1.Micro'))

    def test_false_when_shape_absent(self):
        self.assertFalse(app._shape_offered_in_ad(self.compute, 't', AD, 'VM.Standard.E5.Flex'))

    def test_none_when_lookup_fails(self):
        self.compute.list_shapes_raises = True
        self.assertIsNone(app._shape_offered_in_ad(self.compute, 't', AD, 'VM.Standard.E2.1.Micro'))

    def test_none_when_list_empty(self):
        self.compute.list_shapes_empty = True
        self.assertIsNone(app._shape_offered_in_ad(self.compute, 't', AD, 'VM.Standard.E2.1.Micro'))


class PreflightCheckTests(PreflightTestBase):
    def test_passes_when_everything_valid(self):
        problems, shape_available = app.preflight_launch_check(
            self.config, make_account(), self.compute, self.network,
            self.identity, [AD],
        )
        self.assertEqual(problems, [])
        self.assertTrue(shape_available)

    def test_shape_not_offered_is_not_fatal_but_reported(self):
        # The real-world ap-kulai case: E2.1.Micro absent from ListShapes. This
        # must NOT be a fatal blocker — the loop should wait for it instead.
        self.compute.shapes_offered = ['VM.Standard.A1.Flex', 'VM.Standard.E4.Flex']
        problems, shape_available = app.preflight_launch_check(
            self.config, make_account(), self.compute, self.network,
            self.identity, [AD],
        )
        self.assertEqual(problems, [])
        self.assertFalse(shape_available)

    def test_flags_image_wrong_region(self):
        self.compute.images = {}  # image OCID not readable in this region
        problems, _ = app.preflight_launch_check(
            self.config, make_account(), self.compute, self.network,
            self.identity, [AD],
        )
        self.assertTrue(any('Image' in p and 'region-specific' in p for p in problems))

    def test_flags_image_not_available(self):
        self.compute.images = {'ocid1.image.apkulai': 'PROVISIONING'}
        problems, _ = app.preflight_launch_check(
            self.config, make_account(), self.compute, self.network,
            self.identity, [AD],
        )
        self.assertTrue(any("'PROVISIONING'" in p and 'Image' in p for p in problems))

    def test_flags_subnet_missing(self):
        self.network.subnets = {}
        problems, _ = app.preflight_launch_check(
            self.config, make_account(), self.compute, self.network,
            self.identity, [AD],
        )
        self.assertTrue(any('Subnet' in p and 'region-specific' in p for p in problems))


class ChipsetMismatchTests(PreflightTestBase):
    """An image built for the other CPU architecture is fatal, not a retry."""

    def test_flags_arm_image_on_an_amd_shape(self):
        # The shape switch this feature exists for: an aarch64 Ubuntu image left
        # over from an Ampere A1 hunt against an E2.1.Micro (AMD) shape.
        self.compute.image_names = {
            'ocid1.image.apkulai': 'Canonical-Ubuntu-24.04-aarch64-2025.09.15-0',
        }
        problems, _ = app.preflight_launch_check(
            self.config, make_account(shape='VM.Standard.E2.1.Micro'),
            self.compute, self.network, self.identity, [AD],
        )
        self.assertEqual(len(problems), 1)
        self.assertIn('built for ARM (aarch64)', problems[0])
        self.assertIn('AMD/Intel (x86_64)', problems[0])
        self.assertIn('re-scan OS images', problems[0])

    def test_flags_x86_image_on_an_arm_shape_via_oci_compatibility(self):
        # An x86 display name carries no marker, so OCI's compatibility list is
        # the authority — and it lists only x86 shapes for this image.
        self.compute.image_shape_compat = ['VM.Standard.E2.1.Micro', 'VM.Standard.E4.Flex']
        problems, _ = app.preflight_launch_check(
            self.config, make_account(shape='VM.Standard.A1.Flex'), self.compute,
            self.network, self.identity, [AD],
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("shape 'VM.Standard.A1.Flex'", problems[0])
        self.assertIn('not compatible with this shape', problems[0])

    def test_matching_chipset_passes(self):
        self.compute.image_shape_compat = ['VM.Standard.E2.1.Micro']
        problems, _ = app.preflight_launch_check(
            self.config, make_account(), self.compute, self.network,
            self.identity, [AD],
        )
        self.assertEqual(problems, [])

    def test_compatibility_lookup_failure_is_not_fatal(self):
        # If OCI cannot answer, the launch is not blocked on a guess.
        self.compute.shape_compat_raises = True
        problems, _ = app.preflight_launch_check(
            self.config, make_account(), self.compute, self.network,
            self.identity, [AD],
        )
        self.assertEqual(problems, [])


class LoopPreflightIntegrationTests(PreflightTestBase):
    def test_loop_aborts_on_fatal_image_problem(self):
        # A wrong-region image OCID is fatal: waiting cannot fix it.
        self.compute.images = {}
        app.run_automated_creation(
            self.config, make_account(), self.compute, self.network,
            self.identity, retry_delay=0, max_attempts=5,
        )
        self.assertEqual(self.compute.launch_calls, [])
        logs = self.log_text()
        self.assertIn('Pre-flight check failed', logs)
        self.assertIn('region-specific', logs)
        self.assertFalse(app.automation_running)

    def test_loop_aborts_when_image_chipset_does_not_match_the_shape(self):
        # Waiting cannot fix an ARM image on an AMD shape: the loop must stop
        # with the re-scan instruction instead of spending attempts on 404s.
        self.compute.image_names = {
            'ocid1.image.apkulai': 'Canonical-Ubuntu-24.04-aarch64-2025.09.15-0',
        }
        app.run_automated_creation(
            self.config, make_account(), self.compute, self.network,
            self.identity, retry_delay=0, max_attempts=5,
        )
        self.assertEqual(self.compute.launch_calls, [])
        logs = self.log_text()
        self.assertIn('Pre-flight check failed', logs)
        self.assertIn('built for ARM (aarch64)', logs)
        self.assertIn('re-scan OS images', logs)
        self.assertIn('pre-flight check failed', logs)  # exit reason
        self.assertFalse(app.automation_running)

    def test_loop_waits_when_shape_not_offered(self):
        # Shape never appears: the loop must NOT exit immediately — it waits,
        # logging live, and only stops when the attempt limit is reached.
        self.compute.shapes_offered = ['VM.Standard.A1.Flex']  # no E2.1.Micro, ever
        app.run_automated_creation(
            self.config, make_account(), self.compute, self.network,
            self.identity, retry_delay=0, max_attempts=4,
        )
        self.assertEqual(self.compute.launch_calls, [])  # never launched a 404
        logs = self.log_text()
        self.assertIn('WARNING: No shape availability', logs)
        self.assertIn(
            "Currently, shape 'VM.Standard.E2.1.Micro' is not available in your Oracle Cloud region",
            logs,
        )
        self.assertIn('Please wait for Oracle Cloud to offer it there', logs)
        self.assertIn('Waiting for Oracle Cloud to offer it in your region', logs)
        self.assertIn('still not offered', logs)
        self.assertIn('Retry limit reached (4 attempts)', logs)
        self.assertNotIn('sending instance launch request', logs)
        self.assertFalse(app.automation_running)

    def test_loop_launches_once_shape_appears(self):
        # Shape is missing at first, then Oracle adds it mid-hunt: the loop
        # waits, then launches automatically once the shape shows up.
        self.compute.shapes_offered = ['VM.Standard.A1.Flex']
        self.compute.offer_shape_from_call = 4  # appears after a couple of waits
        app.run_automated_creation(
            self.config, make_account(), self.compute, self.network,
            self.identity, retry_delay=0, max_attempts=20,
        )
        self.assertEqual(len(self.compute.launch_calls), 1)
        logs = self.log_text()
        self.assertIn('No shape availability', logs)
        self.assertIn('is now offered', logs)
        self.assertIn('SUCCESS! Instance created', logs)
        self.assertFalse(app.automation_running)

    def test_loop_launches_when_preflight_passes(self):
        app.run_automated_creation(
            self.config, make_account(), self.compute, self.network,
            self.identity, retry_delay=0, max_attempts=5,
        )
        self.assertEqual(len(self.compute.launch_calls), 1)
        self.assertIn('SUCCESS! Instance created', self.log_text())
        self.assertFalse(app.automation_running)


if __name__ == '__main__':
    unittest.main()
