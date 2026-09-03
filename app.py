# ============================================================
# BOOT VOLUME MANAGER — paste this block ABOVE the line:
#     if __name__ == '__main__':
# ============================================================

bv_task_lock = threading.Lock()
bv_task_running = False


# ---------- helpers ----------

def wait_instance_state(compute_client, instance_id, target, timeout_sec=300):
    waited = 0
    while waited < timeout_sec:
        try:
            st = compute_client.get_instance(instance_id=instance_id).data.lifecycle_state
            if st == target:
                return True
        except Exception:
            pass
        time.sleep(4)
        waited += 4
    return False


def wait_boot_volume_state(block_client, bv_id, target, timeout_sec=240):
    waited = 0
    while waited < timeout_sec:
        try:
            st = block_client.get_boot_volume(boot_volume_id=bv_id).data.lifecycle_state
            if st == target:
                return True
        except Exception:
            pass
        time.sleep(3)
        waited += 3
    return False


def wait_bv_attached(compute_client, tenancy, ad_name, instance_id, bv_id, timeout_sec=240):
    waited = 0
    while waited < timeout_sec:
        try:
            atts = compute_client.list_boot_volume_attachments(
                compartment_id=tenancy, availability_domain=ad_name,
                instance_id=instance_id
            ).data
            for a in atts:
                if a.boot_volume_id == bv_id and a.lifecycle_state == 'ATTACHED':
                    return True
        except Exception:
            pass
        time.sleep(3)
        waited += 3
    return False


def find_bv_attachment(compute_client, identity_client, tenancy, boot_volume_id):
    """Return (attachment_id, instance_id) if the boot volume is attached, else (None, None)."""
    ads = identity_client.list_availability_domains(compartment_id=tenancy).data
    for ad in ads:
        try:
            atts = compute_client.list_boot_volume_attachments(
                compartment_id=tenancy, availability_domain=ad.name,
                boot_volume_id=boot_volume_id
            ).data
            for att in atts:
                if att.lifecycle_state == 'ATTACHED':
                    return att.id, att.instance_id
        except Exception:
            continue
    return None, None


def get_total_boot_storage_gb(block_client, identity_client, tenancy):
    total = 0
    ads = identity_client.list_availability_domains(compartment_id=tenancy).data
    for ad in ads:
        try:
            bvs = block_client.list_boot_volumes(compartment_id=tenancy, availability_domain=ad.name).data
            total += sum(int(v.size_in_gbs) for v in bvs
                         if v.lifecycle_state != 'TERMINATED' and v.size_in_gbs)
        except Exception:
            continue
    return total


def list_boot_volumes_detailed(config, compute_client, block_client, identity_client):
    tenancy = config['tenancy']
    ads = identity_client.list_availability_domains(compartment_id=tenancy).data
    instances = {}
    try:
        for i in compute_client.list_instances(compartment_id=tenancy).data:
            if i.lifecycle_state not in ('TERMINATED', 'TERMINATING'):
                instances[i.id] = i
    except Exception:
        pass
    volumes = []
    for ad in ads:
        try:
            bvs = block_client.list_boot_volumes(compartment_id=tenancy, availability_domain=ad.name).data
        except Exception:
            continue
        att_by_bv = {}
        try:
            atts = compute_client.list_boot_volume_attachments(
                compartment_id=tenancy, availability_domain=ad.name
            ).data
            for att in atts:
                if att.lifecycle_state == 'ATTACHED':
                    att_by_bv[att.boot_volume_id] = att
        except Exception:
            pass
        for bv in bvs:
            if bv.lifecycle_state in ('TERMINATED', 'TERMINATING'):
                continue
            att = att_by_bv.get(bv.id)
            inst = instances.get(att.instance_id) if att else None
            volumes.append({
                'id': bv.id,
                'name': bv.display_name or 'Unnamed',
                'size_gb': int(bv.size_in_gbs) if getattr(bv, 'size_in_gbs', None) else 0,
                'state': bv.lifecycle_state,
                'ad': ad.name,
                'is_attached': att is not None,
                'attachment_id': att.id if att else None,
                'instance_id': att.instance_id if att else None,
                'instance_name': (inst.display_name if inst else None) if att else None,
                'instance_state': (inst.lifecycle_state if inst else 'UNKNOWN') if att else None,
                'time_created': bv.time_created.isoformat() if getattr(bv, 'time_created', None) else None
            })
    volumes.sort(key=lambda v: (not v['is_attached'], (v['name'] or '').lower()))
    return volumes


# ---------- endpoints ----------

