import datetime
import functools
import html
import importlib
import os
import random
import re
import threading
import time
import urllib.parse

import requests
from flask import Flask, Response, jsonify, render_template, request

# The OCI SDK is large. Keep it out of the web process until an OCI operation is
# actually requested. This makes health checks and the UI cheap on small hosts,
# while keeping the SDK available for all API endpoints.
oci = None
_oci_import_lock = threading.Lock()


def get_oci():
    global oci
    if oci is None:
        with _oci_import_lock:
            if oci is None:
                oci = importlib.import_module('oci')
    return oci


def create_oci_client(client_class, config):
    """Create a bounded client and let this app, not the SDK, control retries."""
    sdk = get_oci()
    try:
        retry_strategy = sdk.retry.NoneRetryStrategy()
    except AttributeError:
        # A minimal SDK-compatible test double may not expose retry helpers.
        return client_class(config)
    kwargs = {
        'retry_strategy': retry_strategy,
        'timeout': (10, 60),
    }
    try:
        return client_class(config, **kwargs)
    except TypeError as exc:
        # Keeps simple test doubles and older compatible SDK clients usable.
        if 'unexpected keyword' not in str(exc):
            raise
        return client_class(config)

# ---- Timezone Configuration (Phnom Penh - ICT, UTC+7) ----
from zoneinfo import ZoneInfo
PHNOM_PENH_TZ = ZoneInfo("Asia/Phnom_Penh")

def get_phnom_penh_time():
    return datetime.datetime.now(PHNOM_PENH_TZ)

def format_phnom_penh_time(dt=None):
    if dt is None:
        dt = get_phnom_penh_time()
    return dt.strftime('%Y-%m-%d %H:%M:%S')

app = Flask(__name__)
# A config/private key is normally only a few KB. Reject accidental uploads and
# oversized JSON before Flask buffers them in memory.
try:
    _max_content_length = int(os.environ.get('MAX_CONTENT_LENGTH', 64 * 1024))
except (TypeError, ValueError):
    _max_content_length = 64 * 1024
app.config['MAX_CONTENT_LENGTH'] = max(4096, min(_max_content_length, 1024 * 1024))

# ---- Security headers ----
@app.after_request
def add_security_headers(response):
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response


# ---- Config ----
ADMIN_PASSWORD = os.environ.get('APP_PASSWORD')
if not ADMIN_PASSWORD:
    print("WARNING: APP_PASSWORD not set. Running WITHOUT authentication. Set APP_PASSWORD to enable Basic Auth.")


def env_int(name, default, minimum, maximum):
    """Read a bounded integer without making a bad env var crash the app."""
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


# The high ceiling allows a genuine 24/7 hunt: at a 60s retry delay, 100000
# attempts is roughly 69 days of continuous retrying. The default is still a
# conservative 100 so an accidentally orphaned loop does not retry forever.
MAX_ATTEMPTS = env_int('MAX_ATTEMPTS', 100, 1, 100000)
OCI_API_SLOTS = env_int('OCI_API_SLOTS', 2, 1, 8)
USAGE_CACHE_SECONDS = env_int('USAGE_CACHE_SECONDS', 20, 0, 300)

# ---- Shared state ----
# A bounded list is enough for the terminal and cannot grow forever during a
# long-running retry loop.
global_logs = []
global_log_base = 0
logs_lock = threading.Lock()

# Keep the single-process state intentional: the deployment commands below use
# one Gunicorn worker. Multiple workers would each run their own loop.
automation_lock = threading.Lock()
automation_running = False
automation_shape = None
stop_event = threading.Event()
oci_api_slots = threading.BoundedSemaphore(OCI_API_SLOTS)

# The quota screen can otherwise issue the same expensive OCI calls repeatedly.
# Only the result and a non-secret account key are cached; private keys are not.
usage_cache = {}
usage_cache_lock = threading.Lock()

# ---- Telegram live log settings ----
tg_live_lock = threading.Lock()
tg_live_enabled = False
tg_live_bot_token = None
tg_live_chat_id = None
tg_live_last_sent = 0
tg_live_min_interval = 3  # seconds between live log sends


def add_log(message):
    global global_log_base
    timestamp = format_phnom_penh_time()
    line = f"[{timestamp}] {message}"
    print(line)
    with logs_lock:
        global_logs.append(line)
        if len(global_logs) > 200:
            global_logs.pop(0)
            global_log_base += 1

    # Send to Telegram if live logging is enabled
    _send_live_log_to_telegram(line)

def _send_live_log_to_telegram(line):
    """Send a single log line to Telegram if live logging is enabled. Throttled to avoid rate limits."""
    global tg_live_enabled, tg_live_bot_token, tg_live_chat_id, tg_live_last_sent

    with tg_live_lock:
        if not tg_live_enabled or not tg_live_bot_token or not tg_live_chat_id:
            return

        now = time.time()
        if now - tg_live_last_sent < tg_live_min_interval:
            return
        tg_live_last_sent = now

    # Send outside the lock to avoid blocking
    try:
        clean_msg = line
        if len(clean_msg) > 4000:
            clean_msg = clean_msg[:4000] + "..."
        clean_msg = html.escape(clean_msg)

        url = f"https://api.telegram.org/bot{tg_live_bot_token}/sendMessage"
        payload = {
            "chat_id": tg_live_chat_id,
            "text": f"<code>{clean_msg}</code>",
            "parse_mode": "HTML"
        }
        requests.post(url, json=payload, timeout=5)
    except Exception:
        pass


def build_config(data):
    return {
        "user": data.get('user'),
        "fingerprint": data.get('fingerprint'),
        "tenancy": data.get('tenancy'),
        "region": data.get('region'),
        "key_content": data.get('private_key')
    }


def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not ADMIN_PASSWORD:
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or auth.password != ADMIN_PASSWORD:
            return Response(
                'Authentication required',
                401,
                {'WWW-Authenticate': 'Basic realm="OCI Provisioner"'}
            )
        return f(*args, **kwargs)
    return decorated


def limit_oci_requests(f):
    """Keep concurrent OCI calls bounded on a low-memory/small-CPU host."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not oci_api_slots.acquire(timeout=2):
            return jsonify({
                'success': False,
                'error': 'The server is busy with another OCI request. Try again shortly.'
            }), 503
        try:
            return f(*args, **kwargs)
        finally:
            oci_api_slots.release()
    return decorated


def hold_oci_slot(f):
    """Hold one slot for the whole background provisioning job."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not oci_api_slots.acquire(timeout=5):
            add_log('Provisioning could not start: OCI request limit is busy.')
            with automation_lock:
                global automation_running, automation_shape
                automation_running = False
                automation_shape = None
            return
        try:
            return f(*args, **kwargs)
        finally:
            oci_api_slots.release()
    return decorated


def usage_cache_key(config):
    # Do not include key_content in the cache key or retain it in memory.
    return (
        config.get('tenancy'),
        config.get('user'),
        config.get('fingerprint'),
        config.get('region')
    )


def get_cached_usage(key):
    if not USAGE_CACHE_SECONDS:
        return None
    now = time.monotonic()
    with usage_cache_lock:
        cached = usage_cache.get(key)
        if cached and now - cached[0] < USAGE_CACHE_SECONDS:
            return cached[1]
        if cached:
            usage_cache.pop(key, None)
    return None


def cache_usage(key, usage):
    if not USAGE_CACHE_SECONDS:
        return
    with usage_cache_lock:
        # There is normally one account per process. Keep this bounded if a
        # public deployment receives requests for many tenancies.
        if len(usage_cache) >= 8 and key not in usage_cache:
            oldest = min(usage_cache, key=lambda item: usage_cache[item][0])
            usage_cache.pop(oldest, None)
        usage_cache[key] = (time.monotonic(), usage)


