from django.contrib.auth.views import LogoutView
from django.urls import path

from accounts.models import User
from accounts.views import SectionLoginView, notifications, open_notification

from . import views

app_name = 'partner'

urlpatterns = [
    path('', views.home, name='home'),
    path('login/', SectionLoginView.as_view(
        template_name='partner/login.html',
        allowed_roles=(User.Role.HANDYMAN,),
        next_page='partner:home',
    ), name='login'),
    path('logout/', LogoutView.as_view(next_page='partner:login'), name='logout'),
    path('notifications/', notifications, {'base_template': 'partner/base.html'}, name='notifications'),
    path('notifications/<int:pk>/', open_notification, name='open_notification'),

    # P01
    path('apply/', views.apply, name='apply'),
    path('apply/resubmit/', views.resubmit, name='resubmit'),
    # P02
    path('profile/', views.profile, name='profile'),
    path('availability/', views.availability, name='availability'),
    # P03
    path('training/', views.training, name='training'),
    path('training/quiz/', views.quiz, name='quiz'),
    # P04 to P07
    path('jobs/', views.jobs, name='jobs'),
    path('jobs/<int:pk>/', views.job_detail, name='job_detail'),
    path('jobs/<int:pk>/decline/', views.decline_job, name='decline_job'),
    path('jobs/<int:pk>/quote/', views.submit_quote, name='submit_quote'),
    path('jobs/<int:pk>/progress/', views.update_progress, name='update_progress'),
    path('jobs/<int:pk>/photos/', views.upload_photos, name='upload_photos'),
    path('jobs/<int:pk>/parts/', views.add_part, name='add_part'),
]