@app.route('/api/list-boot-volumes', methods=['POST'])
@require_auth
def api_list_boot_volumes():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    try:
        oci.config.validate_config(config)
        volumes = list_boot_volumes_detailed(
            config,
            oci.core.ComputeClient(config),
            oci.core.BlockstorageClient(config),
            oci.identity.IdentityClient(config)
        )
        return jsonify({'success': True, 'boot_volumes': volumes})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/instance-power', methods=['POST'])
@require_auth
def api_instance_power():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    instance_id = data.get('instance_id')
    action = (data.get('action') or '').upper()
    if not instance_id or action not in ('START', 'STOP', 'SOFTSTOP', 'RESET', 'SOFTRESET'):
        return jsonify({'success': False, 'error': 'instance_id and valid action (START/STOP/SOFTSTOP/RESET) required'})
    try:
        oci.config.validate_config(config)
        compute_client = oci.core.ComputeClient(config)
        try:
            name = compute_client.get_instance(instance_id=instance_id).data.display_name
        except Exception:
            name = instance_id[:20]
        compute_client.instance_action(instance_id=instance_id, action=action)
        add_log(f"Instance '{name}' power action '{action}' sent.")
        return jsonify({'success': True, 'message': f"'{action}' sent to '{name}'"})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/detach-boot-volume', methods=['POST'])
@require_auth
def api_detach_boot_volume():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    boot_volume_id = data.get('boot_volume_id')
    if not boot_volume_id:
        return jsonify({'success': False, 'error': 'boot_volume_id required'})
    try:
        oci.config.validate_config(config)
        compute_client = oci.core.ComputeClient(config)
        identity_client = oci.identity.IdentityClient(config)
        attachment_id, instance_id = find_bv_attachment(compute_client, identity_client, config['tenancy'], boot_volume_id)
        if not attachment_id:
            return jsonify({'success': False, 'error': 'Boot volume is not attached to any instance'})
        inst = compute_client.get_instance(instance_id=instance_id).data
        if inst.lifecycle_state != 'STOPPED':
            return jsonify({'success': False, 'error': f"Instance '{inst.display_name}' is {inst.lifecycle_state} — stop it first, then detach."})
        compute_client.detach_boot_volume(boot_volume_attachment_id=attachment_id)
        add_log(f"Boot volume detach initiated from '{inst.display_name}'.")
        return jsonify({'success': True, 'message': f"Detach initiated from '{inst.display_name}'. Volume becomes AVAILABLE shortly."})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/attach-boot-volume', methods=['POST'])
@require_auth
def api_attach_boot_volume():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    boot_volume_id = data.get('boot_volume_id')
    instance_id = data.get('instance_id')
    if not boot_volume_id or not instance_id:
        return jsonify({'success': False, 'error': 'boot_volume_id and instance_id required'})
    try:
        oci.config.validate_config(config)
        compute_client = oci.core.ComputeClient(config)
        block_client = oci.core.BlockstorageClient(config)
        inst = compute_client.get_instance(instance_id=instance_id).data
        bv = block_client.get_boot_volume(boot_volume_id=boot_volume_id).data
        if inst.lifecycle_state != 'STOPPED':
            return jsonify({'success': False, 'error': f"Instance must be STOPPED to attach a boot volume (current: {inst.lifecycle_state})."})
        if bv.lifecycle_state != 'AVAILABLE':
            return jsonify({'success': False, 'error': f"Boot volume must be AVAILABLE (current: {bv.lifecycle_state})."})
        if bv.availability_domain != inst.availability_domain:
            return jsonify({'success': False, 'error': f"AD mismatch: volume is in {bv.availability_domain.split(':')[-1]}, instance is in {inst.availability_domain.split(':')[-1]}."})
        compute_client.attach_boot_volume(
            attach_boot_volume_details=oci.core.models.AttachBootVolumeDetails(
                instance_id=instance_id, boot_volume_id=boot_volume_id
            )
        )
        add_log(f"Boot volume '{bv.display_name}' attach initiated -> '{inst.display_name}'.")
        return jsonify({'success': True, 'message': f"Attach initiated to '{inst.display_name}'. Start the instance once attached."})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/delete-boot-volume', methods=['POST'])
@require_auth
def api_delete_boot_volume():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    boot_volume_id = data.get('boot_volume_id')
    if not boot_volume_id:
        return jsonify({'success': False, 'error': 'boot_volume_id required'})
    try:
        oci.config.validate_config(config)
        block_client = oci.core.BlockstorageClient(config)
        bv = block_client.get_boot_volume(boot_volume_id=boot_volume_id).data
        if bv.lifecycle_state != 'AVAILABLE':
            return jsonify({'success': False, 'error': f"Boot volume must be AVAILABLE/detached to delete (current: {bv.lifecycle_state}). Detach it first."})
        block_client.delete_boot_volume(boot_volume_id=boot_volume_id)
        add_log(f"Boot volume '{bv.display_name}' ({int(bv.size_in_gbs)} GB) deletion initiated.")
        return jsonify({'success': True, 'message': f"Boot volume '{bv.display_name}' deletion initiated"})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/resize-boot-volume', methods=['POST'])
