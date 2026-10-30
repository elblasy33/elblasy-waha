import { Component, markup, onMounted, onWillDestroy, onWillStart, proxy, signal, useProps } from "@odoo/owl";
import { useEmojiPicker } from "@web/core/emoji_picker/emoji_picker";
import { deserializeDateTime } from "@web/core/l10n/dates";
import { localization } from "@web/core/l10n/localization";
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_plugin";

const { DateTime } = luxon;
const MODEL = "elblasy.waha.conversation";
const READ_MORE_CHARS = 700;
const RTL_FIRST_STRONG = /^[^A-Za-z\u00C0-\u024F\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]*[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]/;
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

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.bus = useService("bus_service");
        this.state = proxy({
            sessions: [],
            sessionId: 0,
            search: "",
            filter: "all", // all | unread | label
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
        const onMessage = (payload) => this.onBusMessage(payload);
        const onSession = async () => {
            this.state.sessions = await this.orm.call(MODEL, "inbox_sessions", []);
        };
        onMounted(() => {
            this.bus.subscribe("elblasy_waha/message", onMessage);
            this.bus.subscribe("elblasy_waha/session", onSession);
            this.bus.start?.();
        });
        onWillDestroy(() => {
            this.bus.unsubscribe?.("elblasy_waha/message", onMessage);
            this.bus.unsubscribe?.("elblasy_waha/session", onSession);
            clearTimeout(this._searchTimer);
        });
    }

    // ------------------------------------------------------------------ data
    async loadConversations() {
        this.state.conversations = await this.orm.call(MODEL, "inbox_conversations", [], {
            session_id: this.state.sessionId || null,
            search: this.state.search || null,
            label_id: (this.state.filter === "label" && this.state.labelFilter) || null,
            unread: this.state.filter === "unread",
        });
        const current = this.state.current;
        if (current) {
            const fresh = this.state.conversations.find((c) => c.id === current.id);
            if (fresh) {
                Object.assign(current, fresh, { unread: 0 });
            }
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
        this.state.current = conv;
        this.state.messages = [];
        this.state.labelsOpen = false;
        this.state.expanded = {};
        await this.loadMessages();
        if (conv.unread) {
            await this.orm.call(MODEL, "inbox_mark_read", [[conv.id]]);
            conv.unread = 0;
        }
        this.composerRef()?.focus();
    }

    closeConversation() {
        this.state.current = null;
        this.state.messages = [];
    }

    onThreadScroll(ev) {
        if (ev.target.scrollTop < 80 && this.state.hasMore && !this.state.loadingOlder) {
            this.loadOlder();
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
        this.state.sending = true;
        // one token per click: a retried/duplicated request cannot create two messages
        const token = newClientToken();
        try {
            await this.orm.call(MODEL, "inbox_send_text", [[this.state.current.id]], {
                text,
                client_token: token,
            });
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

    // ------------------------------------------------------------------ display helpers
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
            rows.push({ key: m.id, message: m, first: !previous || previous.direction !== m.direction });
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
        return rtlText !== (localization.direction === "rtl");
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
        if (["location", "contact", "poll", "sticker"].includes(m.type)) {
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
    isBareMedia(m) {
        return (
            !m.body &&
            !m.revoked &&
            m.attachments.length > 0 &&
            m.attachments.every((a) => ["image", "video"].includes(this.kindOf(a)))
        );
    }
}

registry.category("actions").add("elblasy_waha_inbox", WahaInbox);
