# -*- coding: utf-8 -*-
# Generated by Django 1.9 on 2016-04-25 20:15
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('osf_models', '0019_auto_20160425_1216'),
    ]

    operations = [
        migrations.AlterField(
            model_name='tag',
            name='_id',
            field=models.CharField(max_length=1024, unique=True),
        ),
        migrations.AlterField(
            model_name='tag',
            name='lower',
            field=models.CharField(max_length=1024, unique=True),
        ),
    ]