@require_auth
def api_resize_boot_volume():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    boot_volume_id = data.get('boot_volume_id')
    new_size = data.get('new_size_gb')
    if not boot_volume_id or not new_size:
        return jsonify({'success': False, 'error': 'boot_volume_id and new_size_gb required'})
    try:
        new_size = int(new_size)
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'new_size_gb must be a number'})
    try:
        oci.config.validate_config(config)
        block_client = oci.core.BlockstorageClient(config)
        identity_client = oci.identity.IdentityClient(config)
        bv = block_client.get_boot_volume(boot_volume_id=boot_volume_id).data
        current = int(bv.size_in_gbs)
        if new_size <= current:
            return jsonify({'success': False, 'error': f"New size must be LARGER than current ({current} GB). OCI cannot shrink volumes."})
        total = get_total_boot_storage_gb(block_client, identity_client, config['tenancy'])
        projected = total - current + new_size
        if projected > 200:
            return jsonify({'success': False, 'error': f"Would exceed 200 GB free tier (total {total} - this {current} + new {new_size} = {projected} GB)."})
        block_client.update_boot_volume(
            boot_volume_id=boot_volume_id,
            update_boot_volume_details=oci.core.models.UpdateBootVolumeDetails(size_in_gbs=new_size)
        )
        add_log(f"Boot volume '{bv.display_name}' resized: {current} GB -> {new_size} GB.")
        return jsonify({'success': True, 'message': f"Resized {current} GB -> {new_size} GB. Expand partition + filesystem inside the OS to use it."})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ---------- full "re-do" workflow (background thread) ----------

