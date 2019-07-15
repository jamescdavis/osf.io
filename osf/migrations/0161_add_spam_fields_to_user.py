# -*- coding: utf-8 -*-
# Generated by Django 1.11.15 on 2018-10-24 17:50
from __future__ import unicode_literals

from django.db import migrations, models
from bulk_update.helper import bulk_update
import osf.models.spam
import osf.utils.datetime_aware_jsonfield
import osf.utils.fields


TAG_MAP = {
    'spam_flagged': osf.models.spam.SpamStatus.FLAGGED,
    'spam_confirmed': osf.models.spam.SpamStatus.SPAM,
    'ham_confirmed': osf.models.spam.SpamStatus.HAM
}

def add_spam_status_to_tagged_users(state, schema):
    OSFUser = state.get_model('osf', 'osfuser')
    users_with_tag = OSFUser.objects.filter(tags__name__in=TAG_MAP.keys()).prefetch_related('tags')
    users_to_update = []
    for user in users_with_tag:
        for tag, value in TAG_MAP.items():
            if user.tags.filter(system=True, name=tag).exists():
                user.spam_status = value
        users_to_update.append(user)
    bulk_update(users_to_update, update_fields=['spam_status'])

def remove_spam_status_from_tagged_users(state, schema):
    OSFUser = state.get_model('osf', 'osfuser')
    users_with_tag = OSFUser.objects.filter(tags__name__in=TAG_MAP.keys())
    users_with_tag.update(spam_status=None)


class Migration(migrations.Migration):

    dependencies = [
        ('osf', '0160_merge_20190408_1618'),
    ]

    operations = [
        migrations.AddField(
            model_name='osfuser',
            name='date_last_reported',
            field=osf.utils.fields.NonNaiveDateTimeField(blank=True, db_index=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name='osfuser',
            name='reports',
            field=osf.utils.datetime_aware_jsonfield.DateTimeAwareJSONField(blank=True, default=dict, encoder=osf.utils.datetime_aware_jsonfield.DateTimeAwareJSONEncoder, validators=[osf.models.spam._validate_reports]),
        ),
        migrations.AddField(
            model_name='osfuser',
            name='spam_data',
            field=osf.utils.datetime_aware_jsonfield.DateTimeAwareJSONField(blank=True, default=dict, encoder=osf.utils.datetime_aware_jsonfield.DateTimeAwareJSONEncoder),
        ),
        migrations.AddField(
            model_name='osfuser',
            name='spam_pro_tip',
            field=models.CharField(blank=True, default=None, max_length=200, null=True),
        ),
        migrations.AddField(
            model_name='osfuser',
            name='spam_status',
            field=models.IntegerField(blank=True, db_index=True, default=None, null=True),
        ),
        migrations.RunPython(add_spam_status_to_tagged_users, remove_spam_status_from_tagged_users),
    ]