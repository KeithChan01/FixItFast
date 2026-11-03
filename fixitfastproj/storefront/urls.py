from django.contrib.auth.views import LogoutView
from django.urls import path

from accounts.models import User
from accounts.views import SectionLoginView, notifications, open_notification

from . import views

app_name = 'storefront'

urlpatterns = [
    path('', views.home, name='home'),
    path('login/', SectionLoginView.as_view(
        template_name='storefront/login.html',
        allowed_roles=(User.Role.HOMEOWNER,),
        next_page='storefront:home',
    ), name='login'),
    path('logout/', LogoutView.as_view(next_page='storefront:home'), name='logout'),
    path('notifications/', notifications, {'base_template': 'storefront/base.html'}, name='notifications'),
    path('notifications/<int:pk>/', open_notification, name='open_notification'),

    # H01
    path('register/', views.register, name='register'),
    path('account/', views.account, name='account'),
    path('account/addresses/<int:pk>/default/', views.address_default, name='address_default'),
    path('account/addresses/<int:pk>/delete/', views.address_delete, name='address_delete'),
    # H02
    path('services/', views.services_list, name='services'),
    path('tiers/', views.tiers, name='tiers'),
    # H03 to H07: booking flow
    path('book/', views.job_request, name='job_request'),
    path('jobs/<int:pk>/tier/', views.choose_tier, name='choose_tier'),
    path('jobs/<int:pk>/slots/', views.choose_slot, name='choose_slot'),
    path('jobs/<int:pk>/handyman/', views.recommendation, name='recommendation'),
    path('jobs/<int:pk>/quotation/', views.quotation, name='quotation'),
    # H08
    path('jobs/', views.jobs, name='jobs'),
    path('jobs/<int:pk>/', views.job_detail, name='job_detail'),
    path('jobs/<int:pk>/cancel/', views.cancel_job, name='cancel_job'),
    path('jobs/<int:pk>/receipt/', views.receipt, name='receipt'),
    # H09
    path('parts/<int:pk>/', views.part_decision, name='part_decision'),
    # H10
    path('jobs/<int:pk>/confirm/', views.confirm_job, name='confirm_job'),
    # H11
    path('jobs/<int:pk>/claim/', views.raise_claim, name='raise_claim'),
]
