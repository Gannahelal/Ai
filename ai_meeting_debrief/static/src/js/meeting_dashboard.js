/** @odoo-module **/

import { Component, useState, onMounted } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

function toDateStr(d) {
    return d.toISOString().split("T")[0];
}

class MeetingDashboard extends Component {
    static template = "ai_meeting_debrief.MeetingDashboard";

    setup() {
        this.orm    = useService("orm");
        this.action = useService("action");

        const today    = new Date();
        const firstDay = new Date(today.getFullYear(), today.getMonth(), 1);

        this.state = useState({
            stats:    null,
            loading:  true,
            error:    null,
            dateFrom: toDateStr(firstDay),
            dateTo:   toDateStr(today),
        });
        onMounted(() => this.loadStats());
    }

    async loadStats() {
        this.state.loading = true;
        this.state.error   = null;
        try {
            const stats = await this.orm.call(
                "ai.meeting.debrief",
                "get_dashboard_stats",
                [this.state.dateFrom, this.state.dateTo]
            );
            this.state.stats = stats;
        } catch (e) {
            this.state.error = e.message || "Failed to load dashboard data.";
        } finally {
            this.state.loading = false;
        }
    }

    onDateFromChange(ev) {
        this.state.dateFrom = ev.target.value;
        if (this.state.dateFrom && this.state.dateTo) this.loadStats();
    }

    onDateToChange(ev) {
        this.state.dateTo = ev.target.value;
        if (this.state.dateFrom && this.state.dateTo) this.loadStats();
    }

    setThisMonth() {
        const today = new Date();
        this.state.dateFrom = toDateStr(new Date(today.getFullYear(), today.getMonth(), 1));
        this.state.dateTo   = toDateStr(today);
        this.loadStats();
    }

    setLast3Months() {
        const today = new Date();
        const from  = new Date(today);
        from.setMonth(from.getMonth() - 3);
        this.state.dateFrom = toDateStr(from);
        this.state.dateTo   = toDateStr(today);
        this.loadStats();
    }

    setThisYear() {
        const today = new Date();
        this.state.dateFrom = toDateStr(new Date(today.getFullYear(), 0, 1));
        this.state.dateTo   = toDateStr(today);
        this.loadStats();
    }

    setAllTime() {
        this.state.dateFrom = "2000-01-01";
        this.state.dateTo   = toDateStr(new Date());
        this.loadStats();
    }

    openAllMeetings() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "All Meetings",
            res_model: "ai.meeting.debrief",
            views: [[false, "list"], [false, "form"]],
        });
    }

    openAnalyzedMeetings() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Analyzed Meetings",
            res_model: "ai.meeting.debrief",
            views: [[false, "list"], [false, "form"]],
            domain: [["state", "=", "done"]],
        });
    }

    openHRAlerts() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "HR Alerts",
            res_model: "ai.meeting.debrief",
            views: [[false, "list"], [false, "form"]],
            domain: [["hr_alert", "=", true]],
        });
    }

    openPendingActions() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Pending Action Items",
            res_model: "ai.meeting.action.item",
            views: [[false, "list"]],
            domain: [["state", "=", "pending"]],
        });
    }

    openMeeting(id) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "ai.meeting.debrief",
            res_id: id,
            views: [[false, "form"]],
        });
    }

    openNewMeeting() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "ai.meeting.debrief",
            views: [[false, "form"]],
        });
    }
}

registry.category("actions").add("ai_meeting_debrief.dashboard", MeetingDashboard);
