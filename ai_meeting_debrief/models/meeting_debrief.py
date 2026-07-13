import json
import base64
import logging
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_SENTIMENT_LABELS = {
    'positive': 'Positive 😊',
    'neutral': 'Neutral 😐',
    'tense': 'Tense 😤',
    'conflict': 'Conflict 😡',
}


class MeetingDebrief(models.Model):
    _name = 'ai.meeting.debrief'
    _description = 'AI Meeting Debrief'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc, id desc'

    name = fields.Char(string='Meeting Title', required=True, tracking=True)
    date = fields.Datetime(string='Meeting Date', default=fields.Datetime.now, tracking=True)
    meeting_platform = fields.Selection([
        ('google_meet', 'Google Meet'),
        ('teams', 'Microsoft Teams'),
        ('zoom', 'Zoom'),
        ('in_person', 'In Person'),
        ('other', 'Other'),
    ], string='Platform', default='other')
    department_id = fields.Many2one('hr.department', string='Department')
    attendee_ids = fields.Many2many('hr.employee', string='Attendees')

    video_file = fields.Binary(string='Meeting Recording')
    video_filename = fields.Char(string='Recording Filename')

    transcript = fields.Text(string='Meeting Transcript')
    transcript_file = fields.Binary(string='Transcript File (.txt)')
    transcript_filename = fields.Char(string='Filename')

    state = fields.Selection([
        ('draft', 'Draft'),
        ('done', 'Analyzed'),
    ], string='Status', default='draft', tracking=True)

    action_item_ids = fields.One2many('ai.meeting.action.item', 'debrief_id', string='Action Items')
    decision_ids = fields.One2many('ai.meeting.decision', 'debrief_id', string='Decisions')

    sentiment = fields.Selection([
        ('positive', 'Positive'),
        ('neutral', 'Neutral'),
        ('tense', 'Tense'),
        ('conflict', 'Conflict'),
    ], string='Sentiment', tracking=True)
    sentiment_score = fields.Float(string='Tension Score', digits=(3, 2))
    sentiment_explanation = fields.Text(string='AI Notes on Tone')
    hr_alert = fields.Boolean(string='HR Alert', default=False, tracking=True)

    ai_raw_response = fields.Text(string='AI Raw Response')
    meeting_summary = fields.Text(string='Meeting Brief', readonly=True)
    custom_prompt = fields.Text(
        string='Custom Instructions',
        help='Tell the AI what to focus on. Example: "Highlight all budget-related decisions" '
             'or "Who is responsible for the project timeline?"',
    )

    calendar_event_id = fields.Many2one(
        'calendar.event', string='Calendar Event', ondelete='set null', index=True,
    )
    videocall_location = fields.Char(
        string='Meeting URL', related='calendar_event_id.videocall_location',
        readonly=True, store=False,
    )
    privacy_mode = fields.Boolean(
        string='Privacy Mode', default=False, tracking=True,
        help='When enabled, this meeting is excluded from AI processing.',
    )

    action_item_count = fields.Integer(compute='_compute_counts', string='Action Items')
    task_count = fields.Integer(compute='_compute_counts', string='Tasks')

    @api.depends('action_item_ids', 'action_item_ids.task_id')
    def _compute_counts(self):
        for rec in self:
            rec.action_item_count = len(rec.action_item_ids)
            rec.task_count = len(rec.action_item_ids.filtered('task_id').mapped('task_id'))

    def action_analyze(self):
        self.ensure_one()

        if self.privacy_mode:
            raise UserError(_(
                "Privacy Mode is enabled for this meeting. "
                "Disable it first to run AI analysis."
            ))

        provider = self.env['ir.config_parameter'].sudo().get_param(
            'ai_meeting_debrief.ai_provider', 'gemini'
        )

        # Validate API key for the chosen provider
        if provider == 'openai':
            api_key = self.env['ir.config_parameter'].sudo().get_param('ai_meeting_debrief.openai_api_key')
            if not api_key:
                raise UserError(_("OpenAI API key is not configured. Go to Settings → AI Meeting Debrief."))
        elif provider == 'claude':
            api_key = self.env['ir.config_parameter'].sudo().get_param('ai_meeting_debrief.claude_api_key')
            if not api_key:
                raise UserError(_("Claude API key is not configured. Go to Settings → AI Meeting Debrief."))
        else:
            api_key = self.env['ir.config_parameter'].sudo().get_param('ai_meeting_debrief.gemini_api_key')
            if not api_key:
                raise UserError(_(
                    "Gemini API key is not configured. "
                    "Go to Settings → AI Meeting Debrief to add your API key."
                ))

        # Determine input source: video > text file > pasted transcript
        use_video = bool(self.video_file)
        transcript = None

        if use_video and provider != 'gemini':
            # OpenAI and Claude don't support direct video — transcribe with Whisper first
            openai_key = self.env['ir.config_parameter'].sudo().get_param(
                'ai_meeting_debrief.openai_api_key'
            )
            if not openai_key:
                raise UserError(_(
                    "Video/audio transcription for %s requires an OpenAI API key (Whisper).\n"
                    "Go to Settings → AI Meeting Debrief → add your OpenAI API key, "
                    "or paste the transcript as text instead."
                ) % provider.upper())
            _logger.info("Transcribing video with OpenAI Whisper before sending to %s", provider)
            video_data = base64.b64decode(self.video_file)
            transcript = self._transcribe_with_whisper(
                video_data, self.video_filename or 'recording.mp4', openai_key
            )
            use_video = False  # from here on treat as text analysis

        if not use_video:
            if not transcript:
                transcript = self.transcript
            if not transcript and self.transcript_file:
                try:
                    transcript = base64.b64decode(self.transcript_file).decode('utf-8')
                except Exception:
                    raise UserError(_("Could not read transcript file. Please ensure it is a plain UTF-8 text file."))
            # Auto-pull from the linked calendar event's notes if still empty
            if (not transcript or not transcript.strip()) and self.calendar_event_id:
                transcript = self.calendar_event_id.meeting_notes
            if not transcript or not transcript.strip():
                raise UserError(_(
                    "No transcript found. Paste a transcript here, upload a file, "
                    "or write Meeting Notes on the linked calendar event."
                ))

        # Clear any previous analysis results
        self.action_item_ids.unlink()
        self.decision_ids.unlink()
        self.write({
            'sentiment': False,
            'sentiment_explanation': False,
            'hr_alert': False,
            'ai_raw_response': False,
            'meeting_summary': False,
        })

        gemini_file_name = None
        try:
            if use_video:
                video_data = base64.b64decode(self.video_file)
                mime_type = self._detect_mime_type(self.video_filename)
                file_uri, gemini_file_name = self._upload_video_to_gemini(
                    video_data, self.video_filename or 'recording', api_key
                )
                if gemini_file_name:
                    self._wait_for_file_active(gemini_file_name, api_key)
                ai_data = self._get_ai_response(
                    None, provider, api_key,
                    file_uri=file_uri, file_mime_type=mime_type, timeout=600,
                )
            else:
                ai_data = self._get_ai_response(transcript, provider, api_key, timeout=60)

            self.ai_raw_response = json.dumps(ai_data, indent=2, ensure_ascii=False)
            self._process_ai_response(ai_data)
            self.state = 'done'
        except UserError:
            raise
        except Exception as e:
            _logger.exception("AI Meeting Debrief analysis failed for record %s", self.id)
            raise UserError(_("AI analysis failed: %s") % str(e))
        finally:
            if gemini_file_name:
                self._delete_gemini_file(gemini_file_name, api_key)

    def action_reset_draft(self):
        self.ensure_one()
        self.action_item_ids.unlink()
        self.decision_ids.unlink()
        self.write({
            'state': 'draft',
            'sentiment': False,
            'sentiment_explanation': False,
            'hr_alert': False,
            'ai_raw_response': False,
            'meeting_summary': False,
        })

    def action_open_tasks(self):
        self.ensure_one()
        task_ids = self.action_item_ids.mapped('task_id').ids
        return {
            'type': 'ir.actions.act_window',
            'name': _('Tasks from %s') % self.name,
            'res_model': 'project.task',
            'view_mode': 'list,form',
            'domain': [('id', 'in', task_ids)],
        }

    # -------------------------------------------------------------------------
    # AI Provider Dispatcher
    # -------------------------------------------------------------------------

    def _get_ai_response(self, transcript, provider, api_key,
                         file_uri=None, file_mime_type=None, timeout=60):
        if provider == 'openai':
            return self._call_openai_api(transcript, api_key, timeout=timeout)
        if provider == 'claude':
            return self._call_claude_api(transcript, api_key, timeout=timeout)
        return self._call_gemini_api(
            transcript, api_key,
            file_uri=file_uri, file_mime_type=file_mime_type, timeout=timeout,
        )

    def _call_openai_api(self, transcript, api_key, timeout=60):
        url = 'https://api.openai.com/v1/chat/completions'
        prompt = self._build_prompt(transcript)
        payload = json.dumps({
            'model': 'gpt-4o',
            'messages': [
                {
                    'role': 'system',
                    'content': 'You are an expert meeting analyst. Return ONLY valid JSON, no markdown fences.',
                },
                {'role': 'user', 'content': prompt},
            ],
            'temperature': 0.1,
            'max_tokens': 4096,
            'response_format': {'type': 'json_object'},
        }).encode('utf-8')

        req = urllib.request.Request(url, data=payload, method='POST', headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='ignore')
            raise UserError(_("OpenAI API error (HTTP %d): %s") % (e.code, body[:500]))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise UserError(_("Could not reach OpenAI API: %s") % str(e))

        usage = result.get('usage', {})
        _logger.info(
            "OpenAI token usage — prompt: %s | completion: %s | total: %s",
            usage.get('prompt_tokens', '?'),
            usage.get('completion_tokens', '?'),
            usage.get('total_tokens', '?'),
        )
        try:
            text = result['choices'][0]['message']['content']
            parsed = json.loads(text)
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise UserError(_("Could not parse OpenAI response: %s") % str(e))

        _logger.info("OpenAI response keys: %s", list(parsed.keys()) if isinstance(parsed, dict) else type(parsed))

        # GPT-4o sometimes wraps the result in a top-level key (e.g. "meeting_analysis").
        # Unwrap one level if the expected keys are missing.
        if isinstance(parsed, dict) and 'action_items' not in parsed and 'decisions' not in parsed:
            for v in parsed.values():
                if isinstance(v, dict) and ('action_items' in v or 'decisions' in v):
                    _logger.info("OpenAI: unwrapping nested key into flat structure")
                    parsed = v
                    break

        return parsed

    def _call_claude_api(self, transcript, api_key, timeout=60):
        url = 'https://api.anthropic.com/v1/messages'
        prompt = self._build_prompt(transcript)
        payload = json.dumps({
            'model': 'claude-sonnet-4-6',
            'max_tokens': 4096,
            'messages': [
                {'role': 'user', 'content': prompt},
            ],
        }).encode('utf-8')

        req = urllib.request.Request(url, data=payload, method='POST', headers={
            'Content-Type': 'application/json',
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='ignore')
            raise UserError(_("Claude API error (HTTP %d): %s") % (e.code, body[:500]))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise UserError(_("Could not reach Claude API: %s") % str(e))

        usage = result.get('usage', {})
        _logger.info(
            "Claude token usage — input: %s | output: %s | total: %s",
            usage.get('input_tokens', '?'),
            usage.get('output_tokens', '?'),
            (usage.get('input_tokens') or 0) + (usage.get('output_tokens') or 0),
        )
        try:
            text = result['content'][0]['text']
            return json.loads(text)
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise UserError(_("Could not parse Claude response: %s") % str(e))

    # -------------------------------------------------------------------------
    # Gemini API
    # -------------------------------------------------------------------------

    def _call_gemini_api(self, transcript, api_key, file_uri=None, file_mime_type=None, timeout=60):
        model = self.env['ir.config_parameter'].sudo().get_param(
            'ai_meeting_debrief.gemini_model', 'gemini-2.5-flash-lite'
        )
        url = (
            f'https://generativelanguage.googleapis.com/v1beta/models/'
            f'{model}:generateContent?key={api_key}'
        )

        if file_uri:
            parts = [
                {"text": self._build_video_prompt()},
                {"file_data": {"mime_type": file_mime_type, "file_uri": file_uri}},
            ]
        else:
            parts = [{"text": self._build_prompt(transcript)}]

        payload = json.dumps({
            "contents": [{"parts": parts}],
            "generationConfig": {
                "response_mime_type": "application/json",
                "temperature": 0.1,
                "maxOutputTokens": 4096,
            },
        }).encode('utf-8')

        # Retry up to 3 times on 503 (server overload) with exponential backoff
        retryable_codes = {503, 429}
        max_retries = 3
        last_error = None

        for attempt in range(max_retries):
            req = urllib.request.Request(
                url,
                data=payload,
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    result = json.loads(response.read().decode('utf-8'))
                break  # success — exit retry loop
            except urllib.error.HTTPError as e:
                body = e.read().decode('utf-8', errors='ignore')
                last_error = (e.code, body)
                if e.code in retryable_codes and attempt < max_retries - 1:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    _logger.warning(
                        "Gemini API returned %d (attempt %d/%d), retrying in %ds...",
                        e.code, attempt + 1, max_retries, wait,
                    )
                    time.sleep(wait)
                    continue
                raise UserError(
                    _("Gemini API error (HTTP %d): %s") % (e.code, body[:500])
                )
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                reason = getattr(e, 'reason', e)
                raise UserError(_("Could not reach Gemini API: %s") % str(reason))
        else:
            code, body = last_error
            raise UserError(_("Gemini API error (HTTP %d): %s") % (code, body[:500]))

        if 'error' in result:
            raise UserError(_("Gemini error: %s") % result['error'].get('message', 'Unknown'))

        usage = result.get('usageMetadata', {})
        _logger.info(
            "Gemini token usage — prompt: %s | output: %s | total: %s",
            usage.get('promptTokenCount', '?'),
            usage.get('candidatesTokenCount', '?'),
            usage.get('totalTokenCount', '?'),
        )
        try:
            candidate = result['candidates'][0]
            finish_reason = candidate.get('finishReason', 'STOP')
            if finish_reason == 'SAFETY':
                raise UserError(_("Gemini refused to process this content due to safety filters."))
            text = candidate['content']['parts'][0]['text']
            return json.loads(text)
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise UserError(
                _("Could not parse AI response: %s\n\nRaw response: %s") % (str(e), str(result)[:800])
            )

    def _build_prompt(self, transcript):
        today = date.today().strftime('%Y-%m-%d')
        custom = (self.custom_prompt or '').strip()
        custom_section = f"\nSPECIAL FOCUS (answer inside meeting_summary):\n{custom}\n" if custom else ''
        return f"""You are an expert meeting analyst. Analyze the meeting transcript below and extract structured information.

Return ONLY a valid JSON object — no markdown fences, no explanations, no extra text:
{{
  "meeting_summary": "3–5 paragraph plain-language summary of what this meeting was about, what was discussed, and the key outcomes. Written as if explaining to someone who was not there.",
  "action_items": [
    {{
      "description": "Clear, specific action item (start with a verb)",
      "responsible": "Full name of person responsible, or null",
      "deadline": "YYYY-MM-DD, or null"
    }}
  ],
  "decisions": [
    {{
      "description": "A clear statement of a decision that was made"
    }}
  ],
  "sentiment": "positive",
  "sentiment_explanation": "Brief explanation of the overall tone and atmosphere"
}}

Rules:
- meeting_summary: comprehensive, readable, no bullet points — full paragraphs only
- sentiment MUST be exactly one of: positive, neutral, tense, conflict
- For deadlines, use {today} as today's reference; return null if unclear
- Extract ALL action items (even implicit ones) and ALL decisions
- Raised voices, strong disagreements, passive-aggressive language → tense or conflict
- responsible is the name as mentioned in transcript, or null if unclear
{custom_section}
TRANSCRIPT:
{transcript}"""

    def _build_video_prompt(self):
        today = date.today().strftime('%Y-%m-%d')
        custom = (self.custom_prompt or '').strip()
        custom_section = f"\nSPECIAL FOCUS (answer inside meeting_summary):\n{custom}\n" if custom else ''
        return f"""You are an expert meeting analyst. Watch and listen to this meeting recording carefully, then extract structured information from everything said.

Return ONLY a valid JSON object — no markdown fences, no explanations, no extra text:
{{
  "meeting_summary": "3–5 paragraph plain-language summary of what this meeting was about, what was discussed, and the key outcomes. Written as if explaining to someone who was not there.",
  "action_items": [
    {{
      "description": "Clear, specific action item (start with a verb)",
      "responsible": "Full name of person responsible, or null",
      "deadline": "YYYY-MM-DD, or null"
    }}
  ],
  "decisions": [
    {{
      "description": "A clear statement of a decision that was made"
    }}
  ],
  "sentiment": "positive",
  "sentiment_explanation": "Brief explanation of the overall tone and atmosphere"
}}

Rules:
- meeting_summary: comprehensive, readable, no bullet points — full paragraphs only
- sentiment MUST be exactly one of: positive, neutral, tense, conflict
- For deadlines, use {today} as today's reference; return null if unclear
- Extract ALL action items (even implicit ones) and ALL decisions
- Raised voices, strong disagreements, passive-aggressive language → tense or conflict
- responsible is the speaker's name, or null if unclear
{custom_section}"""

    # -------------------------------------------------------------------------
    # Video upload helpers (Gemini Files API)
    # -------------------------------------------------------------------------

    _MIME_MAP = {
        'mp4': 'video/mp4', 'mpeg': 'video/mpeg', 'mpg': 'video/mpeg',
        'mov': 'video/quicktime', 'avi': 'video/x-msvideo',
        'wmv': 'video/x-ms-wmv', 'webm': 'video/webm',
        '3gp': 'video/3gpp', 'mkv': 'video/x-matroska',
        'mp3': 'audio/mp3', 'wav': 'audio/wav', 'm4a': 'audio/mp4',
        'aac': 'audio/aac', 'flac': 'audio/flac', 'ogg': 'audio/ogg',
    }

    def _detect_mime_type(self, filename):
        if filename and '.' in filename:
            ext = filename.rsplit('.', 1)[-1].lower()
            return self._MIME_MAP.get(ext, 'video/mp4')
        return 'video/mp4'

    def _upload_video_to_gemini(self, video_data, filename, api_key):
        """Upload via Gemini Files API resumable upload. Returns (file_uri, file_name)."""
        mime_type = self._detect_mime_type(filename)
        file_size = len(video_data)

        # Step 1 — initiate resumable upload session
        init_url = f'https://generativelanguage.googleapis.com/upload/v1beta/files?key={api_key}'
        init_body = json.dumps({
            'file': {'display_name': filename or 'meeting_recording'}
        }).encode('utf-8')

        req = urllib.request.Request(init_url, data=init_body, method='POST', headers={
            'Content-Type': 'application/json',
            'X-Goog-Upload-Protocol': 'resumable',
            'X-Goog-Upload-Command': 'start',
            'X-Goog-Upload-Header-Content-Length': str(file_size),
            'X-Goog-Upload-Header-Content-Type': mime_type,
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                upload_url = resp.headers.get('X-Goog-Upload-URL')
        except (urllib.error.HTTPError, TimeoutError, OSError) as e:
            code = getattr(e, 'code', 0)
            body = e.read().decode('utf-8', errors='ignore') if hasattr(e, 'read') else str(e)
            raise UserError(_("Failed to initiate video upload (HTTP %d): %s") % (code, body[:300]))

        if not upload_url:
            raise UserError(_("Gemini Files API did not return an upload URL."))

        # Step 2 — upload the file bytes
        req = urllib.request.Request(upload_url, data=video_data, method='POST', headers={
            'Content-Length': str(file_size),
            'X-Goog-Upload-Offset': '0',
            'X-Goog-Upload-Command': 'upload, finalize',
        })
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                result = json.loads(resp.read().decode('utf-8'))
        except (urllib.error.HTTPError, TimeoutError, OSError) as e:
            code = getattr(e, 'code', 0)
            body = e.read().decode('utf-8', errors='ignore') if hasattr(e, 'read') else str(e)
            raise UserError(_("Failed to upload video (HTTP %d): %s") % (code, body[:300]))

        file_info = result.get('file', {})
        return file_info.get('uri'), file_info.get('name')

    def _wait_for_file_active(self, file_name, api_key, max_wait=300):
        """Poll Gemini until the uploaded file is in ACTIVE state."""
        url = f'https://generativelanguage.googleapis.com/v1beta/{file_name}?key={api_key}'
        deadline = time.time() + max_wait
        while time.time() < deadline:
            req = urllib.request.Request(url, method='GET')
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    info = json.loads(resp.read().decode('utf-8'))
                state = info.get('state', '')
                if state == 'ACTIVE':
                    return
                if state == 'FAILED':
                    raise UserError(_("Gemini failed to process the uploaded video file."))
            except (urllib.error.HTTPError, TimeoutError, OSError):
                pass
            time.sleep(5)
        raise UserError(_("Timeout: Gemini took too long to process the video (max %ds).") % max_wait)

    def _delete_gemini_file(self, file_name, api_key):
        """Delete file from Gemini Files API after analysis (privacy + quota cleanup)."""
        url = f'https://generativelanguage.googleapis.com/v1beta/{file_name}?key={api_key}'
        req = urllib.request.Request(url, method='DELETE')
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception:
            _logger.warning("Could not delete Gemini file %s", file_name)

    def _transcribe_with_whisper(self, video_data, filename, api_key, timeout=300):
        """Send audio/video to OpenAI Whisper API and return plain-text transcript."""
        max_bytes = 25 * 1024 * 1024  # Whisper limit: 25 MB
        if len(video_data) > max_bytes:
            raise UserError(_(
                "The uploaded file is larger than 25 MB. "
                "Whisper (OpenAI transcription) supports up to 25 MB. "
                "Please trim the recording or paste the transcript manually."
            ))

        boundary = b'----WhisperBoundary' + str(int(time.time())).encode()
        ext = (filename or 'audio.mp4').rsplit('.', 1)[-1].lower()
        safe_name = (filename or 'audio.mp4').encode('ascii', errors='replace')

        def field(name, value):
            return (
                b'--' + boundary + b'\r\n'
                b'Content-Disposition: form-data; name="' + name.encode() + b'"\r\n\r\n'
                + value.encode() + b'\r\n'
            )

        body = (
            field('model', 'whisper-1') +
            field('response_format', 'text') +
            b'--' + boundary + b'\r\n'
            b'Content-Disposition: form-data; name="file"; filename="' + safe_name + b'"\r\n'
            b'Content-Type: audio/' + ext.encode() + b'\r\n\r\n' +
            video_data + b'\r\n'
            b'--' + boundary + b'--\r\n'
        )

        req = urllib.request.Request(
            'https://api.openai.com/v1/audio/transcriptions',
            data=body,
            method='POST',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': f'multipart/form-data; boundary={boundary.decode()}',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                transcript = resp.read().decode('utf-8')
                _logger.info("Whisper transcription complete (%d chars)", len(transcript))
                return transcript
        except urllib.error.HTTPError as e:
            err = e.read().decode('utf-8', errors='ignore')
            raise UserError(_("Whisper API error (HTTP %d): %s") % (e.code, err[:500]))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise UserError(_("Could not reach Whisper API: %s") % str(e))

    # -------------------------------------------------------------------------
    # Processing AI response
    # -------------------------------------------------------------------------

    def _process_ai_response(self, data):
        self._create_action_items(data.get('action_items', []))
        self._create_decisions(data.get('decisions', []))

        sentiment = data.get('sentiment', 'neutral')
        if sentiment not in ('positive', 'neutral', 'tense', 'conflict'):
            sentiment = 'neutral'

        self.write({
            'sentiment': sentiment,
            'sentiment_explanation': data.get('sentiment_explanation', ''),
            'meeting_summary': data.get('meeting_summary', ''),
        })

        if data.get('decisions'):
            self._log_decisions_to_chatter(data['decisions'])

        if sentiment in ('tense', 'conflict'):
            self._trigger_hr_alert(sentiment, data.get('sentiment_explanation', ''))

    def _create_action_items(self, items_data):
        for item_data in items_data:
            description = (item_data.get('description') or '').strip()
            if not description:
                continue

            employee = self._find_employee(item_data.get('responsible'))
            deadline = self._parse_date(item_data.get('deadline'))

            action_item = self.env['ai.meeting.action.item'].create({
                'debrief_id': self.id,
                'description': description,
                'responsible_name': item_data.get('responsible') or '',
                'responsible_id': employee.id if employee else False,
                'deadline': deadline,
            })

            self._create_task(action_item)

            if employee and employee.user_id:
                self._create_activity(action_item, employee)

    def _create_task(self, action_item):
        project = self._get_default_project()

        user_ids = []
        if action_item.responsible_id and action_item.responsible_id.user_id:
            user_ids = [(4, action_item.responsible_id.user_id.id)]

        task = self.env['project.task'].sudo().create({
            'name': action_item.description,
            'project_id': project.id,
            'user_ids': user_ids,
            'date_deadline': action_item.deadline,
            'description': f'<p>Action item from meeting: <b>{self.name}</b></p>',
        })
        action_item.task_id = task

    def _get_default_project(self):
        param = self.env['ir.config_parameter'].sudo().get_param(
            'ai_meeting_debrief.default_project_id'
        )
        if param:
            try:
                project = self.env['project.project'].sudo().browse(int(param))
                if project.exists():
                    return project
            except (ValueError, TypeError):
                pass

        project = self.env['project.project'].sudo().search(
            [('name', '=', 'Meeting Action Items')], limit=1
        )
        if not project:
            project = self.env['project.project'].sudo().create({
                'name': 'Meeting Action Items',
            })
        return project

    def _create_activity(self, action_item, employee):
        try:
            activity_type = self.env.ref('mail.mail_activity_data_todo')
        except Exception:
            activity_type = self.env['mail.activity.type'].search([], limit=1)

        if not activity_type:
            return

        self.env['mail.activity'].sudo().create({
            'activity_type_id': activity_type.id,
            'summary': action_item.description,
            'user_id': employee.user_id.id,
            'res_model_id': self.env['ir.model']._get_id(self._name),
            'res_id': self.id,
            'date_deadline': action_item.deadline or fields.Date.today(),
        })

    def _create_decisions(self, decisions_data):
        for i, d in enumerate(decisions_data, 1):
            desc = (d.get('description') or '').strip()
            if desc:
                self.env['ai.meeting.decision'].create({
                    'debrief_id': self.id,
                    'sequence': i * 10,
                    'description': desc,
                })

    def _log_decisions_to_chatter(self, decisions_data):
        lines = ['<b>📋 Decisions Made in This Meeting:</b><ul>']
        for d in decisions_data:
            desc = (d.get('description') or '').strip()
            if desc:
                lines.append(f'<li>{desc}</li>')
        lines.append('</ul>')
        self.message_post(body=''.join(lines))

    def _trigger_hr_alert(self, sentiment, explanation):
        self.hr_alert = True
        emoji = '⚠️' if sentiment == 'tense' else '🚨'
        label = sentiment.title()
        self.message_post(
            body=(
                f'<b>{emoji} HR Alert — Meeting Tension Detected</b><br/>'
                f'Sentiment: <b>{label}</b><br/><br/>'
                f'{explanation}'
            ),
            subtype_xmlid='mail.mt_note',
        )

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _find_employee(self, name):
        if not name or not str(name).strip():
            return self.env['hr.employee']
        return self.env['hr.employee'].search(
            [('name', 'ilike', str(name).strip())], limit=1
        )

    def _parse_date(self, date_str):
        if not date_str:
            return False
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y'):
            try:
                return datetime.strptime(str(date_str).strip(), fmt).date()
            except (ValueError, AttributeError):
                continue
        return False

    # -------------------------------------------------------------------------
    # Zoom transcript integration
    # -------------------------------------------------------------------------

    def _get_zoom_access_token(self):
        """OAuth Server-to-Server token for Zoom API."""
        params = self.env['ir.config_parameter'].sudo()
        account_id = params.get_param('ai_meeting_debrief.zoom_account_id')
        client_id = params.get_param('ai_meeting_debrief.zoom_client_id')
        client_secret = params.get_param('ai_meeting_debrief.zoom_client_secret')
        if not all([account_id, client_id, client_secret]):
            return None
        credentials = base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode()
        url = f'https://zoom.us/oauth/token?grant_type=account_credentials&account_id={account_id}'
        req = urllib.request.Request(url, method='POST', headers={
            'Authorization': f'Basic {credentials}',
            'Content-Type': 'application/x-www-form-urlencoded',
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode('utf-8')).get('access_token')
        except Exception as e:
            _logger.warning("Zoom token error: %s", e)
            return None

    def _extract_zoom_meeting_id(self, url):
        """Extract numeric meeting ID from a zoom.us URL."""
        match = re.search(r'/j/(\d+)', url or '')
        return match.group(1) if match else None

    def _parse_vtt_transcript(self, vtt_content):
        """Convert WebVTT to plain readable text."""
        lines = []
        for line in vtt_content.splitlines():
            line = line.strip()
            if not line or line == 'WEBVTT' or '-->' in line or line.isdigit():
                continue
            lines.append(line)
        return '\n'.join(lines)

    def _fetch_zoom_transcript(self):
        """
        Call Zoom Recordings API, download the VTT transcript, return plain text.
        Returns None (silently) if recording/transcript is not available yet.
        """
        if not self.calendar_event_id:
            return None
        location = self.calendar_event_id.videocall_location or ''
        if 'zoom.us' not in location:
            return None
        meeting_id = self._extract_zoom_meeting_id(location)
        if not meeting_id:
            return None
        access_token = self._get_zoom_access_token()
        if not access_token:
            return None

        # Step 1 — get recording list for this meeting
        req = urllib.request.Request(
            f'https://api.zoom.us/v2/meetings/{meeting_id}/recordings',
            headers={'Authorization': f'Bearer {access_token}'},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            _logger.info("Zoom recordings API HTTP %d for meeting %s", e.code, meeting_id)
            return None
        except Exception as e:
            _logger.warning("Zoom recordings API error: %s", e)
            return None

        # Step 2 — find the TRANSCRIPT file
        transcript_file = next(
            (f for f in result.get('recording_files', [])
             if f.get('file_type') == 'TRANSCRIPT'),
            None,
        )
        if not transcript_file:
            _logger.info("No TRANSCRIPT file in Zoom recording for meeting %s", meeting_id)
            return None

        download_url = transcript_file.get('download_url')
        if not download_url:
            return None

        # Step 3 — download the VTT file
        req = urllib.request.Request(f'{download_url}?access_token={access_token}')
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                vtt = resp.read().decode('utf-8', errors='ignore')
        except Exception as e:
            _logger.warning("Zoom VTT download error: %s", e)
            return None

        return self._parse_vtt_transcript(vtt)

    def action_fetch_zoom_transcript(self):
        """Manual button: fetch Zoom transcript and populate the transcript field."""
        self.ensure_one()
        params = self.env['ir.config_parameter'].sudo()
        if not params.get_param('ai_meeting_debrief.zoom_account_id'):
            raise UserError(_(
                "Zoom credentials are not configured. "
                "Go to Settings → AI Meeting Debrief → Zoom Integration."
            ))
        transcript = self._fetch_zoom_transcript()
        if not transcript:
            raise UserError(_(
                "Could not fetch Zoom transcript. Possible reasons:\n"
                "• Recording is still processing (Zoom takes 5–15 min after meeting ends)\n"
                "• Cloud Recording or Audio Transcript is not enabled in your Zoom account\n"
                "• The meeting URL does not contain a valid meeting ID\n"
                "• Zoom credentials (Account ID / Client ID / Secret) are incorrect"
            ))
        self.transcript = transcript
        self.message_post(
            body=_("<b>Zoom transcript fetched automatically.</b> Click Analyze with AI to generate insights."),
            subtype_xmlid='mail.mt_note',
        )

    # -------------------------------------------------------------------------
    # Google Meet transcript integration (Google Drive API)
    # -------------------------------------------------------------------------

    def _create_service_account_jwt(self, creds, scope):
        """Create a signed RS256 JWT for Google Service Account authentication."""
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
        except ImportError:
            _logger.error("'cryptography' package not available — cannot sign Google JWT")
            return None

        now = int(time.time())
        header_b64 = base64.urlsafe_b64encode(
            json.dumps({"alg": "RS256", "typ": "JWT"}).encode()
        ).rstrip(b'=')
        payload_b64 = base64.urlsafe_b64encode(
            json.dumps({
                "iss": creds['client_email'],
                "scope": scope,
                "aud": "https://oauth2.googleapis.com/token",
                "exp": now + 3600,
                "iat": now,
            }).encode()
        ).rstrip(b'=')

        message = header_b64 + b'.' + payload_b64
        private_key = serialization.load_pem_private_key(
            creds['private_key'].encode(), password=None
        )
        signature = private_key.sign(message, asym_padding.PKCS1v15(), hashes.SHA256())
        sig_b64 = base64.urlsafe_b64encode(signature).rstrip(b'=')
        return (message + b'.' + sig_b64).decode()

    def _get_google_access_token(self):
        """Exchange Service Account JWT for a Google Drive access token."""
        json_str = self.env['ir.config_parameter'].sudo().get_param(
            'ai_meeting_debrief.google_service_account_json'
        )
        if not json_str:
            return None
        try:
            creds = json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            _logger.warning("Google Service Account JSON is not valid JSON")
            return None

        jwt_token = self._create_service_account_jwt(
            creds, 'https://www.googleapis.com/auth/drive.readonly'
        )
        if not jwt_token:
            return None

        data = urllib.parse.urlencode({
            'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
            'assertion': jwt_token,
        }).encode()
        req = urllib.request.Request(
            'https://oauth2.googleapis.com/token',
            data=data,
            method='POST',
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode()).get('access_token')
        except Exception as e:
            _logger.warning("Google token exchange failed: %s", e)
            return None

    def _fetch_google_meet_transcript(self):
        """
        Search Google Drive for a Meet transcript by date range (±1 day around the meeting).
        Google names transcripts using the room code, not the calendar event title,
        so we search for any Google Doc with 'Transcript' in the name created near the meeting date.
        Returns None silently if not found or not configured.
        """
        if not self.calendar_event_id:
            return None
        location = self.calendar_event_id.videocall_location or ''
        if 'meet.google.com' not in location:
            return None

        access_token = self._get_google_access_token()
        if not access_token:
            return None

        # Build date window: midnight before meeting day to midnight after
        meeting_date = self.date or fields.Date.today()
        from datetime import datetime as dt
        day_start = dt(meeting_date.year, meeting_date.month, meeting_date.day, 0, 0, 0)
        day_end = dt(meeting_date.year, meeting_date.month, meeting_date.day, 23, 59, 59)
        # RFC 3339 format required by Drive API
        day_start_str = day_start.strftime('%Y-%m-%dT%H:%M:%S')
        day_end_str = day_end.strftime('%Y-%m-%dT%H:%M:%S')

        # Search for Google Docs with "Transcript" in name created on the meeting day
        query = (
            "name contains 'Transcript' "
            "and mimeType = 'application/vnd.google-apps.document' "
            f"and createdTime >= '{day_start_str}' "
            f"and createdTime <= '{day_end_str}' "
            "and trashed = false"
        )
        search_url = (
            'https://www.googleapis.com/drive/v3/files'
            '?q=' + urllib.parse.quote(query) +
            '&orderBy=createdTime+desc&pageSize=10&fields=files(id,name,createdTime)'
        )
        req = urllib.request.Request(
            search_url,
            headers={'Authorization': f'Bearer {access_token}'},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
        except Exception as e:
            _logger.warning("Google Drive search failed: %s", e)
            return None

        files = result.get('files', [])
        if not files:
            _logger.info("No Google Meet transcript found in Drive for date: %s", meeting_date)
            return None

        _logger.info("Google Meet transcript found: %s", files[0].get('name'))

        # Export the most recent matching file as plain text
        file_id = files[0]['id']
        export_url = (
            f'https://www.googleapis.com/drive/v3/files/{file_id}/export'
            '?mimeType=text/plain'
        )
        req = urllib.request.Request(
            export_url,
            headers={'Authorization': f'Bearer {access_token}'},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode('utf-8', errors='ignore')
        except Exception as e:
            _logger.warning("Google Drive export failed: %s", e)
            return None

    def action_fetch_google_meet_transcript(self):
        """Manual button: fetch Google Meet transcript from Drive with step-by-step diagnostics."""
        self.ensure_one()

        # Step 1 — Service account configured?
        sa_json = self.env['ir.config_parameter'].sudo().get_param(
            'ai_meeting_debrief.google_service_account_json'
        )
        if not sa_json:
            raise UserError(_(
                "Step 1 FAILED: Google Service Account is not configured.\n"
                "Go to Settings → AI Meeting Debrief → Google Meet Integration → paste JSON → Save."
            ))

        # Step 2 — Linked to a calendar event?
        if not self.calendar_event_id:
            raise UserError(_(
                "Step 2 FAILED: This debrief is not linked to a Calendar Event.\n"
                "Open the debrief form → set the 'Calendar Event' field to your Google Meet event,\n"
                "or create the debrief by clicking 'Create Debrief' from the Calendar event itself."
            ))

        # Step 3 — Calendar event has a Meet link?
        location = self.calendar_event_id.videocall_location or ''
        if 'meet.google.com' not in location:
            raise UserError(_(
                "Step 3 FAILED: The linked calendar event has no Google Meet link.\n"
                "Video call location found: '%s'\n"
                "Make sure the calendar event was created with Google Meet conferencing."
            ) % (location or '(empty)'))

        # Step 4 — Can we get a Google access token?
        access_token = self._get_google_access_token()
        if not access_token:
            raise UserError(_(
                "Step 4 FAILED: Could not authenticate with Google Drive.\n"
                "Possible causes:\n"
                "• Service Account JSON is invalid or incomplete\n"
                "• 'private_key' field is corrupted (check for missing \\n characters)\n"
                "• Google Drive API is not enabled in your Google Cloud project\n"
                "Go to console.cloud.google.com → APIs & Services → Enable 'Google Drive API'."
            ))

        # Step 5 — Search Drive for a transcript created on the meeting date
        # Google names transcripts using the room code (e.g. "frh-dzyb-wis ... - Transcript")
        # so we search by "Transcript" keyword + date range instead of meeting name
        from datetime import datetime as dt
        meeting_date = self.date or fields.Date.today()
        day_start_str = dt(meeting_date.year, meeting_date.month, meeting_date.day, 0, 0, 0).strftime('%Y-%m-%dT%H:%M:%S')
        day_end_str = dt(meeting_date.year, meeting_date.month, meeting_date.day, 23, 59, 59).strftime('%Y-%m-%dT%H:%M:%S')
        query = (
            "name contains 'Transcript' "
            "and mimeType = 'application/vnd.google-apps.document' "
            f"and createdTime >= '{day_start_str}' "
            f"and createdTime <= '{day_end_str}' "
            "and trashed = false"
        )
        search_url = (
            'https://www.googleapis.com/drive/v3/files'
            '?q=' + urllib.parse.quote(query) +
            '&orderBy=createdTime+desc&pageSize=10&fields=files(id,name,createdTime)'
        )
        req = urllib.request.Request(
            search_url,
            headers={'Authorization': f'Bearer {access_token}'},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
        except Exception as e:
            raise UserError(_(
                "Step 5 FAILED: Google Drive search request failed.\n"
                "Error: %s"
            ) % str(e))

        files = result.get('files', [])
        if not files:
            raise UserError(_(
                "Step 5 FAILED: No transcript found in Google Drive.\n"
                "Searched for Google Docs with 'Transcript' in name on date: %s\n\n"
                "Possible causes:\n"
                "• Transcript still processing — wait 5–10 minutes after meeting ends\n"
                "• Transcript feature not enabled in Google Workspace Admin Console\n"
                "• 'Meet Recordings' folder not shared with service account:\n"
                "  %s\n"
                "  → Open Google Drive → right-click 'Meet Recordings' → Share → add this email"
            ) % (self.date, self._get_service_account_email()))

        # Step 6 — Export transcript as plain text
        file_id = files[0]['id']
        file_name = files[0].get('name', file_id)
        export_url = (
            f'https://www.googleapis.com/drive/v3/files/{file_id}/export'
            '?mimeType=text/plain'
        )
        req = urllib.request.Request(
            export_url,
            headers={'Authorization': f'Bearer {access_token}'},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                transcript = resp.read().decode('utf-8', errors='ignore')
        except Exception as e:
            raise UserError(_(
                "Step 6 FAILED: Found transcript '%s' but could not download it.\n"
                "Error: %s"
            ) % (file_name, str(e)))

        if not transcript.strip():
            raise UserError(_(
                "Step 6 FAILED: Transcript document '%s' is empty.\n"
                "The meeting may not have had any spoken words captured."
            ) % file_name)

        self.transcript = transcript
        self.message_post(
            body=_("<b>Google Meet transcript fetched from Drive.</b> "
                   "Source file: %s. Click Analyze with AI to generate insights.") % file_name,
            subtype_xmlid='mail.mt_note',
        )

    def _get_service_account_email(self):
        """Return client_email from the stored service account JSON, for error messages."""
        try:
            sa_json = self.env['ir.config_parameter'].sudo().get_param(
                'ai_meeting_debrief.google_service_account_json'
            )
            return json.loads(sa_json).get('client_email', '(unknown)')
        except Exception:
            return '(could not parse JSON)'

    def action_fetch_google_meet_recording(self):
        """Fetch Google Meet MP4 recording from Google Drive and attach as video_file."""
        self.ensure_one()

        # Step 1 — Service account configured?
        sa_json = self.env['ir.config_parameter'].sudo().get_param(
            'ai_meeting_debrief.google_service_account_json'
        )
        if not sa_json:
            raise UserError(_(
                "Google Service Account is not configured.\n"
                "Go to Settings → AI Meeting Debrief → Google Meet Integration → paste JSON → Save."
            ))

        # Step 2 — Linked to a calendar event?
        if not self.calendar_event_id:
            raise UserError(_(
                "This debrief is not linked to a Calendar Event.\n"
                "Set the 'Calendar Event' field to your Google Meet event, or create the "
                "debrief by clicking 'Create Debrief' from the Calendar event itself."
            ))

        # Step 3 — Calendar event has a Meet link?
        location = self.calendar_event_id.videocall_location or ''
        if 'meet.google.com' not in location:
            raise UserError(_(
                "The linked calendar event has no Google Meet link.\n"
                "Video call location found: '%s'\n"
                "Make sure the calendar event was created with Google Meet conferencing."
            ) % (location or '(empty)'))

        # Step 4 — Authenticate with Google Drive
        access_token = self._get_google_access_token()
        if not access_token:
            raise UserError(_(
                "Could not authenticate with Google Drive.\n"
                "Possible causes:\n"
                "• Service Account JSON is invalid or incomplete\n"
                "• Google Drive API is not enabled in your Google Cloud project\n"
                "Go to console.cloud.google.com → APIs & Services → Enable 'Google Drive API'."
            ))

        # Step 5 — Search Drive for MP4 recording
        # Use ±2 days around the meeting date to absorb timezone differences
        # and Google's variable processing delay.
        from datetime import datetime as dt, timedelta as tdelta
        meeting_date = self.date or fields.Date.today()
        window_start = (meeting_date - tdelta(days=1))
        window_end   = (meeting_date + tdelta(days=2))

        day_start = dt(
            window_start.year, window_start.month, window_start.day, 0, 0, 0
        ).strftime('%Y-%m-%dT%H:%M:%SZ')
        day_end = dt(
            window_end.year, window_end.month, window_end.day, 23, 59, 59
        ).strftime('%Y-%m-%dT%H:%M:%SZ')

        query = (
            "mimeType = 'video/mp4' "
            f"and createdTime >= '{day_start}' "
            f"and createdTime <= '{day_end}' "
            "and trashed = false"
        )
        files = self._drive_search(access_token, query)

        # Fallback — if nothing in the date window, check the last 7 days
        # so the user gets a meaningful error instead of "not found"
        if not files:
            fallback_query = "mimeType = 'video/mp4' and trashed = false"
            any_videos = self._drive_search(access_token, fallback_query, page_size=5)

            sa_email = self._get_service_account_email()
            if not any_videos:
                raise UserError(_(
                    "No MP4 files are visible to the service account at all.\n\n"
                    "ACTION REQUIRED — share the 'Meet Recordings' folder:\n"
                    "1. Open drive.google.com\n"
                    "2. Find the 'Meet Recordings' folder (created automatically by Google Meet)\n"
                    "3. Right-click → Share\n"
                    "4. Add this email as Viewer:\n"
                    "   %s\n"
                    "5. Click Send, then try again.\n\n"
                    "If the folder does not exist yet, your first recording will create it.\n"
                    "Make sure Cloud Recording is enabled in Google Workspace Admin Console:\n"
                    "  admin.google.com → Apps → Google Workspace → Google Meet → Recording"
                ) % sa_email)
            else:
                names = ', '.join(f['name'] for f in any_videos)
                raise UserError(_(
                    "No recording found for the meeting date: %s\n\n"
                    "The service account CAN see Drive (found: %s) but not a recording "
                    "near this meeting's date.\n\n"
                    "Possible causes:\n"
                    "• Recording is still processing — wait 10–30 min after meeting ends and retry\n"
                    "• The recording was saved to a different date (timezone difference)\n"
                    "• Cloud Recording was not started during this specific meeting"
                ) % (self.date, names))

        # Pick the recording closest to the meeting date
        file_info = files[0]
        file_id   = file_info['id']
        file_name = file_info.get('name', 'recording.mp4')
        file_size = int(file_info.get('size', 0))
        size_mb   = file_size / 1024 / 1024

        _logger.info("Google Meet recording found: %s (%.1f MB)", file_name, size_mb)

        # Step 6 — Download the video file
        download_url = f'https://www.googleapis.com/drive/v3/files/{file_id}?alt=media'
        req = urllib.request.Request(
            download_url,
            headers={'Authorization': f'Bearer {access_token}'},
        )
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                video_data = resp.read()
        except Exception as e:
            raise UserError(_(
                "Found recording '%s' but could not download it.\nError: %s"
            ) % (file_name, str(e)))

        # Step 7 — Attach to the debrief record
        self.write({
            'video_file':     base64.b64encode(video_data).decode(),
            'video_filename': file_name,
        })
        self.message_post(
            body=_(
                "<b>Google Meet recording fetched from Drive.</b><br/>"
                "File: <b>%s</b> (%.1f MB)<br/>"
                "Click <b>Analyze with AI</b> to send the video to Gemini and generate insights."
            ) % (file_name, size_mb),
            subtype_xmlid='mail.mt_note',
        )

    def _drive_search(self, access_token, query, page_size=10):
        """Run a Google Drive files.list query and return the files list."""
        url = (
            'https://www.googleapis.com/drive/v3/files'
            '?q=' + urllib.parse.quote(query) +
            f'&orderBy=createdTime+desc&pageSize={page_size}'
            '&fields=files(id,name,size,createdTime)'
        )
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {access_token}'})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode()).get('files', [])
        except Exception as e:
            raise UserError(_("Google Drive search failed: %s") % str(e))

    # -------------------------------------------------------------------------
    # Dashboard data
    # -------------------------------------------------------------------------

    @api.model
    def get_dashboard_stats(self, date_from=None, date_to=None):
        today = date.today()

        try:
            df = datetime.strptime(date_from, '%Y-%m-%d').date() if date_from else today.replace(day=1)
        except (ValueError, TypeError):
            df = today.replace(day=1)

        try:
            dt = datetime.strptime(date_to, '%Y-%m-%d').date() if date_to else today
        except (ValueError, TypeError):
            dt = today

        date_domain = [
            ('date', '>=', df.strftime('%Y-%m-%d 00:00:00')),
            ('date', '<=', dt.strftime('%Y-%m-%d 23:59:59')),
        ]

        ActionItem = self.env['ai.meeting.action.item']
        ai_domain = [
            ('debrief_id.date', '>=', df.strftime('%Y-%m-%d 00:00:00')),
            ('debrief_id.date', '<=', dt.strftime('%Y-%m-%d 23:59:59')),
        ]
        total_actions   = ActionItem.search_count(ai_domain)
        done_actions    = ActionItem.search_count(ai_domain + [('state', '=', 'done')])
        pending_actions = ActionItem.search_count(ai_domain + [('state', '=', 'pending')])
        completion_pct  = round(done_actions / total_actions * 100) if total_actions else 0

        recent = self.search(date_domain, order='date desc, id desc', limit=5)
        recent_data = [
            {
                'id': m.id,
                'name': m.name,
                'date': m.date.strftime('%b %d, %Y') if m.date else '',
                'state': m.state,
                'state_label': 'Analyzed' if m.state == 'done' else 'Draft',
                'action_item_count': len(m.action_item_ids),
            }
            for m in recent
        ]

        return {
            'total_meetings':     self.search_count(date_domain),
            'analyzed_in_period': self.search_count([('state', '=', 'done')] + date_domain),
            'pending_actions':    pending_actions,
            'hr_alerts':          self.search_count([('hr_alert', '=', True)] + date_domain),
            'action_stats': {
                'total':          total_actions,
                'done':           done_actions,
                'pending':        pending_actions,
                'completion_pct': completion_pct,
            },
            'recent_meetings': recent_data,
        }

    # -------------------------------------------------------------------------
    # Cron: auto-create draft debriefs for recently ended calendar events
    # -------------------------------------------------------------------------

    @api.model
    def _cron_auto_create_debriefs(self):
        now = fields.Datetime.now()
        two_hours_ago = now - timedelta(hours=2)

        events = self.env['calendar.event'].sudo().search([
            ('stop', '>=', two_hours_ago),
            ('stop', '<=', now),
            ('auto_debrief_generated', '=', False),
            ('user_id.active', '=', True),
        ])

        for event in events:
            existing = self.search([('calendar_event_id', '=', event.id)], limit=1)
            if existing:
                event.sudo().write({'auto_debrief_generated': True})
                continue

            partner_ids = event.partner_ids
            employees = self.env['hr.employee'].sudo().search([
                '|',
                ('work_contact_id', 'in', partner_ids.ids),
                ('user_id.partner_id', 'in', partner_ids.ids),
            ])

            debrief = self.sudo().create({
                'name': event.name,
                'date': event.start,
                'calendar_event_id': event.id,
                'attendee_ids': [(6, 0, employees.ids)],
                'state': 'draft',
            })

            # Try to auto-fetch transcript: Zoom first, then Google Meet
            auto_transcript = (
                debrief._fetch_zoom_transcript()
                or debrief._fetch_google_meet_transcript()
            )
            if auto_transcript:
                debrief.sudo().write({'transcript': auto_transcript})
                msg = _(
                    "<b>Your meeting has ended.</b><br/>"
                    "Transcript fetched automatically. "
                    "Click <b>Analyze with AI</b> to generate insights."
                )
            else:
                msg = _(
                    "<b>Your meeting has ended.</b><br/>"
                    "Open this debrief, add a transcript, and click <b>Analyze with AI</b>."
                )

            debrief.message_post(
                body=msg,
                partner_ids=[event.user_id.partner_id.id] if event.user_id.partner_id else [],
                subtype_xmlid='mail.mt_note',
            )

            event.sudo().write({'auto_debrief_generated': True})
