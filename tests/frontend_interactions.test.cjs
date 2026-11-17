const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function loadFrontend({ layoutValue = null, unavailableStorage = false } = {}) {
    let options;
    const storage = {
        getItem(key) {
            if (key !== 'supermew.layout.v1') return null;
            if (unavailableStorage) throw new Error('Storage disabled');
            return layoutValue;
        },
        setItem() { if (unavailableStorage) throw new Error('Storage disabled'); },
        removeItem() {}
    };
    const sandbox = {
        Vue: { createApp(value) { options = value; return { mount() {} }; } },
        window: { innerWidth: 1440 },
        document: { documentElement: { getBoundingClientRect: () => ({width: 1440}) } },
        location: { protocol: 'http:', host: 'localhost', origin: 'http://localhost' },
        sessionStorage: storage,
        localStorage: storage,
        URL
    };
    const script = fs.readFileSync(path.join(__dirname, '../frontend/script.js'), 'utf8');
    vm.runInNewContext(script, sandbox);
    return options;
}

function createViewModel(options = loadFrontend()) {
    const context = options.data();
    for (const [name, method] of Object.entries(options.methods)) {
        context[name] = method.bind(context);
    }
    for (const [name, getter] of Object.entries(options.computed)) {
        Object.defineProperty(context, name, { get: () => getter.call(context) });
    }
    return context;
}

for (const [handler, send] of [['handleUserKeydown', 'sendUserMessage'], ['handleAdminKeydown', 'sendAdminReply']]) {
    test(`${handler}: IME confirmation and Shift+Enter preserve the draft; Enter sends once`, () => {
        const options = loadFrontend();
        let sends = 0;
        let prevented = 0;
        const context = { [send]() { sends += 1; } };
        const event = { key: 'Enter', isComposing: true, shiftKey: false, preventDefault() { prevented += 1; } };
        options.methods[handler].call(context, event);
        assert.equal(sends, 0);
        assert.equal(prevented, 0);
        options.methods[handler].call(context, { ...event, isComposing: false, shiftKey: true });
        assert.equal(sends, 0);
        options.methods[handler].call(context, { ...event, isComposing: false });
        assert.equal(sends, 1);
        assert.equal(prevented, 1);
    });
}

test('pending authentication ignores repeated submits', async () => {
    const options = loadFrontend();
    let requests = 0;
    await options.methods.handleAuthSubmit.call({ authLoading: true, rawFetch() { requests += 1; } });
    assert.equal(requests, 0);
});

test('authentication failure becomes inline feedback and allows retry', async () => {
    const options = loadFrontend();
    const context = {
        authLoading: false,
        authError: '',
        authMode: 'login',
        authForm: { username: 'operator', password: 'wrong-password' },
        rawFetch: async () => ({ ok: false, json: async () => ({ detail: '用户名或密码错误' }) })
    };
    await options.methods.handleAuthSubmit.call(context);
    assert.equal(context.authError, '用户名或密码错误');
    assert.equal(context.authLoading, false);
    assert.equal(context.authForm.password, 'wrong-password');
});

test('editing focuses only the requested field after the Vue update', () => {
    const actions = [];
    const source = fs.readFileSync(path.join(__dirname, '../frontend/script.js'), 'utf8');
    const context = {
        Vue: { createApp(value) { return { mount() { context.methods = value.methods; } }; } },
        window: {}, location: { protocol: 'http:', host: 'localhost', origin: 'http://localhost' },
        sessionStorage: { getItem: () => null },
        document: { getElementById(id) {
            actions.push(id);
            return { focus(value) { actions.push(value.preventScroll); }, scrollIntoView(value) { actions.push(value.block); } };
        } },
        URL
    };
    vm.runInNewContext(source, context);
    let afterRender;
    context.methods.focusField.call({ $nextTick(callback) { afterRender = callback; } }, 'product-name');
    assert.deepEqual(actions, []);
    afterRender();
    assert.deepEqual(actions, ['product-name', true, 'nearest']);
});