# ---- 24/7 keep-alive (anti-sleep) ----
# Railway's "Serverless" feature (formerly App Sleeping) stops a service
# roughly 5-10 minutes after its last OUTBOUND network traffic. Inbound
# requests alone do not keep it awake. The pinger below sends small periodic
# outbound requests (by default to this app's own public /healthz endpoint)
# so a deployment stays awake around the clock and the in-memory provisioning
# loop keeps hunting for free-tier capacity. Pings are tiny HTTP GETs and the
# feature can be disabled with KEEPALIVE_ENABLED=false or from the UI.
def _env_bool(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


KEEPALIVE_ENABLED_DEFAULT = _env_bool('KEEPALIVE_ENABLED', True)
# Railway sleeps a service at least 5 minutes after its last outbound packet,
# sampled on an interval. Pinging every 240s (max 295s) stays comfortably
# under that window while generating almost no traffic.
KEEPALIVE_INTERVAL_DEFAULT = env_int('KEEPALIVE_INTERVAL_SECONDS', 240, 30, 295)
KEEPALIVE_TIMEOUT_SECONDS = env_int('KEEPALIVE_TIMEOUT_SECONDS', 10, 2, 30)
KEEPALIVE_MAX_URLS = 5
KEEPALIVE_MIN_INTERVAL = 30
KEEPALIVE_MAX_INTERVAL = 295
KEEPALIVE_FAIL_LOG_SECONDS = 300

# Railway injects RAILWAY_PUBLIC_DOMAIN once the service has a public domain;
# older templates may expose RAILWAY_STATIC_URL instead.
_KEEPALIVE_DOMAIN_VARS = ('RAILWAY_PUBLIC_DOMAIN', 'RAILWAY_STATIC_URL')


def _parse_keepalive_urls(raw):
    """Turn a comma/space separated string into validated ping targets."""
    urls = []
    for part in re.split(r'[,\s]+', raw or ''):
        part = part.strip()
        if not part or len(part) > 500 or len(urls) >= KEEPALIVE_MAX_URLS:
            continue
        parsed = urllib.parse.urlsplit(part)
        if parsed.scheme in ('http', 'https') and parsed.netloc and part not in urls:
            urls.append(part)
    return urls


def _default_keepalive_urls():
    urls = _parse_keepalive_urls(os.environ.get('KEEPALIVE_URLS', ''))
    if urls:
        return urls
    for var in _KEEPALIVE_DOMAIN_VARS:
        domain = os.environ.get(var, '').strip()
        if not domain:
            continue
        if '://' in domain:
            candidate = domain.rstrip('/') + '/healthz'
        else:
            candidate = 'https://%s/healthz' % domain
        parsed = _parse_keepalive_urls(candidate)
        if parsed:
            return parsed
    return []


_keepalive_lock = threading.Lock()
_keepalive_cfg = {
    'enabled': KEEPALIVE_ENABLED_DEFAULT,
    'interval': KEEPALIVE_INTERVAL_DEFAULT,
    'urls': _default_keepalive_urls(),
}
_keepalive_stats = {
    'started_at': None,
    'last_attempt': None,
    'last_success': None,
    'last_status_code': None,
    'last_error': None,
    'consecutive_failures': 0,
    'total_pings': 0,
    'total_failures': 0,
    'last_fail_log': 0.0,
}
_keepalive_thread = None
_keepalive_stop = threading.Event()


def _keepalive_url_for_log(url):
    """Strip query strings; some pinger URLs embed private tokens there."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def _keepalive_log(message):
    add_log('Keep-alive: ' + message)


def _keepalive_ping_target(url):
    """One outbound request. Any HTTP response keeps the host's sleep timer
    reset; only transport-level errors (DNS, refused, timeout) count as
    failures because those mean no request went out."""
    try:
        response = requests.get(
            url,
            timeout=KEEPALIVE_TIMEOUT_SECONDS,
            headers={'User-Agent': 'oci-provisioner-keepalive/1.0'},
        )
        return True, response.status_code, None
    except requests.RequestException as exc:
        return False, None, str(exc)[:200]


def keepalive_ping_once():
    """Ping every configured target once and record the outcome."""
    with _keepalive_lock:
        targets = list(_keepalive_cfg['urls'])
    if not targets:
        return

    now = time.time()
    any_ok = False
    first_error = None
    primary_code = None
    for index, url in enumerate(targets):
        ok, status_code, error = _keepalive_ping_target(url)
        if index == 0:
            primary_code = status_code
        any_ok = any_ok or ok
        if not ok and first_error is None:
            first_error = error or 'HTTP %s' % status_code

    with _keepalive_lock:
        stats = _keepalive_stats
        stats['last_attempt'] = now
        stats['total_pings'] += 1
        if any_ok:
            recovered = stats['consecutive_failures'] >= 3
            stats['consecutive_failures'] = 0
            stats['last_success'] = now
            stats['last_status_code'] = primary_code
            stats['last_error'] = None
            if recovered:
                _keepalive_log('ping recovered')
        else:
            stats['consecutive_failures'] += 1
            stats['total_failures'] += 1
            stats['last_error'] = first_error
            if now - stats['last_fail_log'] >= KEEPALIVE_FAIL_LOG_SECONDS:
                stats['last_fail_log'] = now
                _keepalive_log(
                    'ping failed (%s). On Railway, make sure the service has a '
                    'public domain (it sets RAILWAY_PUBLIC_DOMAIN automatically) '
                    'or set KEEPALIVE_URLS.' % first_error
                )


def _keepalive_loop():
    while not _keepalive_stop.is_set():
        with _keepalive_lock:
            interval = _keepalive_cfg['interval']
            has_targets = bool(_keepalive_cfg['urls'])
        if not has_targets:
            # No target configured yet; idle until the operator adds one.
            if _keepalive_stop.wait(30):
                break
            continue
        keepalive_ping_once()
        if _keepalive_stop.wait(interval):
            break


def _keepalive_thread_running():
    return _keepalive_thread is not None and _keepalive_thread.is_alive()


def _keepalive_start_thread():
    global _keepalive_thread
    if _keepalive_thread_running():
        return
    _keepalive_stop.clear()
    with _keepalive_lock:
        _keepalive_stats['started_at'] = time.time()
        interval = _keepalive_cfg['interval']
        targets = [_keepalive_url_for_log(u) for u in _keepalive_cfg['urls']]
    _keepalive_thread = threading.Thread(
        target=_keepalive_loop, name='oci-keepalive', daemon=True
    )
    _keepalive_thread.start()
    _keepalive_log(
        'started — staying awake 24/7 by pinging %d target(s) every %ds: %s'
        % (len(targets), interval, ', '.join(targets) if targets else '(none yet)')
    )


def _keepalive_stop_thread():
    global _keepalive_thread
    was_running = _keepalive_thread_running()
    _keepalive_stop.set()
    _keepalive_thread = None
    if was_running:
        _keepalive_log('stopped — the host may sleep this service when idle')


def keepalive_apply(enabled=None, interval=None, urls=None):
    """Validate and apply keep-alive settings, starting/stopping the pinger.

    Runtime changes live in memory only: after a restart or redeploy the
    environment variables apply again. Returns (ok, error).
    """
    with _keepalive_lock:
        new_cfg = dict(_keepalive_cfg)

    if enabled is not None:
        new_cfg['enabled'] = bool(enabled)

    if interval is not None:
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            return False, 'interval_seconds must be an integer'
        new_cfg['interval'] = max(KEEPALIVE_MIN_INTERVAL, min(interval, KEEPALIVE_MAX_INTERVAL))

    if urls is not None:
        if isinstance(urls, str):
            raw_text = urls
        elif isinstance(urls, (list, tuple)):
            raw_text = ' '.join(str(item) for item in urls)
        else:
            return False, 'urls must be a list of http(s) URLs or a string'
        raw_count = len([p for p in re.split(r'[,\s]+', raw_text) if p.strip()])
        parsed = _parse_keepalive_urls(raw_text)
        if raw_count and not parsed:
            return False, 'No valid http(s) URLs found (max %d, 500 chars each)' % KEEPALIVE_MAX_URLS
        new_cfg['urls'] = parsed

    with _keepalive_lock:
        _keepalive_cfg.clear()
        _keepalive_cfg.update(new_cfg)
        should_run = new_cfg['enabled']

    if should_run:
        _keepalive_start_thread()
    else:
        _keepalive_stop_thread()
    return True, ''


def keepalive_boot():
    ok, error = keepalive_apply(
        enabled=KEEPALIVE_ENABLED_DEFAULT,
        interval=KEEPALIVE_INTERVAL_DEFAULT,
    )
    if not ok:
        print('Keep-alive: configuration error: %s' % error)
    elif KEEPALIVE_ENABLED_DEFAULT and not _keepalive_cfg['urls']:
        print(
            'Keep-alive: enabled but no target URL is known yet. On Railway, '
            'generate a public domain (it provides RAILWAY_PUBLIC_DOMAIN) or '
            'set KEEPALIVE_URLS. You can also configure targets from the UI.'
        )


def keepalive_status():
    """Full keep-alive state for the authenticated API/UI (URLs redacted)."""
    with _keepalive_lock:
        cfg = dict(_keepalive_cfg)
        stats = dict(_keepalive_stats)
    now = time.time()
    return {
        'enabled': cfg['enabled'],
        'running': bool(cfg['enabled'] and _keepalive_thread_running()),
        'interval_seconds': cfg['interval'],
        'targets': [_keepalive_url_for_log(u) for u in cfg['urls']],
        'target_count': len(cfg['urls']),
        'started_at': stats['started_at'],
        'last_attempt_ago': (now - stats['last_attempt']) if stats['last_attempt'] else None,
        'last_success_ago': (now - stats['last_success']) if stats['last_success'] else None,
        'last_status_code': stats['last_status_code'],
        'last_error': stats['last_error'],
        'consecutive_failures': stats['consecutive_failures'],
        'total_pings': stats['total_pings'],
        'total_failures': stats['total_failures'],
    }


def keepalive_public_status():
    """Two cheap booleans for the unauthenticated /healthz endpoint."""
    with _keepalive_lock:
        enabled = _keepalive_cfg['enabled']
    return {'enabled': enabled, 'running': bool(enabled and _keepalive_thread_running())}


keepalive_boot()


# ---- Boot volume manager ----
# Create, attach, detach, re-attach, replace and remove (delete) boot volumes.
# Every mutating operation runs as a single process-local background job,
# mirroring the provisioning loop: progress goes to the shared live log (and
# the Telegram live stream when enabled), so the HTTP request itself returns
# immediately and never risks a Gunicorn timeout on slow OCI state changes.
BV_JOB_OPERATIONS = ('create', 'detach', 'attach', 'delete', 'instance-action', 'replace')
BV_POWER_ACTIONS = ('SOFTSTOP', 'STOP', 'START')
BV_POLL_SECONDS = 4
BV_WAIT_ATTACHMENT_SECONDS = 240
BV_WAIT_INSTANCE_SECONDS = 420
BV_WAIT_DELETE_SECONDS = 180

bv_job_lock = threading.Lock()
bv_job_running = False
bv_job_operation = None
bv_job_target = None
bv_stop_event = threading.Event()


class _BvJobError(Exception):
    """An operator-fixable job failure (wrong state, stop request, timeout)."""


def _bv_log(message):
    add_log('Boot volume: ' + message)


def _bv_ocid(value):
    value = str(value or '')
    return (value[:24] + '...') if len(value) > 24 else value


def _bv_error_text(exc):
    text = getattr(exc, 'message', None) or str(exc)
    return str(text)[:200]


def _bv_volume_label(volume):
    name = getattr(volume, 'display_name', None) or _bv_ocid(getattr(volume, 'id', '?'))
    return '%s (%s GB)' % (name, getattr(volume, 'size_in_gbs', '?'))


def _bv_instance_label(instance):
    return getattr(instance, 'display_name', None) or _bv_ocid(getattr(instance, 'id', '?'))


def _bv_wait(getter, target_states, timeout_seconds, label):
    """Poll a lifecycle-state getter until a target state, stop, or timeout."""
    if isinstance(target_states, str):
        target_states = (target_states,)
    deadline = time.monotonic() + timeout_seconds
    while True:
        if bv_stop_event.is_set():
            raise _BvJobError('Stopped by user.')
        try:
            state = getter()
        except _BvJobError:
            raise
        except Exception as exc:
            raise _BvJobError('Polling %s failed: %s' % (label, _bv_error_text(exc)))
        if state in target_states:
            return state
        if time.monotonic() >= deadline:
            raise _BvJobError(
                'Timed out waiting for %s to reach %s (last state: %s).'
                % (label, '/'.join(target_states), state or 'unknown')
            )
        time.sleep(BV_POLL_SECONDS)


def _bv_instance_state(compute_client, instance_id):
    return getattr(compute_client.get_instance(instance_id=instance_id).data, 'lifecycle_state', None)


def _bv_volume_state(block_client, boot_volume_id):
    return getattr(block_client.get_boot_volume(boot_volume_id=boot_volume_id).data, 'lifecycle_state', None)


def _bv_attachment_state(compute_client, attachment_id):
    return getattr(
        compute_client.get_boot_volume_attachment(boot_volume_attachment_id=attachment_id).data,
        'lifecycle_state', None
    )


def _bv_list_attachments(compute_client, compartment_id, availability_domain, instance_id=None):
    kwargs = {'compartment_id': compartment_id, 'availability_domain': availability_domain}
    if instance_id:
        kwargs['instance_id'] = instance_id
    return compute_client.list_boot_volume_attachments(**kwargs).data


def _bv_volume_attachment(compute_client, compartment_id, volume):
    """The live (non-DETACHED) attachment of a boot volume, if any."""
    for att in _bv_list_attachments(
        compute_client, compartment_id, getattr(volume, 'availability_domain', None)
    ):
        if att.boot_volume_id == volume.id and getattr(att, 'lifecycle_state', '') != 'DETACHED':
            return att
    return None


def _bv_instance_attachment(compute_client, compartment_id, instance):
    """The live (non-DETACHED) boot volume attachment of an instance, if any."""
    for att in _bv_list_attachments(
        compute_client, compartment_id, getattr(instance, 'availability_domain', None),
        instance_id=instance.id
    ):
        if getattr(att, 'lifecycle_state', '') != 'DETACHED':
            return att
    return None


def _bv_require_stopped(compute_client, instance_id, operation):
    state = _bv_instance_state(compute_client, instance_id)
    if state != 'STOPPED':
        raise _BvJobError(
            'Cannot %s while the instance is %s. Stop it first — the power '
            'buttons are in this panel (Replace stops the instance for you).'
            % (operation, state or 'unknown')
        )


def _bv_power(compute_client, instance, action):
    label = _bv_instance_label(instance)
    target = 'RUNNING' if action == 'START' else 'STOPPED'
    current = getattr(instance, 'lifecycle_state', None)
    if current == target:
        _bv_log('Instance %s is already %s — nothing to do.' % (label, target))
        return current
    _bv_log('Power %s on instance %s ...' % (action, label))
    compute_client.instance_action(instance_id=instance.id, action=action)
    state = _bv_wait(
        lambda: _bv_instance_state(compute_client, instance.id),
        target, BV_WAIT_INSTANCE_SECONDS, 'instance power ' + action
    )
    _bv_log('Instance %s is now %s.' % (label, state))
    return state


def _bv_detach_volume(compute_client, compartment_id, volume):
    label = _bv_volume_label(volume)
    attachment = _bv_volume_attachment(compute_client, compartment_id, volume)
    if not attachment:
        _bv_log('%s has no active attachment — it is already detached.' % label)
        return None
    _bv_require_stopped(compute_client, attachment.instance_id, 'detach')
    _bv_log('Detaching %s from instance %s ...' % (label, _bv_ocid(attachment.instance_id)))
    compute_client.detach_boot_volume(boot_volume_attachment_id=attachment.id)
    _bv_wait(
        lambda: _bv_attachment_state(compute_client, attachment.id),
        'DETACHED', BV_WAIT_ATTACHMENT_SECONDS, 'detach of ' + label
    )
    _bv_log('%s detached — now AVAILABLE for attach, replace or delete.' % label)
    return attachment.instance_id


def _bv_attach_volume(compute_client, compartment_id, volume, instance):
    sdk = get_oci()
    label = _bv_volume_label(volume)
    inst_label = _bv_instance_label(instance)
    state = getattr(volume, 'lifecycle_state', None)
    if state != 'AVAILABLE':
        raise _BvJobError(
            '%s is %s — only AVAILABLE (fully detached) volumes can attach. '
            'Detach it from its current instance first.' % (label, state or 'unknown')
        )
    if getattr(volume, 'availability_domain', None) != getattr(instance, 'availability_domain', None):
        raise _BvJobError(
            '%s lives in %s but instance %s is in %s. Boot volumes only attach '
            'inside their own availability domain.' % (
                label, getattr(volume, 'availability_domain', '?'),
                inst_label, getattr(instance, 'availability_domain', '?')
            )
        )
    existing = _bv_instance_attachment(compute_client, compartment_id, instance)
    if existing:
        raise _BvJobError(
            'Instance %s already has a boot volume (%s). Detach that one first '
            '— or use Replace, which swaps the disks safely in one job.'
            % (inst_label, _bv_ocid(existing.boot_volume_id))
        )
    _bv_require_stopped(compute_client, instance.id, 'attach')
    details = sdk.core.models.AttachBootVolumeDetails(
        instance_id=instance.id, boot_volume_id=volume.id
    )
    _bv_log('Attaching %s to instance %s ...' % (label, inst_label))
    attachment = compute_client.attach_boot_volume(attach_boot_volume_details=details).data
    attachment_id = getattr(attachment, 'id', None)
    if attachment_id:
        _bv_wait(
            lambda: _bv_attachment_state(compute_client, attachment_id),
            'ATTACHED', BV_WAIT_ATTACHMENT_SECONDS, 'attach of ' + label
        )
    _bv_log('%s attached to %s.' % (label, inst_label))
    return attachment


def _bv_delete_volume(compute_client, block_client, compartment_id, volume):
    label = _bv_volume_label(volume)
    state = getattr(volume, 'lifecycle_state', None)
    if state != 'AVAILABLE':
        raise _BvJobError(
            '%s is %s — detach it from its instance before deleting. Deleting '
            'is permanent.' % (label, state or 'unknown')
        )
    _bv_log('Deleting %s (permanent) ...' % label)
    block_client.delete_boot_volume(boot_volume_id=volume.id)

    def _gone_state():
        try:
            return block_client.get_boot_volume(boot_volume_id=volume.id).data.lifecycle_state
        except Exception as exc:
            if getattr(exc, 'status', None) == 404 or 'not found' in str(exc).lower():
                return 'TERMINATED'
            raise

    _bv_wait(_gone_state, 'TERMINATED', BV_WAIT_DELETE_SECONDS, 'deletion of ' + label)
    _bv_log('%s deleted — its size no longer counts against the 200 GB free-tier storage.' % label)


def _bv_create_volume(
    block_client, compartment_id, availability_domain, display_name,
    size_in_gbs=None, backup_id=None,
):
    """Create an empty boot volume or restore one from a boot volume backup."""
    sdk = get_oci()
    details_args = {
        'compartment_id': compartment_id,
        'availability_domain': availability_domain,
        'display_name': display_name[:64] if display_name else None,
    }
    if backup_id:
        details_args['source_details'] = (
            sdk.core.models.BootVolumeSourceFromBootVolumeBackupDetails(
                id=backup_id
            )
        )
    if size_in_gbs is not None:
        details_args['size_in_gbs'] = size_in_gbs

    details = sdk.core.models.CreateBootVolumeDetails(**details_args)
    if backup_id:
        _bv_log('Restoring a fresh boot volume from backup %s ...' % _bv_ocid(backup_id))
        wait_label = 'restore of new boot volume'
    else:
        _bv_log(
            'Creating an empty %s GB boot volume in %s ...'
            % (size_in_gbs, availability_domain)
        )
        wait_label = 'creation of new boot volume'

    volume = block_client.create_boot_volume(create_boot_volume_details=details).data
    volume_id = getattr(volume, 'id', None)
    if volume_id:
        _bv_wait(
            lambda: _bv_volume_state(block_client, volume_id),
            'AVAILABLE', BV_WAIT_ATTACHMENT_SECONDS, wait_label
        )
        volume = block_client.get_boot_volume(boot_volume_id=volume_id).data
    _bv_log('New boot volume ready: %s.' % _bv_volume_label(volume))
    return volume


def _bv_create_from_backup(block_client, compartment_id, availability_domain, backup_id, display_name):
    return _bv_create_volume(
        block_client, compartment_id, availability_domain, display_name,
        backup_id=backup_id,
    )


def _bv_get_volume(block_client, boot_volume_id):
    try:
        return block_client.get_boot_volume(boot_volume_id=boot_volume_id).data
    except Exception as exc:
        raise _BvJobError(
            'Boot volume %s is not readable: %s' % (_bv_ocid(boot_volume_id), _bv_error_text(exc))
        )


def _bv_get_instance(compute_client, instance_id):
    try:
        return compute_client.get_instance(instance_id=instance_id).data
    except Exception as exc:
        raise _BvJobError(
            'Instance %s is not readable: %s' % (_bv_ocid(instance_id), _bv_error_text(exc))
        )


def _bv_replace_flow(compute_client, block_client, tenancy, params):
    """Stop -> detach old -> attach replacement -> start, with rollback.

    The replacement source is either an existing AVAILABLE boot volume in the
    same AD, or a boot volume backup (a fresh volume is restored from it first).
    If attaching the replacement fails, the original disk is re-attached and
    the instance started again — best effort — so the VM is not left naked.
    """
    instance = _bv_get_instance(compute_client, params.get('instance_id'))
    inst_label = _bv_instance_label(instance)
    ad_name = getattr(instance, 'availability_domain', None)
    source_type = params.get('source_type')
    source_id = params.get('source_id')
    delete_old = bool(params.get('delete_old'))
    power_action = params.get('power_action') or 'SOFTSTOP'
    _bv_log('Replace on instance %s in %s.' % (inst_label, ad_name or 'unknown AD'))

    # 1. The instance must be STOPPED before its boot disk can move.
    state = getattr(instance, 'lifecycle_state', None)
    if state != 'STOPPED':
        _bv_power(compute_client, instance, power_action)
    else:
        _bv_log('Instance %s is already STOPPED.' % inst_label)

    # 2. Find the disk currently booting the instance.
    attachment = _bv_instance_attachment(compute_client, tenancy, instance)
    if not attachment:
        raise _BvJobError(
            'Instance %s has no boot volume attached — use plain Attach instead.' % inst_label
        )
    old_volume = _bv_get_volume(block_client, attachment.boot_volume_id)
    old_label = _bv_volume_label(old_volume)
    _bv_log('Current disk: %s.' % old_label)

    # 3. Resolve the replacement disk before touching the old one.
    new_volume = None
    if source_type == 'backup':
        new_name = 'replace-%s-%s' % (inst_label, int(time.time()))
        new_volume = _bv_create_from_backup(block_client, tenancy, ad_name, source_id, new_name)
    elif source_type == 'boot_volume':
        new_volume = _bv_get_volume(block_client, source_id)
        if getattr(new_volume, 'availability_domain', None) != ad_name:
            raise _BvJobError(
                'Replacement %s is in %s but the instance is in %s. Boot volumes '
                'never cross availability domains.' % (
                    _bv_volume_label(new_volume),
                    getattr(new_volume, 'availability_domain', '?'), ad_name
                )
            )
        if getattr(new_volume, 'id', None) == getattr(old_volume, 'id', None):
            raise _BvJobError('The selected replacement is the disk already attached. Pick another source.')
        state = getattr(new_volume, 'lifecycle_state', None)
        if state != 'AVAILABLE':
            raise _BvJobError(
                'Replacement %s is %s — it must be AVAILABLE (detached) first.'
                % (_bv_volume_label(new_volume), state or 'unknown')
            )
    else:
        raise _BvJobError("Replace needs a replacement source: an AVAILABLE boot volume or a backup.")

    # 4. Detach the old disk, then attach the replacement with rollback.
    _bv_detach_volume(compute_client, tenancy, old_volume)
    try:
        _bv_attach_volume(compute_client, tenancy, new_volume, instance)
    except Exception as exc:
        failure = _bv_error_text(exc)
        _bv_log('Attach of the replacement failed (%s) — rolling back to %s.' % (failure, old_label))
        try:
            old_refreshed = _bv_get_volume(block_client, old_volume.id)
            _bv_attach_volume(compute_client, tenancy, old_refreshed, instance)
            _bv_log('Rollback attached the original disk again; starting the instance.')
            compute_client.instance_action(instance_id=instance.id, action='START')
            _bv_wait(
                lambda: _bv_instance_state(compute_client, instance.id),
                'RUNNING', BV_WAIT_INSTANCE_SECONDS, 'instance start after rollback'
            )
            _bv_log('Instance %s is RUNNING again on its original disk.' % inst_label)
        except Exception as rollback_exc:
            _bv_log(
                'ROLLBACK FAILED: %s — re-attach %s manually in the OCI Console.'
                % (_bv_error_text(rollback_exc), old_label)
            )
        raise _BvJobError('Replace aborted during attach: %s' % failure)

    # 5. Boot the instance on the replacement disk.
    compute_client.instance_action(instance_id=instance.id, action='START')
    _bv_wait(
        lambda: _bv_instance_state(compute_client, instance.id),
        'RUNNING', BV_WAIT_INSTANCE_SECONDS, 'instance start'
    )
    _bv_log('Replace complete: %s now boots from %s.' % (inst_label, _bv_volume_label(new_volume)))

    # 6. Optionally free the old disk afterwards (best effort).
    if delete_old:
        try:
            old_refreshed = _bv_get_volume(block_client, old_volume.id)
            _bv_delete_volume(compute_client, block_client, tenancy, old_refreshed)
        except Exception as exc:
            _bv_log(
                'Old disk kept (delete failed: %s) — it is detached; delete it later from this panel.'
                % _bv_error_text(exc)
            )
    else:
        _bv_log(
            'Old disk %s kept (detached). Verify the new disk, then delete the old '
            'one from this panel to reclaim its storage.' % old_label
        )
    return {'instance': inst_label, 'new_disk': _bv_volume_label(new_volume), 'old_disk_deleted': delete_old}


def _bv_send_telegram(bot_token, chat_id, message):
    if not bot_token or not chat_id:
        return
    ok, err = send_telegram_message(bot_token, chat_id, message)
    if not ok:
        add_log('Boot volume Telegram alert failed: %s' % err)


def _bv_job_begin(operation, target):
    global bv_job_running, bv_job_operation, bv_job_target
    with bv_job_lock:
        if bv_job_running:
            return "A boot volume job is already running ('%s'). Wait for it or stop it from the panel." % bv_job_operation
        bv_job_running = True
        bv_job_operation = operation
        bv_job_target = target
        bv_stop_event.clear()
    return None


def _bv_job_finish():
    global bv_job_running, bv_job_operation, bv_job_target
    with bv_job_lock:
        bv_job_running = False
        bv_job_operation = None
        bv_job_target = None


def boot_volume_job_status():
    with bv_job_lock:
        return {
            'running': bv_job_running,
            'operation': bv_job_operation,
            'target': _bv_ocid(bv_job_target) if bv_job_target else None,
        }


def run_boot_volume_job(operation, config, params, telegram_bot_token=None, telegram_chat_id=None):
    """Single background worker for every mutating boot volume operation.

    Holds one OCI slot for the whole job, exactly like the provisioning loop,
    so a long stop/detach/attach sequence cannot starve normal UI requests.
    """
    telegram_bot_token = (telegram_bot_token or '').strip() or None
    telegram_chat_id = (telegram_chat_id or '').strip() or None

    if not oci_api_slots.acquire(timeout=5):
        add_log('Boot volume job could not start: OCI request limit is busy.')
        _bv_job_finish()
        return
    try:
        sdk = get_oci()
        compute_client = create_oci_client(sdk.core.ComputeClient, config)
        block_client = create_oci_client(sdk.core.BlockstorageClient, config)
        tenancy = config['tenancy']
        _bv_log("Job '%s' started." % operation)
        replace_summary = None

        if bv_stop_event.is_set():
            # Stop was pressed before the first mutation: change nothing.
            raise _BvJobError('Stopped by user before any OCI change was made.')

        if operation == 'create':
            _bv_create_volume(
                block_client,
                tenancy,
                params.get('availability_domain'),
                params.get('display_name'),
                size_in_gbs=params.get('size_gb'),
                backup_id=params.get('backup_id'),
            )

        elif operation == 'detach':
            volume = _bv_get_volume(block_client, params.get('boot_volume_id'))
            _bv_detach_volume(compute_client, tenancy, volume)

        elif operation == 'attach':
            volume = _bv_get_volume(block_client, params.get('boot_volume_id'))
            instance = _bv_get_instance(compute_client, params.get('instance_id'))
            _bv_attach_volume(compute_client, tenancy, volume, instance)

        elif operation == 'delete':
            volume = _bv_get_volume(block_client, params.get('boot_volume_id'))
            _bv_delete_volume(compute_client, block_client, tenancy, volume)

        elif operation == 'instance-action':
            instance = _bv_get_instance(compute_client, params.get('instance_id'))
            _bv_power(compute_client, instance, params.get('power_action'))

        elif operation == 'replace':
            replace_summary = _bv_replace_flow(compute_client, block_client, tenancy, params)

        else:
            raise _BvJobError("Unknown boot volume operation '%s'." % operation)

        _bv_log("Job '%s' finished successfully." % operation)
        if operation == 'replace' and replace_summary:
            _bv_send_telegram(
                telegram_bot_token, telegram_chat_id,
                "&#128260; <b>Boot disk replaced</b>\n\n"
                "<b>Instance:</b> %s\n"
                "<b>Now boots from:</b> %s\n"
                "<b>Old disk:</b> %s\n"
                "<b>Time:</b> %s (Phnom Penh)" % (
                    _telegram_value(replace_summary['instance']),
                    _telegram_value(replace_summary['new_disk']),
                    'deleted' if replace_summary['old_disk_deleted'] else 'kept (detached)',
                    _telegram_value(format_phnom_penh_time()),
                )
            )
    except _BvJobError as exc:
        _bv_log("Job '%s' failed: %s" % (operation, exc))
        if operation == 'replace':
            _bv_send_telegram(
                telegram_bot_token, telegram_chat_id,
                "&#10060; <b>Boot disk replace failed</b>\n\n"
                "<b>Reason:</b> %s\n"
                "<b>Time:</b> %s (Phnom Penh)" % (
                    _telegram_value(str(exc)[:200]),
                    _telegram_value(format_phnom_penh_time()),
                )
            )
    except Exception as exc:
        _bv_log("Job '%s' crashed: %s" % (operation, _bv_error_text(exc)))
    finally:
        oci_api_slots.release()
        _bv_job_finish()


@app.route('/api/boot-volumes/list', methods=['POST'])
@require_auth
@limit_oci_requests
def list_boot_volumes():
    """Inventory for the boot volume manager: instances, volumes, backups."""
    data = request.json or {}
    config = build_config(data)

    try:
        sdk = get_oci()
        sdk.config.validate_config(config)
        compute_client = create_oci_client(sdk.core.ComputeClient, config)
        block_client = create_oci_client(sdk.core.BlockstorageClient, config)
        identity_client = create_oci_client(sdk.identity.IdentityClient, config)
        tenancy = config['tenancy']

        ads = identity_client.list_availability_domains(compartment_id=tenancy).data
        availability_domains = []
        for ad in ads:
            ad_name = getattr(ad, 'name', None)
            if ad_name:
                availability_domains.append({
                    'name': ad_name,
                    'ad_short': ad_name.split(':')[-1],
                })

        instance_index = {}
        instances = []
        for inst in compute_client.list_instances(compartment_id=tenancy).data:
            state = getattr(inst, 'lifecycle_state', '')
            if state == 'TERMINATED':
                continue
            instance_index[inst.id] = inst
            ad_name = getattr(inst, 'availability_domain', '') or ''
            instances.append({
                'id': inst.id,
                'name': _bv_instance_label(inst),
                'state': state,
                'shape': getattr(inst, 'shape', ''),
                'availability_domain': ad_name,
                'ad_short': ad_name.split(':')[-1],
                'boot_volume_id': None,
            })
        instance_by_id = {item['id']: item for item in instances}

        volumes = []
        total_storage_gb = 0
        for ad in ads:
            ad_name = getattr(ad, 'name', None)
            attachments_by_volume = {}
            for att in _bv_list_attachments(compute_client, tenancy, ad_name):
                if getattr(att, 'lifecycle_state', '') != 'DETACHED':
                    attachments_by_volume[att.boot_volume_id] = att
                    inst_info = instance_by_id.get(att.instance_id)
                    if inst_info:
                        inst_info['boot_volume_id'] = att.boot_volume_id

            boot_volumes = block_client.list_boot_volumes(
                compartment_id=tenancy, availability_domain=ad_name
            ).data
            for vol in boot_volumes:
                if getattr(vol, 'lifecycle_state', '') == 'TERMINATED':
                    continue
                size = int(getattr(vol, 'size_in_gbs', 0) or 0)
                total_storage_gb += size
                att = attachments_by_volume.get(vol.id)
                attached_instance = instance_index.get(att.instance_id) if att else None
                vol_ad = getattr(vol, 'availability_domain', '') or ad_name or ''
                volumes.append({
                    'id': vol.id,
                    'name': _bv_volume_label(vol).rsplit(' (', 1)[0],
                    'size_gb': size,
                    'state': getattr(vol, 'lifecycle_state', ''),
                    'availability_domain': vol_ad,
                    'ad_short': vol_ad.split(':')[-1],
                    'attached': att is not None,
                    'instance_id': att.instance_id if att else None,
                    'instance_name': (
                        _bv_instance_label(attached_instance) if attached_instance
                        else ('Unknown instance' if att else None)
                    ),
                })
        volumes.sort(key=lambda item: (not item['attached'], item['name']))
        instances.sort(key=lambda item: (item['state'] != 'RUNNING', item['name']))

        # Backups are replacement sources for a brand-new disk. Listing them is
        # best effort: some tenancies block backups even when volumes are fine.
        backups = []
        try:
            volume_names = {item['id']: item['name'] for item in volumes}
            for backup in block_client.list_boot_volume_backups(compartment_id=tenancy).data:
                if getattr(backup, 'lifecycle_state', '') != 'AVAILABLE':
                    continue
                source_id = getattr(backup, 'boot_volume_id', None)
                backups.append({
                    'id': backup.id,
                    'name': getattr(backup, 'display_name', None) or _bv_ocid(backup.id),
                    'size_gb': int(getattr(backup, 'size_in_gbs', 0) or 0),
                    'state': getattr(backup, 'lifecycle_state', ''),
                    'source_boot_volume_id': source_id,
                    'source_name': volume_names.get(source_id),
                })
        except Exception as exc:
            _bv_log('Backup listing skipped (%s) — volumes still usable.' % _bv_error_text(exc))

        return jsonify({
            'success': True,
            'availability_domains': availability_domains,
            'instances': instances,
            'boot_volumes': volumes,
            'backups': backups,
            'total_storage_gb': total_storage_gb,
            'storage_limit_gb': 200,
            'job': boot_volume_job_status(),
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/boot-volumes/action', methods=['POST'])
@require_auth
@limit_oci_requests
def boot_volume_action():
    """Validate and queue one boot volume operation as a background job."""
    data = request.json or {}
    operation = str(data.get('operation', '')).strip().lower()
    if operation not in BV_JOB_OPERATIONS:
        return jsonify({
            'success': False,
            'error': "operation must be one of: %s" % ', '.join(BV_JOB_OPERATIONS)
        })

    config = build_config(data)
    try:
        get_oci().config.validate_config(config)
    except Exception as e:
        return jsonify({'success': False, 'error': 'Invalid OCI config: %s' % e})

    params = {}
    target = None

    if operation == 'create':
        availability_domain = str(data.get('availability_domain', '')).strip()
        if not availability_domain:
            return jsonify({'success': False, 'error': 'availability_domain is required'})

        display_name = str(data.get('display_name', '') or '').strip() or None
        backup_id = str(data.get('backup_id', '') or '').strip() or None
        params.update({
            'availability_domain': availability_domain,
            'display_name': display_name,
            'backup_id': backup_id,
        })
        if not backup_id:
            raw_size = data.get('size_gb')
            if raw_size is None or raw_size == '' or isinstance(raw_size, bool):
                return jsonify({'success': False, 'error': 'size_gb is required when creating an empty volume'})
            try:
                if isinstance(raw_size, float) and not raw_size.is_integer():
                    raise ValueError('size must be a whole number')
                size_gb = int(raw_size)
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': 'size_gb must be a whole number between 50 and 32768'})
            if size_gb < 50 or size_gb > 32768:
                return jsonify({'success': False, 'error': 'size_gb must be between 50 and 32768'})
            params['size_gb'] = size_gb
        target = display_name or backup_id or availability_domain

    elif operation in ('detach', 'delete'):
        boot_volume_id = str(data.get('boot_volume_id', '')).strip()
        if not boot_volume_id:
            return jsonify({'success': False, 'error': 'boot_volume_id is required'})
        params['boot_volume_id'] = boot_volume_id
        target = boot_volume_id

    elif operation == 'attach':
        boot_volume_id = str(data.get('boot_volume_id', '')).strip()
        instance_id = str(data.get('instance_id', '')).strip()
        if not boot_volume_id or not instance_id:
            return jsonify({'success': False, 'error': 'boot_volume_id and instance_id are required'})
        params['boot_volume_id'] = boot_volume_id
        params['instance_id'] = instance_id
        target = boot_volume_id

    elif operation == 'instance-action':
        instance_id = str(data.get('instance_id', '')).strip()
        power_action = str(data.get('power_action', '')).strip().upper()
        if not instance_id:
            return jsonify({'success': False, 'error': 'instance_id is required'})
        if power_action not in BV_POWER_ACTIONS:
            return jsonify({
                'success': False,
                'error': "power_action must be one of: %s" % ', '.join(BV_POWER_ACTIONS)
            })
        params['instance_id'] = instance_id
        params['power_action'] = power_action
        target = instance_id

    elif operation == 'replace':
        instance_id = str(data.get('instance_id', '')).strip()
        source_type = str(data.get('source_type', '')).strip().lower()
        source_id = str(data.get('source_id', '')).strip()
        if not instance_id:
            return jsonify({'success': False, 'error': 'instance_id is required'})
        if source_type not in ('boot_volume', 'backup'):
            return jsonify({'success': False, 'error': "source_type must be 'boot_volume' or 'backup'"})
        if not source_id:
            return jsonify({'success': False, 'error': 'source_id (replacement volume or backup) is required'})
        power_action = str(data.get('power_action', 'SOFTSTOP')).strip().upper() or 'SOFTSTOP'
        if power_action not in ('SOFTSTOP', 'STOP'):
            return jsonify({'success': False, 'error': "power_action for replace must be SOFTSTOP or STOP"})
        params['instance_id'] = instance_id
        params['source_type'] = source_type
        params['source_id'] = source_id
        params['delete_old'] = bool(data.get('delete_old', False))
        params['power_action'] = power_action
        target = instance_id

    busy_error = _bv_job_begin(operation, target)
    if busy_error:
        return jsonify({'success': False, 'error': busy_error})

    try:
        thread = threading.Thread(
            target=run_boot_volume_job,
            args=(
                operation, config, params,
                data.get('telegram_bot_token'), data.get('telegram_chat_id'),
            ),
            daemon=True,
        )
        thread.start()
    except Exception as e:
        _bv_job_finish()
        return jsonify({'success': False, 'error': str(e)})

    return jsonify({
        'success': True,
        'message': "Boot volume job '%s' started — progress appears in Live output." % operation,
        'job': boot_volume_job_status(),
    })


@app.route('/api/boot-volumes/status', methods=['GET'])
@require_auth
def boot_volume_status():
    return jsonify({'success': True, 'job': boot_volume_job_status()})


@app.route('/api/boot-volumes/stop', methods=['POST'])
@require_auth
def boot_volume_stop():
    bv_stop_event.set()
    _bv_log('Stop requested by user — the current step finishes, then the job aborts.')
    return jsonify({'success': True, 'message': 'Stop signal sent to the boot volume job.'})


@app.route('/healthz')
def healthz():
    """Cheap unauthenticated health check for Railway, Docker and systemd."""
    return jsonify({'status': 'ok', 'keepalive': keepalive_public_status()})


@app.route('/')
def home():
    try:
        return render_template('index.html')
    except Exception as e:
        return f"Flask Template Error: {str(e)}", 500


@app.route('/api/list-images', methods=['POST'])
@require_auth
@limit_oci_requests
def list_available_images():
    data = request.json or {}
    config = build_config(data)
    shape = data.get('shape')
    all_os_mode = data.get('all_os_mode', False)

    try:
        get_oci().config.validate_config(config)
        compute = create_oci_client(oci.core.ComputeClient, config)

        # Ask OCI for only the data the UI needs. The normal mode is Ubuntu-only
        # and this keeps the response small in tenancies with many custom images.
        kwargs = {'compartment_id': config['tenancy'], 'limit': 50}
        if shape:
            kwargs['shape'] = shape
        if not all_os_mode:
            kwargs['operating_system'] = 'Canonical Ubuntu'

        images = compute.list_images(**kwargs).data

        min_dt = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc).astimezone(PHNOM_PENH_TZ)
        images = sorted(
            images,
            key=lambda i: i.time_created.astimezone(PHNOM_PENH_TZ) if i.time_created else min_dt,
            reverse=True
        )

        valid = []
        for img in images:
            if getattr(img, 'lifecycle_state', '') != 'AVAILABLE':
                continue

            os_name = (getattr(img, 'operating_system', '') or '').lower()
            version = (getattr(img, 'operating_system_version', '') or '').strip()
            display_name = (img.display_name or '').lower()

            if not all_os_mode:
                if 'ubuntu' not in os_name:
                    continue
                major = 0
                if version:
                    try:
                        major = int(str(version).split('.')[0])
                    except (ValueError, IndexError):
                        major = 0
                else:
                    m = re.search(r'ubuntu[-_\s]?(\d+)', display_name)
                    if m:
                        major = int(m.group(1))
                if major < 18:
                    continue

            valid.append({
                'id': img.id,
                'name': img.display_name or f"{getattr(img, 'operating_system', 'Unknown')} {version}",
                'version': version,
                'os': getattr(img, 'operating_system', 'Unknown'),
                'os_version': version
            })

        return jsonify({'success': True, 'images': valid[:50]})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/list-subnets', methods=['POST'])
@require_auth
@limit_oci_requests
def list_available_subnets():
    data = request.json or {}
    config = build_config(data)

    try:
        get_oci().config.validate_config(config)
        network_client = create_oci_client(oci.core.VirtualNetworkClient, config)

        tenancy = config['tenancy']
        vcns = network_client.list_vcns(compartment_id=tenancy).data
        if not vcns:
            return jsonify({'success': False, 'error': 'No VCNs found in this tenancy'})

        all_subnets = []
        for vcn in vcns:
            subnets = network_client.list_subnets(
                compartment_id=tenancy,
                vcn_id=vcn.id
            ).data
            for sn in subnets:
                if getattr(sn, 'lifecycle_state', '') != 'AVAILABLE':
                    continue
                all_subnets.append({
                    'id': sn.id,
                    'name': sn.display_name or 'Unnamed',
                    'cidr': sn.cidr_block or 'N/A',
                    'vcn_name': vcn.display_name or 'Unnamed VCN',
                    'vcn_id': vcn.id,
                    'ad': sn.availability_domain or 'Regional',
                    'public': getattr(sn, 'prohibit_public_ip_on_vnic', False) == False,
                    'dns': sn.dns_label or 'N/A'
                })

        return jsonify({'success': True, 'subnets': all_subnets})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/test-launch', methods=['POST'])
@require_auth
@limit_oci_requests
def test_launch():
    """Debug endpoint: validates launch params without actually creating instance."""
    data = request.json or {}
    config = build_config(data)

    try:
        get_oci().config.validate_config(config)
        compute_client = create_oci_client(oci.core.ComputeClient, config)
        network_client = create_oci_client(oci.core.VirtualNetworkClient, config)
        identity_client = create_oci_client(oci.identity.IdentityClient, config)
        block_client = create_oci_client(oci.core.BlockstorageClient, config)

        tenancy = config['tenancy']
        ads = identity_client.list_availability_domains(compartment_id=tenancy).data
        vcns = network_client.list_vcns(compartment_id=tenancy).data
        subnets = []
        if vcns:
            subnets = network_client.list_subnets(compartment_id=tenancy, vcn_id=vcns[0].id).data

        image_id = data.get('image_id')
        shape = data.get('shape')
        subnet_id = data.get('subnet_id')

        # Validate image exists
        image_valid = False
        image_details = None
        if image_id:
            try:
                img = compute_client.get_image(image_id=image_id).data
                image_valid = getattr(img, 'lifecycle_state', '') == 'AVAILABLE'
                image_details = {
                    'display_name': img.display_name,
                    'os': getattr(img, 'operating_system', 'N/A'),
                    'os_version': getattr(img, 'operating_system_version', 'N/A'),
                    'size_in_mbs': getattr(img, 'size_in_mbs', 'N/A'),
                    'lifecycle_state': getattr(img, 'lifecycle_state', 'N/A')
                }
            except Exception as e:
                image_details = {'error': str(e)[:100]}

        # Validate subnet
        subnet_valid = False
        subnet_details = None
        if subnet_id:
            try:
                sn = network_client.get_subnet(subnet_id=subnet_id).data
                subnet_valid = getattr(sn, 'lifecycle_state', '') == 'AVAILABLE'
                subnet_details = {
                    'display_name': sn.display_name,
                    'cidr_block': getattr(sn, 'cidr_block', 'N/A'),
                    'availability_domain': getattr(sn, 'availability_domain', 'Regional'),
                    'prohibit_public_ip': getattr(sn, 'prohibit_public_ip_on_vnic', False),
                    'lifecycle_state': getattr(sn, 'lifecycle_state', 'N/A')
                }
            except Exception as e:
                subnet_details = {'error': str(e)[:100]}

        # Check shape compatibility with image
        shape_compat = []
        if image_id:
            try:
                shapes = compute_client.list_image_shape_compatibility_entries(image_id=image_id).data
                shape_compat = [s.shape for s in shapes]
            except Exception as e:
                shape_compat = ['Error: ' + str(e)[:80]]

        # Free tier check
        ok, err = check_free_tier_limits(config, data, compute_client, block_client, identity_client)

        return jsonify({
            'success': True,
            'debug': {
                'region': config.get('region'),
                'ad': ads[0].name if ads else 'N/A',
                'ads_available': [ad.name for ad in ads],
                'vcns_found': len(vcns),
                'subnets_found': len(subnets),
                'image_valid': image_valid,
                'image_details': image_details,
                'subnet_valid': subnet_valid,
                'subnet_details': subnet_details,
                'shape': shape,
                'shape_compatible_with_image': shape_compat,
                'free_tier_ok': ok,
                'free_tier_error': err
            }
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/open-firewall', methods=['POST'])
@require_auth
@limit_oci_requests
def open_firewall():
    data = request.json or {}
    config = build_config(data)
    subnet_id = data.get('subnet_id')
    ports = data.get('ports', 'all')
    cidr = data.get('cidr', '0.0.0.0/0')
    direction = data.get('direction', 'ingress')

    if not subnet_id:
        return jsonify({'success': False, 'error': 'subnet_id required'})

    try:
        get_oci().config.validate_config(config)
        network_client = create_oci_client(oci.core.VirtualNetworkClient, config)

        subnet = network_client.get_subnet(subnet_id=subnet_id).data

        port_list = []
        if ports == 'all' or ports == '*':
            port_list = ['all']
        else:
            port_list = [p.strip() for p in str(ports).split(',') if p.strip()]

        directions_to_add = []
        if direction in ('ingress', 'both'):
            directions_to_add.append('INGRESS')
        if direction in ('egress', 'both'):
            directions_to_add.append('EGRESS')

        nsg_ids = getattr(subnet, 'network_security_group_ids', [])
        if nsg_ids and len(nsg_ids) > 0:
            rules = []
            for dir in directions_to_add:
                for port in port_list:
                    if port == 'all':
                        rules.append(oci.core.models.AddSecurityRuleDetails(
                            direction=dir, protocol='all',
                            source=cidr if dir == 'INGRESS' else None,
                            destination=cidr if dir == 'EGRESS' else None,
                            description='OCI Provisioner: ' + dir.lower() + ' all traffic'
                        ))
                    else:
                        rules.append(oci.core.models.AddSecurityRuleDetails(
                            direction=dir, protocol='6',
                            source=cidr if dir == 'INGRESS' else None,
                            destination=cidr if dir == 'EGRESS' else None,
                            tcp_options=oci.core.models.TcpOptions(
                                destination_port_range=oci.core.models.PortRange(min=int(port), max=int(port))
                            ),
                            description='OCI Provisioner: ' + dir.lower() + ' port ' + port
                        ))

            result = network_client.add_network_security_group_security_rules(
                network_security_group_id=nsg_ids[0],
                add_network_security_group_security_rules_details=oci.core.models.AddNetworkSecurityGroupSecurityRulesDetails(
                    security_rules=rules
                )
            )
            return jsonify({
                'success': True,
                'method': 'NSG',
                'nsg_id': nsg_ids[0],
                'rules_added': len(result.data.security_rules),
                'ports': ports,
                'cidr': cidr,
                'direction': direction
            })

        sec_list_ids = getattr(subnet, 'security_list_ids', [])
        if not sec_list_ids:
            return jsonify({'success': False, 'error': 'No security list or NSG found on subnet'})

        sec_list = network_client.get_security_list(security_list_id=sec_list_ids[0]).data

        new_ingress = list(getattr(sec_list, 'ingress_security_rules', []))
        new_egress = list(getattr(sec_list, 'egress_security_rules', []))
        added = []

        for dir in directions_to_add:
            existing = new_ingress if dir == 'INGRESS' else new_egress
            for port in port_list:
                if port == 'all':
                    already = any(getattr(r, 'source' if dir == 'INGRESS' else 'destination', '') == cidr and getattr(r, 'protocol', '') == 'all' for r in existing)
                    if not already:
                        rule = oci.core.models.IngressSecurityRule(
                            source=cidr, protocol='all', is_stateless=False,
                            description='OCI Provisioner: ' + dir.lower() + ' all traffic'
                        ) if dir == 'INGRESS' else oci.core.models.EgressSecurityRule(
                            destination=cidr, protocol='all', is_stateless=False,
                            description='OCI Provisioner: ' + dir.lower() + ' all traffic'
                        )
                        existing.append(rule)
                        added.append(dir.lower() + ':all')
                else:
                    already = any(
                        getattr(r, 'source' if dir == 'INGRESS' else 'destination', '') == cidr and 
                        getattr(r, 'protocol', '') == '6' and
                        getattr(getattr(r, 'tcp_options', None), 'destination_port_range', None) and
                        getattr(getattr(r, 'tcp_options', None), 'destination_port_range').min == int(port)
                        for r in existing
                    )
                    if not already:
                        rule = oci.core.models.IngressSecurityRule(
                            source=cidr, protocol='6', is_stateless=False,
                            tcp_options=oci.core.models.TcpOptions(
                                destination_port_range=oci.core.models.PortRange(min=int(port), max=int(port))
                            ),
                            description='OCI Provisioner: ' + dir.lower() + ' port ' + port
                        ) if dir == 'INGRESS' else oci.core.models.EgressSecurityRule(
                            destination=cidr, protocol='6', is_stateless=False,
                            tcp_options=oci.core.models.TcpOptions(
                                destination_port_range=oci.core.models.PortRange(min=int(port), max=int(port))
                            ),
                            description='OCI Provisioner: ' + dir.lower() + ' port ' + port
                        )
                        existing.append(rule)
                        added.append(dir.lower() + ':' + port)

        if not added:
            return jsonify({'success': True, 'already_open': True, 'message': 'Rule(s) already exist', 'ports': ports, 'cidr': cidr, 'direction': direction})

        network_client.update_security_list(
            security_list_id=sec_list_ids[0],
            update_security_list_details=oci.core.models.UpdateSecurityListDetails(
                ingress_security_rules=new_ingress,
                egress_security_rules=new_egress
            )
        )

        return jsonify({
            'success': True,
            'method': 'SecurityList',
            'sec_list_id': sec_list_ids[0],
            'rules_added': len(added),
            'ports_added': added,
            'ports': ports,
            'cidr': cidr,
            'direction': direction
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/scan-security-rules', methods=['POST'])
@require_auth
@limit_oci_requests
def scan_security_rules():
    """Scan existing security rules on a subnet."""
    data = request.json or {}
    config = build_config(data)
    subnet_id = data.get('subnet_id')

    if not subnet_id:
        return jsonify({'success': False, 'error': 'subnet_id required'})

    try:
        get_oci().config.validate_config(config)
        network_client = create_oci_client(oci.core.VirtualNetworkClient, config)

        subnet = network_client.get_subnet(subnet_id=subnet_id).data

        rules = []

        # Check NSG rules
        nsg_ids = getattr(subnet, 'network_security_group_ids', [])
        for nsg_id in nsg_ids:
            nsg = network_client.get_network_security_group(network_security_group_id=nsg_id).data
            nsg_rules = network_client.list_network_security_group_security_rules(network_security_group_id=nsg_id).data
            for r in nsg_rules:
                rules.append({
                    'type': 'NSG',
                    'direction': r.direction,
                    'protocol': r.protocol,
                    'source': getattr(r, 'source', 'N/A'),
                    'destination': getattr(r, 'destination', 'N/A'),
                    'description': getattr(r, 'description', '')
                })

        # Check Security List rules
        sec_list_ids = getattr(subnet, 'security_list_ids', [])
        for sec_id in sec_list_ids:
            sec_list = network_client.get_security_list(security_list_id=sec_id).data

            # Ingress rules
            for r in getattr(sec_list, 'ingress_security_rules', []):
                tcp_opts = getattr(r, 'tcp_options', None)
                port_range = None
                if tcp_opts and getattr(tcp_opts, 'destination_port_range', None):
                    port_range = str(tcp_opts.destination_port_range.min)
                    if tcp_opts.destination_port_range.max != tcp_opts.destination_port_range.min:
                        port_range += '-' + str(tcp_opts.destination_port_range.max)

                rules.append({
                    'type': 'SecurityList',
                    'direction': 'INGRESS',
                    'protocol': getattr(r, 'protocol', 'N/A'),
                    'source': getattr(r, 'source', 'N/A'),
                    'destination': 'N/A',
                    'port_range': port_range,
                    'description': getattr(r, 'description', '')
                })

            # Egress rules
            for r in getattr(sec_list, 'egress_security_rules', []):
                tcp_opts = getattr(r, 'tcp_options', None)
                port_range = None
                if tcp_opts and getattr(tcp_opts, 'destination_port_range', None):
                    port_range = str(tcp_opts.destination_port_range.min)
                    if tcp_opts.destination_port_range.max != tcp_opts.destination_port_range.min:
                        port_range += '-' + str(tcp_opts.destination_port_range.max)

                rules.append({
                    'type': 'SecurityList',
                    'direction': 'EGRESS',
                    'protocol': getattr(r, 'protocol', 'N/A'),
                    'source': 'N/A',
                    'destination': getattr(r, 'destination', 'N/A'),
                    'port_range': port_range,
                    'description': getattr(r, 'description', '')
                })

        return jsonify({'success': True, 'rules': rules, 'nsg_count': len(nsg_ids), 'sec_list_count': len(sec_list_ids)})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


def check_free_tier_limits(config, account_config, compute_client, block_client, identity_client):
    tenancy = config['tenancy']
    requested_shape = account_config.get('shape')
    try:
        requested_boot_gb = int(account_config.get('boot_volume_gb', 50))
    except (TypeError, ValueError):
        requested_boot_gb = 50
    if requested_boot_gb < 50:
        requested_boot_gb = 50

    ads = identity_client.list_availability_domains(compartment_id=tenancy).data
    total_storage = 0
    for ad in ads:
        boot_volumes = block_client.list_boot_volumes(
            compartment_id=tenancy,
            availability_domain=ad.name
        ).data
        total_storage += sum(
            int(v.size_in_gbs) for v in boot_volumes
            if v.lifecycle_state != 'TERMINATED'
        )

    if total_storage + requested_boot_gb > 200:
        return False, (
            f"Storage would exceed 200 GB free tier limit "
            f"(used {total_storage} GB + requested {requested_boot_gb} GB)"
        )

    instances = compute_client.list_instances(compartment_id=tenancy).data

    if requested_shape == 'VM.Standard.E2.1.Micro':
        micro_count = sum(
            1 for inst in instances
            if inst.shape == 'VM.Standard.E2.1.Micro'
            and inst.lifecycle_state != 'TERMINATED'
        )
        if micro_count >= 2:
            return False, f"Free tier allows only 2 Micro instances (found {micro_count})"
        return True, ""

    if requested_shape == 'VM.Standard.A1.Flex':
        # These defaults match the Always Free allocation and the UI. The old
        # 4 OCPU / 24 GB defaults rejected valid requests when fields were absent.
        try:
            requested_ocpus = int(account_config.get('ocpus', 2))
        except (TypeError, ValueError):
            requested_ocpus = 2
        try:
            requested_memory = int(account_config.get('memory', 12))
        except (TypeError, ValueError):
            requested_memory = 12

        total_ocpus = 0
        total_memory = 0
        for inst in instances:
            if inst.shape == 'VM.Standard.A1.Flex' and inst.lifecycle_state != 'TERMINATED':
                cfg = inst.shape_config
                if cfg:
                    total_ocpus += int(cfg.ocpus or 0)
                    total_memory += int(cfg.memory_in_gbs or 0)

        if total_ocpus + requested_ocpus > 2:
            return False, (
                f"A1 OCPUs would exceed 2 (used {total_ocpus} + requested {requested_ocpus})"
            )
        if total_memory + requested_memory > 12:
            return False, (
                f"A1 memory would exceed 12 GB (used {total_memory} + requested {requested_memory})"
            )
        return True, ""

    return True, ""


def get_free_tier_usage(config, compute_client, block_client, identity_client):
    tenancy = config['tenancy']
    ads = identity_client.list_availability_domains(compartment_id=tenancy).data

    total_storage = 0
    for ad in ads:
        boot_volumes = block_client.list_boot_volumes(
            compartment_id=tenancy,
            availability_domain=ad.name
        ).data
        total_storage += sum(
            int(v.size_in_gbs) for v in boot_volumes
            if v.lifecycle_state != 'TERMINATED'
        )
    storage_remaining = max(0, 200 - total_storage)

    instances = compute_client.list_instances(compartment_id=tenancy).data

    micro_count = sum(
        1 for inst in instances
        if inst.shape == 'VM.Standard.E2.1.Micro'
        and inst.lifecycle_state != 'TERMINATED'
    )
    micro_remaining = max(0, 2 - micro_count)

    total_ocpus = 0
    total_memory = 0
    arm_instances = []
    for inst in instances:
        if inst.shape == 'VM.Standard.A1.Flex' and inst.lifecycle_state != 'TERMINATED':
            cfg = inst.shape_config
            if cfg:
                ocpus = int(cfg.ocpus or 0)
                memory = int(cfg.memory_in_gbs or 0)
                total_ocpus += ocpus
                total_memory += memory
                arm_instances.append({
                    'name': inst.display_name,
                    'ocpus': ocpus,
                    'memory': memory,
                    'state': inst.lifecycle_state
                })

    ocpus_remaining = max(0, 2 - total_ocpus)
    memory_remaining = max(0, 12 - total_memory)

    return {
        'storage': {
            'used_gb': total_storage,
            'limit_gb': 200,
            'remaining_gb': storage_remaining,
            'percent': round((total_storage / 200) * 100, 1) if total_storage > 0 else 0
        },
        'micro': {
            'used': micro_count,
            'limit': 2,
            'remaining': micro_remaining,
            'percent': round((micro_count / 2) * 100, 1) if micro_count > 0 else 0
        },
        'arm': {
            'used_ocpus': total_ocpus,
            'limit_ocpus': 2,
            'remaining_ocpus': ocpus_remaining,
            'used_memory_gb': total_memory,
            'limit_memory_gb': 12,
            'remaining_memory_gb': memory_remaining,
            'instances': arm_instances,
            'ocpu_percent': round((total_ocpus / 2) * 100, 1) if total_ocpus > 0 else 0,
            'memory_percent': round((total_memory / 12) * 100, 1) if total_memory > 0 else 0
        }
    }


def send_telegram_message(bot_token, chat_id, message):
    if not bot_token or not chat_id:
        return False, "Missing bot token or chat ID"
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }
        response = requests.post(url, json=payload, timeout=10)
        data = response.json()
        if data.get("ok"):
            return True, "Message sent"
        else:
            return False, data.get("description", "Unknown Telegram error")
    except Exception as e:
        return False, str(e)


def get_oci_user_details(config, identity_client):
    """Return display name and email without retaining the OCI private key."""
    try:
        get_oci()
        user_ocid = config.get('user')
        if not user_ocid:
            add_log("Username detection skipped: no user OCID in config")
            return {'display_name': None, 'email': None}

        add_log("Fetching user info from Identity API...")
        user = identity_client.get_user(user_id=user_ocid).data

        name = getattr(user, 'name', None)
        email = getattr(user, 'email', None)
        desc = getattr(user, 'description', None)

        if name and email:
            result = f"{name} ({email})"
        elif name:
            result = name
        elif email:
            result = email
        elif desc and desc != user_ocid:
            result = desc
        else:
            result = user_ocid

        add_log(f"Detected OCI user: {result}")
        return {'display_name': result, 'email': email}

    except Exception as e:
        if oci is not None and isinstance(e, oci.exceptions.ServiceError):
            add_log(f"Identity API error (status {e.status}): {e.message}")
        else:
            add_log(f"Error fetching user info: {str(e)}")
        return {'display_name': None, 'email': None}


def get_oci_username(config, identity_client):
    """Compatibility helper used by callers that only need the display name."""
    return get_oci_user_details(config, identity_client).get('display_name')


def _telegram_value(value, fallback='N/A'):
    value = fallback if value is None or value == '' else value
    return html.escape(str(value))


def send_telegram_attempt_update(bot_token, chat_id, attempt, email, region, ad, fingerprint):
    """Send safe retry context; never send the OCI private key to Telegram."""
    if not bot_token or not chat_id:
        return
    message = (
        f"&#128260; <b>OCI Provisioning Attempt {attempt}</b>\n\n"
        f"<b>OCI email:</b> {_telegram_value(email)}\n"
        f"<b>Location:</b> {_telegram_value(region)} / AD {_telegram_value(ad)}\n"
        f"<b>OCI key fingerprint:</b> {_telegram_value(fingerprint, 'not provided')}\n"
        f"<b>Status:</b> Sending launch request"
    )
    ok, error = send_telegram_message(bot_token, chat_id, message)
    if not ok:
        add_log(f"Telegram attempt update failed: {error}")


def _shape_offered_in_ad(compute_client, compartment_id, ad_name, shape):
    """Is `shape` actually offered in this availability domain?

    OCI's ``ListShapes`` for an availability domain is the authoritative list of
    shapes that can be launched there. A shape that is absent from that list
    cannot be created in the region at all — OCI rejects the launch with a
    permanent ``404 NotAuthorizedOrNotFound`` ("Authorization failed or requested
    resource not found"), which *looks* like an auth/OCID problem but is really
    "this shape is not offered here" (e.g. ``VM.Standard.E2.1.Micro`` is not
    available in every region).

    Returns True/False when the answer is known, or None when the check is
    inconclusive (the SDK call failed, or the list came back empty) so callers
    never block a launch on an unknown.
    """
    try:
        shapes = compute_client.list_shapes(
            compartment_id=compartment_id,
            availability_domain=ad_name
        ).data
    except Exception:
        return None
    names = {getattr(s, 'shape', None) for s in (shapes or [])}
    if not names:
        return None
    return shape in names


def preflight_launch_check(config, account_config, compute_client, network_client,
                           identity_client, ad_list):
    """Validate launch inputs against the configured region before the loop runs.

    The single most confusing OCI failure is a launch that fails with
    ``404 NotAuthorizedOrNotFound``. OCI deliberately makes that one error mean
    both "you lack permission" and "the resource does not exist / is not offered
    here", so the loop's generic guesswork can never pinpoint the cause. This
    check resolves each referenced resource against the configured region up
    front so the user is told exactly what is wrong instead of watching attempts
    silently 404.

    Returns ``(problems, shape_available)``:

    * ``problems`` — fatal blockers for *this* region (a bad image or subnet
      OCID, e.g. one copied from another region). Retrying or waiting cannot fix
      these, so the caller should abort.
    * ``shape_available`` — ``True`` if the shape is offered in at least one AD,
      ``False`` if it is definitively not offered anywhere, or ``None`` if the
      check was inconclusive. ``False`` is **not** fatal: regions gain shapes
      over time, so the caller should wait for Oracle Cloud to offer it rather
      than exit.
    """
    tenancy = config['tenancy']
    region = config.get('region', 'unknown')
    shape = account_config.get('shape')
    image_id = account_config.get('image_id')
    subnet_id = account_config.get('subnet_id')
    problems = []

    # 1. The image OCID must exist and be AVAILABLE in *this* region. Image
    #    OCIDs are region-specific; a valid OCID from another region 404s here.
    if image_id:
        try:
            img = compute_client.get_image(image_id=image_id).data
            state = getattr(img, 'lifecycle_state', '')
            if state != 'AVAILABLE':
                problems.append(
                    f"Image {image_id} is '{state or 'unknown'}', not AVAILABLE, "
                    f"in region '{region}'."
                )
        except Exception as e:
            problems.append(
                f"Image {image_id} could not be read in region '{region}' "
                f"({_bv_error_text(e)}). Image OCIDs are region-specific — this one is "
                f"most likely from a different region. Re-pick it from the image "
                f"list for '{region}'."
            )

    # 2. The subnet must exist and be AVAILABLE, also in this region.
    if subnet_id:
        try:
            sn = network_client.get_subnet(subnet_id=subnet_id).data
            state = getattr(sn, 'lifecycle_state', '')
            if state != 'AVAILABLE':
                problems.append(
                    f"Subnet {subnet_id} is '{state or 'unknown'}', not AVAILABLE, "
                    f"in region '{region}'."
                )
        except Exception as e:
            problems.append(
                f"Subnet {subnet_id} could not be read in region '{region}' "
                f"({_bv_error_text(e)}). Subnet OCIDs are region-specific — re-pick "
                f"it from the subnet list for '{region}'."
            )

    # 3. Is the shape actually offered in this region yet? Not fatal — reported
    #    back to the caller so it can wait for Oracle Cloud to add the shape.
    shape_available = None
    if shape and ad_list:
        checked_any = False
        offered_any = False
        for ad in ad_list:
            offered = _shape_offered_in_ad(compute_client, tenancy, ad, shape)
            if offered is None:
                continue
            checked_any = True
            if offered:
                offered_any = True
                break
        if checked_any:
            shape_available = offered_any

    return problems, shape_available


@hold_oci_slot
def run_automated_creation(config, account_config, compute_client, network_client, identity_client,
                           retry_delay=60, randomize_delay=False, random_min=25, random_max=60,
                           telegram_bot_token=None, telegram_chat_id=None,
                           max_attempts=MAX_ATTEMPTS):
    global automation_running

    oci_username = None
    oci_email = None
    target_region = config.get('region', 'unknown')
    target_name = account_config.get('display_name', 'AlwaysFree-Bot')

    try:
        get_oci()
        oci_user = get_oci_user_details(config, identity_client)
        oci_username = oci_user.get('display_name')
        oci_email = oci_user.get('email')
        if oci_username:
            add_log(f"OCI username detected: {oci_username}")
    except Exception as e:
        add_log(f"Could not detect OCI username: {str(e)}")

    try:
        block_client = create_oci_client(oci.core.BlockstorageClient, config)
        ok, err = check_free_tier_limits(
            config, account_config, compute_client, block_client, identity_client
        )
        if not ok:
            add_log(f"Free tier limit check failed: {err}")
            return

        add_log(f"Initializing infrastructure scan inside: {target_region}...")

        ads = identity_client.list_availability_domains(
            compartment_id=config['tenancy']
        ).data
        ad_list = [ad.name for ad in ads] if ads else []
        add_log(f"Availability domains found: {len(ad_list)} — {', '.join(ad_list)}")
        if not ad_list:
            add_log("Error: No availability domains found for this tenancy.")
            return

        # Handle AD preference from user
        ad_preference = account_config.get('ad_preference', '')
        if ad_preference and ad_preference in ad_list:
            # Move preferred AD to front of list
            ad_list.remove(ad_preference)
            ad_list.insert(0, ad_preference)
            add_log(f"Using preferred AD: {ad_preference}")
        elif ad_preference:
            add_log(f"Preferred AD '{ad_preference}' not found, using auto-rotation")

        subnet_id = account_config.get('subnet_id')
        if not subnet_id:
            vcns = network_client.list_vcns(compartment_id=config['tenancy']).data
            if not vcns:
                add_log("Error: No VCN found.")
                return
            subnets = network_client.list_subnets(
                compartment_id=config['tenancy'],
                vcn_id=vcns[0].id
            ).data
            if not subnets:
                add_log("Error: No subnet found.")
                return
            subnet_id = subnets[0].id
            add_log("Auto-selected subnet: " + subnet_id[:20] + "...")
        else:
            add_log("Using selected subnet: " + subnet_id[:20] + "...")

        image_id = account_config.get('image_id')
        if not image_id:
            add_log("Error: No OS image selected.")
            return

        ssh_key = account_config.get('ssh_key', '').strip()
        if not ssh_key:
            add_log("Error: SSH public key is required.")
            return

        valid_prefixes = ('ssh-rsa', 'ssh-ed25519', 'ssh-dss', 'ecdsa-sha2-nistp256',
                          'ecdsa-sha2-nistp384', 'ecdsa-sha2-nistp521', 'sk-ssh-ed25519')
        if not any(ssh_key.startswith(p) for p in valid_prefixes):
            add_log("Error: SSH key does not appear to be a valid public key.")
            return

        boot_gb = int(account_config.get('boot_volume_gb', 50))
        if boot_gb < 50:
            add_log("Boot volume raised to minimum 50 GB.")
            boot_gb = 50

        add_log(f"Setup Verified -> Subnet: {subnet_id[:20]}... | "
                f"Image: {image_id[:20]}... | Zone: {ad_list[0] if ad_list else 'N/A'}")
        add_log(f"Debug -> Shape: {account_config['shape']} | Boot: {boot_gb}GB | "
                f"OCPUs: {account_config.get('ocpus', 'N/A')} | RAM: {account_config.get('memory', 'N/A')}GB")
        add_log(f"Debug -> Subnet details: assign_public_ip=True")

        is_arm = account_config.get('shape') == "VM.Standard.A1.Flex"
        shape_config = None
        if is_arm:
            ocpus = int(account_config.get('ocpus', 2))
            memory = int(account_config.get('memory', 12))
            shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=ocpus, memory_in_gbs=memory
            )
            add_log(f"Debug -> ARM shape config: ocpus={ocpus}, memory={memory}")

        instance_details = oci.core.models.LaunchInstanceDetails(
            compartment_id=config['tenancy'],
            availability_domain=ad_list[0] if ad_list else '',
            shape=account_config['shape'],
            shape_config=shape_config,
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=image_id,
                boot_volume_size_in_gbs=boot_gb
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id,
                assign_public_ip=True
            ),
            metadata={"ssh_authorized_keys": ssh_key},
            display_name=target_name
        )

        add_log(f"Launching provisioning loop for '{target_name}'...")

        attempts = 0
        success = False
        ad_index = 0
        max_attempts = max(1, min(int(max_attempts), 100000))
        add_log(f"Retry limit: {max_attempts} attempts")

        # Shuffle AD list for random order (speeds up finding capacity)
        import random as _random
        if len(ad_list) > 1:
            _random.shuffle(ad_list)
            add_log(f"AD order randomized for faster discovery: {', '.join(ad_list)}")

        # Pre-flight: resolve every referenced resource against this region
        # before spending attempts. A 404 NotAuthorizedOrNotFound is ambiguous
        # by design (permission *or* missing/not-offered resource), so we pin
        # the cause down here.
        preflight_problems, shape_available = preflight_launch_check(
            config, account_config, compute_client, network_client,
            identity_client, ad_list
        )
        if preflight_problems:
            # Only truly fatal config errors land here (bad image/subnet OCID
            # for this region). Retrying or waiting cannot clear these.
            add_log("Pre-flight check failed — the launch cannot succeed as configured:")
            for problem in preflight_problems:
                add_log(f"  - {problem}")
            add_log(
                "Fix the item(s) above (region, image, subnet) and start again. "
                "Retrying will not clear a wrong OCID."
            )
            return

        # A shape Oracle has not offered in this region yet is NOT fatal — OCI
        # regions gain shapes over time. Do not exit: wait and re-check, and
        # launch automatically the moment the shape appears. `shape_confirmed`
        # gates launch_instance so we don't hammer a guaranteed 404 meanwhile.
        shape_confirmed = shape_available is not False
        if shape_available is False:
            add_log(
                f"No shape availability: '{account_config['shape']}' is not offered in "
                f"region '{target_region}' yet (ADs: {', '.join(ad_list)})."
            )
            add_log(
                f"Waiting for Oracle Cloud to add '{account_config['shape']}' to this region — "
                f"re-checking every attempt and launching as soon as it appears. "
                f"Stop the loop anytime to cancel."
            )

        # Never leave a daemon thread retrying forever. This is especially
        # important on Railway/VPS instances with limited CPU and memory.
        while attempts < max_attempts:
            attempts += 1

            if stop_event.is_set():
                add_log("Provisioning loop stopped by user.")
                break

            # Rotate through availability domains (randomized order)
            current_ad = ad_list[ad_index % len(ad_list)] if ad_list else ''

            actual_delay = retry_delay
            if randomize_delay:
                actual_delay = random.randint(random_min, random_max)
                add_log(f"Dynamic retry: waiting {actual_delay}s (randomized {random_min}-{random_max}s)")

            # Shape-availability gate. Until Oracle offers the shape in this
            # region, every launch is a permanent 404, so wait instead of
            # hammering — and resume the instant the shape shows up.
            if not shape_confirmed:
                offered = _shape_offered_in_ad(
                    compute_client, config['tenancy'], current_ad, account_config['shape']
                )
                if offered is True:
                    shape_confirmed = True
                    add_log(
                        f"Shape '{account_config['shape']}' is now offered in '{current_ad}' — "
                        f"resuming launch attempts."
                    )
                elif offered is False:
                    if len(ad_list) > 1:
                        add_log(f"Attempt {attempts}: no shape availability — trying AD '{current_ad}'...")
                    else:
                        add_log(
                            f"Attempt {attempts}: no shape availability — "
                            f"'{account_config['shape']}' still not offered in '{current_ad}'. "
                            f"Waiting for Oracle Cloud to add it..."
                        )
                    if len(ad_list) > 1:
                        ad_index += 1
                    if stop_event.wait(actual_delay):
                        add_log("Provisioning loop stopped while waiting.")
                        break
                    continue
                # offered is None (inconclusive): fall through and try to launch.

            if len(ad_list) > 1:
                add_log(f"Attempt {attempts}: trying AD '{current_ad}'...")

            # Send one compact, structured Telegram update per launch attempt.
            # The private key itself is never sent; only its OCI fingerprint is.
            send_telegram_attempt_update(
                telegram_bot_token,
                telegram_chat_id,
                attempts,
                oci_email,
                target_region,
                current_ad,
                config.get('fingerprint')
            )

            # Update instance details with current AD
            instance_details.availability_domain = current_ad

            try:
                add_log(f"Attempt {attempts}: sending instance launch request...")
                compute_client.launch_instance(instance_details)
                add_log("SUCCESS! Instance created and running.")
                success = True
                if telegram_bot_token and telegram_chat_id:
                    instance_name = account_config.get('display_name', 'AlwaysFree-Bot')
                    shape = account_config.get('shape', 'Unknown')
                    region = config.get('region', 'unknown')
                    pp_time = format_phnom_penh_time()
                    tg_msg = (
                        f"&#9989; <b>OCI Provisioner Success!</b>\n\n"
                        f"<b>Attempt:</b> {attempts}\n"
                        f"<b>OCI email:</b> {_telegram_value(oci_email)}\n"
                        f"<b>Instance:</b> {_telegram_value(instance_name)}\n"
                        f"<b>Shape:</b> {_telegram_value(shape)}\n"
                        f"<b>Location:</b> {_telegram_value(region)} / AD {_telegram_value(current_ad)}\n"
                        f"<b>OCI key fingerprint:</b> {_telegram_value(config.get('fingerprint'), 'not provided')}\n"
                        f"<b>Time:</b> {_telegram_value(pp_time)} (Phnom Penh)\n"
                        f"<b>Status:</b> Running\n\n"
                        f"Your Always Free instance has been successfully provisioned!"
                    )
                    tg_ok, tg_err = send_telegram_message(telegram_bot_token, telegram_chat_id, tg_msg)
                    if tg_ok:
                        add_log("Telegram success alert sent.")
                    else:
                        add_log(f"Telegram alert failed: {tg_err}")
                break

            except oci.exceptions.ServiceError as e:
                msg = str(e)
                code = getattr(e, 'code', 'N/A')
                status = getattr(e, 'status', 'N/A')
                add_log(f"Debug -> ServiceError code={code}, status={status}, msg={e.message[:120]}")
                if "Out of capacity" in msg or status in (500, 429, 503, 504):
                    user_info = f" [user: {oci_username}]" if oci_username else ""
                    add_log(f"Capacity busy in '{target_region}' AD '{current_ad}'.{user_info} Retrying...")
                    if len(ad_list) > 1:
                        ad_index += 1
                        next_ad = ad_list[ad_index % len(ad_list)]
                        add_log(f"Switching to next AD: '{next_ad}'")
                elif "NotAuthorizedOrNotFound" in msg or "Authorization failed" in msg or status == 404:
                    # OCI makes one 404 mean both "no permission" and "resource
                    # missing / not offered here". The image and subnet were
                    # already validated for this region and the shape gate only
                    # lets us launch once the shape is offered, so a 404 *here*
                    # is almost always no free quota for the shape in THIS AD
                    # (OCI reports that as a 404, not 429/500) or the shape has
                    # not fully rolled out to this AD yet. Both clear up over
                    # time, so wait and retry rather than exiting.
                    add_log(
                        f"404 NotAuthorizedOrNotFound in region '{target_region}' AD '{current_ad}'."
                    )
                    add_log(
                        f"  Image, subnet and shape were validated for '{target_region}', so this "
                        f"is most likely **no free quota for '{account_config['shape']}' in this AD** "
                        f"(OCI reports that as a 404) — or Oracle has not fully rolled the shape out "
                        f"to this AD yet. Waiting rather than exiting."
                    )
                    add_log(
                        f"  If it never clears: the API key's user may lack 'manage instance-family' "
                        f"/ 'manage volume-family', or the region may have no free quota at all."
                    )
                    if len(ad_list) > 1:
                        ad_index += 1
                        next_ad = ad_list[ad_index % len(ad_list)]
                        add_log(
                            f"Switching to AD '{next_ad}' — Always-Free quota is distributed per AD, "
                            f"so another AD may have capacity."
                        )
                    else:
                        add_log(
                            f"Only one availability domain in '{target_region}'. Waiting for quota to "
                            f"free up or for Oracle to roll out '{account_config['shape']}' here. Stop "
                            f"anytime; raise MAX_ATTEMPTS to keep hunting longer."
                        )
                    # Fall through to the wait below and retry — never exit here.
                else:
                    add_log(f"OCI API error: {e.message}")
                    if len(ad_list) > 1:
                        ad_index += 1
                        add_log(f"Trying next AD...")
                        continue
                    break
            except (ConnectionError, OSError) as e:
                user_info = f" [user: {oci_username}]" if oci_username else ""
                add_log(f"Connection issue in '{target_region}': {type(e).__name__}.{user_info} Retrying...")
            except Exception as e:
                msg = str(e)
                if "Remote end closed connection" in msg or "Connection aborted" in msg or "timeout" in msg.lower():
                    user_info = f" [user: {oci_username}]" if oci_username else ""
                    add_log(f"Network hiccup in '{target_region}': connection dropped.{user_info} Retrying...")
                else:
                    add_log(f"Automation engine failure: {msg}")
                    break

            if stop_event.wait(actual_delay):
                add_log("Provisioning loop stopped while waiting.")
                break

        if not success:
            if attempts >= max_attempts and not stop_event.is_set():
                add_log(f"Retry limit reached ({max_attempts} attempts).")
            add_log("Provisioning loop ended without success.")
            if telegram_bot_token and telegram_chat_id:
                pp_time = format_phnom_penh_time()
                tg_msg = (
                    f"&#10060; <b>OCI Provisioner Stopped</b>\n\n"
                    f"<b>Attempts:</b> {attempts}\n"
                    f"<b>OCI email:</b> {_telegram_value(oci_email)}\n"
                    f"<b>Location:</b> {_telegram_value(target_region)}\n"
                    f"<b>OCI key fingerprint:</b> {_telegram_value(config.get('fingerprint'), 'not provided')}\n"
                    f"Loop stopped without success.\n"
                    f"<b>Time:</b> {_telegram_value(pp_time)} (Phnom Penh)"
                )
                send_telegram_message(telegram_bot_token, telegram_chat_id, tg_msg)

    except Exception as e:
        msg = str(e)
        if "Remote end closed connection" in msg or "Connection aborted" in msg:
            add_log(f"Network connection lost. Loop ended.")
        else:
            add_log(f"Automation engine failure: {msg}")
        if telegram_bot_token and telegram_chat_id:
            pp_time = format_phnom_penh_time()
            tg_msg = (
                f"&#10060; <b>OCI Provisioner Error</b>\n\n"
                f"<b>Attempt:</b> {attempts if 'attempts' in locals() else 0}\n"
                f"<b>OCI email:</b> {_telegram_value(oci_email)}\n"
                f"<b>Location:</b> {_telegram_value(target_region)}\n"
                f"<b>OCI key fingerprint:</b> {_telegram_value(config.get('fingerprint'), 'not provided')}\n"
                f"Automation engine failure:\n{_telegram_value(msg[:200])}\n"
                f"<b>Time:</b> {_telegram_value(pp_time)} (Phnom Penh)"
            )
            send_telegram_message(telegram_bot_token, telegram_chat_id, tg_msg)

    finally:
        with automation_lock:
            automation_running = False
            automation_shape = None


@app.route('/api/free-tier-status', methods=['POST'])
@require_auth
@limit_oci_requests
def free_tier_status():
    data = request.json or {}
    config = build_config(data)

    try:
        get_oci().config.validate_config(config)
        usage = get_cached_usage(usage_cache_key(config))
        if usage is None:
            compute_client = create_oci_client(oci.core.ComputeClient, config)
            block_client = create_oci_client(oci.core.BlockstorageClient, config)
            identity_client = create_oci_client(oci.identity.IdentityClient, config)
            usage = get_free_tier_usage(config, compute_client, block_client, identity_client)
            cache_usage(usage_cache_key(config), usage)

        return jsonify({
            'success': True,
            'usage': usage
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/status', methods=['GET'])
@require_auth
def get_status():
    with automation_lock:
        return jsonify({
            'success': True,
            'running': automation_running,
            'shape': automation_shape,
            'keepalive': keepalive_status(),
            'boot_volume_job': boot_volume_job_status()
        })


@app.route('/api/keepalive', methods=['GET', 'POST'])
@require_auth
def keepalive_endpoint():
    """Inspect or change the 24/7 keep-alive pinger at runtime."""
    if request.method == 'POST':
        data = request.json or {}
        ok, error = keepalive_apply(
            enabled=data.get('enabled') if 'enabled' in data else None,
            interval=data.get('interval_seconds') if 'interval_seconds' in data else None,
            urls=data.get('urls') if 'urls' in data else None,
        )
        if not ok:
            return jsonify({'success': False, 'error': error})
    return jsonify({'success': True, 'keepalive': keepalive_status()})


@app.route('/api/auto-launch-loop', methods=['POST'])
@require_auth
@limit_oci_requests
def auto_launch():
    global automation_running, tg_live_enabled, tg_live_bot_token, tg_live_chat_id, tg_live_last_sent
    data = request.json or {}
    config = build_config(data)

    try:
        get_oci().config.validate_config(config)
    except Exception as e:
        return jsonify({'success': False, 'error': f"Invalid OCI config: {e}"})

    requested_shape = data.get('shape', '')

    # Configure Telegram live logging
    bot_token = data.get('telegram_bot_token', '').strip()
    chat_id = data.get('telegram_chat_id', '').strip()
    enable_live = data.get('telegram_live_log', False)

    with tg_live_lock:
        tg_live_enabled = bool(enable_live and bot_token and chat_id)
        tg_live_bot_token = bot_token if enable_live else None
        tg_live_chat_id = chat_id if enable_live else None
        tg_live_last_sent = 0

    if enable_live and (not bot_token or not chat_id):
        return jsonify({'success': False, 'error': 'Telegram live log enabled but bot token or chat ID is missing'})

    with automation_lock:
        if automation_running:
            if automation_shape and automation_shape != requested_shape:
                return jsonify({
                    'success': False,
                    'error': f"A provisioning loop is already running for shape '{automation_shape}'. Stop it first before starting '{requested_shape}'."
                })
            return jsonify({
                'success': False,
                'error': 'A provisioning loop is already running.'
            })
        automation_running = True
        automation_shape = requested_shape
        stop_event.clear()

    try:
        compute_client = create_oci_client(oci.core.ComputeClient, config)
        network_client = create_oci_client(oci.core.VirtualNetworkClient, config)
        identity_client = create_oci_client(oci.identity.IdentityClient, config)

        retry_delay = env_int('RETRY_DELAY_DEFAULT', 60, 10, 3600)
        try:
            retry_delay = max(10, min(int(data.get('retry_delay', retry_delay)), 3600))
        except (TypeError, ValueError):
            pass

        randomize_delay = bool(data.get('randomize_delay', False))
        try:
            random_min = max(10, min(int(data.get('random_min', 25)), 3600))
        except (TypeError, ValueError):
            random_min = 25
        try:
            random_max = max(random_min, min(int(data.get('random_max', 60)), 3600))
        except (TypeError, ValueError):
            random_max = max(random_min, 60)

        thread = threading.Thread(
            target=run_automated_creation,
            args=(config, data, compute_client, network_client, identity_client,
                  retry_delay, randomize_delay, random_min, random_max,
                  data.get('telegram_bot_token'), data.get('telegram_chat_id'),
                  MAX_ATTEMPTS),
            daemon=True
        )
        thread.start()

        return jsonify({
            'success': True,
            'message': 'Provisioning loop started.' + (' Live Telegram logging enabled.' if tg_live_enabled else '')
        })

    except Exception as e:
        with automation_lock:
            automation_running = False
            automation_shape = None
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/stop-loop', methods=['POST'])
@require_auth
def stop_loop():
    global tg_live_enabled
    stop_event.set()
    with tg_live_lock:
        tg_live_enabled = False
    return jsonify({'success': True, 'message': 'Stop signal sent.'})


@app.route('/api/logs', methods=['GET'])
@require_auth
def fetch_live_logs():
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except (TypeError, ValueError):
        offset = 0
    with logs_lock:
        # Offsets are absolute, so the browser keeps working after the bounded
        # in-memory log drops its oldest lines.
        start = max(0, offset - global_log_base)
        batch = global_logs[start:]
        total = global_log_base + len(global_logs)
    return jsonify({'logs': batch, 'next_offset': total})


@app.route('/api/test-telegram', methods=['POST'])
@require_auth
def test_telegram():
    data = request.json or {}
    bot_token = data.get('bot_token', '').strip()
    chat_id = data.get('chat_id', '').strip()
    if not bot_token or not chat_id:
        return jsonify({'success': False, 'error': 'Bot token and chat ID are required'})
    pp_time = format_phnom_penh_time()
    ok, err = send_telegram_message(
        bot_token, chat_id,
        f"&#9989; <b>OCI Instance loop Connected</b>\n\n"
        f"Your Telegram alerts are now active.\n"
        f"<b>Server Time:</b> {pp_time} (Phnom Penh, ICT)\n\n"
        f"You will receive notifications when provisioning succeeds or fails."
    )
    if ok:
        return jsonify({'success': True, 'message': 'Test message sent successfully'})
    return jsonify({'success': False, 'error': err})


@app.route('/api/send-telegram', methods=['POST'])
@require_auth
def send_telegram():
    data = request.json or {}
    ok, err = send_telegram_message(
        data.get('bot_token'), data.get('chat_id'), data.get('message', '')
    )
    return jsonify({'success': ok, 'error': err})


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
