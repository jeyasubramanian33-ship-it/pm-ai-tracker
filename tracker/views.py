from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import (
    AIClientIntakeForm,
    ClientUpdateForm,
    ProjectForm,
    RoleRegistrationForm,
    TaskForm,
)
from .models import Project, Task, UpdateLog, UserProfile
from .services.local_ai import (
    OllamaError,
    analyze_client_update,
    generate_client_followup,
    generate_project_summary,
    list_local_models,
)


def _get_profile(user):
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


def _is_coordinator(user):
    return user.is_authenticated and _get_profile(user).is_coordinator


def _require_coordinator(user):
    if not _is_coordinator(user):
        raise PermissionDenied('Only coordinators can perform this action.')


def _accessible_projects(user):
    if _is_coordinator(user):
        return Project.objects.filter(coordinator=user)
    return Project.objects.filter(client_user=user)


def _get_project_or_404(user, pk):
    return get_object_or_404(_accessible_projects(user), pk=pk)


def register_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        form = RoleRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, f'Welcome, {user.username}! Your account is ready.')
            return redirect('dashboard')
    else:
        form = RoleRegistrationForm()

    return render(request, 'registration/register.html', {'form': form})


@login_required
def dashboard(request):
    profile = _get_profile(request.user)
    projects = _accessible_projects(request.user).annotate(
        total_tasks=Count('tasks'),
        done_tasks=Count('tasks', filter=Q(tasks__status=Task.Status.DONE)),
    )
    today = timezone.localdate()
    base_tasks = Task.objects.filter(project__in=projects).select_related('project')

    if profile.is_coordinator:
        urgent_tasks = base_tasks.filter(
            Q(status__in=[Task.Status.BLOCKED, Task.Status.WAITING_CLIENT])
            | (Q(due_date__lt=today) & ~Q(status=Task.Status.DONE))
        ).distinct()[:10]
        recent_client_updates = UpdateLog.objects.filter(
            project__in=projects,
            entry_type=UpdateLog.EntryType.CLIENT_UPDATE,
        ).select_related('project', 'task', 'created_by')[:6]

        context = {
            'is_coordinator': True,
            'projects': projects[:6],
            'active_projects_count': projects.filter(status=Project.Status.ACTIVE).count(),
            'blocked_count': base_tasks.filter(status=Task.Status.BLOCKED).count(),
            'waiting_client_count': base_tasks.filter(status=Task.Status.WAITING_CLIENT).count(),
            'overdue_count': base_tasks.filter(due_date__lt=today).exclude(status=Task.Status.DONE).count(),
            'urgent_tasks': urgent_tasks,
            'recent_client_updates': recent_client_updates,
        }
        return render(request, 'tracker/dashboard.html', context)

    # Client dashboard
    waiting_tasks = base_tasks.filter(status=Task.Status.WAITING_CLIENT)
    context = {
        'is_coordinator': False,
        'projects': projects,
        'total_projects_count': projects.count(),
        'open_tasks_count': base_tasks.exclude(status=Task.Status.DONE).count(),
        'waiting_on_me_count': waiting_tasks.count(),
        'done_tasks_count': base_tasks.filter(status=Task.Status.DONE).count(),
        'waiting_tasks': waiting_tasks[:10],
    }
    return render(request, 'tracker/dashboard.html', context)


@login_required
def project_list(request):
    is_coord = _is_coordinator(request.user)
    projects = _accessible_projects(request.user).annotate(
        total_tasks=Count('tasks'),
        open_tasks=Count('tasks', filter=~Q(tasks__status=Task.Status.DONE)),
        blocked_tasks=Count('tasks', filter=Q(tasks__status=Task.Status.BLOCKED)),
    )
    return render(
        request,
        'tracker/project_list.html',
        {'projects': projects, 'is_coordinator': is_coord},
    )


@login_required
def project_create(request):
    _require_coordinator(request.user)
    if request.method == 'POST':
        form = ProjectForm(request.POST)
        if form.is_valid():
            project = form.save(commit=False)
            project.coordinator = request.user
            project.save()
            messages.success(request, f'Project "{project.name}" created.')
            return redirect('project_detail', pk=project.pk)
    else:
        form = ProjectForm()
    return render(request, 'tracker/project_form.html', {'form': form, 'is_edit': False})


@login_required
def project_edit(request, pk):
    _require_coordinator(request.user)
    project = _get_project_or_404(request.user, pk)
    if request.method == 'POST':
        form = ProjectForm(request.POST, instance=project)
        if form.is_valid():
            form.save()
            messages.success(request, 'Project details updated.')
            return redirect('project_detail', pk=project.pk)
    else:
        form = ProjectForm(instance=project)
    return render(
        request,
        'tracker/project_form.html',
        {'form': form, 'project': project, 'is_edit': True},
    )


