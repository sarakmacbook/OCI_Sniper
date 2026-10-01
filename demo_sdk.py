"""In-memory OCI SDK double for UI demos and Arena previews.

Enabled only when the app is started with ``DEMO_MODE=1``: ``app.py`` swaps its
OCI SDK binding for the fakes below, so the whole UI can be clicked through —
scan images and subnets, quota, start the provisioning loop, watch a second
start get refused, stop it and read the live log — with **no credentials and no
Oracle Cloud API calls**.

Every log line written while demo mode is on is prefixed ``[demo]`` by
``app.add_log`` so a demo run can never be mistaken for a real launch, and the
UI shows a DEMO banner. Nothing here is used in production: without ``DEMO_MODE``
this module is never imported.

Behaviour worth knowing while clicking around:

* ``launch_instance`` fails with ``OutOfHostCapacity`` (the real, expected
  free-tier error) for the first ``DEMO_CAPACITY_AFTER_ATTEMPTS`` attempts
  (default 3, minimum 1) and then succeeds, so the hunting loop, its retries,
  the duplicate-start refusal and the success line can all be seen in a minute.
* A successful launch adds a fake instance and boot volume to the shared state,
  so the quota panel and the boot volume inventory update as if it were real.
* Panels that would need write access to a real account (firewall rules, boot
  volume jobs) fail with a clear ``501 Demo mode`` service error instead of
  pretending to succeed.
"""
import datetime
import os
import threading
import types

# ---- Tunables ---------------------------------------------------------------
def _env_int(name, default, minimum=1):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


CAPACITY_AFTER_ATTEMPTS_DEFAULT = _env_int('DEMO_CAPACITY_AFTER_ATTEMPTS', 3)

REGIONS = ('ap-kulai-1', 'ap-singapore-1', 'eu-frankfurt-1')


def _resp(data):
    return types.SimpleNamespace(data=data)


def _obj(**kwargs):
    return types.SimpleNamespace(**kwargs)


class ServiceError(Exception):
    """Stand-in for ``oci.exceptions.ServiceError`` (code + status + message)."""

    def __init__(self, code='Error', status=500, message='demo error'):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.headers = {}


class _FakeModel:
    """Accepts any ``oci.core.models.*`` keyword arguments."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __repr__(self):
        return f"<demo model {self.__class__.__name__}>"


class _ModelsNamespace:
    """Returns a permissive model class for any model name the app asks for."""

    def __getattr__(self, name):
        return type(name, (_FakeModel,), {})


def _not_implemented(what):
    return ServiceError(
        code='NotImplemented',
        status=501,
        message=f'Demo mode: {what} is not simulated — use a real OCI key for that panel.',
    )


# ---- Shared demo state ------------------------------------------------------
class DemoState:
    """Everything the fake clients read and write, so the UI stays consistent."""

    def __init__(self):
        self.lock = threading.Lock()
        self.capacity_after_attempts = CAPACITY_AFTER_ATTEMPTS_DEFAULT
        self.launch_attempts = 0
        self.instances = []
        self.boot_volumes = []
        self.instances_created = 0

    def reset(self, capacity_after_attempts=None):
        with self.lock:
            self.capacity_after_attempts = (
                CAPACITY_AFTER_ATTEMPTS_DEFAULT
                if capacity_after_attempts is None
                else max(1, int(capacity_after_attempts))
            )
            self.launch_attempts = 0
            self.instances = []
            self.boot_volumes = []
            self.instances_created = 0


_state = DemoState()


def reset(capacity_after_attempts=None):
    """Reset the demo cloud (used by tests and when re-running a demo)."""
    _state.reset(capacity_after_attempts)


# ---- Fake resources ---------------------------------------------------------
def _availability_domains(region):
    """Three ADs per region, named like OCI's (``<region>-AD-1``)."""
    return [f'{region}-AD-{i}' for i in (1, 2, 3)]


def _images(region):
    now = datetime.datetime.now(datetime.timezone.utc)
    specs = [
        ('Canonical Ubuntu', '24.04', 'Canonical-Ubuntu-24.04-aarch64-2025.09.15-0'),
        ('Canonical Ubuntu', '22.04', 'Canonical-Ubuntu-22.04-aarch64-2025.09.15-0'),
        ('Canonical Ubuntu', '22.04', 'Canonical-Ubuntu-22.04-2025.09.15-0'),
        ('Oracle Linux', '9', 'Oracle-Linux-9.5-aarch64-2025.08.30-0'),
    ]
    images = []
    for index, (os_name, version, display) in enumerate(specs):
        images.append(_obj(
            id=f'ocid1.image.oc1.{region}.demo{index}',
            display_name=display,
            operating_system=os_name,
            operating_system_version=version,
            lifecycle_state='AVAILABLE',
            size_in_mbs=47694,
            time_created=now - datetime.timedelta(days=index * 9),
        ))
    return images


def _vcns(region):
    return [_obj(
        id=f'ocid1.vcn.oc1.{region}.demo',
        display_name='demo-vcn',
        cidr_blocks=['10.0.0.0/16'],
        lifecycle_state='AVAILABLE',
        dns_label='demovcn',
    )]


