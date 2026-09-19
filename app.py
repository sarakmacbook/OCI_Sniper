import os
import re
import time
import random
import threading
import datetime
import functools
import requests
from flask import Flask, render_template, request, jsonify, Response
import oci

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

# ---- Security Headers ----
@app.after_request
def add_security_headers(response):
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

# ---- Config ----
ADMIN_PASSWORD = os.environ.get('APP_PASSWORD')
if not ADMIN_PASSWORD:
    print("WARNING: APP_PASSWORD not set. Running WITHOUT authentication. Set APP_PASSWORD to enable Basic Auth.")

MAX_ATTEMPTS = int(os.environ.get('MAX_ATTEMPTS', 100))

# ---- Shared state ----
global_logs = []
logs_lock = threading.Lock()

automation_lock = threading.Lock()
automation_running = False
automation_shape = None
stop_event = threading.Event()

# ---- Telegram live log settings ----
tg_live_lock = threading.Lock()
tg_live_enabled = False
tg_live_bot_token = None
tg_live_chat_id = None
tg_live_last_sent = 0
tg_live_min_interval = 3  # seconds between live log sends


def add_log(message):
    timestamp = format_phnom_penh_time()
    line = f"[{timestamp}] {message}"
    print(line)
    with logs_lock:
        global_logs.append(line)
        if len(global_logs) > 200:
            global_logs.pop(0)

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


@app.route('/')
def home():
    try:
        return render_template('index.html')
    except Exception as e:
        return f"Flask Template Error: {str(e)}", 500


@app.route('/api/list-images', methods=['POST'])
@require_auth
def list_available_images():
    data = request.json or {}
    config = build_config(data)
    shape = data.get('shape')
    all_os_mode = data.get('all_os_mode', False)

    try:
        oci.config.validate_config(config)
        compute = oci.core.ComputeClient(config)

        kwargs = {'compartment_id': config['tenancy']}
        if shape:
            kwargs['shape'] = shape

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
def list_available_subnets():
    data = request.json or {}
    config = build_config(data)

    try:
        oci.config.validate_config(config)
        network_client = oci.core.VirtualNetworkClient(config)
        identity_client = oci.identity.IdentityClient(config)

        tenancy = config['tenancy']
        ads = identity_client.list_availability_domains(compartment_id=tenancy).data

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
def test_launch():
    """Debug endpoint: validates launch params without actually creating instance."""
    data = request.json or {}
    config = build_config(data)

    try:
        oci.config.validate_config(config)
        compute_client = oci.core.ComputeClient(config)
        network_client = oci.core.VirtualNetworkClient(config)
        identity_client = oci.identity.IdentityClient(config)
        block_client = oci.core.BlockstorageClient(config)

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


