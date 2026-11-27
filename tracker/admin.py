from django.contrib import admin

from .models import Project, Task, UpdateLog, UserProfile


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'company_name')
    list_filter = ('role',)
    search_fields = ('user__username', 'user__email', 'company_name')


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'client_name', 'coordinator', 'client_user', 'status', 'updated_at')
    list_filter = ('status',)
    search_fields = ('name', 'client_name')


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('title', 'project', 'assignee_name', 'status', 'priority', 'due_date')
    list_filter = ('status', 'priority', 'project')
    search_fields = ('title', 'assignee_name', 'description')


@admin.register(UpdateLog)
class UpdateLogAdmin(admin.ModelAdmin):
    list_display = ('project', 'task', 'entry_type', 'is_internal_only', 'created_by', 'created_at')
    list_filter = ('entry_type', 'is_internal_only')
    search_fields = ('raw_content', 'ai_output')