def _subnets(region):
    vcn_id = _vcns(region)[0].id
    return [
        _obj(
            id=f'ocid1.subnet.oc1.{region}.public',
            display_name='public-subnet',
            cidr_block='10.0.0.0/24',
            availability_domain=None,
            prohibit_public_ip_on_vnic=False,
            dns_label='public',
            lifecycle_state='AVAILABLE',
            vcn_id=vcn_id,
            network_security_group_ids=[],
            security_list_ids=[_security_list_id(region)],
        ),
        _obj(
            id=f'ocid1.subnet.oc1.{region}.private',
            display_name='private-subnet',
            cidr_block='10.0.1.0/24',
            availability_domain=None,
            prohibit_public_ip_on_vnic=True,
            dns_label='private',
            lifecycle_state='AVAILABLE',
            vcn_id=vcn_id,
            network_security_group_ids=[],
            security_list_ids=[_security_list_id(region)],
        ),
    ]


def _security_list_id(region):
    return f'ocid1.securitylist.oc1.{region}.demo'


SHAPES = (
    'VM.Standard.E2.1.Micro',
    'VM.Standard.A1.Flex',
    'VM.Standard.E4.Flex',
    'VM.Standard.E5.Flex',
)


# ---- Fake clients -----------------------------------------------------------
class _BaseClient:
    def __init__(self, config=None):
        self.config = config or {}
        self.region = self.config.get('region') or REGIONS[0]


class FakeIdentityClient(_BaseClient):
    def list_availability_domains(self, **kwargs):
        return _resp([_obj(name=ad, id=f'ocid1.availabilitydomain.demo.{ad}')
                      for ad in _availability_domains(self.region)])

    def get_user(self, **kwargs):
        return _resp(_obj(
            id=kwargs.get('user_id') or 'ocid1.user.demo',
            name='demo-user',
            email='demo@example.com',
            description='Demo mode: no real OCI user',
            lifecycle_state='ACTIVE',
        ))


class FakeComputeClient(_BaseClient):
    def list_images(self, **kwargs):
        wanted_os = (kwargs.get('operating_system') or '').lower()
        images = _images(self.region)
        if wanted_os:
            images = [i for i in images if wanted_os in i.operating_system.lower()]
        return _resp(images)

    def get_image(self, image_id=None, **kwargs):
        for image in _images(self.region):
            if image.id == image_id:
                return _resp(image)
        raise ServiceError(
            code='NotAuthorizedOrNotFound',
            status=404,
            message='Authorization failed or requested resource not found.',
        )

    def list_shapes(self, **kwargs):
        return _resp([_obj(shape=name) for name in SHAPES])

    def list_image_shape_compatibility_entries(self, image_id=None, **kwargs):
        return _resp([_obj(shape=name) for name in SHAPES])

    def list_instances(self, **kwargs):
        with _state.lock:
            return _resp(list(_state.instances))

    def get_instance(self, instance_id=None, **kwargs):
        with _state.lock:
            for inst in _state.instances:
                if inst.id == instance_id:
                    return _resp(inst)
        raise _not_implemented('reading an instance that does not exist')

    def launch_instance(self, launch_instance_details=None, **kwargs):
        details = launch_instance_details or _obj()
        with _state.lock:
            _state.launch_attempts += 1
            attempt = _state.launch_attempts
            capacity_after = _state.capacity_after_attempts
            ad = getattr(details, 'availability_domain', 'AD-1')
            if attempt < capacity_after:
                raise ServiceError(
                    code='OutOfHostCapacity',
                    status=500,
                    message=(f'Out of host capacity in {ad}. '
                             f'(demo attempt {attempt}; capacity opens at {capacity_after})'),
                )
            _state.instances_created += 1
            number = _state.instances_created
            shape = getattr(details, 'shape', 'VM.Standard.E2.1.Micro')
            shape_config = getattr(details, 'shape_config', None)
            source = getattr(details, 'source_details', None)
            boot_gb = int(getattr(source, 'boot_volume_size_in_gbs', 50) or 50)
            instance = _obj(
                id=f'ocid1.instance.oc1.{self.region}.demo{number}',
                display_name=getattr(details, 'display_name', 'AlwaysFree-Bot'),
                shape=shape,
                shape_config=shape_config,
                lifecycle_state='RUNNING',
                availability_domain=ad,
                compartment_id=getattr(details, 'compartment_id', None),
                time_created=datetime.datetime.now(datetime.timezone.utc),
            )
            _state.instances.append(instance)
            _state.boot_volumes.append(_obj(
                id=f'ocid1.bootvolume.oc1.{self.region}.demo{number}',
                display_name=f'{instance.display_name}-boot',
                size_in_gbs=boot_gb,
                lifecycle_state='AVAILABLE',
                availability_domain=ad,
                compartment_id=instance.compartment_id,
            ))
            # Real launches also return the instance but need a moment to boot.
            return _resp(instance)

    # Boot volume manager: read-only parts return an empty inventory, writes say
    # clearly that they are not simulated.
    def list_boot_volume_attachments(self, **kwargs):
        return _resp([])

    def get_boot_volume_attachment(self, **kwargs):
        raise _not_implemented('boot volume attachments')

    def attach_boot_volume(self, **kwargs):
        raise _not_implemented('attaching a boot volume')

    def detach_boot_volume(self, **kwargs):
        raise _not_implemented('detaching a boot volume')

    def instance_action(self, **kwargs):
        raise _not_implemented('instance start/stop')

    def terminate_instance(self, **kwargs):
        raise _not_implemented('terminating an instance')