@app.route('/api/list-vnics', methods=['POST'])
@require_auth
def list_vnics():
    """List VNICs (network interfaces) for debugging network setup."""
    data = request.json or {}
    config = build_config(data)
    target_subnet_id = data.get('subnet_id', '').strip() or None

    try:
        oci.config.validate_config(config)
        compute_client = oci.core.ComputeClient(config)
        network_client = oci.core.VirtualNetworkClient(config)
        tenancy = config['tenancy']

        # Pre-fetch all VCNs and subnets for name resolution
        vcns = {v.id: v for v in network_client.list_vcns(compartment_id=tenancy).data}
        subnets = {s.id: s for s in network_client.list_subnets(compartment_id=tenancy).data}
        instances = {i.id: i for i in compute_client.list_instances(compartment_id=tenancy).data}

        # List all VNIC attachments in the tenancy
        vnics = []
        vnic_attachments = compute_client.list_vnic_attachments(compartment_id=tenancy).data
        for att in vnic_attachments:
            try:
                vnic = network_client.get_vnic(vnic_id=att.vnic_id).data
                subnet = subnets.get(vnic.subnet_id)
                vcn = vcns.get(subnet.vcn_id) if subnet else None

                # Filter by subnet if specified
                if target_subnet_id and vnic.subnet_id != target_subnet_id:
                    continue

                instance = instances.get(att.instance_id)

                vnics.append({
                    'id': vnic.id,
                    'display_name': vnic.display_name or 'Unnamed',
                    'private_ip': vnic.private_ip,
                    'public_ip': vnic.public_ip or 'None',
                    'subnet_id': vnic.subnet_id,
                    'subnet_name': subnet.display_name if subnet else 'Unknown',
                    'vcn_id': vcn.id if vcn else 'Unknown',
                    'vcn_name': vcn.display_name if vcn else 'Unknown',
                    'lifecycle_state': vnic.lifecycle_state,
                    'is_primary': getattr(att, 'is_primary', False),
                    'instance_id': att.instance_id,
                    'instance_name': instance.display_name if instance else 'Unknown'
                })
            except Exception:
                pass

        # Build VCN summary for the dropdown
        vcn_list = [{'id': v.id, 'name': v.display_name or 'Unnamed'} for v in vcns.values()]

        return jsonify({
            'success': True,
            'vnics': vnics,
            'vcns': vcn_list,
            'filtered_by_subnet': target_subnet_id
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/open-firewall', methods=['POST'])
@require_auth
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
        oci.config.validate_config(config)
        network_client = oci.core.VirtualNetworkClient(config)

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
def scan_security_rules():
    """Scan existing security rules on a subnet."""
    data = request.json or {}
    config = build_config(data)
    subnet_id = data.get('subnet_id')

    if not subnet_id:
        return jsonify({'success': False, 'error': 'subnet_id required'})

    try:
        oci.config.validate_config(config)
        network_client = oci.core.VirtualNetworkClient(config)

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
    requested_boot_gb = int(account_config.get('boot_volume_gb', 50))
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
        requested_ocpus = int(account_config.get('ocpus', 4))
        requested_memory = int(account_config.get('memory', 24))

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


def get_oci_username(config, identity_client):
    try:
        user_ocid = config.get('user')
        if not user_ocid:
            add_log("Username detection skipped: no user OCID in config")
            return None

        add_log(f"Fetching user info from Identity API...")
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
        return result

    except oci.exceptions.ServiceError as e:
        add_log(f"Identity API error (status {e.status}): {e.message}")
        return None
    except Exception as e:
        add_log(f"Error fetching user info: {str(e)}")
        return None


def run_automated_creation(config, account_config, compute_client, network_client, identity_client,
                           retry_delay=60, randomize_delay=False, random_min=25, random_max=60,
                           telegram_bot_token=None, telegram_chat_id=None):
    global automation_running

    oci_username = None
    target_region = config.get('region', 'unknown')
    target_name = account_config.get('display_name', 'AlwaysFree-Bot')

    try:
        oci_username = get_oci_username(config, identity_client)
        if oci_username:
            add_log(f"OCI username detected: {oci_username}")
    except Exception as e:
        add_log(f"Could not detect OCI username: {str(e)}")

    try:
        block_client = oci.core.BlockstorageClient(config)
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

        # Shuffle AD list for random order (speeds up finding capacity)
        import random as _random
        if len(ad_list) > 1:
            _random.shuffle(ad_list)
            add_log(f"AD order randomized for faster discovery: {', '.join(ad_list)}")

        while True:
            attempts += 1

            if stop_event.is_set():
                add_log("Provisioning loop stopped by user.")
                break

            # Rotate through availability domains (randomized order)
            current_ad = ad_list[ad_index % len(ad_list)] if ad_list else ''
            if len(ad_list) > 1:
                add_log(f"Attempt {attempts}: trying AD '{current_ad}'...")

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
                    user_line = f"<b>User:</b> {oci_username}\n" if oci_username else ""
                    tg_msg = (
                        f"&#9989; <b>OCI Provisioner Success!</b>\n\n"
                        f"<b>Instance:</b> {instance_name}\n"
                        f"<b>Shape:</b> {shape}\n"
                        f"<b>Region:</b> {region}\n"
                        f"{user_line}"
                        f"<b>Time:</b> {pp_time} (Phnom Penh)\n"
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
                    add_log(f"Auth/NotFound error — possible causes:")
                    add_log(f"  1. Image {image_id[:25]}... not found in AD {current_ad}")
                    add_log(f"  2. Shape {account_config['shape']} not available in this AD")
                    add_log(f"  3. Subnet {subnet_id[:25]}... missing permissions")
                    add_log(f"  4. Check OCI Console > Instances > Create — test manually")
                    if len(ad_list) > 1:
                        ad_index += 1
                        add_log(f"Trying next AD...")
                        continue
                    break
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

            actual_delay = retry_delay
            if randomize_delay:
                actual_delay = random.randint(random_min, random_max)
                add_log(f"Dynamic retry: waiting {actual_delay}s (randomized {random_min}-{random_max}s)")

            if stop_event.wait(actual_delay):
                add_log("Provisioning loop stopped while waiting.")
                break

        if not success:
            add_log("Provisioning loop ended without success.")
            if telegram_bot_token and telegram_chat_id:
                user_line = f"<b>User:</b> {oci_username}\n" if oci_username else ""
                pp_time = format_phnom_penh_time()
                tg_msg = (
                    f"&#10060; <b>OCI Provisioner Stopped</b>\n\n"
                    f"{user_line}"
                    f"Loop stopped after {attempts} attempts without success.\n"
                    f"<b>Region:</b> {config.get('region', 'unknown')}\n"
                    f"<b>Time:</b> {pp_time} (Phnom Penh)"
                )
                send_telegram_message(telegram_bot_token, telegram_chat_id, tg_msg)

    except Exception as e:
        msg = str(e)
        if "Remote end closed connection" in msg or "Connection aborted" in msg:
            add_log(f"Network connection lost. Loop ended.")
        else:
            add_log(f"Automation engine failure: {msg}")
        if telegram_bot_token and telegram_chat_id:
            user_line = f"<b>User:</b> {oci_username}\n" if oci_username else ""
            pp_time = format_phnom_penh_time()
            tg_msg = (
                f"&#10060; <b>OCI Provisioner Error</b>\n\n"
                f"{user_line}"
                f"Automation engine failure:\n{msg[:200]}\n"
                f"<b>Time:</b> {pp_time} (Phnom Penh)"
            )
            send_telegram_message(telegram_bot_token, telegram_chat_id, tg_msg)

    finally:
        with automation_lock:
            automation_running = False
            automation_shape = None


@app.route('/api/free-tier-status', methods=['POST'])
@require_auth
def free_tier_status():
    data = request.json or {}
    config = build_config(data)

    try:
        oci.config.validate_config(config)
        compute_client = oci.core.ComputeClient(config)
        block_client = oci.core.BlockstorageClient(config)
        identity_client = oci.identity.IdentityClient(config)

        usage = get_free_tier_usage(config, compute_client, block_client, identity_client)

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
            'shape': automation_shape
        })


@app.route('/api/auto-launch-loop', methods=['POST'])
@require_auth
def auto_launch():
    global automation_running, tg_live_enabled, tg_live_bot_token, tg_live_chat_id
    data = request.json or {}
    config = build_config(data)

    try:
        oci.config.validate_config(config)
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
        compute_client = oci.core.ComputeClient(config)
        network_client = oci.core.VirtualNetworkClient(config)
        identity_client = oci.identity.IdentityClient(config)

        retry_delay = int(data.get('retry_delay', 60))
        if retry_delay < 10:
            retry_delay = 10

        randomize_delay = data.get('randomize_delay', False)
        random_min = int(data.get('random_min', 25))
        random_max = int(data.get('random_max', 60))

        thread = threading.Thread(
            target=run_automated_creation,
            args=(config, data, compute_client, network_client, identity_client,
                  retry_delay, randomize_delay, random_min, random_max,
                  data.get('telegram_bot_token'), data.get('telegram_chat_id')),
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
    offset = int(request.args.get('offset', 0))
    with logs_lock:
        batch = global_logs[offset:]
        total = len(global_logs)
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
