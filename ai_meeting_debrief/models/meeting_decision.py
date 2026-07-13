from odoo import fields, models


class MeetingDecision(models.Model):
    _name = 'ai.meeting.decision'
    _description = 'Meeting Decision'
    _order = 'sequence asc, id asc'

    debrief_id = fields.Many2one('ai.meeting.debrief', required=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    description = fields.Text(string='Decision', required=True)
