"""FixItFast URL configuration.

    /           storefront for homeowners
    /partner/   handyman partner section (separate login)
    /staff/     internal admin panel for employees (the default Django admin is not used)
"""
from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

urlpatterns = [
    path('', include('storefront.urls')),
    path('partner/', include('partner.urls')),
    path('staff/', include('adminpanel.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
