import json
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Project, Task, UpdateLog, UserProfile
from .services.local_ai import OllamaError, analyze_client_update


class _FakeHTTPResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode('utf-8')

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class TrackerWorkflowTests(TestCase):
    def setUp(self):
        self.coordinator = User.objects.create_user(username='coord1', password='Password123!')
        self.coordinator.profile.role = UserProfile.Role.COORDINATOR
        self.coordinator.profile.save()

        self.client_user = User.objects.create_user(username='client1', password='Password123!')
        self.client_user.profile.role = UserProfile.Role.CLIENT
        self.client_user.profile.save()

        self.other_client = User.objects.create_user(username='client2', password='Password123!')
        self.other_client.profile.role = UserProfile.Role.CLIENT
        self.other_client.profile.save()

        self.project = Project.objects.create(
            name='Website Redesign',
            client_name='Acme Corp',
            client_user=self.client_user,
            coordinator=self.coordinator,
        )
        self.task = Task.objects.create(
            project=self.project,
            title='Approve homepage mockup',
            assignee_name='Riya',
            status=Task.Status.WAITING_CLIENT,
            priority=Task.Priority.HIGH,
        )

    def test_register_creates_client_profile(self):
        resp = self.client.post(
            reverse('register'),
            {
                'username': 'newclient',
                'email': 'client@example.com',
                'role': UserProfile.Role.CLIENT,
                'company_name': 'Beta LLC',
                'password1': 'StrongPass99!',
                'password2': 'StrongPass99!',
            },
        )
        self.assertRedirects(resp, reverse('dashboard'))
        user = User.objects.get(username='newclient')
        self.assertEqual(user.profile.role, UserProfile.Role.CLIENT)
        self.assertEqual(user.profile.company_name, 'Beta LLC')

    def test_client_scoping_and_internal_log_privacy(self):
        UpdateLog.objects.create(
            project=self.project,
            entry_type=UpdateLog.EntryType.CLIENT_UPDATE,
            is_internal_only=False,
            raw_content='Visible client update',
            created_by=self.client_user,
        )
        UpdateLog.objects.create(
            project=self.project,
            entry_type=UpdateLog.EntryType.INTERNAL_HANDOFF,
            is_internal_only=True,
            raw_content='Secret internal handoff',
            ai_output='Assign dev to hotfix immediately.',
            created_by=self.coordinator,
        )

        # Assigned client sees project & public log, not internal log
        self.client.force_login(self.client_user)
        resp = self.client.get(reverse('project_detail', args=[self.project.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Visible client update')
        self.assertNotContains(resp, 'Secret internal handoff')
        self.assertNotContains(resp, 'Assign dev to hotfix immediately.')

        # Client cannot access coordinator-only routes
        for url_name in ('project_create',):
            self.assertEqual(self.client.get(reverse(url_name)).status_code, 403)
        self.assertEqual(
            self.client.get(reverse('ai_client_intake', args=[self.project.pk])).status_code,
            403,
        )

        # Unassigned client cannot view project
        self.client.force_login(self.other_client)
        self.assertEqual(
            self.client.get(reverse('project_detail', args=[self.project.pk])).status_code,
            404,
        )

    def test_client_can_submit_update_and_coordinator_can_change_status(self):
        self.client.force_login(self.client_user)
        resp = self.client.post(
            reverse('submit_client_update', args=[self.project.pk]),
            {'task': self.task.pk, 'message': 'We approved the mockup, ready to build!'},
        )
        self.assertRedirects(resp, reverse('project_detail', args=[self.project.pk]))
        self.assertTrue(
            UpdateLog.objects.filter(
                project=self.project,
                entry_type=UpdateLog.EntryType.CLIENT_UPDATE,
                raw_content__icontains='approved the mockup',
            ).exists()
        )

        self.client.force_login(self.coordinator)
        resp = self.client.post(
            reverse('task_quick_status', args=[self.task.pk]),
            {'status': Task.Status.IN_PROGRESS},
        )
        self.assertRedirects(resp, reverse('project_detail', args=[self.project.pk]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.IN_PROGRESS)

    @patch('tracker.services.local_ai.urllib.request.urlopen')
    def test_analyze_client_update_sanitizes_ollama_json(self, mock_urlopen):
        ollama_reply = {
            'message': {
                'content': json.dumps({
                    'summary': 'Client approved the homepage and requested mobile spacing fix.',
                    'status_updates': [
                        {'task_id': self.task.id, 'new_status': 'in_progress', 'reason': 'Approved'},
                        {'task_id': 999999, 'new_status': 'done', 'reason': 'Fake task'},
                        {'task_id': self.task.id, 'new_status': 'invalid_status', 'reason': 'Bad'},
                    ],
                    'new_tasks': [
                        {'title': 'Adjust mobile header spacing', 'priority': 'high', 'description': 'Per client email'},
                    ],
                    'internal_handoff': '- Riya: start homepage build\n- Team: fix mobile header spacing',
                })
            }
        }
        mock_urlopen.return_value = _FakeHTTPResponse(ollama_reply)

        result = analyze_client_update(
            project=self.project,
            tasks=[self.task],
            client_message='Homepage approved! Also please fix mobile header spacing.',
            model_name='llama3.2',
        )
        self.assertEqual(len(result['status_updates']), 1)
        self.assertEqual(result['status_updates'][0]['new_status'], 'in_progress')
        self.assertEqual(len(result['new_tasks']), 1)
        self.assertEqual(result['new_tasks'][0]['title'], 'Adjust mobile header spacing')
        self.assertIn('start homepage build', result['internal_handoff'])

    @patch('tracker.services.local_ai.urllib.request.urlopen')
    def test_ai_intake_apply_creates_task_updates_status_and_saves_internal_handoff(self, mock_urlopen):
        mock_urlopen.side_effect = OSError('Connection refused')
        self.client.force_login(self.coordinator)

        # Even if Ollama is offline, page renders gracefully with error message
        resp = self.client.post(
            reverse('ai_client_intake', args=[self.project.pk]),
            {'action': 'analyze', 'client_message': 'Mockup approved!', 'model_name': 'llama3.2'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Could not connect to local Ollama')

        # Applying reviewed suggestions updates DB cleanly
        apply_resp = self.client.post(
            reverse('ai_client_intake', args=[self.project.pk]),
            {
                'action': 'apply',
                'client_message': 'Mockup approved! Add mobile spacing fix.',
                'apply_status': [str(self.task.id)],
                f'new_status_{self.task.id}': Task.Status.IN_PROGRESS,
                'apply_new_task': ['0'],
                'new_task_title_0': 'Fix mobile spacing',
                'new_task_priority_0': Task.Priority.HIGH,
                'new_task_desc_0': 'Requested by client',
                'internal_handoff': '- Riya: proceed with homepage\n- Fix mobile spacing',
            },
        )
        self.assertRedirects(apply_resp, reverse('project_detail', args=[self.project.pk]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.IN_PROGRESS)
        self.assertTrue(Task.objects.filter(project=self.project, title='Fix mobile spacing').exists())
        self.assertTrue(
            UpdateLog.objects.filter(
                project=self.project,
                entry_type=UpdateLog.EntryType.INTERNAL_HANDOFF,
                is_internal_only=True,
            ).exists()
        )
