from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('history/', views.signals_history, name='signals_history'),
    path('api/signal-type-variables/', views.get_signal_type_variables, name='get_signal_type_variables'),
]

