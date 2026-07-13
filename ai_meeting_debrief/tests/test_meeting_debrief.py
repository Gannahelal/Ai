from datetime import timedelta
from unittest.mock import patch
from odoo import fields
from odoo.tests import TransactionCase
from odoo.exceptions import UserError


SAMPLE_AI_RESPONSE = {
    "action_items": [
        {
            "description": "Finish the API integration",
            "responsible": "Sara",
            "deadline": "2026-07-02",
        },
        {
            "description": "Update the test cases",
            "responsible": "Mohamed",
            "deadline": "2026-07-03",
        },
        {
            "description": "Review the database migration script",
            "responsible": "Ahmed",
            "deadline": "2026-07-04",
        },
    ],
    "decisions": [
        {"description": "Delay the client demo to July 5th"},
    ],
    "sentiment": "neutral",
    "sentiment_score": 0.3,
    "sentiment_explanation": "Meeting was mostly productive with minor tension.",
}

SAMPLE_TRANSCRIPT = """
Attendees: Ahmed, Sara, Mohamed

Ahmed: Sara, did you finish the API integration?
Sara: Not yet, I need two more days. Should be done by July 2nd.
Mohamed: That is blocking us.
Ahmed: Sara, please prioritize this. Mohamed, update the test cases by July 3rd.
Mohamed: Yes, I will handle that.
Ahmed: We decided to delay the client demo to July 5th.
Ahmed: I will review the migration script by July 4th.
"""


