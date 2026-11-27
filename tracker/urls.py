from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('register/', views.register_view, name='register'),
    path('login/', auth_views.LoginView.as_view(template_name='registration/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('projects/', views.project_list, name='project_list'),
    path('projects/new/', views.project_create, name='project_create'),
    path('projects/<int:pk>/', views.project_detail, name='project_detail'),
    path('projects/<int:pk>/edit/', views.project_edit, name='project_edit'),
    path('projects/<int:pk>/tasks/new/', views.task_create, name='task_create'),
    path('tasks/<int:pk>/edit/', views.task_edit, name='task_edit'),
    path('tasks/<int:pk>/status/', views.task_quick_status, name='task_quick_status'),
    path('tasks/<int:pk>/delete/', views.task_delete, name='task_delete'),
    path('projects/<int:pk>/client-update/', views.submit_client_update, name='submit_client_update'),
    path('projects/<int:pk>/ai-intake/', views.ai_client_intake, name='ai_client_intake'),
    path('projects/<int:pk>/ai-followup/', views.ai_followup_and_summary, name='ai_followup'),
]
