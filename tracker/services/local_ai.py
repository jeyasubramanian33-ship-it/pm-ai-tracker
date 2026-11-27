import json
import urllib.error
import urllib.request

from django.conf import settings

from tracker.models import Task


class OllamaError(Exception):
    """Raised when the local Ollama server is unreachable or returns an invalid response."""


def _base_url():
    return getattr(settings, 'OLLAMA_BASE_URL', 'http://localhost:11434').rstrip('/')


def _default_model():
    return getattr(settings, 'OLLAMA_MODEL', 'llama3.2')


def _timeout():
    return getattr(settings, 'OLLAMA_TIMEOUT', 60)


def list_local_models():
    """Return available Ollama model names, or an empty list if Ollama is offline."""
    url = f'{_base_url()}/api/tags'
    req = urllib.request.Request(url, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return []
    return [m.get('name') for m in payload.get('models', []) if m.get('name')]


def _chat(messages, model_name=None, json_mode=False):
    """Send a chat completion request to the local Ollama server."""
    chosen_model = (model_name or '').strip() or _default_model()
    body = {
        'model': chosen_model,
        'messages': messages,
        'stream': False,
    }
    if json_mode:
        body['format'] = 'json'

    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(
        f'{_base_url()}/api/chat',
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise OllamaError(f'Ollama returned HTTP {exc.code}: {detail}') from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OllamaError(
            f'Could not connect to local Ollama at {_base_url()}. '
            'Make sure Ollama is running (`ollama serve`) and the model is pulled.'
        ) from exc
    except json.JSONDecodeError as exc:
        raise OllamaError('Received invalid JSON from local Ollama server.') from exc

    content = payload.get('message', {}).get('content', '').strip()
    if not content:
        raise OllamaError('Local Ollama returned an empty response.')
    return content


def _extract_json(text):
    """Parse JSON from model output, handling optional markdown fences."""
    cleaned = text.strip()
    if cleaned.startswith('```'):
        lines = cleaned.splitlines()
        if len(lines) >= 2 and lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        cleaned = '\n'.join(lines).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise OllamaError(f'Model did not return valid JSON: {cleaned[:200]}') from exc


def analyze_client_update(project, tasks, client_message, model_name=None):
    """Analyze a client message and return validated task suggestions + internal brief."""
    task_map = {task.id: task for task in tasks}
    valid_statuses = {choice[0] for choice in Task.Status.choices}
    valid_priorities = {choice[0] for choice in Task.Priority.choices}

    task_lines = [
        f'- ID {t.id}: "{t.title}" | status={t.status} | priority={t.priority} | assignee={t.assignee_name or "Unassigned"}'
        for t in tasks
    ]
    tasks_context = '\n'.join(task_lines) if task_lines else '(No existing tasks yet)'

    system_prompt = (
        'You are an experienced Project Coordinator assistant. '
        'Analyze a client update for a software/service project and return ONLY valid JSON with these exact keys:\n'
        '{\n'
        '  "summary": "1-2 sentence summary of the client message",\n'
        '  "status_updates": [\n'
        '    {"task_id": 123, "new_status": "todo|in_progress|waiting_client|blocked|done", "reason": "short reason"}\n'
        '  ],\n'
        '  "new_tasks": [\n'
        '    {"title": "short task title", "priority": "low|medium|high", "description": "short details"}\n'
        '  ],\n'
        '  "internal_handoff": "concise bullet-point brief for the internal team covering changes, priorities, blockers, and next steps"\n'
        '}\n'
        'Only suggest status_updates for task_ids present in the provided list and only when the client message clearly implies a status change.'
    )

    user_prompt = (
        f'Project: {project.name}\n'
        f'Client: {project.client_name}\n\n'
        f'Existing Tasks:\n{tasks_context}\n\n'
        f'Client Message:\n{client_message}'
    )

    raw = _chat(
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        model_name=model_name,
        json_mode=True,
    )
    parsed = _extract_json(raw)

    cleaned_updates = []
    for item in parsed.get('status_updates', []) or []:
        if not isinstance(item, dict):
            continue
        task_id = item.get('task_id')
        new_status = item.get('new_status')
        if task_id in task_map and new_status in valid_statuses:
            task = task_map[task_id]
            if task.status != new_status:
                cleaned_updates.append({
                    'task': task,
                    'new_status': new_status,
                    'new_status_label': dict(Task.Status.choices)[new_status],
                    'reason': str(item.get('reason', '')).strip(),
                })

    cleaned_new_tasks = []
    for item in parsed.get('new_tasks', []) or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get('title', '')).strip()
        if not title:
            continue
        priority = item.get('priority', Task.Priority.MEDIUM)
        if priority not in valid_priorities:
            priority = Task.Priority.MEDIUM
        cleaned_new_tasks.append({
            'title': title[:200],
            'priority': priority,
            'description': str(item.get('description', '')).strip(),
        })

    return {
        'summary': str(parsed.get('summary', '')).strip(),
        'status_updates': cleaned_updates,
        'new_tasks': cleaned_new_tasks,
        'internal_handoff': str(parsed.get('internal_handoff', '')).strip(),
    }


def generate_client_followup(project, attention_tasks, model_name=None):
    """Draft a polite, clear client follow-up message for pending/blocked/overdue tasks."""
    task_lines = [
        f'- "{t.title}" | status: {t.get_status_display()} | due: {t.due_date or "No due date"} | notes: {t.description[:120] or "N/A"}'
        for t in attention_tasks
    ]
    tasks_context = '\n'.join(task_lines) if task_lines else '(No pending or blocked items)'

    system_prompt = (
        'You are a Project Coordinator writing a polite, concise, professional email/message to a client. '
        'Clearly list what input, approval, or information is needed from them, along with any deadlines. '
        'Return only the ready-to-send message text (with a short Subject line at the top).'
    )
    user_prompt = (
        f'Project: {project.name}\n'
        f'Client Name: {project.client_name}\n\n'
        f'Items needing client attention or follow-up:\n{tasks_context}'
    )
    return _chat(
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        model_name=model_name,
        json_mode=False,
    )


def generate_project_summary(project, tasks, model_name=None):
    """Generate a concise coordinator health summary across all project tasks."""
    task_lines = [
        f'- "{t.title}" | status: {t.get_status_display()} | priority: {t.get_priority_display()} '
        f'| assignee: {t.assignee_name or "Unassigned"} | due: {t.due_date or "None"}'
        f'{" (OVERDUE)" if t.is_overdue else ""}'
        for t in tasks
    ]
    tasks_context = '\n'.join(task_lines) if task_lines else '(No tasks yet)'

    system_prompt = (
        'You are a Project Coordinator preparing a crisp status summary for a daily/weekly check-in. '
        'Organize the response into short bullet sections: '
        '1) Overall Health, 2) In Progress / On Track, 3) Blocked or Waiting on Client, 4) Recommended Next Steps.'
    )
    user_prompt = (
        f'Project: {project.name} ({project.get_status_display()})\n'
        f'Client: {project.client_name}\n\n'
        f'Tasks:\n{tasks_context}'
    )
    return _chat(
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        model_name=model_name,
        json_mode=False,
    )
