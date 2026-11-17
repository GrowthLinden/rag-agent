const { createApp } = Vue;

const DEFAULT_API_BASE_URL = 'http://127.0.0.1:8000';
const configuredApiBaseUrl = (window.SUPERMEW_API_BASE_URL || '').trim().replace(/\/+$/, '');
const configuredWsBaseUrl = (window.SUPERMEW_WS_BASE_URL || '').trim().replace(/\/+$/, '');
const API_BASE_URL = configuredApiBaseUrl || (location.protocol === 'file:' || !location.host ? DEFAULT_API_BASE_URL : '');
const API_ORIGIN_LABEL = API_BASE_URL || (location.origin && location.origin !== 'null' ? location.origin : DEFAULT_API_BASE_URL);
const AUTH_TOKEN_KEY = 'accessToken';
const LAYOUT_STORAGE_KEY = 'supermew.layout.v1';
const PANEL_SIZES = {
    sidebar: { min: 176, max: 320, initial: 208, compact: 64 },
    queue: { min: 224, max: 440, initial: 272 },
    mobile: 680,
    compact: 960,
    workspaceInsets: 34, // 工作台两侧各 16px 外边距，加上 2px 边框。
    minChat: 360
};

function clampPanelWidth(value, min, max, fallback) {
    return typeof value === 'number' && Number.isFinite(value)
        ? Math.round(Math.min(max, Math.max(min, value)))
        : fallback;
}

function readLayoutPreferences() {
    let saved;
    try {
        saved = JSON.parse(localStorage.getItem(LAYOUT_STORAGE_KEY)) || {};
    } catch (_) {
        saved = {};
    }
    return {
        sidebarWidth: clampPanelWidth(saved.sidebarWidth, PANEL_SIZES.sidebar.min, PANEL_SIZES.sidebar.max, PANEL_SIZES.sidebar.initial),
        queueWidth: clampPanelWidth(saved.queueWidth, PANEL_SIZES.queue.min, PANEL_SIZES.queue.max, PANEL_SIZES.queue.initial),
        sidebarCollapsed: saved.sidebarCollapsed === true,
        queueCollapsed: saved.queueCollapsed === true
    };
}

function resizeComposer(field) {
    field.style.height = '0px';
    field.style.height = `${field.scrollHeight}px`;
}

function normalizePath(path) {
    return path.startsWith('/') ? path : `/${path}`;
}

