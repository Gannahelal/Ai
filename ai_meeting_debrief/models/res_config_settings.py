from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    ai_provider = fields.Selection([
        ('gemini', 'Google Gemini'),
        ('openai', 'OpenAI GPT-4o'),
        ('claude', 'Anthropic Claude'),
    ], string='AI Provider',
       config_parameter='ai_meeting_debrief.ai_provider',
       default='gemini',
    )
    gemini_api_key = fields.Char(
        string='Gemini API Key',
        config_parameter='ai_meeting_debrief.gemini_api_key',
    )
    gemini_model = fields.Char(
        string='Gemini Model',
        config_parameter='ai_meeting_debrief.gemini_model',
        default='gemini-2.5-flash-lite',
    )
    openai_api_key = fields.Char(
        string='OpenAI API Key',
        config_parameter='ai_meeting_debrief.openai_api_key',
    )
    claude_api_key = fields.Char(
        string='Claude API Key',
        config_parameter='ai_meeting_debrief.claude_api_key',
    )
    zoom_account_id = fields.Char(
        string='Zoom Account ID',
        config_parameter='ai_meeting_debrief.zoom_account_id',
    )
    zoom_client_id = fields.Char(
        string='Zoom Client ID',
        config_parameter='ai_meeting_debrief.zoom_client_id',
    )
    zoom_client_secret = fields.Char(
        string='Zoom Client Secret',
        config_parameter='ai_meeting_debrief.zoom_client_secret',
    )
    google_service_account_json = fields.Char(
        string='Google Service Account JSON',
        config_parameter='ai_meeting_debrief.google_service_account_json',
    )
    ai_meeting_default_project_id = fields.Many2one(
        'project.project',
        string='Default Project for Tasks',
    )

    @api.model
    def get_values(self):
        res = super().get_values()
        project_id = self.env['ir.config_parameter'].sudo().get_param(
            'ai_meeting_debrief.default_project_id'
        )
        if project_id:
            try:
                res['ai_meeting_default_project_id'] = int(project_id)
            except (ValueError, TypeError):
                pass
        return res

    def set_values(self):
        super().set_values()
        self.env['ir.config_parameter'].sudo().set_param(
            'ai_meeting_debrief.default_project_id',
            self.ai_meeting_default_project_id.id or '',
        )
