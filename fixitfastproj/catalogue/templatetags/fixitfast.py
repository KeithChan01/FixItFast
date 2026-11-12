"""Shared template helpers. Load with {% load fixitfast %}."""
from decimal import Decimal

from django import template
from django.utils.html import format_html

from catalogue.models import Tier

register = template.Library()

STATUS_COLOURS = {
    'DRAFT': 'secondary', 'REQUESTED': 'info', 'ASSIGNED': 'info', 'QUOTE_PENDING': 'warning',
    'QUOTE_REVIEW': 'warning', 'AWAITING_PAYMENT': 'primary', 'SCHEDULED': 'primary',
    'ON_THE_WAY': 'primary', 'IN_PROGRESS': 'primary', 'COMPLETED': 'success',
    'CANCELLED': 'secondary', 'DECLINED': 'danger',
    # documents, applications, quotations, parts, claims
    'PENDING': 'warning', 'APPROVED': 'success', 'REJECTED': 'danger', 'UNDER_REVIEW': 'warning',
    'RETURNED': 'warning', 'RELEASED': 'success', 'ACCEPTED': 'success', 'SUBMITTED': 'warning',
    'RETURN_VISIT': 'info', 'RESOLVED': 'success', 'NOT_REQUIRED': 'secondary',
}


@register.filter
def money(value):
    """S$1,234.50"""
    if value in (None, ''):
        return '–'
    return f'S${Decimal(value):,.2f}'


@register.filter
def whole_money(value):
    """S$85, for predicted ranges, which are already rounded to S$5."""
    if value in (None, ''):
        return '–'
    return f'S${Decimal(value):,.0f}'


@register.simple_tag
def status_badge(value, label):
    return format_html('<span class="badge text-bg-{}">{}</span>', STATUS_COLOURS.get(value, 'secondary'), label)


@register.simple_tag
def tier_badge(tier):
    if not tier:
        return ''
    return format_html('<span class="badge badge-tier-{}">{}</span>', tier, Tier(tier).label)


@register.filter
def get_item(mapping, key):
    return mapping.get(key)

CATEGORY_ICONS = {
    'Plumbing': 'bi-droplet', 'Electrical': 'bi-lightning-charge', 'Appliance Repair': 'bi-plug',
    'Furniture Assembly': 'bi-box-seam', 'Minor Installation': 'bi-wrench-adjustable',
}

@register.filter
def category_icon(name):
    return CATEGORY_ICONS.get(name, 'bi-tools')