test('invalid or unavailable saved layout recovers without blocking the app', () => {
    for (const settings of [{ layoutValue: '{broken' }, { unavailableStorage: true }]) {
        const options = loadFrontend(settings);
        const context = createViewModel(options);
        assert.equal(context.sidebarWidth, 208);
        assert.equal(context.queueWidth, 272);
        assert.doesNotThrow(() => context.saveLayout());
        context.toggleSidebar();
        assert.equal(context.sidebarWidth, 64);
    }
    const context = createViewModel(loadFrontend({ layoutValue: JSON.stringify({
        sidebarWidth: 99999, queueWidth: -100, sidebarCollapsed: 'true', queueCollapsed: true
    }) }));
    assert.equal(context.sidebarWidth, 320);
    assert.equal(context.queueWidth, 224);
    assert.equal(context.layout.sidebarCollapsed, false);
    assert.equal(context.queueCollapsed, true);
});

test('narrow viewports reserve chat space without overwriting saved panel widths', () => {
    const context = createViewModel();
    context.resizePanel('sidebar', 320);
    context.resizePanel('queue', 440);
    context.viewportWidth = 960;
    assert.equal(context.queueWidth, 246);
    assert.equal(context.viewportWidth - context.sidebarWidth - 34 - context.queueWidth, 360);
    assert.equal(context.layout.queueWidth, 440);
    context.viewportWidth = 1440;
    assert.equal(context.queueWidth, 440);
    context.viewportWidth = 680;
    assert.equal(context.sidebarWidth, 64);
    assert.equal(context.queueWidth, 262);
    context.layout.queueCollapsed = true;
    context.viewportWidth = 390;
    assert.equal(context.queueCollapsed, false);
    assert.equal(context.layout.queueCollapsed, true);
    context.viewportWidth = 679.2;
    assert.equal(context.queueCollapsed, false);
    context.viewportWidth = 959.2;
    assert.equal(context.compactNavigation, true);
});

test('panel collapse and restoration retain the selected session and draft', () => {
    const context = createViewModel();
    const session = { session_id: 'existing', status: 'human_active' };
    context.selectedConversation = session;
    context.adminReply = '待发送回复';
    const focused = [];
    context.focusField = id => focused.push(id);
    context.toggleQueue();
    context.toggleSidebar();
    context.toggleQueue();
    context.toggleSidebar();
    assert.equal(context.selectedConversation, session);
    assert.equal(context.adminReply, '待发送回复');
    assert.deepEqual(focused, ['queue-toggle', 'conversation-filter']);
    assert.equal(context.sidebarWidth, 208);
});

function createResizeHandle() {
    const component = loadFrontend().components.UiResizeHandle;
    const events = [];
    const context = {
        ...component.data(), min: 224, max: 440, initial: 272, value: 272,
        $emit(name, value) {
            events.push([name, value]);
            if (name === 'resize') context.value = value;
        }
    };
    for (const [name, method] of Object.entries(component.methods)) {
        context[name] = method.bind(context);
    }
    return { context, events };
}

test('splitter keyboard changes stay bounded and ignore unrelated keys', () => {
    const { context, events } = createResizeHandle();
    let prevented = 0;
    const press = (key, shiftKey = false) => context.keydown({ key, shiftKey, preventDefault() { prevented += 1; } });
    press('ArrowRight');
    assert.equal(context.value, 280);
    press('ArrowLeft', true);
    assert.equal(context.value, 248);
    press('Home');
    assert.equal(context.value, 224);
    press('ArrowLeft');
    assert.equal(context.value, 224);
    press('End');
    assert.equal(context.value, 440);
    const count = events.length;
    press('Tab');
    assert.equal(events.length, count);
    assert.equal(prevented, 5);
    context.reset();
    assert.equal(context.value, 272);
});

test('splitter pointer capture handles cancellation and commits once', () => {
    const { context, events } = createResizeHandle();
    const captures = new Set();
    const target = {
        setPointerCapture(id) { captures.add(id); },
        hasPointerCapture(id) { return captures.has(id); },
        releasePointerCapture(id) { captures.delete(id); }
    };
    const event = { button: 0, pointerId: 1, clientX: 100, currentTarget: target };
    context.start({ ...event, button: 2 });
    assert.equal(context.pointerId, null);
    context.start(event);
    context.move({ ...event, pointerId: 2, clientX: 500 });
    assert.equal(context.value, 272);
    context.move({ ...event, clientX: 1000 });
    assert.equal(context.value, 440);
    context.move({ ...event, clientX: -1000 });
    assert.equal(context.value, 224);
    assert.equal(events.filter(([name]) => name === 'commit').length, 0);
    context.finish(event);
    context.finish(event);
    assert.equal(context.pointerId, null);
    assert.equal(captures.size, 0);
    assert.equal(events.filter(([name]) => name === 'commit').length, 1);
});
