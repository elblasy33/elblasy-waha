import { Component, markup, onMounted, onWillDestroy, onWillStart, proxy, signal, useProps } from "@odoo/owl";
import { useEmojiPicker } from "@web/core/emoji_picker/emoji_picker";
import { deserializeDateTime } from "@web/core/l10n/dates";
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_plugin";

const { DateTime } = luxon;
const MODEL = "elblasy.waha.conversation";
const READ_MORE_CHARS = 700;
const RTL_FIRST_STRONG = /^[^A-Za-z\u00C0-\u024F\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]*[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]/;
const AUTHOR_COLORS = ["#06cf9c", "#e542a3", "#53bdeb", "#ffd279", "#a5b337", "#fc9775", "#7f66ff", "#1f7aec"];
const AVATAR_COLORS = ["#25d366", "#00a884", "#53bdeb", "#7f66ff", "#ff7a59", "#f15c6d", "#ffbc38", "#009de2"];

/** Unique per click. crypto.randomUUID only exists on HTTPS/localhost, plain-HTTP installs need a fallback. */
function newClientToken() {
    if (globalThis.crypto?.randomUUID) {
        return globalThis.crypto.randomUUID();
    }
    const random = new Uint32Array(4);
    globalThis.crypto.getRandomValues(random);
    return Array.from(random, (n) => n.toString(16).padStart(8, "0")).join("-");
}

