import { Component, onWillDestroy, onWillStart, proxy, useProps } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { formatFloat } from "@web/core/utils/numbers";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_plugin";

const { DateTime } = luxon;

export class WahaDashboard extends Component {
    static template = "elblasy_waha_connector.Dashboard";
    props = useProps(standardActionServiceProps);

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = proxy({ data: null, sessionId: 0, loading: false });
        onWillStart(() => this.load());
        this._timer = setInterval(() => this.load(), 60000);
        onWillDestroy(() => clearInterval(this._timer));
    }

    async load() {
        this.state.loading = true;
        try {
            this.state.data = await this.orm.call("elblasy.waha.dashboard", "get_data", [], {
                session_id: this.state.sessionId || null,
            });
        } finally {
            this.state.loading = false;
        }
    }

    onSessionChange(ev) {
        this.state.sessionId = parseInt(ev.target.value) || 0;
        return this.load();
    }

    get todayLabel() {
        return DateTime.local().toLocaleString(DateTime.DATE_HUGE);
    }

    get cards() {
        const t = this.state.data.today;
        const y = this.state.data.yesterday;
        return [
            { key: "new_chats", label: _t("New chats"), icon: "chat_bubble", value: t.new_chats, prev: y.new_chats, up: "good" },
            { key: "received", label: _t("Messages received"), icon: "download", value: t.received, prev: y.received, up: "neutral" },
            { key: "sent", label: _t("Replies by the team"), icon: "send", value: t.sent, prev: y.sent, up: "good" },
            {
                key: "waiting", label: _t("Waiting for a reply"), icon: "schedule", value: this.state.data.waiting_count,
                alert: this.state.data.waiting_count > 0,
            },
            {
                key: "response", label: _t("Average response"), icon: "speed", value: t.avg_response, prev: y.avg_response,
                unit: _t("min"), up: "bad", sub: t.fast_pct !== null ? _t("%s within 15 min", `${t.fast_pct}%`) : "",
            },
            { key: "auto", label: _t("Automatic replies"), icon: "autorenew", value: t.auto, prev: y.auto, up: "neutral" },
            { key: "documents", label: _t("Documents sent"), icon: "description", value: t.documents, up: "good" },
            { key: "failed", label: _t("Failed messages"), icon: "error", value: t.failed, prev: y.failed, up: "bad", alert: t.failed > 0 },
        ];
    }

    display(card) {
        if (card.value === null || card.value === undefined) {
            return "—";
        }
        return card.unit ? `${formatFloat(card.value, { digits: [16, 1] })} ${card.unit}` : String(card.value);
    }

    trend(card) {
        if (card.prev === undefined || card.prev === null || card.value === null) {
            return null;
        }
        const diff = card.value - card.prev;
        if (!diff) {
            return { text: _t("same as yesterday"), cls: "text-muted", icon: "remove" };
        }
        const better = card.up === "neutral" ? null : (diff > 0) === (card.up === "good");
        return {
            text: _t("%s vs yesterday", (diff > 0 ? "+" : "") + formatFloat(diff, { digits: [16, card.unit ? 1 : 0] })),
            cls: better === null ? "text-muted" : better ? "text-success" : "text-danger",
            icon: diff > 0 ? "north_east" : "south_east",
        };
    }

    get maxDay() {
        return Math.max(1, ...this.state.data.days.map((d) => Math.max(d.received, d.sent)));
    }

    get maxHour() {
        return Math.max(1, ...this.state.data.hours);
    }

    dayShort(date) {
        return DateTime.fromISO(date).toFormat("ccc d");
    }

    pct(value, max) {
        return `${Math.round((100 * value) / max)}%`;
    }

    heat(value) {
        return value ? 0.15 + (0.85 * value) / this.maxHour : 0.04;
    }

    waited(minutes) {
        if (minutes < 60) {
            return _t("%s min", minutes);
        }
        if (minutes < 1440) {
            return _t("%s h", Math.floor(minutes / 60));
        }
        return _t("%s days", Math.floor(minutes / 1440));
    }

    money(value) {
        return `${formatFloat(value || 0, { digits: [16, 0] })} ${this.state.data.currency}`;
    }

    todayDomain(extra = []) {
        const r = this.state.data.range;
        return [["date", ">=", r.start], ["date", "<", r.end], ...extra];
    }

    openCard(card) {
        const messages = (name, extra, context = {}) =>
            this.action.doAction({
                type: "ir.actions.act_window", name, res_model: "elblasy.waha.report.message",
                views: [[false, "list"], [false, "pivot"], [false, "graph"]], domain: this.todayDomain(extra), context,
            });
        switch (card.key) {
            case "waiting":
                return this.action.doAction("elblasy_waha_connector.waha_report_waiting_action");
            case "received":
                return messages(card.label, [["direction", "=", "in"]]);
            case "sent":
            case "response":
                return messages(card.label, [["direction", "=", "out"], ["is_auto", "=", false]]);
            case "auto":
                return messages(card.label, [["is_auto", "=", true]]);
            case "failed":
                return messages(card.label, [["delivery_status", "=", "failed"]]);
            case "documents":
                return this.action.doAction({
                    type: "ir.actions.act_window", name: card.label, res_model: "elblasy.waha.report.document",
                    views: [[false, "list"], [false, "pivot"]], domain: this.todayDomain(),
                });
            default:
                return this.action.doAction("elblasy_waha_connector.waha_report_chat_action");
        }
    }

    openChat(conv) {
        return this.action.doAction({
            type: "ir.actions.client", tag: "elblasy_waha_inbox", name: _t("WhatsApp Inbox"),
            context: { waha_conversation_id: conv.id },
        });
    }

    openBusiness(row) {
        return this.action.doAction({
            type: "ir.actions.act_window", name: row.name, res_model: "elblasy.waha.report.business",
            views: [[false, "list"], [false, "pivot"], [false, "graph"]],
            domain: [["tracked_model_id", "=", row.id], ["date", ">=", this.state.data.range.month]],
            context: { search_default_from_whatsapp: 1 },
        });
    }
}

registry.category("actions").add("elblasy_waha_dashboard", WahaDashboard);