class FakeNetworkClient(_BaseClient):
    def list_vcns(self, **kwargs):
        return _resp(_vcns(self.region))

    def list_subnets(self, **kwargs):
        return _resp(_subnets(self.region))

    def get_subnet(self, subnet_id=None, **kwargs):
        for subnet in _subnets(self.region):
            if subnet.id == subnet_id:
                return _resp(subnet)
        raise ServiceError(
            code='NotAuthorizedOrNotFound',
            status=404,
            message='Authorization failed or requested resource not found.',
        )

    def get_vcn(self, **kwargs):
        return _resp(_vcns(self.region)[0])

    def get_network_security_group(self, **kwargs):
        raise _not_implemented('network security groups')

    def list_network_security_group_security_rules(self, **kwargs):
        return _resp([])

    def get_security_list(self, security_list_id=None, **kwargs):
        # Readable and empty: scanning shows the demo subnet really has no rules,
        # while changing rules below says clearly that it is not simulated.
        return _resp(_obj(
            id=security_list_id or _security_list_id(self.region),
            display_name='demo-security-list',
            lifecycle_state='AVAILABLE',
            ingress_security_rules=[],
            egress_security_rules=[],
        ))

    def add_network_security_group_security_rules(self, **kwargs):
        raise _not_implemented('firewall rule changes')

    def update_security_list(self, **kwargs):
        raise _not_implemented('firewall rule changes')


class FakeBlockstorageClient(_BaseClient):
    def list_boot_volumes(self, **kwargs):
        ad = kwargs.get('availability_domain')
        with _state.lock:
            volumes = list(_state.boot_volumes)
        if ad:
            volumes = [v for v in volumes if v.availability_domain == ad]
        return _resp(volumes)

    def get_boot_volume(self, boot_volume_id=None, **kwargs):
        with _state.lock:
            for volume in _state.boot_volumes:
                if volume.id == boot_volume_id:
                    return _resp(volume)
        raise _not_implemented('reading a boot volume that does not exist')

    def list_boot_volume_backups(self, **kwargs):
        return _resp([])

    def create_boot_volume(self, **kwargs):
        raise _not_implemented('creating a boot volume')

    def delete_boot_volume(self, **kwargs):
        raise _not_implemented('deleting a boot volume')


# ---- SDK namespace ----------------------------------------------------------
def _validate_config(config):
    """Mirror ``oci.config.validate_config`` for the fields the UI sends."""
    missing = [
        field for field in ('user', 'tenancy', 'fingerprint', 'region')
        if not (config or {}).get(field)
    ]
    if not (config or {}).get('key_content') and not (config or {}).get('key_file'):
        missing.append('key_content')
    if missing:
        raise ServiceError(
            code='InvalidConfig',
            status=400,
            message=f'Demo mode: config is missing {", ".join(missing)}.',
        )


def _build_sdk():
    return types.SimpleNamespace(
        config=types.SimpleNamespace(validate_config=_validate_config),
        core=types.SimpleNamespace(
            ComputeClient='demo-compute',
            VirtualNetworkClient='demo-network',
            BlockstorageClient='demo-block',
            models=_ModelsNamespace(),
        ),
        identity=types.SimpleNamespace(IdentityClient='demo-identity'),
        exceptions=types.SimpleNamespace(ServiceError=ServiceError),
        # create_oci_client asks for this before building a real client; the demo
        # never reaches that branch because it replaces create_oci_client itself.
        retry=types.SimpleNamespace(NoneRetryStrategy=lambda: None),
    )


SDK = _build_sdk()

_CLIENT_FACTORIES = {
    'demo-compute': FakeComputeClient,
    'demo-network': FakeNetworkClient,
    'demo-block': FakeBlockstorageClient,
    'demo-identity': FakeIdentityClient,
}


def create_client(client_class, config):
    factory = _CLIENT_FACTORIES.get(client_class)
    if factory is None:
        raise ServiceError(
            code='NotImplemented',
            status=501,
            message=f'Demo mode: unknown client {client_class!r}.',
        )
    return factory(config)


def install(module):
    """Point an imported ``app`` module at the demo SDK instead of real OCI.

    Also flips ``module.DEMO_MODE`` on, which is what prefixes every log line
    with ``[demo]`` and shows the DEMO banner in the UI — so "demo mode is on"
    and "the fake SDK is installed" can never disagree.
    """
    module.oci = SDK
    module.get_oci = lambda: SDK
    module.create_oci_client = create_client
    module.DEMO_MODE = True
    try:
        module.add_log(
            'DEMO MODE: the OCI API is stubbed in memory — no credentials needed, '
            'no instance is created, and every line is marked [demo].'
        )
    except Exception:
        pass
    return SDK