@login_required
def project_detail(request, pk):
    is_coord = _is_coordinator(request.user)
    project = _get_project_or_404(request.user, pk)

    tasks = project.tasks.all()
    status_filter = request.GET.get('status', '').strip()
    priority_filter = request.GET.get('priority', '').strip()
    search_query = request.GET.get('q', '').strip()

    if status_filter:
        tasks = tasks.filter(status=status_filter)
    if priority_filter:
        tasks = tasks.filter(priority=priority_filter)
    if search_query:
        tasks = tasks.filter(
            Q(title__icontains=search_query)
            | Q(description__icontains=search_query)
            | Q(assignee_name__icontains=search_query)
        )

    logs = project.update_logs.select_related('task', 'created_by')
    if not is_coord:
        logs = logs.filter(is_internal_only=False)

    client_update_form = ClientUpdateForm(project=project)

    context = {
        'project': project,
        'tasks': tasks,
        'logs': logs[:25],
        'is_coordinator': is_coord,
        'status_choices': Task.Status.choices,
        'priority_choices': Task.Priority.choices,
        'status_filter': status_filter,
        'priority_filter': priority_filter,
        'search_query': search_query,
        'client_update_form': client_update_form,
    }
    return render(request, 'tracker/project_detail.html', context)


@login_required
def task_create(request, pk):
    _require_coordinator(request.user)
    project = _get_project_or_404(request.user, pk)
    if request.method == 'POST':
        form = TaskForm(request.POST)
        if form.is_valid():
            task = form.save(commit=False)
            task.project = project
            task.save()
            messages.success(request, f'Task "{task.title}" added.')
            return redirect('project_detail', pk=project.pk)
    else:
        form = TaskForm()
    return render(
        request,
        'tracker/task_form.html',
        {'form': form, 'project': project, 'is_edit': False},
    )


@login_required
def task_edit(request, pk):
    _require_coordinator(request.user)
    task = get_object_or_404(Task, pk=pk, project__coordinator=request.user)
    old_status = task.status
    if request.method == 'POST':
        form = TaskForm(request.POST, instance=task)
        if form.is_valid():
            updated_task = form.save()
            if updated_task.status != old_status:
                UpdateLog.objects.create(
                    project=updated_task.project,
                    task=updated_task,
                    entry_type=UpdateLog.EntryType.STATUS_CHANGE,
                    is_internal_only=False,
                    raw_content=(
                        f'Task "{updated_task.title}" status updated to '
                        f'{updated_task.get_status_display()}.'
                    ),
                    created_by=request.user,
                )
            messages.success(request, f'Task "{updated_task.title}" updated.')
            return redirect('project_detail', pk=updated_task.project.pk)
    else:
        form = TaskForm(instance=task)
    return render(
        request,
        'tracker/task_form.html',
        {'form': form, 'project': task.project, 'task': task, 'is_edit': True},
    )


@login_required
@require_POST
def task_quick_status(request, pk):
    _require_coordinator(request.user)
    task = get_object_or_404(Task, pk=pk, project__coordinator=request.user)
    new_status = request.POST.get('status', '')
    valid_statuses = {choice[0] for choice in Task.Status.choices}
    if new_status in valid_statuses and new_status != task.status:
        old_label = task.get_status_display()
        task.status = new_status
        task.save()
        UpdateLog.objects.create(
            project=task.project,
            task=task,
            entry_type=UpdateLog.EntryType.STATUS_CHANGE,
            is_internal_only=False,
            raw_content=f'Task "{task.title}" moved from {old_label} to {task.get_status_display()}.',
            created_by=request.user,
        )
        messages.success(request, f'Updated "{task.title}" to {task.get_status_display()}.')
    return redirect('project_detail', pk=task.project.pk)


@login_required
@require_POST
def task_delete(request, pk):
    _require_coordinator(request.user)
    task = get_object_or_404(Task, pk=pk, project__coordinator=request.user)
    project_pk = task.project.pk
    title = task.title
    task.delete()
    messages.success(request, f'Task "{title}" deleted.')
    return redirect('project_detail', pk=project_pk)


@login_required
@require_POST
def submit_client_update(request, pk):
    project = _get_project_or_404(request.user, pk)
    form = ClientUpdateForm(request.POST, project=project)
    if form.is_valid():
        UpdateLog.objects.create(
            project=project,
            task=form.cleaned_data['task'],
            entry_type=UpdateLog.EntryType.CLIENT_UPDATE,
            is_internal_only=False,
            raw_content=form.cleaned_data['message'],
            created_by=request.user,
        )
        messages.success(request, 'Update posted to the project timeline.')
    else:
        messages.error(request, 'Please enter an update message before submitting.')
    return redirect('project_detail', pk=project.pk)