function resolveApiUrl(path) {
    if (/^https?:\/\//i.test(path)) return path;
    return `${API_BASE_URL}${normalizePath(path)}`;
}

function resolveWsUrl(path) {
    if (/^wss?:\/\//i.test(path)) return path;
    const base = configuredWsBaseUrl || API_ORIGIN_LABEL;
    const url = new URL(base);
    const protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${url.host}${normalizePath(path)}`;
}

function sanitizeHtmlFallback(html) {
    const template = document.createElement('template');
    template.innerHTML = html || '';
    const blockedTags = ['script', 'style', 'iframe', 'object', 'embed', 'link', 'meta'];
    template.content.querySelectorAll(blockedTags.join(',')).forEach(node => node.remove());
    template.content.querySelectorAll('*').forEach(node => {
        for (const attr of [...node.attributes]) {
            const name = attr.name.toLowerCase();
            const value = (attr.value || '').trim().toLowerCase();
            if (name.startsWith('on') || name === 'srcdoc' || ((name === 'href' || name === 'src') && value.startsWith('javascript:'))) {
                node.removeAttribute(attr.name);
            }
        }
    });
    return template.innerHTML;
}

function sanitizeRenderedMarkdown(html) {
    if (window.DOMPurify) {
        return window.DOMPurify.sanitize(html, {
            USE_PROFILES: { html: true },
            ADD_ATTR: ['target']
        });
    }
    return sanitizeHtmlFallback(html);
}

createApp({
    components: {
        UiIcon: {
            props: { name: { type: String, required: true } },
            template: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><use :href="\'icons.svg#\' + name"></use></svg>'
        },
        UiResizeHandle: {
            props: {
                label: String,
                controls: String,
                value: Number,
                min: Number,
                max: Number,
                initial: Number
            },
            emits: ['resize', 'commit'],
            data() {
                return { pointerId: null, startX: 0, startValue: 0 };
            },
            methods: {
                setWidth(value) {
                    this.$emit('resize', clampPanelWidth(value, this.min, this.max, this.initial));
                },
                start(event) {
                    if (event.button !== 0 || this.pointerId !== null) return;
                    this.pointerId = event.pointerId;
                    this.startX = event.clientX;
                    this.startValue = this.value;
                    event.currentTarget.setPointerCapture(event.pointerId);
                },
                move(event) {
                    if (event.pointerId === this.pointerId) {
                        this.setWidth(this.startValue + event.clientX - this.startX);
                    }
                },
                finish(event) {
                    if (event.pointerId !== this.pointerId) return;
                    this.pointerId = null;
                    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
                        event.currentTarget.releasePointerCapture(event.pointerId);
                    }
                    this.$emit('commit');
                },
                keydown(event) {
                    const step = event.shiftKey ? 32 : 8;
                    const values = { ArrowLeft: this.value - step, ArrowRight: this.value + step, Home: this.min, End: this.max };
                    if (!Object.hasOwn(values, event.key)) return;
                    event.preventDefault();
                    this.setWidth(values[event.key]);
                    this.$emit('commit');
                },
                reset() {
                    this.setWidth(this.initial);
                    this.$emit('commit');
                }
            },
            template: `<div class="resize-handle" :class="{resizing: pointerId !== null}" role="separator" tabindex="0"
                aria-orientation="vertical" :aria-label="label" :aria-controls="controls"
                :aria-valuemin="min" :aria-valuemax="max" :aria-valuenow="value" :aria-valuetext="value + ' 像素'"
                :title="label + '（左右方向键调整，双击还原）'"
                @pointerdown.prevent="start" @pointermove="move" @pointerup="finish" @pointercancel="finish"
                @lostpointercapture="finish" @keydown="keydown" @dblclick="reset"></div>`
        }
    },
    directives: {
        autosize: {
            mounted(field) {
                resizeComposer(field);
                // 面板拖动会改变自动换行，只在宽度变化时重新计算，避免流式消息触发反复测量。
                let previousWidth = field.clientWidth;
                field._composerObserver = new ResizeObserver(() => {
                    if (field.clientWidth !== previousWidth) {
                        previousWidth = field.clientWidth;
                        resizeComposer(field);
                    }
                });
                field._composerObserver.observe(field);
            },
            updated(field, binding) {
                if (binding.value !== binding.oldValue) resizeComposer(field);
            },
            unmounted(field) {
                field._composerObserver.disconnect();
            }
        }
    },
    data() {
        return {
            layout: readLayoutPreferences(),
            viewportWidth: document.documentElement.getBoundingClientRect().width,
            panelSizes: PANEL_SIZES,
            token: sessionStorage.getItem(AUTH_TOKEN_KEY) || '',
            currentUser: null,
            authMode: 'login',
            authLoading: false,
            authError: '',
            authForm: { username: '', password: '' },
            activeView: 'chat',

            sessionId: localStorage.getItem('customerSessionId') || `session_${Date.now()}`,
            sessionStatus: 'ai_active',
            messages: [],
            sessions: [],
            showUserHistory: false,
            userInput: '',
            isLoading: false,
            activeCustomerStreamMessageId: '',
            seenAiStreamEvents: new Set(),
            customerWs: null,

            adminWs: null,
            adminFilters: [
                { label: '全部', value: '' },
                { label: 'AI处理中', value: 'ai_active' },
                { label: '请求人工服务', value: 'handoff_requested', badge: 'handoff_requested' },
                { label: '人工处理中', value: 'human_active' },
                { label: '已完成', value: 'closed' }
            ],
            adminStatusFilter: '',
            adminConversationCounts: {},
            adminConversations: [],
            selectedConversation: null,
            adminMessages: [],
            adminReply: '',

            users: [],
            userKeyword: '',
            userForm: { username: '', password: '', role: 'user' },
            editingUserId: null,
            userSaving: false,
            products: [],
            productKeyword: '',
            productForm: {
                name: '',
                category: '',
                price: '',
                stock: 0,
                material: '',
                specifications: '',
                image_url: ''
            },
            editingProductId: null,
            selectedProductImage: null,
            productSaving: false,
            productNotice: '',
            documents: [],
            ragPipeline: null,
            ragTestQuery: '',
            ragTestLoading: false,
            ragTestResult: null,
            selectedFile: null,
            isUploading: false,
            uploadProgress: ''
        };
    },
    computed: {
        compactNavigation() {
            return this.viewportWidth < PANEL_SIZES.compact || this.layout.sidebarCollapsed;
        },
        sidebarWidth() {
            return this.compactNavigation ? PANEL_SIZES.sidebar.compact : this.layout.sidebarWidth;
        },
        queueMaxWidth() {
            // 收窄视口时保留聊天所需空间；用户保存的宽度不被临时视口覆盖。
            const minChat = this.viewportWidth < PANEL_SIZES.compact ? 320 : PANEL_SIZES.minChat;
            return Math.max(PANEL_SIZES.queue.min, Math.min(PANEL_SIZES.queue.max,
                this.viewportWidth - this.sidebarWidth - PANEL_SIZES.workspaceInsets - minChat));
        },
        queueWidth() {
            return Math.min(this.layout.queueWidth, this.queueMaxWidth);
        },
        queueCollapsed() {
            return this.viewportWidth >= PANEL_SIZES.mobile && this.layout.queueCollapsed;
        },
        layoutStyle() {
            return { '--sidebar': `${this.sidebarWidth}px`, '--queue-width': `${this.queueWidth}px` };
        },
        isAuthenticated() {
            return !!this.token && !!this.currentUser;
        },
        isAdmin() {
            return this.currentUser?.role === 'admin';
        },
        canReply() {
            return this.selectedConversation
                && ['handoff_requested', 'human_active'].includes(this.selectedConversation.status)
                && this.adminReply.trim();
        },
        adminHandoffRequestCount() {
            return this.adminConversationCounts.handoff_requested || 0;
        }
    },
    async mounted() {
        window.addEventListener('resize', this.updateViewportWidth);
        localStorage.removeItem(AUTH_TOKEN_KEY);
        localStorage.setItem('customerSessionId', this.sessionId);
        if (this.token) {
            try {
                await this.fetchMe();
                this.afterLogin();
            } catch (_) {
                this.handleLogout();
            }
        }
    },
    beforeUnmount() {
        window.removeEventListener('resize', this.updateViewportWidth);
    },
    methods: {
        updateViewportWidth() {
            // 浏览器缩放后视口可能是小数，不能用取整的 innerWidth 判断布局断点。
            this.viewportWidth = document.documentElement.getBoundingClientRect().width;
        },
        saveLayout() {
            try {
                localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(this.layout));
            } catch (_) {
                // 隐私模式或存储配额不足时，布局仍在当前页面生效。
            }
        },
        resizePanel(panel, width) {
            const sizes = PANEL_SIZES[panel];
            const max = panel === 'queue' ? this.queueMaxWidth : sizes.max;
            this.layout[`${panel}Width`] = clampPanelWidth(width, sizes.min, max, sizes.initial);
        },
        toggleSidebar() {
            this.layout.sidebarCollapsed = !this.layout.sidebarCollapsed;
            this.saveLayout();
        },
        toggleQueue() {
            this.layout.queueCollapsed = !this.layout.queueCollapsed;
            this.saveLayout();
            this.focusField(this.layout.queueCollapsed ? 'queue-toggle' : 'conversation-filter');
        },
        focusField(id) {
            this.$nextTick(() => {
                const field = document.getElementById(id);
                if (!field) return;
                field.focus({ preventScroll: true });
                field.scrollIntoView({ block: 'nearest' });
            });
        },
        backToQueue() {
            this.selectedConversation = null;
            this.focusField('conversation-filter');
        },
        parseMarkdown(text) {
            const normalized = (text || '').replace(
                /(!?\[[^\]]*\]\()((?:\/assets\/|assets\/)[^)]+)(\))/g,
                (_match, prefix, url, suffix) => `${prefix}${this.assetUrl(url)}${suffix}`
            );
            return sanitizeRenderedMarkdown(marked.parse(normalized));
        },
        assetUrl(url) {
            const value = String(url || '').trim();
            if (!value) return '';
            if (/^(https?:|data:|blob:)/i.test(value)) return value;
            if (value.startsWith('/assets/') || value.startsWith('assets/')) {
                return this.apiUrl(value.startsWith('/') ? value : `/${value}`);
            }
            return this.apiUrl(value);
        },
        displayPage(value) {
            if (value === null || value === undefined || value === '') return '-';
            const number = Number(value);
            return Number.isFinite(number) ? number + 1 : String(value);
        },
        normalizeImage(image, fallback = {}) {
            if (!image) return null;
            const url = image.url || image.image_url || '';
            if (!url) return null;
            return {
                url,
                filename: image.filename || fallback.filename || '',
                page_number: image.page_number ?? fallback.page_number ?? null,
                image_index: image.image_index ?? image.chunk_idx ?? fallback.image_index ?? fallback.chunk_idx ?? null,
                caption: image.caption || image.image_caption || fallback.caption || fallback.image_caption || '',
                visual_description: image.visual_description || image.description || fallback.visual_description || ''
            };
        },
        uniqueImages(images) {
            const unique = [];
            const seen = new Set();
            for (const image of images || []) {
                const normalized = this.normalizeImage(image);
                if (!normalized || seen.has(normalized.url)) continue;
                seen.add(normalized.url);
                unique.push(normalized);
            }
            return unique;
        },
        chunkImages(chunk) {
            if (!chunk) return [];
            const images = [];
            if (chunk.image_url) {
                images.push(this.normalizeImage({
                    url: chunk.image_url,
                    filename: chunk.filename,
                    page_number: chunk.page_number,
                    image_index: chunk.chunk_idx,
                    caption: chunk.image_caption,
                    visual_description: chunk.visual_description
                }));
            }
            for (const image of chunk.images || []) {
                images.push(this.normalizeImage(image, chunk));
            }
            return this.uniqueImages(images);
        },
        messageImages(message) {
            const trace = message?.rag_trace;
            const selectedImages = this.uniqueImages(trace?.retrieved_images || []);
            if (selectedImages.length) {
                return selectedImages;
            }
            const images = [];
            if (message?.metadata?.images) images.push(...message.metadata.images);
            if (images.length) return this.uniqueImages(images);

            const firstImageChunk = this.traceChunks(trace).find(chunk => this.chunkImages(chunk).length);
            return firstImageChunk ? this.chunkImages(firstImageChunk).slice(0, 1) : [];
        },
        visibleChunkImages(message, chunk) {
            const selectedImages = this.uniqueImages(message?.rag_trace?.retrieved_images || []);
            if (!selectedImages.length) return [];
            const selectedUrls = new Set(selectedImages.map(image => image.url));
            return this.chunkImages(chunk).filter(image => selectedUrls.has(image.url));
        },
        hasLiveActivity(message) {
            return !!message?.streaming;
        },
        currentAiAction(message) {
            if (!message?.streaming) return '';
            const latestStep = message?.rag_steps?.[message.rag_steps.length - 1];
            return message?.loading_stage || latestStep?.label || '正在处理...';
        },
        ragTraceStatus(message) {
            if (!message?.streaming) return '';
            if (message.answer_done) return '整理中';
            if (message.answer_streaming) return '生成中';
            const latestStep = message?.rag_steps?.[message.rag_steps.length - 1];
            return latestStep?.label || '处理中';
        },
        activitySteps(message) {
            return (message?.rag_steps || []).slice(-3);
        },
        createStreamingAiMessage(localId = `ai_${Date.now()}`) {
            return {
                localId,
                sender_type: 'ai',
                content: '',
                rag_steps: [],
                rag_trace: null,
                streaming: true,
                answer_streaming: false,
                answer_done: false,
                loading_stage: '正在理解问题...'
            };
        },
        ensureCustomerStreamingMessage() {
            let message = this.messages.find(item => item.localId === this.activeCustomerStreamMessageId);
            if (!message) {
                message = [...this.messages].reverse().find(item => item.sender_type === 'ai' && item.streaming);
            }
            if (!message) {
                message = this.createStreamingAiMessage();
                this.messages.push(message);
            }
            this.activeCustomerStreamMessageId = message.localId;
            return message;
        },
        streamEventKey(event) {
            if (!event || !event._stream_id || event._stream_seq === undefined) return '';
            return `${event._stream_id}:${event._stream_seq}`;
        },
        shouldApplyStreamEvent(event) {
            const key = this.streamEventKey(event);
            if (!key) return true;
            if (this.seenAiStreamEvents.has(key)) return false;
            this.seenAiStreamEvents.add(key);
            if (this.seenAiStreamEvents.size > 1000) {
                this.seenAiStreamEvents.clear();
                this.seenAiStreamEvents.add(key);
            }
            return true;
        },
        applyAiStreamEventToMessage(message, event) {
            if (!message || !event) return false;
            if (!Array.isArray(message.rag_steps)) message.rag_steps = [];
            if (event.type === 'content') {
                const text = event.content || '';
                if (text) {
                    message.content += text;
                    message.answer_streaming = true;
                    message.answer_done = false;
                    message.loading_stage = '正在生成回答...';
                }
                return true;
            }
            if (event.type === 'status') {
                message.loading_stage = event.content || '正在处理...';
                return true;
            }
            if (event.type === 'rag_step') {
                const step = event.step || {};
                message.rag_steps.push(step);
                message.answer_streaming = false;
                message.answer_done = false;
                message.loading_stage = step.label || '正在检索知识库...';
                return true;
            }
            if (event.type === 'answer_done') {
                message.answer_streaming = false;
                message.answer_done = true;
                message.loading_stage = '正在整理检索结果...';
                return true;
            }
            if (event.type === 'trace') {
                message.rag_trace = event.rag_trace;
                message.streaming = false;
                message.answer_streaming = false;
                message.answer_done = true;
                message.loading_stage = '';
                return true;
            }
            return false;
        },
        handleCustomerStreamEvent(event, controls = {}) {
            if (!this.shouldApplyStreamEvent(event)) return true;
            const ensureMessage = controls.ensureMessage || (() => this.ensureCustomerStreamingMessage());
            const discardEmptyMessage = controls.discardEmptyMessage || (() => {});

            if (event.type === 'content' || event.type === 'status' || event.type === 'rag_step' || event.type === 'trace' || event.type === 'answer_done') {
                this.applyAiStreamEventToMessage(ensureMessage(), event);
                this.scrollCustomerMessages();
                return true;
            }

            if (event.type === 'handoff_status') {
                discardEmptyMessage();
                this.sessionStatus = event.status || this.sessionStatus;
                if (event.content) {
                    this.messages.push({
                        localId: `system_${Date.now()}`,
                        sender_type: 'system',
                        content: event.content
                    });
                }
                this.scrollCustomerMessages();
                return true;
            }

            if (event.type === 'error') {
                const message = ensureMessage();
                message.streaming = false;
                message.answer_streaming = false;
                message.loading_stage = '';
                this.messages.push({
                    localId: `error_${Date.now()}`,
                    sender_type: 'system',
                    content: event.content
                });
                discardEmptyMessage();
                this.scrollCustomerMessages();
                return true;
            }

            return false;
        },
        imageAlt(image) {
            return image?.caption || image?.visual_description || image?.filename || 'PDF 图片';
        },
        imageMeta(image) {
            const parts = [];
            if (image?.filename) parts.push(image.filename);
            if (image?.page_number !== null && image?.page_number !== undefined && image?.page_number !== '') {
                parts.push(`第 ${this.displayPage(image.page_number)} 页`);
            }
            if (image?.image_index) parts.push(`图 ${image.image_index}`);
            return parts.join(' · ') || '来自知识库 PDF';
        },
        authHeaders(extra = {}) {
            return this.token ? { ...extra, Authorization: `Bearer ${this.token}` } : extra;
        },
        apiUrl(url) {
            return resolveApiUrl(url);
        },
        async rawFetch(url, options = {}) {
            try {
                return await fetch(this.apiUrl(url), options);
            } catch (error) {
                throw new Error(`无法连接后端服务，请确认已启动 ${API_ORIGIN_LABEL}`);
            }
        },
        async authFetch(url, options = {}) {
            const opts = { ...options, headers: this.authHeaders(options.headers || {}) };
            const response = await this.rawFetch(url, opts);
            if (response.status === 401) {
                this.handleLogout();
                throw new Error('登录已过期，请重新登录');
            }
            return response;
        },
        wsUrl(path) {
            return resolveWsUrl(path);
        },
        async fetchMe() {
            const response = await this.authFetch('/auth/me');
            if (!response.ok) throw new Error('认证失败');
            this.currentUser = await response.json();
            this.enforceRoleView();
        },
        async handleAuthSubmit() {
            if (this.authLoading) return;
            this.authError = '';
            const username = this.authForm.username.trim();
            const password = this.authForm.password.trim();
            if (!username || !password) {
                this.authError = '请输入用户名和密码';
                return;
            }

            this.authLoading = true;
            try {
                const payload = { username, password };
                const response = await this.rawFetch(this.authMode === 'login' ? '/auth/login' : '/auth/register', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(data.detail || '提交失败');
                this.token = data.access_token;
                sessionStorage.setItem(AUTH_TOKEN_KEY, this.token);
                localStorage.removeItem(AUTH_TOKEN_KEY);
                this.currentUser = { username: data.username, role: data.role };
                this.authForm.password = '';
                this.afterLogin();
            } catch (error) {
                this.authError = error.message;
            } finally {
                this.authLoading = false;
            }
        },
        enforceRoleView() {
            if (!this.isAuthenticated) return;
            const adminViews = ['workspace', 'users', 'products', 'knowledge'];
            if (this.isAdmin) {
                if (this.activeView === 'chat' || !adminViews.includes(this.activeView)) {
                    this.activeView = 'workspace';
                }
                if (this.customerWs) {
                    this.customerWs.close();
                    this.customerWs = null;
                }
                return;
            }

            if (adminViews.includes(this.activeView) || this.activeView !== 'chat') {
                this.activeView = 'chat';
            }
            this.adminConversations = [];
            this.selectedConversation = null;
            this.adminMessages = [];
            if (this.adminWs) {
                this.adminWs.close();
                this.adminWs = null;
            }
        },
        afterLogin() {
            this.enforceRoleView();
            if (this.isAdmin) {
                this.activeView = 'workspace';
                this.connectAdminWs();
                this.loadAdminConversations();
            } else {
                this.activeView = 'chat';
                this.loadSessions();
                this.loadCurrentSession();
                this.connectCustomerWs();
            }
        },
        handleLogout() {
            this.authError = '';
            this.token = '';
            this.currentUser = null;
            this.messages = [];
            this.sessions = [];
            this.showUserHistory = false;
            this.adminConversations = [];
            this.selectedConversation = null;
            this.closeSockets();
            sessionStorage.removeItem(AUTH_TOKEN_KEY);
            localStorage.removeItem(AUTH_TOKEN_KEY);
        },
        closeSockets() {
            if (this.customerWs) this.customerWs.close();
            if (this.adminWs) this.adminWs.close();
            this.customerWs = null;
            this.adminWs = null;
        },
        connectCustomerWs() {
            if (this.customerWs || !this.token || this.isAdmin) return;
            this.customerWs = new WebSocket(this.wsUrl(`/ws/customer/${encodeURIComponent(this.sessionId)}?token=${encodeURIComponent(this.token)}`));
            this.customerWs.onmessage = (event) => {
                const data = JSON.parse(event.data);
                if (data.type === 'session_deleted') {
                    this.sessionStatus = 'ai_active';
                    this.messages.push({
                        localId: `system_${Date.now()}`,
                        sender_type: 'system',
                        content: data.content || '本次历史会话已删除。'
                    });
                    this.loadSessions();
                    this.scrollCustomerMessages();
                    return;
                }
                if (data.session) this.sessionStatus = data.session.status;
                if (data.type === 'ai_stream') {
                    this.handleCustomerStreamEvent(data.event || {});
                    return;
                }
                if (data.type === 'message' && data.message) {
                    this.addCustomerMessage(data.message);
                }
            };
            this.customerWs.onclose = () => {
                this.customerWs = null;
                if (this.isAuthenticated && !this.isAdmin) {
                    setTimeout(() => this.connectCustomerWs(), 1500);
                }
            };
        },
        connectAdminWs() {
            if (this.adminWs || !this.token || !this.isAdmin) return;
            this.adminWs = new WebSocket(this.wsUrl(`/ws/admin?token=${encodeURIComponent(this.token)}`));
            this.adminWs.onmessage = (event) => {
                const data = JSON.parse(event.data);
                if (data.type === 'session_deleted' && data.session) {
                    this.removeAdminConversation(data.session);
                    return;
                }
                if (data.type === 'user_deleted') {
                    if (this.activeView === 'users') {
                        this.users = this.users.filter(user => user.id !== data.user_id);
                    }
                    this.adminConversations = this.adminConversations.filter(item => item.username !== data.username);
                    if (this.selectedConversation?.username === data.username) {
                        this.selectedConversation = null;
                        this.adminMessages = [];
                    }
                    this.loadAdminConversationCounts();
                    return;
                }
                if (data.type === 'ai_stream') {
                    this.applyAdminStreamEvent(data);
                    return;
                }
                if (data.session) this.upsertConversation(data.session);
                if (data.type === 'message' && data.message && this.isSelected(data.session)) {
                    this.mergeAdminMessage(data.message, data.session);
                    this.scrollAdminMessages();
                }
            };
            this.adminWs.onclose = () => {
                this.adminWs = null;
                if (this.isAuthenticated && this.isAdmin) {
                    setTimeout(() => this.connectAdminWs(), 1500);
                }
            };
        },
        reconnectCustomerWs() {
            if (this.customerWs) this.customerWs.close();
            this.customerWs = null;
            this.connectCustomerWs();
        },
        async loadSessions() {
            if (!this.isAuthenticated || this.isAdmin) return;
            const response = await this.authFetch('/sessions');
            const data = await response.json();
            if (!response.ok) {
                console.warn(data.detail || '加载历史会话失败');
                return;
            }
            this.sessions = data.sessions || [];
        },
        async loadCurrentSession() {
            if (!this.isAuthenticated || this.isAdmin || !this.sessionId) return;
            try {
                const response = await this.authFetch(`/sessions/${encodeURIComponent(this.sessionId)}`);
                if (response.status === 404) return;
                const data = await response.json();
                if (!response.ok) return;
                this.sessionStatus = data.session_status || this.sessionStatus || 'ai_active';
                this.messages = (data.messages || []).map(message => ({
                    id: message.id,
                    sender_type: message.sender_type || (message.type === 'ai' ? 'ai' : (message.type === 'system' ? 'system' : 'user')),
                    content: message.content,
                    timestamp: message.timestamp,
                    rag_trace: message.rag_trace
                })).filter(message => !this.shouldHideCustomerMessage(message));
                this.scrollCustomerMessages();
            } catch (error) {
                console.warn('恢复会话失败', error);
            }
        },
        async openUserSession(sessionId) {
            this.sessionId = sessionId;
            localStorage.setItem('customerSessionId', this.sessionId);
            this.showUserHistory = false;
            await this.loadCurrentSession();
            this.reconnectCustomerWs();
        },
        startNewSession() {
            this.sessionId = `session_${Date.now()}`;
            localStorage.setItem('customerSessionId', this.sessionId);
            this.sessionStatus = 'ai_active';
            this.messages = [];
            this.showUserHistory = false;
            this.reconnectCustomerWs();
        },
        handleUserKeydown(event) {
            // 输入法确认候选词时不发送消息，保留 Enter 发送和 Shift+Enter 换行。
            if (event.isComposing) return;
            if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                this.sendUserMessage();
            }
        },
        requestHuman() {
            this.userInput = '转人工';
            this.sendUserMessage();
        },
        async sendUserMessage() {
            const text = this.userInput.trim();
            if (this.sessionStatus === 'closed') return;
            if (!text || this.isLoading) return;
            this.userInput = '';
            this.messages.push({
                localId: `local_${Date.now()}`,
                sender_type: 'user',
                content: text
            });
            this.scrollCustomerMessages();

            this.isLoading = true;
            const aiMessage = this.createStreamingAiMessage(`ai_${Date.now()}`);
            this.activeCustomerStreamMessageId = aiMessage.localId;
            this.messages.push(aiMessage);
            let aiMessageAdded = true;
            const ensureAiMessage = () => {
                if (!aiMessageAdded) {
                    this.messages.push(aiMessage);
                    aiMessageAdded = true;
                }
                return aiMessage;
            };
            const discardEmptyAiMessage = () => {
                if (aiMessage.content || aiMessage.rag_trace || aiMessage.rag_steps.length) return;
                this.messages = this.messages.filter(item => item.localId !== aiMessage.localId);
                aiMessageAdded = false;
            };
            this.scrollCustomerMessages();
            try {
                const response = await this.authFetch('/chat/stream', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message: text, session_id: this.sessionId })
                });
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';
                let streamDone = false;
                while (!streamDone) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    buffer += decoder.decode(value, { stream: true });
                    let idx;
                    while ((idx = buffer.indexOf('\n\n')) !== -1) {
                        const raw = buffer.slice(0, idx);
                        buffer = buffer.slice(idx + 2);
                        if (!raw.startsWith('data: ')) continue;
                        const payload = raw.slice(6);
                        if (payload === '[DONE]') {
                            streamDone = true;
                            await reader.cancel().catch(() => {});
                            break;
                        }
                        const data = JSON.parse(payload);
                        this.handleCustomerStreamEvent(data, {
                            ensureMessage: ensureAiMessage,
                            discardEmptyMessage: discardEmptyAiMessage
                        });
                        this.scrollCustomerMessages();
                    }
                }
            } catch (error) {
                discardEmptyAiMessage();
                this.messages.push({
                    localId: `error_${Date.now()}`,
                    sender_type: 'system',
                    content: `发送失败：${error.message}`
                });
            } finally {
                aiMessage.streaming = false;
                if (!aiMessage.content && !aiMessage.rag_trace && !aiMessage.rag_steps.length) {
                    discardEmptyAiMessage();
                }
                this.isLoading = false;
                if (this.activeCustomerStreamMessageId === aiMessage.localId) {
                    this.activeCustomerStreamMessageId = '';
                }
                this.loadSessions();
                this.scrollCustomerMessages();
            }
        },
        async closeUserSession() {
            if (!this.isAuthenticated || this.isAdmin || this.sessionStatus === 'closed') return;
            if (!this.messages.length) return;
            const response = await this.authFetch(`/sessions/${encodeURIComponent(this.sessionId)}/close`, {
                method: 'POST'
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                alert(data.detail || '关闭会话失败');
                return;
            }
            if (data.conversation?.status) this.sessionStatus = data.conversation.status;
            if (data.message) this.addCustomerMessage(data.message);
            this.userInput = '';
            await this.loadSessions();
            this.scrollCustomerMessages();
        },
        addCustomerMessage(message) {
            if (this.shouldHideCustomerMessage(message)) return;
            if (this.messages.some(item => item.id && item.id === message.id)) return;
            this.messages.push(message);
            this.scrollCustomerMessages();
        },
        shouldHideCustomerMessage(message) {
            if (message?.sender_type !== 'system') return false;
            const content = (message.content || '').trim();
            return [
                '消息已发送给人工客服，请稍候。',
                '已为你转接人工客服，请稍候。人工客服接入后会继续处理你的问题。'
            ].includes(content);
        },
        scrollCustomerMessages() {
            this.$nextTick(() => {
                if (this.$refs.chatContainer) {
                    this.$refs.chatContainer.scrollTop = this.$refs.chatContainer.scrollHeight;
                }
            });
        },
        senderName(message, scope = 'customer') {
            if (scope === 'admin' && message.sender_type === 'user') {
                return this.selectedConversation?.username || '用户';
            }
            const names = {
                user: '我',
                ai: 'AI 客服',
                admin: '人工客服',
                system: '系统'
            };
            return names[message.sender_type] || '消息';
        },
        statusText(status) {
            return {
                ai_active: 'AI处理中',
                handoff_requested: '请求人工服务',
                human_active: '人工处理中',
                closed: '已完成'
            }[status] || status || '未知状态';
        },
        async loadAdminConversations() {
            if (!this.isAdmin) return;
            const query = this.adminStatusFilter ? `?status=${encodeURIComponent(this.adminStatusFilter)}` : '';
            const response = await this.authFetch(`/admin/conversations${query}`);
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || '加载会话失败');
            this.adminConversations = data.conversations || [];
            if (data.counts) this.adminConversationCounts = data.counts;
        },
        async loadAdminConversationCounts() {
            if (!this.isAdmin) return;
            const response = await this.authFetch('/admin/conversations?status=handoff_requested');
            const data = await response.json().catch(() => ({}));
            if (response.ok && data.counts) {
                this.adminConversationCounts = data.counts;
            }
        },
        matchesAdminFilter(session) {
            if (!this.adminStatusFilter) return true;
            return session?.status === this.adminStatusFilter;
        },
        upsertConversation(session) {
            if (!this.matchesAdminFilter(session)) {
                this.adminConversations = this.adminConversations.filter(item => !this.sameConversation(item, session));
            } else {
                const index = this.adminConversations.findIndex(item => this.sameConversation(item, session));
                if (index >= 0) this.adminConversations.splice(index, 1, session);
                else this.adminConversations.unshift(session);
            }
            if (this.isSelected(session)) this.selectedConversation = session;
            this.loadAdminConversationCounts();
        },
        removeAdminConversation(session) {
            this.adminConversations = this.adminConversations.filter(item => !this.sameConversation(item, session));
            if (this.isSelected(session)) {
                this.selectedConversation = null;
                this.adminMessages = [];
            }
            this.loadAdminConversationCounts();
        },
        sameConversation(a, b) {
            return a && b && a.username === b.username && a.session_id === b.session_id;
        },
        isSelected(session) {
            return this.sameConversation(this.selectedConversation, session);
        },
        conversationStreamKey(session) {
            if (!session) return '';
            return `stream_${session.username}_${session.session_id}`;
        },
        ensureAdminStreamingMessage(session) {
            const localId = this.conversationStreamKey(session);
            let message = this.adminMessages.find(item => item.localId === localId);
            if (!message) {
                message = {
                    localId,
                    sender_type: 'ai',
                    content: '',
                    rag_steps: [],
                    rag_trace: null,
                    streaming: true,
                    answer_streaming: false,
                    answer_done: false,
                    loading_stage: '正在理解问题...'
                };
                this.adminMessages.push(message);
            }
            return message;
        },
        applyAdminStreamEvent(payload) {
            const session = payload.session;
            const event = payload.event || {};
            if (!this.isSelected(session)) return;

            if (event.type === 'content' || event.type === 'status' || event.type === 'rag_step' || event.type === 'trace' || event.type === 'answer_done') {
                const message = this.ensureAdminStreamingMessage(session);
                this.applyAiStreamEventToMessage(message, event);
            } else if (event.type === 'error') {
                this.adminMessages.push({
                    localId: `admin_error_${Date.now()}`,
                    sender_type: 'system',
                    content: event.content || 'AI 响应失败'
                });
            }
            this.scrollAdminMessages();
        },
        mergeAdminMessage(message, session = this.selectedConversation) {
            if (!message) return;
            if (message.id && this.adminMessages.some(item => item.id === message.id)) return;

            if (message.sender_type === 'ai') {
                const localId = this.conversationStreamKey(session);
                const index = this.adminMessages.findIndex(item => item.localId === localId);
                if (index >= 0) {
                    const existing = this.adminMessages[index];
                    this.adminMessages.splice(index, 1, {
                        ...message,
                        rag_steps: existing.rag_steps || [],
                        rag_trace: message.rag_trace || existing.rag_trace,
                        answer_streaming: false,
                        answer_done: true,
                        loading_stage: '',
                        streaming: false
                    });
                    return;
                }
            }

            this.adminMessages.push(message);
        },
        async openConversation(item) {
            if (!this.isAdmin) return;
            const response = await this.authFetch(`/admin/conversations/${encodeURIComponent(item.username)}/${encodeURIComponent(item.session_id)}`);
            const data = await response.json();
            if (!response.ok) {
                alert(data.detail || '打开会话失败');
                return;
            }
            this.selectedConversation = data.conversation;
            this.adminMessages = data.messages || [];
            this.upsertConversation(data.conversation);
            this.scrollAdminMessages();
        },
        async takeoverConversation() {
            if (!this.isAdmin) return;
            if (!this.selectedConversation) return;
            const c = this.selectedConversation;
            const response = await this.authFetch(`/admin/conversations/${encodeURIComponent(c.username)}/${encodeURIComponent(c.session_id)}/takeover`, { method: 'POST' });
            const data = await response.json();
            if (!response.ok) {
                alert(data.detail || '接入失败');
                return;
            }
            this.selectedConversation = data.conversation;
            this.upsertConversation(data.conversation);
            await this.openConversation(data.conversation);
        },
        handleAdminKeydown(event) {
            if (event.isComposing) return;
            if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                this.sendAdminReply();
            }
        },
        async sendAdminReply() {
            if (!this.isAdmin) return;
            const text = this.adminReply.trim();
            if (!text || !this.selectedConversation) return;
            const c = this.selectedConversation;
            this.adminReply = '';
            const response = await this.authFetch(`/admin/conversations/${encodeURIComponent(c.username)}/${encodeURIComponent(c.session_id)}/messages`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: text, session_id: c.session_id })
            });
            const data = await response.json();
            if (!response.ok) {
                alert(data.detail || '发送失败');
                return;
            }
            this.selectedConversation = data.conversation;
            this.adminMessages.push(data.message);
            this.upsertConversation(data.conversation);
            this.scrollAdminMessages();
        },
        async closeConversation() {
            if (!this.isAdmin) return;
            if (!this.selectedConversation || !confirm('确定关闭这个会话吗？')) return;
            const c = this.selectedConversation;
            const response = await this.authFetch(`/admin/conversations/${encodeURIComponent(c.username)}/${encodeURIComponent(c.session_id)}/close`, { method: 'POST' });
            const data = await response.json();
            if (!response.ok) {
                alert(data.detail || '关闭失败');
                return;
            }
            this.selectedConversation = data.conversation;
            this.upsertConversation(data.conversation);
        },
        async deleteConversation() {
            if (!this.isAdmin) return;
            if (!this.selectedConversation || !confirm('确定删除这条历史会话吗？删除后无法恢复。')) return;
            const c = this.selectedConversation;
            const response = await this.authFetch(`/admin/conversations/${encodeURIComponent(c.username)}/${encodeURIComponent(c.session_id)}`, { method: 'DELETE' });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                alert(data.detail || '删除会话失败');
                return;
            }
            this.removeAdminConversation(c);
            await this.loadAdminConversations();
        },
        scrollAdminMessages() {
            this.$nextTick(() => {
                if (this.$refs.adminMessagesContainer) {
                    this.$refs.adminMessagesContainer.scrollTop = this.$refs.adminMessagesContainer.scrollHeight;
                }
            });
        },
        async loadUsers() {
            if (!this.isAdmin) return;
            const keyword = this.userKeyword.trim();
            const query = keyword ? `?keyword=${encodeURIComponent(keyword)}` : '';
            const response = await this.authFetch(`/admin/users${query}`);
            const data = await response.json();
            if (!response.ok) {
                alert(data.detail || '加载用户失败');
                return;
            }
            this.users = data.users || [];
        },
        async clearUserSearch() {
            this.userKeyword = '';
            await this.loadUsers();
        },
        resetUserForm() {
            this.editingUserId = null;
            this.userForm = { username: '', password: '', role: 'user' };
        },
        editUser(user) {
            this.editingUserId = user.id;
            this.userForm = {
                username: user.username,
                password: '',
                role: user.role || 'user'
            };
            this.focusField('user-name');
        },
        async saveUser() {
            if (!this.isAdmin) return;
            const username = this.userForm.username.trim();
            const password = this.userForm.password.trim();
            if (!username) {
                alert('请输入用户名');
                return;
            }
            if (!this.editingUserId && !password) {
                alert('新增用户需要设置密码');
                return;
            }

            this.userSaving = true;
            try {
                const payload = {
                    username,
                    role: this.userForm.role
                };
                if (password) payload.password = password;
                const url = this.editingUserId ? `/admin/users/${this.editingUserId}` : '/admin/users';
                const response = await this.authFetch(url, {
                    method: this.editingUserId ? 'PUT' : 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(data.detail || '保存用户失败');
                await this.loadUsers();
                this.resetUserForm();
            } catch (error) {
                alert(error.message);
            } finally {
                this.userSaving = false;
            }
        },
        async deleteUser(user) {
            if (!this.isAdmin) return;
            if (!confirm(`确定删除用户 ${user.username} 吗？该用户的会话记录也会一并删除。`)) return;
            const response = await this.authFetch(`/admin/users/${user.id}`, { method: 'DELETE' });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                alert(data.detail || '删除用户失败');
                return;
            }
            if (this.editingUserId === user.id) this.resetUserForm();
            await this.loadUsers();
            await this.loadAdminConversations();
        },
        async loadProducts() {
            if (!this.isAdmin) return;
            const keyword = this.productKeyword.trim();
            const query = keyword ? `?keyword=${encodeURIComponent(keyword)}` : '';
            const response = await this.authFetch(`/admin/products${query}`);
            const data = await response.json();
            if (!response.ok) {
                alert(data.detail || '加载商品失败');
                return;
            }
            this.products = data.products || [];
        },
        async clearProductSearch() {
            this.productKeyword = '';
            await this.loadProducts();
        },
        resetProductForm() {
            this.editingProductId = null;
            this.productForm = {
                name: '',
                category: '',
                price: '',
                stock: 0,
                material: '',
                specifications: '',
                image_url: ''
            };
            this.selectedProductImage = null;
            this.productNotice = '';
            if (this.$refs.productImageInput) this.$refs.productImageInput.value = '';
        },
        editProduct(product) {
            this.editingProductId = product.id;
            this.productForm = {
                name: product.name || '',
                category: product.category || '',
                price: product.price || '',
                stock: product.stock ?? 0,
                material: product.material || '',
                specifications: product.specifications || '',
                image_url: product.image_url || ''
            };
            this.selectedProductImage = null;
            this.productNotice = '';
            if (this.$refs.productImageInput) this.$refs.productImageInput.value = '';
            this.focusField('product-name');
        },
        handleProductImageSelect(event) {
            this.selectedProductImage = event.target.files?.[0] || null;
        },
        async saveProduct() {
            if (!this.isAdmin) return;
            const name = this.productForm.name.trim();
            if (!name) {
                alert('请输入商品名');
                return;
            }

            this.productSaving = true;
            this.productNotice = this.editingProductId ? '正在更新商品...' : '正在创建商品...';
            try {
                const formData = new FormData();
                formData.append('name', name);
                formData.append('category', this.productForm.category.trim());
                formData.append('price', this.productForm.price.trim());
                formData.append('stock', String(this.productForm.stock || 0));
                formData.append('material', this.productForm.material.trim());
                formData.append('specifications', this.productForm.specifications.trim());
                if (this.selectedProductImage) {
                    formData.append('image', this.selectedProductImage);
                }
                const url = this.editingProductId ? `/admin/products/${this.editingProductId}` : '/admin/products';
                const response = await this.authFetch(url, {
                    method: this.editingProductId ? 'PUT' : 'POST',
                    body: formData
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(data.detail || '保存商品失败');
                this.productNotice = data.message || '商品已保存';
                await this.loadProducts();
                this.resetProductForm();
            } catch (error) {
                this.productNotice = error.message;
            } finally {
                this.productSaving = false;
            }
        },
        async deleteProduct(product) {
            if (!this.isAdmin) return;
            if (!confirm(`确定删除商品 ${product.name} 吗？`)) return;
            const response = await this.authFetch(`/admin/products/${product.id}`, { method: 'DELETE' });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                alert(data.detail || '删除商品失败');
                return;
            }
            if (this.editingProductId === product.id) this.resetProductForm();
            await this.loadProducts();
        },
        async loadDocuments() {
            if (!this.isAdmin) return;
            const response = await this.authFetch('/documents');
            const data = await response.json();
            if (!response.ok) {
                alert(data.detail || '加载文档失败');
                return;
            }
            this.documents = data.documents || [];
        },
        async loadRagPipeline() {
            if (!this.isAdmin) return;
            const response = await this.authFetch('/admin/rag/pipeline');
            const data = await response.json();
            if (!response.ok) {
                alert(data.detail || '加载索引链路失败');
                return;
            }
            this.ragPipeline = data;
        },
        async refreshKnowledge() {
            await Promise.all([this.loadDocuments(), this.loadRagPipeline()]);
        },
        async runRagDiagnostic() {
            const query = this.ragTestQuery.trim();
            if (!query) return;
            this.ragTestLoading = true;
            this.ragTestResult = null;
            try {
                const response = await this.authFetch('/admin/rag/diagnose', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message: query, session_id: 'admin_rag_diagnostic' })
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || '测试检索失败');
                this.ragTestResult = data;
            } catch (error) {
                this.ragTestResult = {
                    query,
                    meta: { retrieval_mode: 'failed', retrieval_error: error.message },
                    chunks: []
                };
            } finally {
                this.ragTestLoading = false;
            }
        },
        formatBool(value) {
            return value ? '开启' : '关闭';
        },
        hasRagDetails(message) {
            return !!((message.rag_steps && message.rag_steps.length) || message.rag_trace);
        },
        traceChunks(trace) {
            if (!trace) return [];
            return trace.retrieved_chunks || trace.expanded_retrieved_chunks || trace.initial_retrieved_chunks || [];
        },
        formatScore(value) {
            if (value === null || value === undefined || value === '') return '-';
            const number = Number(value);
            return Number.isFinite(number) ? number.toFixed(4) : String(value);
        },
        chunkPreview(text) {
            const normalized = (text || '').replace(/\s+/g, ' ').trim();
            return normalized.length > 220 ? `${normalized.slice(0, 220)}...` : normalized;
        },
        handleFileSelect(event) {
            this.selectedFile = event.target.files?.[0] || null;
            this.uploadProgress = '';
        },
        async uploadDocument() {
            if (!this.isAdmin) return;
            if (!this.selectedFile) return;
            this.isUploading = true;
            this.uploadProgress = '正在上传并处理文档...';
            try {
                const formData = new FormData();
                formData.append('file', this.selectedFile);
                const response = await this.authFetch('/documents/upload', { method: 'POST', body: formData });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || '上传失败');
                this.uploadProgress = data.message;
                this.selectedFile = null;
                if (this.$refs.fileInput) this.$refs.fileInput.value = '';
                await this.refreshKnowledge();
            } catch (error) {
                this.uploadProgress = error.message;
            } finally {
                this.isUploading = false;
            }
        },
        async deleteDocument(filename) {
            if (!this.isAdmin) return;
            if (!confirm(`确定删除文档 ${filename} 吗？`)) return;
            const response = await this.authFetch(`/documents/${encodeURIComponent(filename)}`, { method: 'DELETE' });
            const data = await response.json();
            if (!response.ok) {
                alert(data.detail || '删除失败');
                return;
            }
            await this.refreshKnowledge();
        }
    }
}).mount('#app');
