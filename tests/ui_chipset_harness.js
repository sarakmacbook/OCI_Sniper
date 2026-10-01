// Checks the chipset (ARM vs AMD/x86) behaviour of the UI without a browser.
//
// The page JavaScript is extracted from templates/index.html and run in Node's
// `vm` module against a minimal DOM shim, so the real shipped code is exercised:
// the shape hint, per-chipset image scans, the "re-scan OS images" prompt on a
// shape switch, the refusal to start with a stale image list, and a successful
// start once the image matches the new shape.
//
// Run it with Node (no npm dependencies, no server needed):
//
//     node tests/ui_chipset_harness.js
//
// Exit code 0 = every check passed.
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'templates', 'index.html'), 'utf8');
const js = html.match(/<script>([\s\S]*)<\/script>/)[1];

function makeEl(id) {
    const el = {
        id,
        value: '',
        _innerHTML: '',
        textContent: '',
        disabled: false,
        checked: false,
        files: [],
        options: [],
        style: {},
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        appendChild(child) {
            this.options.push(child);
            // A real <select> auto-selects its first option.
            if (this.options.length === 1) this.value = child.value;
        },
        addEventListener() {},
        removeEventListener() {},
        querySelector() { return null; },
    };
    Object.defineProperty(el, 'innerHTML', {
        get() { return this._innerHTML; },
        set(html) {
            this._innerHTML = html;
            // Replacing innerHTML replaces the children, like the real DOM.
            this.options = [];
            const re = /<option value="([^"]*)"/g;
            let m;
            while ((m = re.exec(html)) !== null) {
                const opt = makeEl('<option>');
                opt.value = m[1];
                this.options.push(opt);
            }
            this.value = this.options.length ? this.options[0].value : '';
        },
        configurable: true,
    });
    return el;
}

const elements = {};
const logs = [];

const documentShim = {
    body: { getAttribute: () => '0' },
    getElementById(id) {
        if (!elements[id]) elements[id] = makeEl(id);
        return elements[id];
    },
    createElement(tag) { return makeEl('<' + tag + '>'); },
    querySelector() { return null; },
    addEventListener() {},
};

const requests = [];
const responses = {};   // url -> () => payload

const ctx = {
    console,
    document: documentShim,
    window: { addEventListener() {} },
    confirm: () => false,
    setInterval: () => 0,
    clearInterval: () => {},
    setTimeout: (fn) => { fn(); return 0; },
    FileReader: class { readAsText() {} },
    parseInt,
    isNaN,
    Math,
    JSON,
    fetch: async (url, opts) => {
        requests.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
        const handler = responses[url];
        if (!handler) throw new Error('unexpected fetch: ' + url);
        return { json: async () => handler(opts && opts.body ? JSON.parse(opts.body) : null) };
    },
    DEMO: false,
};
vm.createContext(ctx);
vm.runInContext(js, ctx);
ctx.addLog = (message, kind) => logs.push({ message, kind });

let failures = 0;
function check(label, condition, extra) {
    if (condition) {
        console.log('  ok  - ' + label);
    } else {
        failures++;
        console.log('  FAIL- ' + label + (extra ? ' :: ' + extra : ''));
    }
}

function armImages() {
    return { success: true, shape: 'VM.Standard.A1.Flex', arch: 'arm', images: [
        { id: 'arm24', name: 'Canonical-Ubuntu-24.04-aarch64-2025.09.15-0', os: 'Canonical Ubuntu', arch: 'arm' },
    ] };
}
function x86Images() {
    return { success: true, shape: 'VM.Standard.E2.1.Micro', arch: 'x86', images: [
        { id: 'x8624', name: 'Canonical-Ubuntu-24.04-2025.09.15-0', os: 'Canonical Ubuntu', arch: 'x86' },
    ] };
}