@login_required
def ai_client_intake(request, pk):
    _require_coordinator(request.user)
    project = _get_project_or_404(request.user, pk)
    tasks = list(project.tasks.all())
    available_models = list_local_models()
    default_model = getattr(settings, 'OLLAMA_MODEL', 'llama3.2')

    analysis = None
    ai_error = None
    source_log_id = request.GET.get('log_id', '').strip()
    initial_message = ''
    if source_log_id.isdigit():
        log_obj = UpdateLog.objects.filter(pk=int(source_log_id), project=project).first()
        if log_obj:
            initial_message = log_obj.raw_content

    if request.method == 'POST':
        action = request.POST.get('action', 'analyze')
        if action == 'apply':
            client_message = request.POST.get('client_message', '').strip()
            internal_handoff = request.POST.get('internal_handoff', '').strip()
            skip_client_log = request.POST.get('skip_client_log') == '1'

            applied_count = 0
            valid_statuses = {choice[0] for choice in Task.Status.choices}
            for task_id_str in request.POST.getlist('apply_status'):
                if not task_id_str.isdigit():
                    continue
                task = project.tasks.filter(pk=int(task_id_str)).first()
                new_status = request.POST.get(f'new_status_{task_id_str}', '')
                if task and new_status in valid_statuses and task.status != new_status:
                    old_label = task.get_status_display()
                    task.status = new_status
                    task.save()
                    UpdateLog.objects.create(
                        project=project,
                        task=task,
                        entry_type=UpdateLog.EntryType.STATUS_CHANGE,
                        is_internal_only=False,
                        raw_content=(
                            f'Task "{task.title}" moved from {old_label} to '
                            f'{task.get_status_display()} (from client update).'
                        ),
                        created_by=request.user,
                    )
                    applied_count += 1

            created_count = 0
            valid_priorities = {choice[0] for choice in Task.Priority.choices}
            for idx_str in request.POST.getlist('apply_new_task'):
                title = request.POST.get(f'new_task_title_{idx_str}', '').strip()
                if not title:
                    continue
                priority = request.POST.get(f'new_task_priority_{idx_str}', Task.Priority.MEDIUM)
                if priority not in valid_priorities:
                    priority = Task.Priority.MEDIUM
                desc = request.POST.get(f'new_task_desc_{idx_str}', '').strip()
                Task.objects.create(
                    project=project,
                    title=title[:200],
                    priority=priority,
                    description=desc,
                )
                created_count += 1

            if client_message and not skip_client_log:
                UpdateLog.objects.create(
                    project=project,
                    entry_type=UpdateLog.EntryType.CLIENT_UPDATE,
                    is_internal_only=False,
                    raw_content=client_message,
                    created_by=request.user,
                )

            if internal_handoff:
                UpdateLog.objects.create(
                    project=project,
                    entry_type=UpdateLog.EntryType.INTERNAL_HANDOFF,
                    is_internal_only=True,
                    raw_content='Internal Team Handoff (generated from client update)',
                    ai_output=internal_handoff,
                    created_by=request.user,
                )

            messages.success(
                request,
                f'Applied {applied_count} status update(s), created {created_count} task(s), '
                'and saved the internal handoff brief.',
            )
            return redirect('project_detail', pk=project.pk)

        # Analyze action
        form = AIClientIntakeForm(request.POST)
        if form.is_valid():
            client_message = form.cleaned_data['client_message']
            model_name = form.cleaned_data['model_name']
            try:
                analysis = analyze_client_update(
                    project=project,
                    tasks=tasks,
                    client_message=client_message,
                    model_name=model_name,
                )
            except OllamaError as exc:
                ai_error = str(exc)
    else:
        form = AIClientIntakeForm(
            initial={
                'client_message': initial_message,
                'model_name': available_models[0] if available_models else default_model,
            }
        )

    return render(
        request,
        'tracker/ai_intake.html',
        {
            'project': project,
            'form': form,
            'analysis': analysis,
            'ai_error': ai_error,
            'available_models': available_models,
            'default_model': default_model,
            'from_existing_log': bool(initial_message),
        },
    )


@login_required
def ai_followup_and_summary(request, pk):
    _require_coordinator(request.user)
    project = _get_project_or_404(request.user, pk)
    tasks = list(project.tasks.all())
    attention_tasks = [
        t for t in tasks
        if t.status in (Task.Status.WAITING_CLIENT, Task.Status.BLOCKED) or t.is_overdue
    ]
    available_models = list_local_models()
    default_model = getattr(settings, 'OLLAMA_MODEL', 'llama3.2')

    generated_text = None
    mode = None
    ai_error = None
    selected_model = request.POST.get('model_name', '').strip() or (
        available_models[0] if available_models else default_model
    )

    if request.method == 'POST':
        mode = request.POST.get('mode', 'followup')
        try:
            if mode == 'summary':
                generated_text = generate_project_summary(project, tasks, model_name=selected_model)
            else:
                target_tasks = attention_tasks or [t for t in tasks if t.status != Task.Status.DONE]
                generated_text = generate_client_followup(project, target_tasks, model_name=selected_model)
                UpdateLog.objects.create(
                    project=project,
                    entry_type=UpdateLog.EntryType.FOLLOWUP_DRAFT,
                    is_internal_only=True,
                    raw_content='AI Client Follow-Up Draft generated for coordinator review.',
                    ai_output=generated_text,
                    created_by=request.user,
                )
        except OllamaError as exc:
            ai_error = str(exc)

    return render(
        request,
        'tracker/ai_followup.html',
        {
            'project': project,
            'attention_tasks': attention_tasks,
            'available_models': available_models,
            'default_model': default_model,
            'selected_model': selected_model,
            'generated_text': generated_text,
            'mode': mode,
            'ai_error': ai_error,
        },
    )
