from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('history/', views.signals_history, name='signals_history'),
    path('api/signal-type-variables/', views.get_signal_type_variables, name='get_signal_type_variables'),
    # Authentication URLs
    path('login/', views.user_login, name='user_login'),
    path('logout/', views.user_logout, name='user_logout'),
    # Profile URLs
    path('profile/', views.profile, name='profile'),
    path('profile/change-password/', views.change_password, name='change_password'),
    # User Management URLs
    path('users/', views.user_management, name='user_management'),
    path('users/create/', views.user_create, name='user_create'),
    path('users/<int:user_id>/edit/', views.user_edit, name='user_edit'),
    path('users/<int:user_id>/delete/', views.user_delete, name='user_delete'),
    # Signal Type Builder URLs
    path('signal-types/', views.signal_types_list, name='signal_types_list'),
    path('signal-types/create/', views.signal_type_create, name='signal_type_create'),
    path('signal-types/<int:signal_type_id>/edit/', views.signal_type_edit, name='signal_type_edit'),
    path('signal-types/<int:signal_type_id>/delete/', views.signal_type_delete, name='signal_type_delete'),
]

