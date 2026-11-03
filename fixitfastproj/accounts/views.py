from django import forms
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView
from django.shortcuts import get_object_or_404, redirect, render


class SectionAuthenticationForm(AuthenticationForm):
    """Login form that only accepts users whose role belongs to the section.

    Users log in with their email address, which is also stored as the username.
    """

    allowed_roles = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].label = 'Email'
        self.fields['username'].widget = forms.EmailInput(attrs={'autofocus': True})

    def clean_username(self):
        # Emails are stored in lower case (RegisterForm.clean_email), so match case-insensitively.
        return self.cleaned_data['username'].strip().lower()

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if user.role not in self.allowed_roles:
            raise forms.ValidationError(
                'This account cannot sign in here. Please use the correct login page.',
                code='wrong_section')


class SectionLoginView(LoginView):
    """Use with as_view(template_name=..., allowed_roles=(...,), next_page=...)."""

    allowed_roles = ()

    def get_form_class(self):
        return type('Form', (SectionAuthenticationForm,), {'allowed_roles': self.allowed_roles})


@login_required
def notifications(request, base_template):
    """List the user's notifications (H08). Opening the page marks them as read."""
    items = list(request.user.notifications.all()[:50])
    request.user.notifications.filter(is_read=False).update(is_read=True)
    return render(request, 'notifications.html', {'items': items, 'base_template': base_template})


@login_required
def open_notification(request, pk):
    item = get_object_or_404(request.user.notifications, pk=pk)
    item.is_read = True
    item.save()
    return redirect(item.link or '/')