def run_boot_volume_adjust(config, instance_id, new_size_gb, tz_name=None, tg_token=None, tg_chat=None):
    """STOP -> detach -> resize -> re-attach -> START, all automatic."""
    global bv_task_running
    set_user_tz(tz_name)
    inst_name = instance_id[:20]
    try:
        compute_client = oci.core.ComputeClient(config)
        block_client = oci.core.BlockstorageClient(config)
        identity_client = oci.identity.IdentityClient(config)
        tenancy = config['tenancy']

        inst = compute_client.get_instance(instance_id=instance_id).data
        inst_name = inst.display_name
        ad_name = inst.availability_domain
        add_log(f"[BV-ADJUST] Target: '{inst_name}' | goal: boot volume -> {new_size_gb} GB")

        atts = compute_client.list_boot_volume_attachments(
            compartment_id=tenancy, availability_domain=ad_name, instance_id=instance_id
        ).data
        att = next((a for a in atts if a.lifecycle_state == 'ATTACHED'), None)
        if not att:
            add_log("[BV-ADJUST] Error: no attached boot volume found on this instance.")
            return
        bv_id = att.boot_volume_id
        bv = block_client.get_boot_volume(boot_volume_id=bv_id).data
        current_size = int(bv.size_in_gbs)
        add_log(f"[BV-ADJUST] Boot volume: '{bv.display_name}' ({current_size} GB)")
        if new_size_gb <= current_size:
            add_log(f"[BV-ADJUST] Error: new size ({new_size_gb}) must be LARGER than current ({current_size} GB). OCI cannot shrink.")
            return
        total = get_total_boot_storage_gb(block_client, identity_client, tenancy)
        if total - current_size + new_size_gb > 200:
            add_log(f"[BV-ADJUST] Error: would exceed 200 GB free tier ({total} - {current_size} + {new_size_gb} > 200).")
            return

        # 1) Stop
        if inst.lifecycle_state != 'STOPPED':
            add_log(f"[BV-ADJUST] Step 1/5: stopping '{inst_name}' (graceful)...")
            compute_client.instance_action(instance_id=instance_id, action='SOFTSTOP')
            if not wait_instance_state(compute_client, instance_id, 'STOPPED', 240):
                add_log("[BV-ADJUST] Graceful stop timed out — forcing STOP...")
                compute_client.instance_action(instance_id=instance_id, action='STOP')
                if not wait_instance_state(compute_client, instance_id, 'STOPPED', 240):
                    add_log("[BV-ADJUST] Error: instance would not stop. Aborting.")
                    return
            add_log("[BV-ADJUST] Instance stopped.")
        else:
            add_log("[BV-ADJUST] Step 1/5: instance already stopped.")

        # 2) Detach
        add_log("[BV-ADJUST] Step 2/5: detaching boot volume...")
        compute_client.detach_boot_volume(boot_volume_attachment_id=att.id)
        if not wait_boot_volume_state(block_client, bv_id, 'AVAILABLE', 240):
            add_log("[BV-ADJUST] Error: detach timed out. Aborting (instance left STOPPED).")
            return
        add_log("[BV-ADJUST] Detached (AVAILABLE).")

        # 3) Resize
        add_log(f"[BV-ADJUST] Step 3/5: resizing {current_size} GB -> {new_size_gb} GB...")
        block_client.update_boot_volume(
            boot_volume_id=bv_id,
            update_boot_volume_details=oci.core.models.UpdateBootVolumeDetails(size_in_gbs=new_size_gb)
        )
        time.sleep(5)
        add_log("[BV-ADJUST] Resize applied.")

        # 4) Re-attach
        add_log("[BV-ADJUST] Step 4/5: re-attaching boot volume...")
        compute_client.attach_boot_volume(
            attach_boot_volume_details=oci.core.models.AttachBootVolumeDetails(
                instance_id=instance_id, boot_volume_id=bv_id
            )
        )
        if not wait_bv_attached(compute_client, tenancy, ad_name, instance_id, bv_id, 240):
            add_log("[BV-ADJUST] Error: re-attach timed out. Volume is AVAILABLE — attach manually from the panel.")
            return
        add_log("[BV-ADJUST] Re-attached.")

        # 5) Start
        add_log(f"[BV-ADJUST] Step 5/5: starting '{inst_name}'...")
        compute_client.instance_action(instance_id=instance_id, action='START')
        add_log(f"[BV-ADJUST] DONE — '{inst_name}' now has a {new_size_gb} GB boot volume.")
        add_log("[BV-ADJUST] Remember: grow the partition + filesystem in the OS (growpart / resize2fs).")
        if tg_token and tg_chat:
            send_telegram_message(tg_token, tg_chat,
                f"&#9989; <b>Boot volume adjusted</b>\n\n"
                f"<b>Instance:</b> {inst_name}\n"
                f"<b>Size:</b> {current_size} GB &rarr; {new_size_gb} GB\n"
                f"<b>Status:</b> Restarted\n"
                f"<b>Time:</b> {format_user_time(tz_name=get_current_tz())}",
                get_current_tz())
    except Exception as e:
        add_log(f"[BV-ADJUST] Failure: {str(e)}")
        if tg_token and tg_chat:
            send_telegram_message(tg_token, tg_chat,
                f"&#10060; <b>Boot volume adjust failed</b>\n\n"
                f"<b>Instance:</b> {inst_name}\n"
                f"<b>Error:</b> {str(e)[:200]}\n"
                f"<b>Time:</b> {format_user_time(tz_name=get_current_tz())}",
                get_current_tz())
    finally:
        with bv_task_lock:
            bv_task_running = False


@app.route('/api/adjust-boot-volume', methods=['POST'])
@require_auth
def api_adjust_boot_volume():
    global bv_task_running
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    instance_id = data.get('instance_id')
    new_size = data.get('new_size_gb')
    if not instance_id or not new_size:
        return jsonify({'success': False, 'error': 'instance_id and new_size_gb required'})
    try:
        new_size = int(new_size)
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'new_size_gb must be a number'})
    if new_size < 50 or new_size > 200:
        return jsonify({'success': False, 'error': 'Size must be between 50 and 200 GB'})
    try:
        oci.config.validate_config(config)
    except Exception as e:
        return jsonify({'success': False, 'error': f"Invalid OCI config: {e}"})
    with bv_task_lock:
        if bv_task_running:
            return jsonify({'success': False, 'error': 'A boot volume task is already running. Wait for it to finish.'})
        bv_task_running = True
    threading.Thread(
        target=run_boot_volume_adjust,
        args=(config, instance_id, new_size, get_current_tz(),
              data.get('telegram_bot_token'), data.get('telegram_chat_id')),
        daemon=True
    ).start()
    return jsonify({'success': True, 'message': f'Adjust started: stop -> detach -> resize to {new_size} GB -> re-attach -> start. Watch live output.'})


# ============================================================
# REPLACE your existing /api/status route with this version
# (so the UI badge + log polling also track boot volume tasks)
# ============================================================
@app.route('/api/status', methods=['GET'])
@require_auth
def get_status():
    with automation_lock:
        running = automation_running
        shape = automation_shape
    with bv_task_lock:
        bv_running = bv_task_running
    return jsonify({'success': True, 'running': running or bv_running,
                    'shape': shape, 'bv_task': bv_running})
