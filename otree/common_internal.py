#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import absolute_import
import os
import sys
import datetime
import collections
import inspect
import re
import random
import string
import errno
import logging
import hashlib
import requests
import json
import uuid


from os.path import dirname, join
from collections import OrderedDict
from importlib import import_module

import channels
import six
from six.moves import urllib

from django.db import connection
from django.apps import apps
from django.conf import settings
from django.template.defaultfilters import title
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect

import otree

# set to False if using runserver
USE_REDIS = True


def add_params_to_url(url, params):
    url_parts = list(urllib.parse.urlparse(url))

    # use OrderedDict because sometimes we want certain params at end
    # for readability/consistency
    query = OrderedDict(urllib.parse.parse_qsl(url_parts[4]))
    query.update(params)
    url_parts[4] = urllib.parse.urlencode(query)
    return urllib.parse.urlunparse(url_parts)


def id_label_name(id, label):
    if label:
        return '{} (label: {})'.format(id, label)
    return '{}'.format(id)


def git_commit_timestamp():
    root_dir = dirname(settings.BASE_DIR)
    try:
        with open(join(root_dir, 'git_commit_timestamp'), 'r') as f:
            return f.read().strip()
    except IOError:
        return ''


SESSION_CODE_CHARSET = string.ascii_lowercase + string.digits

def random_chars(num_chars):
    return ''.join(random.choice(SESSION_CODE_CHARSET) for _ in range(num_chars))


def random_chars_8():
    return random_chars(8)


def random_chars_10():
    return random_chars(10)


def app_name_format(app_name):
    app_label = app_name.split('.')[-1]
    return title(app_label.replace("_", " "))


def directory_name(path):
    return os.path.basename(os.path.normpath(path))


def get_models_module(app_name):
    module_name = '{}.models'.format(app_name)
    return import_module(module_name)


def get_bots_module(app_name):
    try:
        bots_module_name = '{}.bots'.format(app_name)
        bots_module = import_module(bots_module_name)
    except ImportError:
        bots_module_name = '{}.tests'.format(app_name)
        bots_module = import_module(bots_module_name)
    return bots_module


def get_views_module(app_name):
    module_name = '{}.views'.format(app_name)
    return import_module(module_name)


def get_app_constants(app_name):
    '''Return the ``Constants`` object of a app defined in the models.py file.

    Example::

        >>> from otree.common_internal import get_app_constants
        >>> get_app_constants('demo')
        <class demo.models.Constants at 0x7fed46bdb188>

    '''
    return get_models_module(app_name).Constants



def get_dotted_name(Cls):
    return '{}.{}'.format(Cls.__module__, Cls.__name__)


def get_app_label_from_import_path(import_path):
    app_label = import_path.rsplit(".", 1)[0]
    while "." in app_label:
        app_label = app_label.rsplit(".", 1)[-1]
    return app_label


def get_app_name_from_label(app_label):
    '''
    >>> get_app_name_from_label('simple')
    'tests.simple'

    '''
    return apps.get_app_config(app_label).name


def expand_choice_tuples(choices):
    '''allows the programmer to define choices as a list of values rather
    than (value, display_value)

    '''
    if not choices:
        return None
    if not isinstance(choices[0], (list, tuple)):
        choices = [(value, value) for value in choices]
    return choices


def contract_choice_tuples(choices):
    '''Return only values of a choice tuple. If the choices are simple lists
    without display name the same list is returned

    '''
    if not choices:
        return None
    if not isinstance(choices[0], (list, tuple)):
        return choices
    return [value for value, _ in choices]


def min_players_multiple(players_per_group):
    ppg = players_per_group

    if isinstance(ppg, six.integer_types) and ppg >= 1:
        return ppg
    if isinstance(ppg, (list, tuple)):
        return sum(ppg)
    # else, it's probably None
    return 1


def db_table_exists(table_name):
    """Return True if a table already exists"""
    return table_name in connection.introspection.table_names()


def db_status_ok():
    """Try to execute a simple select * for every model registered
    """
    for Model in apps.get_models():
        table_name = Model._meta.db_table
        if not db_table_exists(table_name):
            return False
    return True


def make_hash(s):
    s += settings.SECRET_KEY
    return hashlib.sha224(s.encode()).hexdigest()[:8]


