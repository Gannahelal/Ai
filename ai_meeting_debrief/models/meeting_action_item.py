from odoo import fields, models


class MeetingActionItem(models.Model):
    _name = 'ai.meeting.action.item'
    _description = 'Meeting Action Item'
    _order = 'id asc'

    debrief_id = fields.Many2one('ai.meeting.debrief', required=True, ondelete='cascade')
    description = fields.Char(string='Action Item', required=True)
    responsible_name = fields.Char(string='Responsible (AI)', help='Name as extracted by AI')
    responsible_id = fields.Many2one('hr.employee', string='Assigned To')
    deadline = fields.Date(string='Deadline')
    task_id = fields.Many2one('project.task', string='Task', readonly=True)
    state = fields.Selection([
        ('pending', 'Pending'),
        ('done', 'Done'),
    ], string='Status', default='pending')
