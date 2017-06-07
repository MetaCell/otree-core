#!/usr/bin/env python
# -*- coding: utf-8 -*-

import random
import logging
from collections import OrderedDict
import mock
import datetime
import os
import codecs
import time
from random import randint

from django.db.migrations.loader import MigrationLoader
from django.conf import settings
import pytest
import sys

import otree.session
import otree.common_internal
from otree.models import Session
from .bot import ParticipantBot, Pause
import otree.export

logger = logging.getLogger(__name__)


class SessionBotRunner(object):
    def __init__(self, bots, session_code):
        self.session_code = session_code
        self.stuck = OrderedDict()
        self.playable = OrderedDict()
        self.bots = OrderedDict()
        self.all_bot_codes = {bot.participant.code for bot in bots}
        self.disconnect_checks_counter = 0

        for bot in bots:
            self.playable[bot.participant.code] = bot

    def play_until_end(self):
        while True:
            session = Session.objects.get(code=self.session_code)
            # check if player disconnected and increment internal counter,
            if session.human_participant_disconnected:
                self.disconnect_checks_counter += 1
                # if > 3 iterations player appears disconnected then break out of loop
                if self.disconnect_checks_counter > 3:
                    logger.info('SessionBotRunner: Bots done, participant appears to have disconnected')
                    return

            # make the bot sleep a random number of seconds (1-5) to make it feel 'more human'
            interval = randint(1, 15)
            logger.info('SessionBotRunner: bot sleeping for {} seconds'.format(interval))
            time.sleep(interval)
            # keep un-sticking everyone who's stuck
            stuck_pks = list(self.stuck.keys())
            done, num_submits_made = self.play_until_stuck(stuck_pks)
            if done:
                logger.info('SessionBotRunner: Bots done!')
                return

    def play_until_stuck(self, unstuck_ids=None):
        unstuck_ids = unstuck_ids or []
        for pk in unstuck_ids:
            # the unstuck ID might be a human player, not a bot.
            if pk in self.all_bot_codes:
                self.playable[pk] = self.stuck.pop(pk)
        num_submits_made = 0
        while True:
            # slow down this loop to save CPU on server
            time.sleep(1)
            # round-robin algorithm
            if len(self.playable) == 0:
                if len(self.stuck) == 0:
                    return (True, num_submits_made)
                # stuck
                return (False, num_submits_made)
            # store in a separate list so we don't mutate the iterable
            playable_ids = list(self.playable.keys())
            for pk in playable_ids:
                r = random.random()
                bot = self.playable[pk]
                logger.info('{}: checking if on wait page'.format(pk))
                if bot.on_wait_page():
                    logger.info('...{} is on wait page: {}'.format(pk, bot.path))
                    logger.info(r)
                    self.stuck[pk] = self.playable.pop(pk)
                else:
                    logger.info('...{} is not on wait page'.format(pk))
                    logger.info(r)
                    try:
                        value = next(bot.submits_generator)
                    except StopIteration:
                        # this bot is finished
                        self.playable.pop(pk)
                        logger.info(r)
                        logger.info('{} is done!'.format(pk))
                    else:
                        if isinstance(value, Pause):
                            pause = value
                            self.stuck[pk] = self.playable.pop(pk)
                            self.continue_bots_until_stuck_task(self.session_code, [pk])
                        else:
                            submit = value
                            logger.info('{}: about to submit {}'.format(pk, submit))
                            bot.submit(submit)
                            logger.info('{}: submitted {}'.format(pk, submit))
                            num_submits_made += 1

    def continue_bots_until_stuck_task(self, session_code, unstuck_ids=None):
        try:
            self.play_until_stuck(unstuck_ids)
        except Exception as exc:
            session = Session.objects.get(code=session_code)
            session._bots_errored = True
            session.save()
            logger.error('Bots encountered an error: "{}". For the full traceback, see the server logs.'.format(exc))
            raise
        logger.info('Bots finished')

        session = Session.objects.get(code=session_code)
        session._bots_finished = True
        session.save()


@pytest.mark.django_db(transaction=True)
def test_bots(session_config_name, num_participants, run_export):
    config_name = session_config_name
    session_config = otree.session.SESSION_CONFIGS_DICT[config_name]

    # num_bots is deprecated, because the old default of 12 or 6 was too
    # much, and it doesn't make sense to
    if num_participants is None:
        num_participants = session_config['num_demo_participants']

    num_bot_cases = session_config.get_num_bot_cases()
    for case_number in range(num_bot_cases):
        if num_bot_cases > 1:
            logger.info("Creating '{}' session (test case {})".format(
                config_name, case_number))
        else:
            logger.info("Creating '{}' session".format(config_name))

        session = otree.session.create_session(
            session_config_name=config_name,
            num_participants=num_participants,
            use_cli_bots=True,
            bot_case_number=case_number
        )
        bots = []

        for participant in session.get_participants():
            bot = ParticipantBot(participant)
            bots.append(bot)
            bot.open_start_url()

        bot_runner = SessionBotRunner(bots, session.code)
        bot_runner.play_until_end()
    if run_export:
        # bug: if the user tests multiple session configs,
        # the data will only be exported for the last session config.
        # this is because the DB is cleared after each test case.
        # fix later if this becomes high priority
        export_path = pytest.config.option.export_path

        now = datetime.datetime.now()

        if export_path == 'auto_name':
            export_path = now.strftime('_bots_%b%d_%Hh%Mm%S.%f')[:-5] + 's'

        if os.path.isdir(export_path):
            msg = "Directory '{}' already exists".format(export_path)
            raise IOError(msg)

        os.makedirs(export_path)

        for app in settings.INSTALLED_OTREE_APPS:
            model_module = otree.common_internal.get_models_module(app)
            if model_module.Player.objects.exists():
                fname = "{}.csv".format(app)
                fpath = os.path.join(export_path, fname)
                with codecs.open(fpath, "w", encoding="utf8") as fp:
                    otree.export.export_app(app, fp, file_extension='csv')

        logger.info('Exported CSV to folder "{}"'.format(export_path))


def run_pytests(**kwargs):

    session_config_name = kwargs['session_config_name']
    num_participants = kwargs['num_participants']
    verbosity = kwargs['verbosity']

    this_module = sys.modules[__name__]

    # '-s' is to see print output
    # --tb=short is to show short tracebacks. I think this is
    # more expected and less verbose.
    # With the default pytest long tracebacks,
    # often the code that gets printed is in otree, which is not relevant.
    # also, this is better than using --tb=native, which loses line breaks
    # when a unicode char is contained in the output, and also doesn't get
    # color coded with colorama, the way short tracebacks do.
    argv = [
        this_module.__file__,
        '-s',
        '--tb', 'short'
    ]
    if verbosity == 0:
        argv.append('--quiet')
    if verbosity == 2:
        argv.append('--verbose')

    if session_config_name:
        argv.extend(['--session_config_name', session_config_name])
    if num_participants:
        argv.extend(['--num_participants', num_participants])
    if kwargs['preserve_data']:
        argv.append('--preserve_data')
    if kwargs['export_path']:
        argv.extend(['--export_path', kwargs['export_path']])

    # same hack as in resetdb code
    # because this method uses the serializer
    # it breaks if the app has migrations but they aren't up to date
    with mock.patch.object(
            MigrationLoader,
            'migrations_module',
            return_value='migrations nonexistent hack'
    ):

        return pytest.main(argv)