class TestMeetingDebriefModel(TransactionCase):

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(
            'ai_meeting_debrief.gemini_api_key', 'test-fake-key'
        )
        self.env['ir.config_parameter'].sudo().set_param(
            'ai_meeting_debrief.ai_provider', 'gemini'
        )
        self.debrief = self.env['ai.meeting.debrief'].create({
            'name': 'Test Meeting',
            'transcript': SAMPLE_TRANSCRIPT,
        })

    def _mock_gemini(self, *args, **kwargs):
        return SAMPLE_AI_RESPONSE

    # ------------------------------------------------------------------
    # 01–03  Basic model tests (no API call)
    # ------------------------------------------------------------------

    def test_01_creation(self):
        self.assertEqual(self.debrief.state, 'draft')
        self.assertEqual(self.debrief.name, 'Test Meeting')

    def test_02_analyze_requires_transcript(self):
        empty = self.env['ai.meeting.debrief'].create({'name': 'Empty'})
        with self.assertRaises(UserError):
            empty.action_analyze()

    def test_03_analyze_requires_api_key(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'ai_meeting_debrief.gemini_api_key', ''
        )
        with self.assertRaises(UserError):
            self.debrief.action_analyze()

    # ------------------------------------------------------------------
    # 04–11  Mocked Gemini API
    # ------------------------------------------------------------------

    def test_04_analyze_creates_action_items(self):
        with patch.object(self.debrief.__class__, '_call_gemini_api', self._mock_gemini):
            self.debrief.action_analyze()

        self.assertEqual(self.debrief.state, 'done')
        self.assertEqual(len(self.debrief.action_item_ids), 3)

    def test_05_analyze_creates_decisions(self):
        with patch.object(self.debrief.__class__, '_call_gemini_api', self._mock_gemini):
            self.debrief.action_analyze()

        self.assertEqual(len(self.debrief.decision_ids), 1)
        self.assertIn('demo', self.debrief.decision_ids[0].description.lower())

    def test_06_analyze_sets_sentiment(self):
        with patch.object(self.debrief.__class__, '_call_gemini_api', self._mock_gemini):
            self.debrief.action_analyze()

        self.assertEqual(self.debrief.sentiment, 'neutral')
        self.assertAlmostEqual(self.debrief.sentiment_score, 0.3, places=1)

    def test_07_analyze_creates_tasks(self):
        with patch.object(self.debrief.__class__, '_call_gemini_api', self._mock_gemini):
            self.debrief.action_analyze()

        tasks = self.debrief.action_item_ids.mapped('task_id')
        self.assertEqual(len(tasks), 3)
        self.assertIn('Finish the API integration', tasks.mapped('name'))

    def test_08_hr_alert_triggered_on_conflict(self):
        conflict_response = dict(SAMPLE_AI_RESPONSE, sentiment='conflict', sentiment_score=0.9)
        with patch.object(
            self.debrief.__class__, '_call_gemini_api', lambda *a, **kw: conflict_response
        ):
            self.debrief.action_analyze()

        self.assertTrue(self.debrief.hr_alert)

    def test_09_hr_alert_not_triggered_on_positive(self):
        positive_response = dict(SAMPLE_AI_RESPONSE, sentiment='positive', sentiment_score=0.1)
        with patch.object(
            self.debrief.__class__, '_call_gemini_api', lambda *a, **kw: positive_response
        ):
            self.debrief.action_analyze()

        self.assertFalse(self.debrief.hr_alert)

    def test_10_reset_draft_clears_results(self):
        with patch.object(self.debrief.__class__, '_call_gemini_api', self._mock_gemini):
            self.debrief.action_analyze()

        self.debrief.action_reset_draft()

        self.assertEqual(self.debrief.state, 'draft')
        self.assertEqual(len(self.debrief.action_item_ids), 0)
        self.assertEqual(len(self.debrief.decision_ids), 0)
        self.assertFalse(self.debrief.sentiment)

    def test_11_reanalyze_replaces_old_results(self):
        with patch.object(self.debrief.__class__, '_call_gemini_api', self._mock_gemini):
            self.debrief.action_analyze()
            self.debrief.action_analyze()

        # second run must not double-stack results
        self.assertEqual(len(self.debrief.action_item_ids), 3)

    # ------------------------------------------------------------------
    # 12  Date parsing helper
    # ------------------------------------------------------------------

    def test_12_parse_date_formats(self):
        self.assertEqual(str(self.debrief._parse_date('2026-07-02')), '2026-07-02')
        self.assertEqual(str(self.debrief._parse_date('02/07/2026')), '2026-07-02')
        self.assertFalse(self.debrief._parse_date(None))
        self.assertFalse(self.debrief._parse_date('not-a-date'))

    # ------------------------------------------------------------------
    # 13  Dashboard stats
    # ------------------------------------------------------------------

    def test_13_dashboard_stats_keys(self):
        stats = self.env['ai.meeting.debrief'].get_dashboard_stats()
        for key in ('total_meetings', 'this_month', 'pending_actions',
                    'hr_alerts', 'sentiment_breakdown', 'recent_meetings'):
            self.assertIn(key, stats)

    # ------------------------------------------------------------------
    # 14–16  Privacy mode
    # ------------------------------------------------------------------

    def test_14_privacy_mode_blocks_analysis(self):
        self.debrief.privacy_mode = True
        with self.assertRaises(UserError) as ctx:
            self.debrief.action_analyze()
        self.assertIn('Privacy Mode', str(ctx.exception))

    def test_15_privacy_mode_default_is_false(self):
        new_debrief = self.env['ai.meeting.debrief'].create({'name': 'New'})
        self.assertFalse(new_debrief.privacy_mode)

    def test_16_disabling_privacy_mode_allows_analysis(self):
        self.debrief.privacy_mode = True
        self.debrief.privacy_mode = False
        with patch.object(self.debrief.__class__, '_call_gemini_api', self._mock_gemini):
            self.debrief.action_analyze()
        self.assertEqual(self.debrief.state, 'done')

    # ------------------------------------------------------------------
    # 17–21  Calendar event integration
    # ------------------------------------------------------------------

    def test_17_calendar_event_id_field_exists(self):
        self.assertIn('calendar_event_id', self.env['ai.meeting.debrief']._fields)

    def test_18_create_debrief_from_calendar_event(self):
        event = self.env['calendar.event'].create({
            'name': 'Weekly Sync',
            'start': '2026-07-01 10:00:00',
            'stop': '2026-07-01 11:00:00',
        })
        result = event.action_create_meeting_debrief()

        self.assertEqual(result['type'], 'ir.actions.act_window')
        self.assertEqual(result['res_model'], 'ai.meeting.debrief')

        debrief = self.env['ai.meeting.debrief'].browse(result['res_id'])
        self.assertEqual(debrief.name, 'Weekly Sync')
        self.assertEqual(debrief.calendar_event_id, event)

    def test_19_calendar_event_prefills_date(self):
        event = self.env['calendar.event'].create({
            'name': 'Sync',
            'start': '2026-07-05 09:00:00',
            'stop': '2026-07-05 10:00:00',
        })
        result = event.action_create_meeting_debrief()
        debrief = self.env['ai.meeting.debrief'].browse(result['res_id'])
        self.assertEqual(
            debrief.date.strftime('%Y-%m-%d %H:%M:%S'),
            '2026-07-05 09:00:00',
        )

    def test_20_calendar_event_debrief_count(self):
        event = self.env['calendar.event'].create({
            'name': 'Count Test',
            'start': '2026-07-01 10:00:00',
            'stop': '2026-07-01 11:00:00',
        })
        self.assertEqual(event.ai_debrief_count, 0)

        event.action_create_meeting_debrief()
        event.invalidate_recordset()
        self.assertEqual(event.ai_debrief_count, 1)

    def test_21_google_meet_url_sets_platform(self):
        event = self.env['calendar.event'].create({
            'name': 'Google Meet Call',
            'start': '2026-07-01 10:00:00',
            'stop': '2026-07-01 11:00:00',
            'videocall_location': 'https://meet.google.com/abc-defg-hij',
        })
        result = event.action_create_meeting_debrief()
        debrief = self.env['ai.meeting.debrief'].browse(result['res_id'])
        self.assertEqual(debrief.meeting_platform, 'google_meet')

    # ------------------------------------------------------------------
    # 22–25  Multi-provider AI dispatch
    # ------------------------------------------------------------------

    def test_22_openai_provider_routes_correctly(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'ai_meeting_debrief.ai_provider', 'openai'
        )
        self.env['ir.config_parameter'].sudo().set_param(
            'ai_meeting_debrief.openai_api_key', 'sk-test-key'
        )
        with patch.object(
            self.debrief.__class__, '_call_openai_api',
            lambda self_, *a, **kw: SAMPLE_AI_RESPONSE,
        ):
            self.debrief.action_analyze()

        self.assertEqual(self.debrief.state, 'done')
        self.assertEqual(len(self.debrief.action_item_ids), 3)

    def test_23_claude_provider_routes_correctly(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'ai_meeting_debrief.ai_provider', 'claude'
        )
        self.env['ir.config_parameter'].sudo().set_param(
            'ai_meeting_debrief.claude_api_key', 'sk-ant-test-key'
        )
        with patch.object(
            self.debrief.__class__, '_call_claude_api',
            lambda self_, *a, **kw: SAMPLE_AI_RESPONSE,
        ):
            self.debrief.action_analyze()

        self.assertEqual(self.debrief.state, 'done')
        self.assertEqual(len(self.debrief.action_item_ids), 3)

    def test_24_openai_provider_requires_api_key(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'ai_meeting_debrief.ai_provider', 'openai'
        )
        self.env['ir.config_parameter'].sudo().set_param(
            'ai_meeting_debrief.openai_api_key', ''
        )
        with self.assertRaises(UserError) as ctx:
            self.debrief.action_analyze()
        self.assertIn('OpenAI', str(ctx.exception))

    def test_25_claude_provider_requires_api_key(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'ai_meeting_debrief.ai_provider', 'claude'
        )
        self.env['ir.config_parameter'].sudo().set_param(
            'ai_meeting_debrief.claude_api_key', ''
        )
        with self.assertRaises(UserError) as ctx:
            self.debrief.action_analyze()
        self.assertIn('Claude', str(ctx.exception))

    # ------------------------------------------------------------------
    # 26  Video upload blocked for non-Gemini providers
    # ------------------------------------------------------------------

    def test_26_video_upload_blocked_for_openai(self):
        import base64
        self.env['ir.config_parameter'].sudo().set_param(
            'ai_meeting_debrief.ai_provider', 'openai'
        )
        self.env['ir.config_parameter'].sudo().set_param(
            'ai_meeting_debrief.openai_api_key', 'sk-test'
        )
        self.debrief.video_file = base64.b64encode(b'fake video bytes')
        self.debrief.video_filename = 'recording.mp4'

        with self.assertRaises(UserError) as ctx:
            self.debrief.action_analyze()
        self.assertIn('Gemini', str(ctx.exception))

    # ------------------------------------------------------------------
    # 27–29  Cron auto-debrief creation
    # ------------------------------------------------------------------

    def test_27_cron_creates_debrief_for_ended_event(self):
        now = fields.Datetime.now()
        event = self.env['calendar.event'].create({
            'name': 'Ended Meeting',
            'start': (now - timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S'),
            'stop': (now - timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S'),
        })
        self.assertFalse(event.auto_debrief_generated)

        self.env['ai.meeting.debrief']._cron_auto_create_debriefs()

        debriefs = self.env['ai.meeting.debrief'].search([
            ('calendar_event_id', '=', event.id)
        ])
        self.assertEqual(len(debriefs), 1)
        self.assertEqual(debriefs.name, 'Ended Meeting')
        self.assertEqual(debriefs.state, 'draft')
        self.assertTrue(event.auto_debrief_generated)

    def test_28_cron_does_not_create_duplicate_debriefs(self):
        now = fields.Datetime.now()
        event = self.env['calendar.event'].create({
            'name': 'No Duplicate',
            'start': (now - timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S'),
            'stop': (now - timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S'),
        })

        self.env['ai.meeting.debrief']._cron_auto_create_debriefs()
        self.env['ai.meeting.debrief']._cron_auto_create_debriefs()

        debriefs = self.env['ai.meeting.debrief'].search([
            ('calendar_event_id', '=', event.id)
        ])
        self.assertEqual(len(debriefs), 1)

    def test_29_cron_skips_future_events(self):
        now = fields.Datetime.now()
        event = self.env['calendar.event'].create({
            'name': 'Future Meeting',
            'start': (now + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S'),
            'stop': (now + timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S'),
        })

        self.env['ai.meeting.debrief']._cron_auto_create_debriefs()

        debriefs = self.env['ai.meeting.debrief'].search([
            ('calendar_event_id', '=', event.id)
        ])
        self.assertEqual(len(debriefs), 0)

    # ------------------------------------------------------------------
    # 30  Reset draft preserves calendar link
    # ------------------------------------------------------------------

    def test_30_reset_draft_preserves_calendar_link(self):
        event = self.env['calendar.event'].create({
            'name': 'Linked Event',
            'start': '2026-07-01 10:00:00',
            'stop': '2026-07-01 11:00:00',
        })
        self.debrief.calendar_event_id = event

        with patch.object(self.debrief.__class__, '_call_gemini_api', self._mock_gemini):
            self.debrief.action_analyze()

        self.debrief.action_reset_draft()

        self.assertEqual(self.debrief.calendar_event_id, event)
        self.assertEqual(self.debrief.state, 'draft')
