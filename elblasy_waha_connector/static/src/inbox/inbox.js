import { Component, onMounted, onWillDestroy, onWillStart, proxy, signal, useProps } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_plugin";

const MODEL = "elblasy.waha.conversation";

/** Unique per click. crypto.randomUUID only exists on HTTPS/localhost, plain-HTTP installs need a fallback. */
function newClientToken() {
    if (globalThis.crypto?.randomUUID) {
        return globalThis.crypto.randomUUID();
    }
    const random = new Uint32Array(4);
    globalThis.crypto.getRandomValues(random);
    return Array.from(random, (n) => n.toString(16).padStart(8, "0")).join("-");
}

export class WahaInbox extends Component {
    static template = "elblasy_waha_connector.Inbox";
    props = useProps(standardActionServiceProps);
    threadRef = signal.ref();
    fileRef = signal.ref();

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.bus = useService("bus_service");
        this.state = proxy({
            sessions: [],
            sessionId: 0,
            search: "",
            conversations: [],
            current: null,
            messages: [],
            hasMore: false,
            draft: "",
            sending: false,
            labels: [],
            labelFilter: 0,
            labelsOpen: false,
        });
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
        });
    }

    async loadConversations() {
        this.state.conversations = await this.orm.call(MODEL, "inbox_conversations", [], {
            session_id: this.state.sessionId || null,
            search: this.state.search || null,
            label_id: this.state.labelFilter || null,
        });
    }

    async loadLabels() {
        this.state.labels = await this.orm.call(MODEL, "inbox_labels", [], {
            session_id: this.state.sessionId || null,
        });
    }

    onLabelFilterChange(ev) {
        this.state.labelFilter = parseInt(ev.target.value) || 0;
        return this.loadConversations();
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
            this.state.current.labels = await this.orm.call(
                MODEL,
                "inbox_toggle_label",
                [[this.state.current.id]],
                { label_id: label.id }
            );
            await this.loadConversations();
        } catch (error) {
            this.notification.add(error?.data?.message || _t("The label could not be changed."), {
                type: "danger",
            });
        }
    }

    async onBusMessage({ conversation_id }) {
        await this.loadConversations();
        if (this.state.current && this.state.current.id === conversation_id) {
            await this.loadMessages();
            await this.orm.call(MODEL, "inbox_mark_read", [[conversation_id]]);
        }
    }

    async onSessionChange(ev) {
        this.state.sessionId = parseInt(ev.target.value) || 0;
        this.state.labelFilter = 0;
        await this.loadLabels();
        return this.loadConversations();
    }

    onSearchInput(ev) {
        this.state.search = ev.target.value;
        clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(() => this.loadConversations(), 300);
    }

    async openConversation(conv) {
        this.state.current = conv;
        this.state.messages = [];
        await this.loadMessages();
        if (conv.unread) {
            await this.orm.call(MODEL, "inbox_mark_read", [[conv.id]]);
            conv.unread = 0;
        }
    }

    async loadMessages() {
        const res = await this.orm.call(MODEL, "inbox_messages", [[this.state.current.id]]);
        this.state.messages = res.messages;
        this.state.hasMore = res.has_more;
        this.scrollBottom();
    }

    async loadOlder() {
        const first = this.state.messages[0];
        const res = await this.orm.call(MODEL, "inbox_messages", [[this.state.current.id]], {
            before_id: first && first.id,
        });
        this.state.messages = [...res.messages, ...this.state.messages];
        this.state.hasMore = res.has_more;
    }

    scrollBottom() {
        setTimeout(() => {
            const el = this.threadRef();
            if (el) {
                el.scrollTop = el.scrollHeight;
            }
        }, 0);
    }

    onAttachClick() {
        const input = this.fileRef();
        if (input) {
            input.click();
        }
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
            this.state.draft = "";
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

    onDraftInput(ev) {
        this.state.draft = ev.target.value;
    }

    onDraftKeydown(ev) {
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            this.send();
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
            this.state.draft = "";
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

    statusLabel(status) {
        return { pending: "…", sent: "✓", delivered: "✓✓", read: "✓✓", failed: "⚠", unknown: "?" }[status] || "";
    }
}

registry.category("actions").add("elblasy_waha_inbox", WahaInbox);