def check_pypi_for_updates():
    logging.getLogger("requests").setLevel(logging.WARNING)

    try:
        response = requests.get(
            'http://pypi.python.org/pypi/otree-core/json',
            timeout=5,
        )
    except:
        # could be requests.exceptions.Timeout
        # or another error (404? 500? firewall issue etc)
        return {'pypi_connection_error': True}

    data = json.loads(response.content.decode())

    semver_re = re.compile(r'^(\d+)\.(\d+)\.(\d+)$')

    installed_dotted = otree.__version__
    installed_match = semver_re.match(installed_dotted)

    if installed_match:
        # compare to the latest stable release

        installed_tuple = [int(n) for n in installed_match.groups()]

        releases = data['releases']
        newest_tuple = [0, 0, 0]
        newest_dotted = ''
        for release in releases:
            release_match = semver_re.match(release)
            if release_match:
                release_tuple = [int(n) for n in release_match.groups()]
                if release_tuple > newest_tuple:
                    newest_tuple = release_tuple
                    newest_dotted = release
        newest = newest_tuple
        installed = installed_tuple

        update_needed = (newest > installed and (
                newest[0] > installed[0] or newest[1] > installed[1] or
                newest[2] - installed[2] > 5))

    else:
        # compare to the latest release, whether stable or not
        newest_dotted = data['info']['version'].strip()
        update_needed = newest_dotted != installed_dotted

    if update_needed:
        if sys.version_info[0] == 3:
            pip_command = 'pip3'
        else:
            pip_command = 'pip'
        update_message = (
            'Your otree-core package is out-of-date '
            '(version {}; latest is {}). '
            'You should upgrade with:\n '
            '"{} install --upgrade otree-core"\n '
            'and update your requirements_base.txt.'.format(
                installed_dotted, newest_dotted, pip_command))
    else:
        update_message = ''
    return {
        'pypi_connection_error': False,
        'update_needed': update_needed,
        'installed_version': installed_dotted,
        'newest_version': newest_dotted,
        'update_message': update_message,
    }


def pypi_updates_cli():
    result = check_pypi_for_updates()
    if result['pypi_connection_error']:
        return
    if result['update_needed']:
        print(result['update_message'])


def channels_create_session_group_name(pre_create_id):
    return 'wait_for_session_{}'.format(pre_create_id)


def channels_wait_page_group_name(session_pk, page_index,
                                  model_name, model_pk):

    return 'wait-page-{}-page{}-{}{}'.format(
        session_pk, page_index, model_name, model_pk)


def make_sure_path_exists(path):
    try:
        os.makedirs(path)
    except OSError as exception:
        if exception.errno != errno.EEXIST:
            raise exception


def add_empty_migrations_to_all_apps(project_root):
    # for each app in the project folder,
    # add a migrations folder
    # we do it here instead of modifying the games repo directly,
    # because people on older versions of oTree also install
    # from the same repo,
    # and the old resetdb chokes when it encounters an app with migrations
    # TODO: Simplify all this with pathlib when Python support >= 3.4.
    subfolders = next(os.walk(project_root))[1]
    for subfolder in subfolders:
        # ignore folders that start with "." etc...
        if subfolder[0] in string.ascii_letters + '_':
            app_folder = os.path.join(project_root, subfolder)
            models_file_path = os.path.join(app_folder, 'models.py')
            if os.path.isfile(models_file_path):
                migrations_folder_path = os.path.join(app_folder, 'migrations')
                make_sure_path_exists(migrations_folder_path)
                init_file_path = os.path.join(
                    migrations_folder_path, '__init__.py')
                with open(init_file_path, 'a') as f:
                    f.write('')


def validate_identifier(identifier, identifier_description):
    if re.match(r'^[a-zA-Z0-9_]+$', identifier):
        return identifier
    raise ValueError(
        '{} "{}" can only contain letters, numbers, '
        'and underscores (_)'.format(
            identifier_description,
            identifier
        )
    )


def create_session_and_redirect(session_kwargs):
    pre_create_id = uuid.uuid4().hex
    session_kwargs['_pre_create_id'] = pre_create_id
    channels_group_name = channels_create_session_group_name(
        pre_create_id)
    channels.Channel('otree.create_session').send({
        'kwargs': session_kwargs,
        'channels_group_name': channels_group_name
    })

    wait_for_session_url = reverse(
        'WaitUntilSessionCreated', args=(pre_create_id,)
    )
    return HttpResponseRedirect(wait_for_session_url)


def ensure_superuser_exists(*args, **kwargs):
    """
    Creates our default superuser, returns True for success
    and False for failure
    """
    from django.contrib.auth.models import User
    username = settings.ADMIN_USERNAME
    password = settings.ADMIN_PASSWORD
    logger = logging.getLogger('otree')
    if User.objects.filter(username=username).exists():
        # msg = 'Default superuser exists.'
        # logger.info(msg)
        return True
    if not password:
        return False
    assert User.objects.create_superuser(username, email='',
                                         password=password)
    msg = 'Created superuser "{}"'.format(username)
    logger.info(msg)
    return True


def release_any_stale_locks():
    '''
    Need to release locks in case the server was stopped abruptly,
    and the 'finally' block in each lock did not execute
    '''
    from otree.models_concrete import GlobalLockModel, ParticipantLockModel
    for LockModel in [GlobalLockModel, ParticipantLockModel]:
        try:
            LockModel.objects.filter(locked=True).update(locked=False)
        except:
            # if server is started before DB is synced,
            # this will raise
            # django.db.utils.OperationalError: no such table:
            # otree_globallockmodel
            # we can ignore that because we just want to make sure there are no
            # active locks
            pass


def get_redis_conn():
    from huey.contrib.djhuey import HUEY
    return HUEY.storage.conn