function escapeHtml(text) {
    return text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

/** WhatsApp text formatting on already escaped text: links, *bold*, _italic_, ~strike~, ```mono```. */
export function formatWhatsappText(text) {
    let html = escapeHtml(text || "");
    html = html.replace(/```([\s\S]+?)```/g, "<code>$1</code>");
    html = html.replace(
        /\bhttps?:\/\/[^\s<]+[^\s<.,;:!?)\]'"]/g,
        (url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`
    );
    html = html.replace(/(^|[\s(])\*([^*\n]+)\*(?=$|[\s).,!?:;])/g, "$1<b>$2</b>");
    html = html.replace(/(^|[\s(])_([^_\n]+)_(?=$|[\s).,!?:;])/g, "$1<i>$2</i>");
    html = html.replace(/(^|[\s(])~([^~\n]+)~(?=$|[\s).,!?:;])/g, "$1<s>$2</s>");
    return markup(html);
}

// Odoo 20 icons (Material Symbols names, see web/icons.py)
const PREVIEW = {
    image: ["photo_camera", _t("Photo")],
    video: ["videocam", _t("Video")],
    audio: ["mic", _t("Audio")],
    document: ["description", _t("Document")],
    sticker: ["sticky_note_2", _t("Sticker")],
    location: ["location_on", _t("Location")],
    contact: ["person", _t("Contact")],
    poll: ["bar_chart", _t("Poll")],
    other: ["chat_bubble", _t("Message")],
};
const TICK_ICONS = { pending: "schedule", failed: "error", unknown: "help" };

export class WahaInbox extends Component {
    static template = "elblasy_waha_connector.Inbox";
    props = useProps(standardActionServiceProps);
    threadRef = signal.ref();
    fileRef = signal.ref();
    composerRef = signal.ref();
    emojiRef = signal.ref();
    reactRef = signal.ref();
    rootRef = signal.ref();

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.bus = useService("bus_service");
        this.state = proxy({
            sessions: [],
            sessionId: 0,
            search: "",
            filter: "all", // all | unread | groups | archived | label
            archivedCount: 0,
            historyDone: false,
            downloading: {},
            menuFor: 0, // message whose action menu is open
            replyTo: null,
            editing: null,
            dialog: null, // { kind: 'delete' | 'forward' | 'info', message }
            forwardIds: [],
            forwardSearch: "",
            info: null, // contact info drawer data
            infoView: "main", // main | media | docs | links | search
            infoLoading: false,
            notesDraft: null,
            searchResults: [],
            flashId: 0,
            playing: 0,
            audio: {}, // message id -> { progress, time }
            uiRtl: false, // the layout direction really applied (Odoo.sh mirrors Arabic with rtlcss)
            conversations: [],
            current: null,
            messages: [],
            hasMore: false,
            loadingOlder: false,
            draft: "",
            sending: false,
            labels: [],
            labelFilter: 0,
            labelsOpen: false,
            expanded: {},
        });
        this.reactPicker = useEmojiPicker(
            this.reactRef,
            { onSelect: (codepoints) => this.react(this._reactTarget, codepoints) },
            { position: "bottom-start" }
        );
        this.emojiPicker = useEmojiPicker(
            this.emojiRef,
            { onSelect: (codepoints) => this.insertText(codepoints) },
            { position: "top-start" }
        );
        onWillStart(async () => {
            this.state.sessions = await this.orm.call(MODEL, "inbox_sessions", []);
            await this.loadLabels();
            await this.loadConversations();
        });
        onMounted(() => {
            // opened from a report, a contact...: show that chat directly
            const convId = this.props.action?.context?.waha_conversation_id;
            if (convId) {
                this.openById(convId);
            }
        });
        onMounted(() => {
            // mirror the phone's chat list in the background, then every minute while the inbox is open
            this.refresh();
            this._refreshTimer = setInterval(() => this.refresh(), 60000);
        });
        const onMessage = (payload) => this.onBusMessage(payload);
        const onSession = async () => {
            this.state.sessions = await this.orm.call(MODEL, "inbox_sessions", []);
        };
        const closeMenu = (ev) => {
            if (this.state.menuFor && !ev.target.closest?.(".o_waha_msg_menu, .o_waha_msg_menu_btn")) {
                this.state.menuFor = 0;
            }
        };
        onMounted(() => {
            document.addEventListener("click", closeMenu, true);
            const root = this.rootRef();
            this.state.uiRtl = !!root && getComputedStyle(root).direction === "rtl";
        });
        onWillDestroy(() => document.removeEventListener("click", closeMenu, true));
        onMounted(() => {
            this.bus.subscribe("elblasy_waha/message", onMessage);
            this.bus.subscribe("elblasy_waha/session", onSession);
            this.bus.start?.();
        });
        onWillDestroy(() => {
            this.bus.unsubscribe?.("elblasy_waha/message", onMessage);
            this.bus.unsubscribe?.("elblasy_waha/session", onSession);
            clearTimeout(this._searchTimer);
            clearInterval(this._refreshTimer);
        });
    }

    // ------------------------------------------------------------------ data
    async loadConversations() {
        this.state.conversations = await this.orm.call(MODEL, "inbox_conversations", [], {
            session_id: this.state.sessionId || null,
            search: this.state.search || null,
            label_id: (this.state.filter === "label" && this.state.labelFilter) || null,
            unread: this.state.filter === "unread",
            kind: ["groups", "archived"].includes(this.state.filter) ? this.state.filter : null,
        });
        this.state.archivedCount = await this.orm.call(MODEL, "inbox_archived_count", [], {
            session_id: this.state.sessionId || null,
        });
        const current = this.state.current;
        if (current) {
            const fresh = this.state.conversations.find((c) => c.id === current.id);
            if (fresh) {
                Object.assign(current, fresh, { unread: 0 });
            }
        }
    }

    async refresh() {
        try {
            if (await this.orm.silent.call(MODEL, "inbox_refresh", [], { session_id: this.state.sessionId || null })) {
                await this.loadConversations();
            }
        } catch {
            // WAHA unreachable: the list keeps what Odoo has
        }
    }

    async loadLabels() {
        this.state.labels = await this.orm.call(MODEL, "inbox_labels", [], {
            session_id: this.state.sessionId || null,
        });
    }

    async loadMessages({ keepScroll = false } = {}) {
        const el = this.threadRef();
        const nearBottom = !el || el.scrollHeight - el.scrollTop - el.clientHeight < 120;
        const res = await this.orm.call(MODEL, "inbox_messages", [[this.state.current.id]]);
        this.state.messages = res.messages;
        this.state.hasMore = res.has_more;
        if (!keepScroll || nearBottom) {
            this.scrollBottom();
        }
        this.autoDownload();
    }

    async loadOlder() {
        const first = this.state.messages[0];
        if (!first || this.state.loadingOlder) {
            return;
        }
        this.state.loadingOlder = true;
        const el = this.threadRef();
        const previousHeight = el ? el.scrollHeight : 0;
        try {
            const res = await this.orm.call(MODEL, "inbox_messages", [[this.state.current.id]], {
                before_id: first.id,
            });
            this.state.messages = [...res.messages, ...this.state.messages];
            this.state.hasMore = res.has_more;
            this.autoDownload();
            setTimeout(() => {
                if (el) {
                    el.scrollTop = el.scrollHeight - previousHeight; // stay on the message that was on top
                }
            }, 0);
        } finally {
            this.state.loadingOlder = false;
        }
    }

    async onBusMessage({ conversation_id }) {
        await this.loadConversations();
        if (this.state.current && this.state.current.id === conversation_id) {
            await this.loadMessages({ keepScroll: true });
            await this.orm.call(MODEL, "inbox_mark_read", [[conversation_id]]);
        }
    }

    // ------------------------------------------------------------------ list filters
    async onSessionChange(ev) {
        this.state.sessionId = parseInt(ev.target.value) || 0;
        this.state.labelFilter = 0;
        if (this.state.filter === "label") {
            this.state.filter = "all";
        }
        await this.loadLabels();
        return this.loadConversations();
    }

    onChipsWheel(ev) {
        if (Math.abs(ev.deltaY) > Math.abs(ev.deltaX)) {
            ev.currentTarget.scrollLeft += this.state.uiRtl ? -ev.deltaY : ev.deltaY;
            ev.preventDefault();
        }
    }

    setFilter(filter, labelId = 0) {
        this.state.filter = filter;
        this.state.labelFilter = labelId;
        return this.loadConversations();
    }

    isFilter(filter, labelId = 0) {
        return this.state.filter === filter && (filter !== "label" || this.state.labelFilter === labelId);
    }

    onSearchInput(ev) {
        this.state.search = ev.target.value;
        clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(() => this.loadConversations(), 300);
    }

    // ------------------------------------------------------------------ conversation
    async openConversation(conv) {
        this.closeInfo();
        this.state.current = conv;
        this.state.messages = [];
        this.state.labelsOpen = false;
        this.state.expanded = {};
        this.state.historyDone = false;
        await this.loadMessages();
        if (conv.unread) {
            await this.orm.call(MODEL, "inbox_mark_read", [[conv.id]]);
            conv.unread = 0;
        }
        this.composerRef()?.focus();
        // messages that never reached Odoo (sent before the webhook, or while it was down) come from WhatsApp
        const imported = await this.orm.silent.call(MODEL, "inbox_fetch_history", [[conv.id]]).catch(() => 0);
        if (imported && this.state.current?.id === conv.id) {
            await this.loadMessages({ keepScroll: true });
        }
    }

    /** Scrolled to the top of what Odoo has: ask WhatsApp for older messages, like WhatsApp Web. */
    async loadOlderFromWhatsapp() {
        const conv = this.state.current;
        const first = this.state.messages[0];
        if (!conv || !first || this.state.historyDone || this.state.loadingOlder) {
            return;
        }
        this.state.loadingOlder = true;
        let imported = 0;
        try {
            imported = await this.orm.silent.call(MODEL, "inbox_fetch_history", [[conv.id]], { before: first.date });
        } catch {
            imported = 0;
        } finally {
            this.state.loadingOlder = false;
        }
        if (imported) {
            this.state.hasMore = true;
            await this.loadOlder();
        } else {
            this.state.historyDone = true;
        }
    }

    async downloadMedia(m, { silent = false } = {}) {
        if (this.state.downloading[m.id]) {
            return;
        }
        this.state.downloading[m.id] = true;
        try {
            const fresh = await this.orm.call(MODEL, "inbox_download_media", [[this.state.current.id]], {
                message_id: m.id,
            });
            Object.assign(m, fresh);
        } catch (error) {
            if (!silent) {
                this.notification.add(error?.data?.message || _t("The file could not be downloaded."), {
                    type: "danger",
                });
            }
        } finally {
            delete this.state.downloading[m.id];
        }
    }

    async openById(convId) {
        const listed = this.state.conversations.find((c) => c.id === convId);
        const conv = listed || (await this.orm.call(MODEL, "inbox_conversation", [[convId]]));
        await this.openConversation(conv);
    }

    // ------------------------------------------------------------------ media like WhatsApp
    /** Pictures, stickers and voice notes within the media policy load by themselves, like on the phone. */
    async autoDownload() {
        if (this._autoRunning) {
            return;
        }
        this._autoRunning = true;
        try {
            const conv = this.state.current;
            const queue = this.state.messages.filter((m) => m.auto && !m.attachments.length).slice(-15).reverse();
            for (const m of queue) {
                if (this.state.current !== conv) {
                    break;
                }
                m.auto = false; // one attempt: a failure leaves the download card
                await this.downloadMedia(m, { silent: true });
            }
        } finally {
            this._autoRunning = false;
        }
    }

    audioAttachment(m) {
        return m.attachments.find((a) => (a.mimetype || "").startsWith("audio/"));
    }

    isVoiceBubble(m) {
        return !m.revoked && (m.type === "audio" || !!this.audioAttachment(m));
    }

    async togglePlay(m) {
        if (!this.audioAttachment(m)) {
            await this.downloadMedia(m);
            if (!this.audioAttachment(m)) {
                return;
            }
            await new Promise((resolve) => setTimeout(resolve, 50));
        }
        const el = document.getElementById(`o_waha_audio_${m.id}`);
        if (!el) {
            return;
        }
        if (this.state.playing && this.state.playing !== m.id) {
            document.getElementById(`o_waha_audio_${this.state.playing}`)?.pause();
        }
        if (el.paused) {
            await el.play();
            this.state.playing = m.id;
        } else {
            el.pause();
            this.state.playing = 0;
        }
    }

    onAudioTime(m, ev) {
        const el = ev.target;
        this.state.audio[m.id] = {
            progress: el.duration ? (100 * el.currentTime) / el.duration : 0,
            time: el.currentTime,
        };
    }

    onAudioEnded(m) {
        this.state.playing = 0;
        this.state.audio[m.id] = { progress: 0, time: 0 };
    }

    seekAudio(m, ev) {
        const el = document.getElementById(`o_waha_audio_${m.id}`);
        if (el && el.duration) {
            const rect = ev.currentTarget.getBoundingClientRect();
            const ratio = (ev.clientX - rect.left) / rect.width;
            el.currentTime = el.duration * Math.min(Math.max(this.state.uiRtl ? 1 - ratio : ratio, 0), 1);
        }
    }

    clock(seconds) {
        const total = Math.max(Math.round(seconds || 0), 0);
        return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
    }

    voiceTime(m) {
        const state = this.state.audio[m.id];
        return this.clock(state && state.time ? state.time : m.duration);
    }

    // ------------------------------------------------------------------ contact info drawer
    async openInfo(view = "main") {
        if (!this.state.current) {
            return;
        }
        this.state.infoView = view;
        this.state.infoLoading = true;
        this.state.notesDraft = null;
        try {
            this.state.info = await this.orm.call(MODEL, "inbox_contact_info", [[this.state.current.id]]);
        } finally {
            this.state.infoLoading = false;
        }
        if (view === "search") {
            setTimeout(() => document.querySelector(".o_waha_info_search input")?.focus(), 50);
        }
    }

    closeInfo() {
        this.state.info = null;
        this.state.infoView = "main";
        this.state.searchResults = [];
    }

    setInfoView(view) {
        this.state.infoView = view;
    }

    startNotes() {
        this.state.notesDraft = this.state.info.notes || "";
    }

    async saveNotes() {
        const notes = this.state.notesDraft;
        await this.orm.call(MODEL, "inbox_set_notes", [[this.state.current.id]], { notes });
        this.state.info.notes = (notes || "").trim();
        this.state.notesDraft = null;
    }

    async toggleArchive() {
        const archive = !this.state.info.archived;
        try {
            await this.orm.call(MODEL, "inbox_archive", [[this.state.current.id]], { archive });
            this.state.info.archived = archive;
            this.notification.add(archive ? _t("Chat archived") : _t("Chat unarchived"), { type: "success" });
            await this.loadConversations();
        } catch (error) {
            this.notification.add(error?.data?.message || _t("WhatsApp refused the action."), { type: "danger" });
        }
    }

    async markUnread() {
        try {
            await this.orm.call(MODEL, "inbox_mark_unread", [[this.state.current.id]]);
            this.closeInfo();
            this.closeConversation();
            await this.loadConversations();
        } catch (error) {
            this.notification.add(error?.data?.message || _t("WhatsApp refused the action."), { type: "danger" });
        }
    }

    onSearchMessages(ev) {
        const query = ev.target.value;
        clearTimeout(this._msgSearchTimer);
        this._msgSearchTimer = setTimeout(async () => {
            this.state.searchResults = query.trim().length < 2 ? [] : await this.orm.call(
                MODEL, "inbox_search", [[this.state.current.id]], { query }
            );
        }, 300);
    }

    /** Show a message in the conversation (search result, link...) and highlight it. */
    async jumpTo(messageId) {
        if (!this.state.messages.some((m) => m.id === messageId)) {
            const res = await this.orm.call(MODEL, "inbox_messages", [[this.state.current.id]], { around_id: messageId });
            this.state.messages = res.messages;
            this.state.hasMore = res.has_more;
        }
        this.state.flashId = messageId;
        setTimeout(() => {
            document.getElementById(`o_waha_msg_${messageId}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
        }, 60);
        setTimeout(() => {
            if (this.state.flashId === messageId) {
                this.state.flashId = 0;
            }
        }, 2500);
    }

    openDocuments(doc) {
        return this.action.doAction({
            type: "ir.actions.act_window", name: doc.name, res_model: doc.model,
            views: [[false, "list"], [false, "form"]], domain: doc.domain,
        });
    }

    partnerLine(partner) {
        return [partner.company, partner.email, partner.phone].filter((part) => part).join(" · ");
    }

    roleLabel(role) {
        return { admin: _t("Admin"), superadmin: _t("Owner") }[role] || "";
    }

    closeConversation() {
        this.state.current = null;
        this.state.messages = [];
    }

    onThreadScroll(ev) {
        if (ev.target.scrollTop >= 80 || this.state.loadingOlder) {
            return;
        }
        if (this.state.hasMore) {
            this.loadOlder();
        } else {
            this.loadOlderFromWhatsapp();
        }
    }

    scrollBottom() {
        setTimeout(() => {
            const el = this.threadRef();
            if (el) {
                el.scrollTop = el.scrollHeight;
            }
        }, 0);
    }

    toggleLabelsMenu() {
        this.state.labelsOpen = !this.state.labelsOpen;
    }

    get currentSessionLabels() {
        const current = this.state.current;
        return current ? this.state.labels.filter((l) => l.session_id === current.session_id) : [];
    }

    isLabelOn(label) {
        return !!this.state.current && this.state.current.labels.some((l) => l.id === label.id);
    }

    async toggleLabel(label) {
        try {
            this.state.current.labels = await this.orm.call(MODEL, "inbox_toggle_label", [[this.state.current.id]], {
                label_id: label.id,
            });
            await this.loadConversations();
        } catch (error) {
            this.notification.add(error?.data?.message || _t("The label could not be changed."), {
                type: "danger",
            });
        }
    }

    async createPartner() {
        const conv = this.state.current;
        const partnerId = await this.orm.call(MODEL, "action_create_partner", [[conv.id]]);
        conv.partner_id = partnerId;
        await this.loadConversations();
        if (this.state.info) {
            await this.openInfo(this.state.infoView);
        }
    }

    openPartner() {
        return this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "res.partner",
            res_id: this.state.current.partner_id,
            views: [[false, "form"]],
        });
    }

    get sessionDisconnected() {
        const s = this.state.current && this.state.sessions.find((x) => x.id === this.state.current.session_id);
        return !!s && s.status !== "WORKING";
    }

    // ------------------------------------------------------------------ composer
    insertText(text) {
        const el = this.composerRef();
        if (!el) {
            this.state.draft += text;
            return;
        }
        const start = el.selectionStart ?? this.state.draft.length;
        const end = el.selectionEnd ?? start;
        this.state.draft = this.state.draft.slice(0, start) + text + this.state.draft.slice(end);
        el.value = this.state.draft;
        el.focus();
        el.setSelectionRange(start + text.length, start + text.length);
        this.autoGrow(el);
    }

    autoGrow(el) {
        el.style.height = "auto";
        el.style.height = Math.min(el.scrollHeight, 140) + "px";
    }

    onDraftInput(ev) {
        this.state.draft = ev.target.value;
        this.autoGrow(ev.target);
    }

    onDraftKeydown(ev) {
        if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
            ev.preventDefault();
            this.send();
        }
    }

    resetComposer() {
        this.state.draft = "";
        const el = this.composerRef();
        if (el) {
            el.value = "";
            el.style.height = "auto";
        }
    }

    onAttachClick() {
        this.fileRef()?.click();
    }

    async onFileChange(ev) {
        const file = ev.target.files && ev.target.files[0];
        ev.target.value = ""; // allows choosing the same file again
        if (!file || this.state.sending || !this.state.current) {
            return;
        }
        if (file.size > 15 * 1024 * 1024) {
            this.notification.add(_t("The file is too large (maximum 15 MB)."), { type: "danger" });
            return;
        }
        this.state.sending = true;
        try {
            const data = await new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onload = () => resolve(reader.result.split(",")[1]);
                reader.onerror = reject;
                reader.readAsDataURL(file);
            });
            await this.orm.call(MODEL, "inbox_send_file", [[this.state.current.id]], {
                filename: file.name,
                mimetype: file.type || "",
                data_b64: data,
                caption: this.state.draft.trim() || null,
                client_token: newClientToken(),
            });
            this.resetComposer();
            await this.loadMessages();
            await this.loadConversations();
        } catch (error) {
            this.notification.add(error?.data?.message || _t("The file could not be sent."), {
                type: "danger",
            });
        } finally {
            this.state.sending = false;
        }
    }

    async send() {
        const text = this.state.draft.trim();
        if (!text || this.state.sending || !this.state.current) {
            return;
        }
        if (this.state.editing) {
            return this.saveEdit(text);
        }
        this.state.sending = true;
        // one token per click: a retried/duplicated request cannot create two messages
        const token = newClientToken();
        try {
            await this.orm.call(MODEL, "inbox_send_text", [[this.state.current.id]], {
                text,
                client_token: token,
                reply_to_id: this.state.replyTo?.id || null,
            });
            this.state.replyTo = null;
            this.resetComposer();
            await this.loadMessages();
            await this.loadConversations();
        } catch (error) {
            this.notification.add(error?.data?.message || _t("The message could not be sent."), {
                type: "danger",
            });
        } finally {
            this.state.sending = false;
        }
    }

    // ------------------------------------------------------------------ message menu (like WhatsApp Web)
    get quickReactions() {
        return ["👍", "❤️", "😂", "😮", "😢", "🙏"];
    }

    toggleMenu(m) {
        this.state.menuFor = this.state.menuFor === m.id ? 0 : m.id;
        this._reactTarget = m;
    }

    async messageCall(method, m, kwargs = {}, done = "") {
        this.state.menuFor = 0;
        try {
            const result = await this.orm.call(MODEL, method, [[this.state.current.id]], { message_id: m.id, ...kwargs });
            if (result && typeof result === "object" && result.id === m.id) {
                Object.assign(m, result);
            }
            if (done) {
                this.notification.add(done, { type: "success" });
            }
            return result;
        } catch (error) {
            this.notification.add(error?.data?.message || _t("WhatsApp refused the action."), { type: "danger" });
        }
    }

    react(m, emoji) {
        if (!m) {
            return;
        }
        const mine = m.reactions.find((r) => r.mine);
        // clicking my current reaction again removes it, like WhatsApp
        return this.messageCall("inbox_react", m, { emoji: mine && mine.emoji === emoji ? "" : emoji });
    }

    startReply(m) {
        this.state.menuFor = 0;
        this.state.editing = null;
        this.state.replyTo = m;
        this.composerRef()?.focus();
    }

    startEdit(m) {
        this.state.menuFor = 0;
        this.state.replyTo = null;
        this.state.editing = m;
        this.state.draft = m.body;
        const el = this.composerRef();
        if (el) {
            el.value = m.body;
            el.focus();
            this.autoGrow(el);
        }
    }

    cancelComposerMode() {
        if (this.state.editing) {
            this.resetComposer();
        }
        this.state.replyTo = null;
        this.state.editing = null;
    }

    async saveEdit(text) {
        const m = this.state.editing;
        this.state.sending = true;
        try {
            await this.messageCall("inbox_edit", m, { text });
            this.state.editing = null;
            this.resetComposer();
        } finally {
            this.state.sending = false;
        }
    }

    async copyText(m) {
        this.state.menuFor = 0;
        try {
            await navigator.clipboard.writeText(m.body || "");
            this.notification.add(_t("Copied"), { type: "success" });
        } catch {
            this.notification.add(_t("The browser did not allow copying."), { type: "warning" });
        }
    }

    toggleStar(m) {
        return this.messageCall("inbox_star", m, { star: !m.starred });
    }

    togglePin(m) {
        return this.messageCall("inbox_pin", m, { pin: !m.pinned });
    }

    addNote(m) {
        return this.messageCall("inbox_add_note", m, {}, _t("Added to the contact's notes."));
    }

    openDialog(kind, m) {
        this.state.menuFor = 0;
        this.state.forwardIds = [];
        this.state.forwardSearch = "";
        this.state.dialog = { kind, message: m };
    }

    closeDialog() {
        this.state.dialog = null;
    }

    async deleteMessage(scope) {
        const m = this.state.dialog.message;
        this.closeDialog();
        const ok = await this.messageCall("inbox_delete", m, { scope });
        if (ok) {
            await this.loadMessages({ keepScroll: true });
            await this.loadConversations();
        }
    }

    get forwardTargets() {
        const current = this.state.current;
        const search = this.state.forwardSearch.toLowerCase();
        return this.state.conversations.filter(
            (c) =>
                c.id !== current.id &&
                c.session_id === current.session_id &&
                (!search || (c.name || "").toLowerCase().includes(search) || (c.phone || "").includes(search))
        );
    }

    toggleForward(conv) {
        const ids = this.state.forwardIds;
        const index = ids.indexOf(conv.id);
        if (index >= 0) {
            ids.splice(index, 1);
        } else if (ids.length < 5) {
            ids.push(conv.id);
        }
    }

    async forward() {
        const m = this.state.dialog.message;
        const ids = [...this.state.forwardIds];
        this.closeDialog();
        const count = await this.messageCall("inbox_forward", m, { conversation_ids: ids });
        if (count) {
            this.notification.add(_t("Forwarded to %s chat(s).", count), { type: "success" });
            await this.loadConversations();
        }
    }

    get pinned() {
        return [...this.state.messages].reverse().find((m) => m.pinned && !m.revoked);
    }

    scrollToMessage(m) {
        document.getElementById(`o_waha_msg_${m.id}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    }

    statusText(m) {
        return this.tickTitle(m.status) || (m.direction === "in" ? _t("Received") : "");
    }

    fullDate(value) {
        const dt = this.toLocal(value);
        return dt ? dt.toLocaleString(DateTime.DATETIME_MED) : "";
    }

    // ------------------------------------------------------------------ display helpers
    displayName(conv) {
        const name = conv.name || "";
        return /^\d{7,}$/.test(name) ? "+" + name : name;
    }

    authorColor(sender) {
        let hash = 0;
        for (const char of sender || "") {
            hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
        }
        return AUTHOR_COLORS[hash % AUTHOR_COLORS.length];
    }

    pendingIcon(m) {
        return (PREVIEW[m.type] || PREVIEW.document)[0];
    }

    pendingLabel(m) {
        return (PREVIEW[m.type] || PREVIEW.document)[1];
    }

    initials(name) {
        const words = (name || "?").replace(/[^\p{L}\p{N} ]/gu, " ").trim().split(/\s+/);
        return ((words[0]?.[0] || "?") + (words.length > 1 ? words[words.length - 1][0] : "")).toUpperCase();
    }

    avatarColor(id) {
        return AVATAR_COLORS[id % AVATAR_COLORS.length];
    }

    toLocal(value) {
        return value ? deserializeDateTime(value) : null;
    }

    listDate(value) {
        const dt = this.toLocal(value);
        if (!dt) {
            return "";
        }
        const today = DateTime.local().startOf("day");
        const day = dt.startOf("day");
        const days = Math.round(today.diff(day, "days").days);
        if (days <= 0) {
            return dt.toLocaleString(DateTime.TIME_SIMPLE);
        }
        if (days === 1) {
            return _t("Yesterday");
        }
        if (days < 7) {
            return dt.toFormat("cccc");
        }
        return dt.toLocaleString(DateTime.DATE_SHORT);
    }

    dayLabel(value) {
        const dt = this.toLocal(value);
        const days = Math.round(DateTime.local().startOf("day").diff(dt.startOf("day"), "days").days);
        if (days <= 0) {
            return _t("Today");
        }
        if (days === 1) {
            return _t("Yesterday");
        }
        if (days < 7) {
            return dt.toFormat("cccc");
        }
        return dt.toLocaleString(DateTime.DATE_MED);
    }

    timeOf(value) {
        const dt = this.toLocal(value);
        return dt ? dt.toLocaleString(DateTime.TIME_SIMPLE) : "";
    }

    /** Messages with the day separators and the "first of a group" flag (bubble tail), like WhatsApp. */
    get rows() {
        const rows = [];
        let previousDay = null;
        let previous = null;
        for (const m of this.state.messages) {
            const day = this.toLocal(m.date).toISODate();
            if (day !== previousDay) {
                rows.push({ key: "day-" + day, day: this.dayLabel(m.date) });
                previousDay = day;
                previous = null;
            }
            const first = !previous || previous.direction !== m.direction || previous.sender !== m.sender;
            rows.push({ key: m.id, message: m, first });
            previous = m;
        }
        return rows;
    }

    preview(conv) {
        if (conv.last_revoked) {
            return { icon: "block", text: _t("This message was deleted") };
        }
        const type = conv.last_type;
        if (!type || type === "text") {
            return { icon: "", text: conv.last_body || conv.last_message || "" };
        }
        const [icon, label] = PREVIEW[type] || PREVIEW.other;
        const body = conv.last_body && type !== "contact" && type !== "poll" ? conv.last_body : "";
        return { icon, text: body || label };
    }

    /** Sent / delivered / read are drawn in CSS (grey or blue checks); the other states use an icon. */
    tickClass(status) {
        return (
            {
                pending: "oi o_waha_tick_icon",
                sent: "o_waha_tick o_waha_tick_single",
                delivered: "o_waha_tick",
                read: "o_waha_tick o_waha_tick_read",
                failed: "oi o_waha_tick_icon text-danger",
                unknown: "oi o_waha_tick_icon",
            }[status] || ""
        );
    }

    tickIcon(status) {
        return TICK_ICONS[status];
    }

    tickTitle(status) {
        return (
            {
                pending: _t("Sending"),
                sent: _t("Sent"),
                delivered: _t("Delivered"),
                read: _t("Read"),
                failed: _t("Failed"),
                unknown: _t("Not confirmed"),
            }[status] || ""
        );
    }

    isLong(m) {
        return m.body.length > READ_MORE_CHARS && !this.state.expanded[m.id];
    }

    bodyHtml(m) {
        const text = this.isLong(m) ? m.body.slice(0, READ_MORE_CHARS) + "…" : m.body;
        return formatWhatsappText(text);
    }

    get deletedLabel() {
        return _t("This message was deleted");
    }

    get unsupportedLabel() {
        return _t("Unsupported message, open WhatsApp on the phone to view it");
    }

    /**
     * The time and ticks sit in the bottom corner of the bubble (WhatsApp style). A blank of the same width is put
     * at the visual end of the last line so the text never runs under them: at the logical end when the text has
     * the direction of the interface, at the logical start otherwise.
     */
    spaceAtStart(text) {
        const rtlText = RTL_FIRST_STRONG.test(text || "");
        return rtlText !== this.state.uiRtl;
    }

    spaceStyle(m) {
        let width = this.timeOf(m.date).length * 6.6 + 10;
        if (m.direction === "out") {
            width += 20;
        }
        if (m.edited) {
            width += 42;
        }
        if (m.is_auto) {
            width += 18;
        }
        return `width: ${Math.ceil(width)}px`;
    }

    /** Bubbles ending with a card (document, location...) get a line of their own for the time. */
    needsMetaLine(m) {
        if (m.revoked || this.isBareMedia(m)) {
            return false;
        }
        if (["location", "contact", "poll", "sticker"].includes(m.type) || (this.isMediaWithoutFile(m) && !m.body)) {
            return true;
        }
        return !m.body && m.attachments.length > 0;
    }

    expand(m) {
        this.state.expanded[m.id] = true;
    }

    /** [icon, color class] of a document card. */
    fileIcon(name) {
        const ext = (name || "").split(".").pop().toLowerCase();
        if (ext === "pdf") {
            return ["picture_as_pdf", "o_waha_pdf"];
        }
        if (["doc", "docx", "odt", "rtf"].includes(ext)) {
            return ["article", "o_waha_word"];
        }
        if (["xls", "xlsx", "ods", "csv"].includes(ext)) {
            return ["table_chart", "o_waha_excel"];
        }
        if (["zip", "rar", "7z"].includes(ext)) {
            return ["folder_zip", ""];
        }
        return ["description", ""];
    }

    fileExt(name) {
        const parts = (name || "").split(".");
        return parts.length > 1 ? parts.pop().toUpperCase() : "";
    }

    fileSize(bytes) {
        if (!bytes) {
            return "";
        }
        const units = ["B", "kB", "MB", "GB"];
        let size = bytes;
        let unit = 0;
        while (size >= 1024 && unit < units.length - 1) {
            size /= 1024;
            unit++;
        }
        return `${size < 10 && unit ? size.toFixed(1) : Math.round(size)} ${units[unit]}`;
    }

    mapUrl(m) {
        return `https://www.google.com/maps?q=${m.location.lat},${m.location.lng}`;
    }

    kindOf(attachment) {
        const mime = attachment.mimetype || "";
        if (mime.startsWith("image/")) {
            return "image";
        }
        if (mime.startsWith("video/")) {
            return "video";
        }
        if (mime.startsWith("audio/")) {
            return "audio";
        }
        return "file";
    }

    /** Media without a caption shows the time over the picture, like WhatsApp. */
    /** A file message whose file is not in Odoo yet (media policy, older import...): offer to download it. */
    isMediaWithoutFile(m) {
        return (
            !m.revoked &&
            !m.attachments.length &&
            !this.isVoiceBubble(m) &&
            (m.media_pending || ["image", "video", "audio", "document", "sticker"].includes(m.type))
        );
    }

    isBareMedia(m) {
        return (
            !m.body &&
            !this.isMediaWithoutFile(m) &&
            !m.revoked &&
            m.attachments.length > 0 &&
            m.attachments.every((a) => ["image", "video"].includes(this.kindOf(a)))
        );
    }
}

registry.category("actions").add("elblasy_waha_inbox", WahaInbox);