(async () => {
    
    const el = (id) => documentShim.getElementById(id);
    const run = (code) => vm.runInContext(code, ctx);   // top-level let/const live in the script scope
    const shape = el('shapeSelect');
    const image = el('imageSelect');

    console.log('1. initial state (default shape = Ampere A1 Flex)');
    shape.value = 'VM.Standard.A1.Flex';
    run('lastShapeArch = null');
    ctx.onShapeChange();
    check('shape hint says ARM', /ARM \(aarch64\)/.test(el('shapeArchHint').innerHTML),
          el('shapeArchHint').innerHTML);
    check('image hint asks for a scan', /Not scanned yet/.test(el('imageScanHint').textContent),
          el('imageScanHint').textContent);

    console.log('2. scan images for the ARM shape');
    shape.value = 'VM.Standard.A1.Flex';
    el('cfgUser').value = 'u'; el('cfgKey').value = 'k';
    responses['/api/list-images'] = (body) => (body.shape === 'VM.Standard.A1.Flex' ? armImages() : x86Images());
    await ctx.scanImages();
    check('scannedShape = A1 shape', run('scannedShape') === 'VM.Standard.A1.Flex', String(run('scannedShape')));
    check('arm option offered', image.options.some(o => o.value === 'arm24'));
    check('hint shows the scanned shape', /Scanned for VM.Standard.A1.Flex/.test(el('imageScanHint').textContent),
          el('imageScanHint').textContent);

    console.log('3. switch to AMD E2 Micro: prompt + stale list cleared');
    let asked = null;
    ctx.confirm = (message) => { asked = message; return false; };   // decline the auto re-scan
    shape.value = 'VM.Standard.E2.1.Micro';
    ctx.onShapeChange();
    check('user was asked to re-scan', asked !== null && /Re-scan OS images now\?/.test(asked), String(asked));
    check('prompt names both chipsets', /ARM \(aarch64\) → AMD\/Intel \(x86_64\)/.test(String(asked)), String(asked));
    check('stale image list cleared', image.options.length === 1 && image.options[0].value === '', JSON.stringify(image.options));
    check('no image selected', image.value === '');
    check('warning logged', logs.some(l => /Re-scan OS images for the new shape/.test(l.message)));
    check('hint says not scanned for x86', /Not scanned yet .*AMD\/Intel/.test(el('imageScanHint').textContent),
          el('imageScanHint').textContent);

    console.log('4. start is refused with the stale/absent scan');
    image.value = 'arm24';                  // pretend the old option survived
    el('sshKey').value = 'ssh-rsa AAAA';
    run('scannedShape = null');
    requests.length = 0;
    await ctx.startLoop();
    check('no launch request was sent', requests.every(r => r.url !== '/api/auto-launch-loop'),
          JSON.stringify(requests));
    check('refusal names the chipset', logs.some(l => l.kind === 'error' && /Re-scan OS images so the image matches the chipset/.test(l.message)),
          JSON.stringify(logs[logs.length - 1]));

    console.log('5. re-scan for AMD, then start is allowed');
    ctx.confirm = () => true;
    await ctx.scanImages();
    check('x86-only option list', image.options.length === 1 && image.options[0].value === 'x8624',
          JSON.stringify(image.options));
    check('scannedShape = AMD shape', run('scannedShape') === 'VM.Standard.E2.1.Micro');
    check('hint shows the AMD chipset', /AMD\/Intel \(x86_64\)/.test(el('imageScanHint').textContent),
          el('imageScanHint').textContent);

    responses['/api/auto-launch-loop'] = () => ({ success: false, error: 'stub: refused for the test' });
    image.value = 'x8624';
    el('subnetSelect').value = 'sn';
    el('vmName').value = 'VM-AMD';
    el('ocpus').value = '1'; el('memory').value = '1'; el('bootVol').value = '50';
    el('retryDelay').value = '10'; el('tgToken').value = ''; el('tgChat').value = '';
    el('tgLiveLog').checked = false; el('adSelect').value = '';
    requests.length = 0;
    await ctx.startLoop();
    const launch = requests.find(r => r.url === '/api/auto-launch-loop');
    check('launch request sent with the x86 image + AMD shape',
          !!launch && launch.body.image_id === 'x8624' && launch.body.shape === 'VM.Standard.E2.1.Micro',
          JSON.stringify(launch && launch.body));

    console.log('6. render unavailable-shape logs as a prominent warning');
    responses['/api/logs?offset=0'] = () => ({
        logs: [
            "[2026-10-01 08:23:56] [demo] WARNING: No shape availability — Currently, shape 'VM.Standard.E2.1.Micro' is not available in your Oracle Cloud region 'ap-kulai-1'. Please wait for Oracle Cloud to offer it there."
        ],
        next_offset: 1,
    });
    await ctx.fetchLogs();
    const warning = el('terminalBody').options.find(child => child.className === 'log-line log-shape-warning');
    check('shape warning gets its large callout style', !!warning, JSON.stringify(el('terminalBody').options));
    check('WARNING is rendered as a separate bold headline',
          !!warning && warning.options[1].textContent === 'WARNING',
          warning && warning.options[1] && warning.options[1].textContent);
    check('callout shows the requested region-availability guidance',
          !!warning && /Currently, shape.*not available.*Please wait for Oracle Cloud to offer it there/.test(warning.options[2].textContent),
          warning && warning.options[2] && warning.options[2].textContent);

    console.log(failures ? '\n' + failures + ' FAILURE(S)' : '\nALL UI CHECKS PASSED');
    process.exit(failures ? 1 : 0);
})();
