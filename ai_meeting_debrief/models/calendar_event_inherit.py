from odoo import api, fields, models, _


class CalendarEvent(models.Model):
    _inherit = 'calendar.event'

    auto_debrief_generated = fields.Boolean(
        string='Auto Debrief Generated',
        default=False,
        copy=False,
        help='Prevents duplicate auto-debrief creation by the scheduled action.',
    )
    meeting_notes = fields.Text(
        string='Meeting Notes / Transcript',
        help='Write notes or paste a transcript during/after the meeting. '
             'The AI debrief will use this automatically when you click Analyze.',
    )
    ai_debrief_ids = fields.One2many(
        'ai.meeting.debrief', 'calendar_event_id',
        string='Meeting Debriefs',
    )
    ai_debrief_count = fields.Integer(
        compute='_compute_ai_debrief_count',
        string='Debriefs',
    )

    @api.depends('ai_debrief_ids')
    def _compute_ai_debrief_count(self):
        for event in self:
            event.ai_debrief_count = len(event.ai_debrief_ids)

    def action_create_meeting_debrief(self):
        self.ensure_one()
        partner_ids = self.partner_ids
        employees = self.env['hr.employee'].sudo().search([
            '|',
            ('work_contact_id', 'in', partner_ids.ids),
            ('user_id.partner_id', 'in', partner_ids.ids),
        ])
        vals = {
            'name': self.name,
            'date': self.start,
            'calendar_event_id': self.id,
            'attendee_ids': [(6, 0, employees.ids)],
        }
        if self.videocall_location and 'meet.google.com' in (self.videocall_location or ''):
            vals['meeting_platform'] = 'google_meet'
        debrief = self.env['ai.meeting.debrief'].create(vals)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'ai.meeting.debrief',
            'res_id': debrief.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_view_meeting_debriefs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Debriefs — %s') % self.name,
            'res_model': 'ai.meeting.debrief',
            'view_mode': 'list,form',
            'domain': [('calendar_event_id', '=', self.id)],
            'context': {'default_calendar_event_id': self.id},
        }