"""Tests for the boot volume manager (attach/detach/re-attach/replace/delete).

Run with the project's Python environment:

    python -m unittest tests.test_bootvolumes -v

No network access and no real OCI SDK are needed: ``app.get_oci`` is patched
with a tiny SDK double and the OCI clients are in-memory fakes that model the
lifecycle transitions the app relies on.
"""
import os
import time
import types
import unittest
from unittest import mock

os.environ.setdefault('APP_PASSWORD', 'test-password')

import app  # noqa: E402

AD_1 = 'FAKE:AP-TEST-1-AD-1'
AD_2 = 'FAKE:AP-TEST-1-AD-2'

CRED_PAYLOAD = {
    'user': 'ocid1.user.fake',
    'tenancy': 'ocid1.tenancy.fake',
    'fingerprint': 'aa:bb',
    'region': 'ap-test-1',
    'private_key': 'fake-key',
}


class FakeModel:
    """Stands in for the oci.core.models.* details classes."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


FAKE_MODELS = types.SimpleNamespace(
    AttachBootVolumeDetails=FakeModel,
    CreateBootVolumeDetails=FakeModel,
    BootVolumeSourceFromBootVolumeBackupDetails=FakeModel,
)


class NotFoundError(Exception):
    status = 404
    message = 'not found'


def _resp(data):
    return types.SimpleNamespace(data=data)


class FakeCompute:
    def __init__(self, block):
        self.block = block
        self.instances = {}
        self.attachments = {}
        self._att_seq = 0
        self.fail_next_attach = False
        self.power_calls = []
        self.detach_calls = 0

    # -- instances -----------------------------------------------------
    def get_instance(self, instance_id=None):
        inst = self.instances.get(instance_id)
        if inst is None:
            raise NotFoundError('instance not found')
        return _resp(inst)

    def list_instances(self, compartment_id=None):
        return _resp([i for i in self.instances.values() if i.lifecycle_state != 'TERMINATED'])

    def instance_action(self, instance_id=None, action=None):
        self.power_calls.append(action)
        inst = self.instances[instance_id]
        if action in ('STOP', 'SOFTSTOP'):
            inst.lifecycle_state = 'STOPPED'
        elif action == 'START':
            inst.lifecycle_state = 'RUNNING'

    # -- boot volume attachments ---------------------------------------
    def list_boot_volume_attachments(self, compartment_id=None, availability_domain=None, instance_id=None):
        out = []
        for att in self.attachments.values():
            if instance_id and att.instance_id != instance_id:
                continue
            out.append(att)
        return _resp(out)

    def get_boot_volume_attachment(self, boot_volume_attachment_id=None):
        att = self.attachments.get(boot_volume_attachment_id)
        if att is None:
            raise NotFoundError('attachment not found')
        return _resp(att)

    def detach_boot_volume(self, boot_volume_attachment_id=None):
        self.detach_calls += 1
        att = self.attachments[boot_volume_attachment_id]
        att.lifecycle_state = 'DETACHED'
        if att.boot_volume_id in self.block.volumes:
            self.block.volumes[att.boot_volume_id].lifecycle_state = 'AVAILABLE'

    def attach_boot_volume(self, attach_boot_volume_details=None):
        if self.fail_next_attach:
            self.fail_next_attach = False
            raise Exception('simulated attach failure')
        details = attach_boot_volume_details
        if details.boot_volume_id not in self.block.volumes:
            raise NotFoundError('boot volume not found')
        if details.instance_id not in self.instances:
            raise NotFoundError('instance not found')
        for att in self.attachments.values():
            if att.instance_id == details.instance_id and att.lifecycle_state != 'DETACHED':
                raise Exception('instance already has a boot volume')
        self._att_seq += 1
        att = types.SimpleNamespace(
            id='att-new-%d' % self._att_seq,
            boot_volume_id=details.boot_volume_id,
            instance_id=details.instance_id,
            lifecycle_state='ATTACHED',
        )
        self.attachments[att.id] = att
        self.block.volumes[details.boot_volume_id].lifecycle_state = 'ATTACHED'
        return _resp(att)


class FakeBlock:
    def __init__(self):
        self.volumes = {}
        self.backups = []
        self._vol_seq = 0
        self.created_from_backups = []
        self.created_details = []
        self.deleted = []

    def get_boot_volume(self, boot_volume_id=None):
        vol = self.volumes.get(boot_volume_id)
        if vol is None:
            raise NotFoundError('boot volume not found')
        return _resp(vol)

    def list_boot_volumes(self, compartment_id=None, availability_domain=None):
        return _resp([
            v for v in self.volumes.values()
            if v.availability_domain == availability_domain and v.lifecycle_state != 'TERMINATED'
        ])

    def delete_boot_volume(self, boot_volume_id=None):
        vol = self.volumes.pop(boot_volume_id, None)
        if vol is not None:
            self.deleted.append(boot_volume_id)

    def create_boot_volume(self, create_boot_volume_details=None):
        details = create_boot_volume_details
        self.created_details.append(details)
        self._vol_seq += 1
        backup_id = getattr(getattr(details, 'source_details', None), 'id', None)
        size = getattr(details, 'size_in_gbs', None) or 50
        for backup in self.backups:
            if backup.id == backup_id:
                size = backup.size_in_gbs
                break
        vol = types.SimpleNamespace(
            id='bv-created-%d' % self._vol_seq,
            display_name=getattr(details, 'display_name', None) or 'restored',
            lifecycle_state='AVAILABLE',
            availability_domain=details.availability_domain,
            size_in_gbs=size,
        )
        self.volumes[vol.id] = vol
        self.created_from_backups.append(backup_id)
        return _resp(vol)

    def list_boot_volume_backups(self, compartment_id=None):
        return _resp(list(self.backups))


class FakeIdentity:
    def __init__(self, ads):
        self.ads = ads

    def list_availability_domains(self, compartment_id=None):
        return _resp(self.ads)


def make_world():
    """One stopped instance with an attached boot disk, one spare disk, one backup."""
    block = FakeBlock()
    compute = FakeCompute(block)
    identity = FakeIdentity([types.SimpleNamespace(name=AD_1), types.SimpleNamespace(name=AD_2)])

    inst = types.SimpleNamespace(
        id='i-1', display_name='alpha', lifecycle_state='STOPPED',
        availability_domain=AD_1, shape='VM.Standard.A1.Flex',
    )
    compute.instances['i-1'] = inst
    old_vol = types.SimpleNamespace(
        id='bv-1', display_name='alpha-boot', lifecycle_state='ATTACHED',
        availability_domain=AD_1, size_in_gbs=50,
    )
    spare_vol = types.SimpleNamespace(
        id='bv-2', display_name='spare-disk', lifecycle_state='AVAILABLE',
        availability_domain=AD_1, size_in_gbs=47,
    )
    other_ad_vol = types.SimpleNamespace(
        id='bv-3', display_name='wrong-ad-disk', lifecycle_state='AVAILABLE',
        availability_domain=AD_2, size_in_gbs=50,
    )
    for vol in (old_vol, spare_vol, other_ad_vol):
        block.volumes[vol.id] = vol
    compute.attachments['att-1'] = types.SimpleNamespace(
        id='att-1', boot_volume_id='bv-1', instance_id='i-1', lifecycle_state='ATTACHED',
    )
    block.backups.append(types.SimpleNamespace(
        id='bak-1', display_name='alpha-backup', lifecycle_state='AVAILABLE',
        size_in_gbs=49, boot_volume_id='bv-1',
    ))
    return compute, block, identity


def make_fake_sdk(compute, block, identity):
    """Mimic the parts of the `oci` module the app touches."""
    clients = {'compute': compute, 'network': object(), 'block': block, 'identity': identity}
    return types.SimpleNamespace(
        config=types.SimpleNamespace(validate_config=lambda config: None),
        core=types.SimpleNamespace(
            ComputeClient='compute',
            VirtualNetworkClient='network',
            BlockstorageClient='block',
            models=FAKE_MODELS,
        ),
        identity=types.SimpleNamespace(IdentityClient='identity'),
    ), clients


class BootVolumeTestBase(unittest.TestCase):
    """Patch the OCI boundary with fakes and put job state in a known shape."""

    def setUp(self):
        self.compute, self.block, self.identity = make_world()
        self.sdk, self.clients = make_fake_sdk(self.compute, self.block, self.identity)
        self.config = {k: CRED_PAYLOAD[k] for k in ('user', 'tenancy', 'fingerprint', 'region')}
        self.config['key_content'] = CRED_PAYLOAD['private_key']
        self.patches = [
            mock.patch.object(app, 'get_oci', return_value=self.sdk),
            mock.patch.object(app, 'create_oci_client', side_effect=lambda cls, config: self.clients[cls]),
        ]
        for patcher in self.patches:
            patcher.start()
        app._bv_job_finish()
        app.bv_stop_event.clear()
        with app.logs_lock:
            self._log_mark = app.global_log_base + len(app.global_logs)

    def tearDown(self):
        for patcher in self.patches:
            patcher.stop()
        app._bv_job_finish()
        app.bv_stop_event.clear()

    def run_job(self, operation, params):
        """Run the background worker synchronously (the fakes flip states instantly)."""
        app._bv_job_begin(operation, next(iter(params.values()), None))
        app.run_boot_volume_job(operation, self.config, params)

    def log_text(self):
        """Only the log lines this test produced (the buffer is process-global)."""
        with app.logs_lock:
            start = max(0, self._log_mark - app.global_log_base)
            return '\n'.join(app.global_logs[start:])

    def instance(self):
        return self.compute.instances['i-1']


class BootVolumeJobTests(BootVolumeTestBase):
    def test_create_empty_boot_volume(self):
        self.run_job('create', {
            'availability_domain': AD_2,
            'display_name': 'fresh-disk',
            'size_gb': 75,
        })
        volume = self.block.volumes['bv-created-1']
        details = self.block.created_details[0]
        self.assertEqual(volume.display_name, 'fresh-disk')
        self.assertEqual(volume.availability_domain, AD_2)
        self.assertEqual(volume.size_in_gbs, 75)
        self.assertEqual(volume.lifecycle_state, 'AVAILABLE')
        self.assertIsNone(getattr(details, 'source_details', None))
        self.assertEqual(details.compartment_id, CRED_PAYLOAD['tenancy'])
        self.assertIn('empty 75 GB boot volume', self.log_text())

    def test_create_boot_volume_from_backup_without_attaching(self):
        self.run_job('create', {
            'availability_domain': AD_2,
            'display_name': 'restored-disk',
            'backup_id': 'bak-1',
        })
        volume = self.block.volumes['bv-created-1']
        details = self.block.created_details[0]
        self.assertEqual(self.block.created_from_backups, ['bak-1'])
        self.assertEqual(volume.display_name, 'restored-disk')
        self.assertEqual(volume.availability_domain, AD_2)
        self.assertEqual(volume.size_in_gbs, 49)
        self.assertIsNone(getattr(details, 'size_in_gbs', None))
        self.assertEqual(volume.lifecycle_state, 'AVAILABLE')
        self.assertIn('ready', self.log_text())

    def test_detach_stopped_instance_releases_disk(self):
        self.run_job('detach', {'boot_volume_id': 'bv-1'})
        self.assertEqual(self.block.volumes['bv-1'].lifecycle_state, 'AVAILABLE')
        self.assertEqual(self.compute.attachments['att-1'].lifecycle_state, 'DETACHED')
        self.assertIn('detached', self.log_text())

    def test_detach_running_instance_is_refused(self):
        self.instance().lifecycle_state = 'RUNNING'
        self.run_job('detach', {'boot_volume_id': 'bv-1'})
        self.assertEqual(self.block.volumes['bv-1'].lifecycle_state, 'ATTACHED')
        self.assertIn('Stop it first', self.log_text())

    def test_attach_reattaches_detached_disk(self):
        self.run_job('detach', {'boot_volume_id': 'bv-1'})
        self.run_job('attach', {'boot_volume_id': 'bv-1', 'instance_id': 'i-1'})
        self.assertEqual(self.block.volumes['bv-1'].lifecycle_state, 'ATTACHED')
        live = [a for a in self.compute.attachments.values() if a.lifecycle_state == 'ATTACHED']
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0].instance_id, 'i-1')

    def test_attach_refuses_volume_in_other_ad(self):
        self.run_job('attach', {'boot_volume_id': 'bv-3', 'instance_id': 'i-1'})
        self.assertEqual(self.block.volumes['bv-3'].lifecycle_state, 'AVAILABLE')
        self.assertIn('availability domain', self.log_text())

    def test_attach_refuses_instance_that_already_has_a_disk(self):
        # bv-2 is detached, but i-1 still boots from bv-1: suggest Replace instead.
        self.run_job('attach', {'boot_volume_id': 'bv-2', 'instance_id': 'i-1'})
        self.assertEqual(self.block.volumes['bv-2'].lifecycle_state, 'AVAILABLE')
        self.assertIn('already has a boot volume', self.log_text())

    def test_delete_detached_volume(self):
        self.run_job('delete', {'boot_volume_id': 'bv-2'})
        self.assertIn('bv-2', self.block.deleted)
        self.assertIn('deleted', self.log_text())

    def test_delete_attached_volume_is_refused(self):
        self.run_job('delete', {'boot_volume_id': 'bv-1'})
        self.assertIn('bv-1', self.block.volumes)
        self.assertIn('detach it from its instance', self.log_text())

    def test_instance_power_stop_and_start(self):
        self.instance().lifecycle_state = 'RUNNING'
        self.run_job('instance-action', {'instance_id': 'i-1', 'power_action': 'SOFTSTOP'})
        self.assertEqual(self.instance().lifecycle_state, 'STOPPED')
        self.run_job('instance-action', {'instance_id': 'i-1', 'power_action': 'START'})
        self.assertEqual(self.instance().lifecycle_state, 'RUNNING')
        self.assertEqual(self.compute.power_calls, ['SOFTSTOP', 'START'])

    def test_job_logs_and_releases_state(self):
        app._bv_job_begin('detach', 'bv-1')
        app.run_boot_volume_job('detach', self.config, {'boot_volume_id': 'bv-1'})
        status = app.boot_volume_job_status()
        self.assertFalse(status['running'])
        self.assertIsNone(status['operation'])


class BootVolumeReplaceTests(BootVolumeTestBase):
    def test_replace_with_existing_volume_and_delete_old(self):
        self.instance().lifecycle_state = 'RUNNING'
        self.run_job('replace', {
            'instance_id': 'i-1',
            'source_type': 'boot_volume',
            'source_id': 'bv-2',
            'delete_old': True,
            'power_action': 'SOFTSTOP',
        })
        # Old disk detached and deleted, new disk attached, instance booted.
        self.assertIn('bv-1', self.block.deleted)
        self.assertEqual(self.block.volumes['bv-2'].lifecycle_state, 'ATTACHED')
        self.assertEqual(self.instance().lifecycle_state, 'RUNNING')
        live = [a for a in self.compute.attachments.values()
                if a.instance_id == 'i-1' and a.lifecycle_state == 'ATTACHED']
        self.assertEqual([a.boot_volume_id for a in live], ['bv-2'])
        self.assertIn('Replace complete', self.log_text())

    def test_replace_keeps_old_volume_by_default(self):
        self.run_job('replace', {
            'instance_id': 'i-1',
            'source_type': 'boot_volume',
            'source_id': 'bv-2',
            'delete_old': False,
        })
        self.assertNotIn('bv-1', self.block.deleted)
        self.assertEqual(self.block.volumes['bv-1'].lifecycle_state, 'AVAILABLE')
        self.assertIn('kept (detached)', self.log_text())

    def test_replace_rolls_back_when_attach_fails(self):
        self.compute.fail_next_attach = True
        self.run_job('replace', {
            'instance_id': 'i-1',
            'source_type': 'boot_volume',
            'source_id': 'bv-2',
            'delete_old': True,
        })
        # The failure must leave the VM booting from its ORIGINAL disk again.
        self.assertEqual(self.block.volumes['bv-1'].lifecycle_state, 'ATTACHED')
        self.assertNotIn('bv-1', self.block.deleted)
        self.assertEqual(self.block.volumes['bv-2'].lifecycle_state, 'AVAILABLE')
        self.assertEqual(self.instance().lifecycle_state, 'RUNNING')
        self.assertIn('rolling back', self.log_text())
        self.assertIn('Replace aborted', self.log_text())

    def test_replace_from_backup_restores_new_volume(self):
        self.run_job('replace', {
            'instance_id': 'i-1',
            'source_type': 'backup',
            'source_id': 'bak-1',
            'delete_old': False,
        })
        self.assertEqual(self.block.created_from_backups, ['bak-1'])
        self.assertEqual(self.instance().lifecycle_state, 'RUNNING')
        live = [a for a in self.compute.attachments.values()
                if a.instance_id == 'i-1' and a.lifecycle_state == 'ATTACHED']
        self.assertEqual(len(live), 1)
        self.assertTrue(live[0].boot_volume_id.startswith('bv-created-'))
        self.assertIn('Replace complete', self.log_text())

    def test_replace_refuses_backup_source_with_bad_type(self):
        self.run_job('replace', {
            'instance_id': 'i-1',
            'source_type': 'magic',
            'source_id': 'x',
        })
        self.assertIn('needs a replacement source', self.log_text())

    def test_stop_before_start_makes_no_oci_changes(self):
        # Simulate a stop signal arriving before the job's first mutation by
        # calling the worker directly, bypassing _bv_job_begin (a fresh job
        # start via the API always clears a stale stop signal).
        app.bv_stop_event.set()
        app.run_boot_volume_job('detach', self.config, {'boot_volume_id': 'bv-1'})
        self.assertEqual(self.block.volumes['bv-1'].lifecycle_state, 'ATTACHED')
        self.assertIn('Stopped by user before any OCI change', self.log_text())

    def test_user_stop_aborts_job_at_next_poll(self):
        # Stop pressed while the job is in flight: the current SDK call
        # finishes, then the state poll aborts the job.
        original_action = self.compute.instance_action

        def action_then_user_stop(*args, **kwargs):
            original_action(*args, **kwargs)
            app.bv_stop_event.set()

        self.compute.instance_action = action_then_user_stop
        self.run_job('instance-action', {'instance_id': 'i-1', 'power_action': 'START'})
        self.assertEqual(self.instance().lifecycle_state, 'RUNNING')
        self.assertIn('Stopped by user', self.log_text())
        self.assertNotIn("Job 'instance-action' finished successfully", self.log_text())


class BootVolumeApiTests(BootVolumeTestBase):
    def setUp(self):
        super().setUp()
        self.client = app.app.test_client()
        self.auth = ('operator', 'test-password')

    def post(self, url, payload):
        return self.client.post(url, json=payload, auth=self.auth)

    def wait_for_job(self):
        deadline = time.time() + 5
        while time.time() < deadline:
            if not app.boot_volume_job_status()['running']:
                return True
            time.sleep(0.02)
        return False

    def test_list_requires_auth(self):
        res = self.client.post('/api/boot-volumes/list', json=CRED_PAYLOAD)
        self.assertEqual(res.status_code, 401)

    def test_list_serializes_inventory(self):
        res = self.post('/api/boot-volumes/list', dict(CRED_PAYLOAD))
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(body['total_storage_gb'], 147)
        self.assertEqual(body['storage_limit_gb'], 200)
        self.assertEqual(
            [ad['name'] for ad in body['availability_domains']],
            [AD_1, AD_2],
        )
        attached = [v for v in body['boot_volumes'] if v['attached']]
        detached = [v for v in body['boot_volumes'] if not v['attached']]
        self.assertEqual(len(attached), 1)
        self.assertEqual(attached[0]['instance_name'], 'alpha')
        self.assertEqual(len(detached), 2)
        self.assertEqual(len(body['backups']), 1)
        self.assertEqual(body['backups'][0]['source_name'], 'alpha-boot')
        inst = body['instances'][0]
        self.assertEqual(inst['boot_volume_id'], 'bv-1')
        self.assertEqual(inst['name'], 'alpha')

    def test_action_rejects_unknown_operation(self):
        res = self.post('/api/boot-volumes/action', dict(CRED_PAYLOAD, operation='explode'))
        self.assertFalse(res.get_json()['success'])

    def test_action_validates_required_ids(self):
        res = self.post('/api/boot-volumes/action', dict(CRED_PAYLOAD, operation='detach'))
        self.assertIn('boot_volume_id', res.get_json()['error'])
        res = self.post('/api/boot-volumes/action', dict(CRED_PAYLOAD, operation='attach', boot_volume_id='bv-2'))
        self.assertIn('instance_id', res.get_json()['error'])
        res = self.post('/api/boot-volumes/action',
                        dict(CRED_PAYLOAD, operation='instance-action', instance_id='i-1', power_action='BOOM'))
        self.assertIn('power_action', res.get_json()['error'])
        res = self.post('/api/boot-volumes/action',
                        dict(CRED_PAYLOAD, operation='replace', instance_id='i-1',
                             source_type='nope', source_id='x'))
        self.assertIn('source_type', res.get_json()['error'])
        res = self.post('/api/boot-volumes/action', dict(CRED_PAYLOAD, operation='create', size_gb=50))
        self.assertIn('availability_domain', res.get_json()['error'])
        res = self.post('/api/boot-volumes/action',
                        dict(CRED_PAYLOAD, operation='create', availability_domain=AD_1))
        self.assertIn('size_gb', res.get_json()['error'])
        res = self.post('/api/boot-volumes/action',
                        dict(CRED_PAYLOAD, operation='create', availability_domain=AD_1, size_gb=49))
        self.assertIn('between 50 and 32768', res.get_json()['error'])

    def test_action_runs_detach_job_to_completion(self):
        res = self.post('/api/boot-volumes/action',
                        dict(CRED_PAYLOAD, operation='detach', boot_volume_id='bv-1'))
        body = res.get_json()
        self.assertTrue(body['success'])
        # With instant fakes the job may already be done by the time the
        # response arrives; what matters is the end state.
        self.assertTrue(self.wait_for_job())
        self.assertEqual(self.block.volumes['bv-1'].lifecycle_state, 'AVAILABLE')
        status = self.client.get('/api/boot-volumes/status', auth=self.auth).get_json()
        self.assertFalse(status['job']['running'])

    def test_action_runs_create_job_to_completion(self):
        res = self.post('/api/boot-volumes/action', dict(
            CRED_PAYLOAD,
            operation='create',
            availability_domain=AD_2,
            display_name='api-created',
            size_gb=60,
        ))
        body = res.get_json()
        self.assertTrue(body['success'])
        self.assertTrue(self.wait_for_job())
        created = self.block.volumes.get('bv-created-1')
        self.assertIsNotNone(created)
        self.assertEqual(created.display_name, 'api-created')
        self.assertEqual(created.size_in_gbs, 60)
        self.assertEqual(created.availability_domain, AD_2)
        self.assertEqual(created.lifecycle_state, 'AVAILABLE')

    def test_action_refuses_second_concurrent_job(self):
        app._bv_job_begin('detach', 'bv-1')
        try:
            res = self.post('/api/boot-volumes/action',
                            dict(CRED_PAYLOAD, operation='delete', boot_volume_id='bv-2'))
            body = res.get_json()
            self.assertFalse(body['success'])
            self.assertIn('already running', body['error'])
        finally:
            app._bv_job_finish()

    def test_status_endpoint_includes_boot_volume_job(self):
        res = self.client.get('/api/status', auth=self.auth)
        body = res.get_json()
        self.assertIn('boot_volume_job', body)
        self.assertFalse(body['boot_volume_job']['running'])

    def test_stop_endpoint_signals_the_job(self):
        res = self.client.post('/api/boot-volumes/stop', json={}, auth=self.auth)
        self.assertTrue(res.get_json()['success'])
        self.assertTrue(app.bv_stop_event.is_set())


if __name__ == '__main__':
    unittest.main()
