from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from .models import Project, Task, UpdateLog, UserProfile


class RoleRegistrationForm(UserCreationForm):
    """Registration form allowing signup as either a Coordinator or a Client."""

    email = forms.EmailField(required=False)
    role = forms.ChoiceField(
        choices=UserProfile.Role.choices,
        initial=UserProfile.Role.COORDINATOR,
        help_text='Choose Coordinator to manage projects, or Client to view your projects and post updates.',
    )
    company_name = forms.CharField(
        max_length=120,
        required=False,
        help_text='Optional (useful for Client accounts).',
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'email', 'role', 'company_name')

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.role = self.cleaned_data['role']
            profile.company_name = self.cleaned_data.get('company_name', '').strip()
            profile.save()
        return user


class ProjectForm(forms.ModelForm):
    """Form used by coordinators to create or edit a project."""

    class Meta:
        model = Project
        fields = ('name', 'client_name', 'client_user', 'status', 'description')
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['client_user'].queryset = User.objects.filter(
            profile__role=UserProfile.Role.CLIENT
        ).order_by('username')
        self.fields['client_user'].required = False
        self.fields['client_user'].empty_label = '— No client login assigned —'


class TaskForm(forms.ModelForm):
    """Form used by coordinators to create or edit a task."""

    class Meta:
        model = Task
        fields = ('title', 'assignee_name', 'status', 'priority', 'due_date', 'description')
        widgets = {
            'due_date': forms.DateInput(attrs={'type': 'date'}),
            'description': forms.Textarea(attrs={'rows': 3}),
        }


class ClientUpdateForm(forms.Form):
    """Form allowing a client (or coordinator logging on client's behalf) to post an update."""

    task = forms.ModelChoiceField(
        queryset=Task.objects.none(),
        required=False,
        empty_label='— General project update —',
    )
    message = forms.CharField(
        widget=forms.Textarea(
            attrs={
                'rows': 4,
                'placeholder': 'Share your update, feedback, or questions...',
            }
        ),
        label='Update Message',
    )

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        if project is not None:
            self.fields['task'].queryset = project.tasks.all().order_by('title')


class AIClientIntakeForm(forms.Form):
    """Form for coordinators to paste a client message for local Ollama analysis."""

    client_message = forms.CharField(
        widget=forms.Textarea(
            attrs={
                'rows': 6,
                'placeholder': 'Paste the client email, chat message, or meeting note here...',
            }
        ),
        label='Client Message / Email',
    )
    model_name = forms.CharField(
        max_length=80,
        required=False,
        help_text='Leave blank to use default Ollama model.',
    )
