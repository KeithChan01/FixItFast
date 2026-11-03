from django.contrib.auth.views import LogoutView
from django.urls import path

from accounts.models import User
from accounts.views import SectionLoginView, notifications, open_notification

from . import views

app_name = 'adminpanel'

urlpatterns = [
    # A01
    path('login/', SectionLoginView.as_view(
        template_name='adminpanel/login.html',
        allowed_roles=(User.Role.EMPLOYEE, User.Role.MANAGER),
        next_page='adminpanel:dashboard',
    ), name='login'),
    path('logout/', LogoutView.as_view(next_page='adminpanel:login'), name='logout'),
    path('notifications/', notifications, {'base_template': 'adminpanel/base.html'}, name='notifications'),
    path('notifications/<int:pk>/', open_notification, name='open_notification'),
    # A02
    path('', views.dashboard, name='dashboard'),
    # A03
    path('catalogue/', views.catalogue, name='catalogue'),
    path('catalogue/categories/new/', views.category_form, name='category_new'),
    path('catalogue/categories/<int:pk>/', views.category_form, name='category_edit'),
    path('catalogue/job-types/new/', views.job_type_form, name='job_type_new'),
    path('catalogue/job-types/<int:pk>/', views.job_type_form, name='job_type_edit'),
    # A04
    path('applications/', views.applications, name='applications'),
    path('applications/<int:pk>/', views.application_detail, name='application_detail'),
    path('applications/<int:pk>/decide/', views.decide_application, name='decide_application'),
    path('documents/<int:pk>/review/', views.review_document, name='review_document'),
    # A05
    path('handymen/', views.handymen, name='handymen'),
    path('handymen/<int:pk>/', views.handyman_detail, name='handyman_detail'),
    path('handymen/<int:pk>/action/', views.handyman_action, name='handyman_action'),
    # A06
    path('quotations/', views.quotation_queue, name='quotation_queue'),
    path('quotations/<int:pk>/', views.quotation_detail, name='quotation_detail'),
    # A07
    path('parts/', views.part_queue, name='part_queue'),
    path('parts/<int:pk>/', views.part_detail, name='part_detail'),
    # A08
    path('claims/', views.claims, name='claims'),
    path('claims/<int:pk>/', views.claim_detail, name='claim_detail'),
    # A09
    path('homeowners/', views.homeowners, name='homeowners'),
    path('homeowners/<int:pk>/', views.homeowner_detail, name='homeowner_detail'),
    # A10
    path('jobs/', views.jobs, name='jobs'),
    path('jobs/<int:pk>/', views.job_detail, name='job_detail'),
]
