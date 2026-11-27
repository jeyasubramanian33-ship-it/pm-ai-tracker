from django.contrib.auth.models import User
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone


class UserProfile(models.Model):
    """Stores whether a user logs in as a Coordinator or a Client."""

    class Role(models.TextChoices):
        COORDINATOR = 'coordinator', 'Coordinator'
        CLIENT = 'client', 'Client'

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.COORDINATOR)
    company_name = models.CharField(max_length=120, blank=True)

    def __str__(self):
        return f'{self.user.username} ({self.get_role_display()})'

    @property
    def is_coordinator(self):
        return self.role == self.Role.COORDINATOR

    @property
    def is_client(self):
        return self.role == self.Role.CLIENT


@receiver(post_save, sender=User)
def ensure_user_profile(sender, instance, created, **kwargs):
    """Ensure every Django User always has an associated UserProfile."""
    if created:
        UserProfile.objects.get_or_create(user=instance)


class Project(models.Model):
    """A tracked client project managed by a coordinator."""

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        ON_HOLD = 'on_hold', 'On Hold'
        COMPLETED = 'completed', 'Completed'

    name = models.CharField(max_length=160)
    client_name = models.CharField(max_length=160)
    client_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='client_projects',
        help_text='Optional client login account that can view this project and submit updates.',
    )
    coordinator = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='coordinated_projects',
    )
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', 'name']

    def __str__(self):
        return f'{self.name} ({self.client_name})'


class Task(models.Model):
    """An individual deliverable inside a project assigned to an internal team member."""

    class Status(models.TextChoices):
        TODO = 'todo', 'To Do'
        IN_PROGRESS = 'in_progress', 'In Progress'
        WAITING_CLIENT = 'waiting_client', 'Waiting on Client'
        BLOCKED = 'blocked', 'Blocked'
        DONE = 'done', 'Done'

    class Priority(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High'

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='tasks')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    assignee_name = models.CharField(
        max_length=120,
        blank=True,
        help_text='Internal team member name (tracked without needing a login).',
    )
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.TODO)
    priority = models.CharField(max_length=20, choices=Priority.choices, default=Priority.MEDIUM)
    due_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['due_date', '-priority', '-updated_at']

    def __str__(self):
        return f'{self.title} [{self.get_status_display()}]'

    @property
    def is_overdue(self):
        return bool(self.due_date and self.status != self.Status.DONE and self.due_date < timezone.localdate())


class UpdateLog(models.Model):
    """Timeline entry recording client updates, internal handoffs, and status changes."""

    class EntryType(models.TextChoices):
        CLIENT_UPDATE = 'client_update', 'Client Update'
        INTERNAL_HANDOFF = 'internal_handoff', 'Internal Team Handoff'
        FOLLOWUP_DRAFT = 'followup_draft', 'Client Follow-Up Draft'
        STATUS_CHANGE = 'status_change', 'Status Change'

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='update_logs')
    task = models.ForeignKey(
        Task,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='update_logs',
    )
    entry_type = models.CharField(max_length=30, choices=EntryType.choices, default=EntryType.CLIENT_UPDATE)
    is_internal_only = models.BooleanField(
        default=False,
        help_text='When checked, hidden from client logins.',
    )
    raw_content = models.TextField()
    ai_output = models.TextField(blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='update_logs',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_entry_type_display()} - {self.project.name}'
