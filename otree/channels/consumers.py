#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import logging
import threading
import django.db
import django.utils.timezone
import traceback
import uuid
from datetime import timedelta
from threading import Thread

from django.conf import settings
from channels import Group
from channels.sessions import channel_session, enforce_ordering


import otree.session
from otree.models import Participant, Session
from otree.models_concrete import (CompletedGroupWaitPage, CompletedSubsessionWaitPage)
from otree.common_internal import (channels_wait_page_group_name, channels_create_session_group_name)
from otree.models_concrete import (FailedSessionCreation, ParticipantRoomVisit,
                                   FAILURE_MESSAGE_MAX_LENGTH, BrowserBotsLauncherSessionCode)
from otree.room import ROOM_DICT
from otree.bots.bot import ParticipantBot
from otree.bots.runner import SessionBotRunner

# Get an instance of a logger
logger = logging.getLogger(__name__)

# Connected to websocket.connect
@enforce_ordering(slight=True)
@channel_session
def chat_connect(message, params):
    # Work out game name from path (ignore slashes)
    p = params.split(',')
    session_id = p[0]
    player_id_in_sesssion = p[1]

    reply_channel = message.reply_channel

    logger.info('path: ' + message.content['path'])

    Group("chat-%s" % session_id).add(message.reply_channel)


@enforce_ordering(slight=True)
@channel_session
def chat_receive(message, params):
    p = params.split(',')
    session_id = p[0]
    player_id_in_sesssion = p[1]
    chatmsg = message['text']
    payload = json.dumps({'message': chatmsg, 'sender': player_id_in_sesssion})
    Group("chat-%s" % session_id).send({
        "text": payload
    })

    # check if bot opponent and if we have 1 bot_participant as expected
    session = Session.objects.get(id=session_id)
    if session.bot_opponent and int(session_id) in settings.SESSION_BOTS_MAP:
        # get bot participant / player bot (there can only be one)
        player_bot = settings.SESSION_BOTS_MAP[int(session_id)].player_bots[0]
        # send message to player bot and get response
        responseMsg = player_bot.on_message(chatmsg)
        # broadcast response to the same group
        responsePayload = json.dumps({'message': responseMsg, 'sender': 'bot_player'})
        Group("chat-%s" % session_id).send({
            "text": responsePayload
        })


@enforce_ordering(slight=True)
@channel_session
def chat_disconnect(message, params):
    p = params.split(',')
    session_id = p[0]
    # unregister reply channel forom session group
    Group("chat-%s" % session_id).discard(message.reply_channel)
    # remove session chatbot from global map
    remove_session_bot(int(session_id))


def connect_wait_page(message, params):
    session_pk, page_index, model_name, model_pk = params.split(',')
    session_pk = int(session_pk)
    page_index = int(page_index)
    model_pk = int(model_pk)

    group_name = channels_wait_page_group_name(
        session_pk, page_index, model_name, model_pk
    )
    group = Group(group_name)
    group.add(message.reply_channel)

    # in case message was sent before this web socket connects
    if model_name == 'group':
        ready = CompletedGroupWaitPage.objects.filter(
            page_index=page_index,
            group_pk=model_pk,
            session_id=session_pk,
            after_all_players_arrive_run=True).exists()
    else:  # subsession
        ready = CompletedSubsessionWaitPage.objects.filter(
            page_index=page_index,
            session_id=session_pk,
            after_all_players_arrive_run=True).exists()
    if ready:
        message.reply_channel.send(
            {'text': json.dumps(
                {'status': 'ready'})})


def disconnect_wait_page(message, params):
    session_pk, page_index, model_name, model_pk = params.split(',')
    page_index = int(page_index)
    model_pk = int(model_pk)

    group_name = channels_wait_page_group_name(
        session_pk, page_index, model_name, model_pk
    )
    group = Group(group_name)
    group.discard(message.reply_channel)


def connect_auto_advance(message, params):
    participant_code, page_index, session_code = params.split(',')
    page_index = int(page_index)

    # check if bot_opponent, if so appropriately set the player_disconnected flag
    session = Session.objects.get(code=session_code)
    if session.bot_opponent:
        session.human_participant_disconnected = False
        session.save()

    group = Group('auto-advance-{}'.format(participant_code))
    group.add(message.reply_channel)

    # in case message was sent before this web socket connects

    try:
        participant = Participant.objects.get(code=participant_code)
    except Participant.DoesNotExist:
        message.reply_channel.send(
            {'text': json.dumps(
                # doesn't get shown because not yet localized
                {'error': 'Participant not found in database.'})})
        return
    if participant._index_in_pages > page_index:
        message.reply_channel.send(
            {'text': json.dumps(
                {'new_index_in_pages': participant._index_in_pages})})


def disconnect_auto_advance(message, params):
    participant_code, page_index, session_code = params.split(',')

    # check if bot_opponent, if so appropriately set the player_disconnected flag
    session = Session.objects.get(code=session_code)
    if session.bot_opponent:
        session.human_participant_disconnected = True
        session.save()

    group = Group('auto-advance-{}'.format(participant_code))
    group.discard(message.reply_channel)


def create_session(message):
    group = Group(message['channels_group_name'])

    kwargs = message['kwargs']

    logger.info('session_config_name: ' + kwargs['session_config_name'] + ' num_participants: ' + str(kwargs['num_participants']))

    # because it's launched through web UI
    kwargs['honor_browser_bots_config'] = True
    try:
        otree.session.create_session(**kwargs)
    except Exception as e:

        # full error message is printed to console (though sometimes not?)
        error_message = 'Failed to create session: "{}"'.format(e)
        traceback_str = traceback.format_exc()
        group.send(
            {'text': json.dumps(
                {
                    'error': error_message,
                    'traceback': traceback_str,
                })}
        )
        FailedSessionCreation.objects.create(
            pre_create_id=kwargs['_pre_create_id'],
            message=error_message[:FAILURE_MESSAGE_MAX_LENGTH],
            traceback=traceback_str
        )
        raise

    group.send(
        {'text': json.dumps(
            {'status': 'ready'})}
    )

    if 'room_name' in kwargs:
        Group('room-participants-{}'.format(kwargs['room_name'])).send(
            {'text': json.dumps(
                {'status': 'session_ready'})}
        )


def connect_wait_for_session(message, pre_create_id):
    group = Group(channels_create_session_group_name(pre_create_id))
    group.add(message.reply_channel)

    # in case message was sent before this web socket connects
    if Session.objects.filter(_pre_create_id=pre_create_id, ready=True):
        group.send(
            {'text': json.dumps(
                {'status': 'ready'})}
        )
    else:
        failure = FailedSessionCreation.objects.filter(
            pre_create_id=pre_create_id
        ).first()
        if failure:
            group.send(
                {'text': json.dumps(
                    {'error': failure.message,
                     'traceback': failure.traceback})}
            )


def disconnect_wait_for_session(message, pre_create_id):
    group = Group(
        channels_create_session_group_name(pre_create_id)
    )
    group.discard(message.reply_channel)


def connect_room_admin(message, room):
    Group('room-admin-{}'.format(room)).add(message.reply_channel)

    room_object = ROOM_DICT[room]

    now = django.utils.timezone.now()
    stale_threshold = now - timedelta(seconds=15)
    present_list = ParticipantRoomVisit.objects.filter(
        room_name=room_object.name,
        last_updated__gte=stale_threshold,
    ).values_list('participant_label', flat=True)

    # make it JSON serializable
    present_list = list(present_list)

    message.reply_channel.send({'text': json.dumps({
        'status': 'load_participant_lists',
        'participants_present': present_list,
    })})

    # prune very old visits -- don't want a resource leak
    # because sometimes not getting deleted on WebSocket disconnect
    very_stale_threshold = now - timedelta(minutes=10)
    ParticipantRoomVisit.objects.filter(
        room_name=room_object.name,
        last_updated__lt=very_stale_threshold,
    ).delete()


def disconnect_room_admin(message, room):
    Group('room-admin-{}'.format(room)).discard(message.reply_channel)


def connect_room_participant(message, params):
    room_name, participant_label, tab_unique_id = params.split(',')
    if room_name in ROOM_DICT:
        room = ROOM_DICT[room_name]
    else:
        message.reply_channel.send(
            {'text': json.dumps(
                # doesn't get shown because not yet localized
                {'error': 'Invalid room name "{}".'.format(room_name)})})
        return
    Group('room-participants-{}'.format(room_name)).add(message.reply_channel)

    if room.has_session():
        message.reply_channel.send(
            {'text': json.dumps({'status': 'session_ready'})}
        )
    else:
        try:
            ParticipantRoomVisit.objects.create(
                participant_label=participant_label,
                room_name=room_name,
                tab_unique_id=tab_unique_id
            )
        except django.db.IntegrityError as exc:
            # possible that the tab connected twice
            # without disconnecting in between
            # because of WebSocket failure
            # tab_unique_id is unique=True,
            # so this will throw an integrity error.
            logger.info(
                'ParticipantRoomVisit: not creating a new record because a '
                'database integrity error was thrown. '
                'The exception was: {}: {}'.format(type(exc), exc))
            pass
        Group('room-admin-{}'.format(room_name)).send({'text': json.dumps({
            'status': 'add_participant',
            'participant': participant_label
        })})


def disconnect_room_participant(message, params):
    room_name, participant_label, tab_unique_id = params.split(',')
    if room_name in ROOM_DICT:
        room = ROOM_DICT[room_name]
    else:
        message.reply_channel.send(
            {'text': json.dumps(
                # doesn't get shown because not yet localized
                {'error': 'Invalid room name "{}".'.format(room_name)})})
        return

    Group('room-participants-{}'.format(room_name)).discard(
        message.reply_channel)

    # should use filter instead of get,
    # because if the DB is recreated,
    # the record could already be deleted
    ParticipantRoomVisit.objects.filter(
        participant_label=participant_label,
        room_name=room_name,
        tab_unique_id=tab_unique_id).delete()

    if room.has_participant_labels():
        if not ParticipantRoomVisit.objects.filter(
            participant_label=participant_label,
            room_name=room_name
        ).exists():
            # it's ok if there is a race condition --
            # in JS removing a participant is idempotent
            Group('room-admin-{}'.format(room_name)).send({'text': json.dumps({
                'status': 'remove_participant',
                'participant': participant_label
            })})
    else:
        Group('room-admin-{}'.format(room_name)).send({'text': json.dumps({
            'status': 'remove_participant',
        })})


def connect_browser_bots_client(message, session_code):
    Group('browser-bots-client-{}'.format(session_code)).add(
        message.reply_channel)


def disconnect_browser_bots_client(message, session_code):
    Group('browser-bots-client-{}'.format(session_code)).discard(
        message.reply_channel)


def connect_browser_bot(message):

    Group('browser_bot_wait').add(message.reply_channel)
    launcher_session_info = BrowserBotsLauncherSessionCode.objects.first()
    if launcher_session_info:
        message.reply_channel.send(
            {'text': json.dumps({'status': 'session_ready'})}
        )


def disconnect_browser_bot(message):
    Group('browser_bot_wait').discard(message.reply_channel)


@enforce_ordering(slight=True)
@channel_session
def ws_matchmaking_connect(message, params):
    # Work out game name from path (ignore slashes)
    paramsList = params.split(',')
    # we always have at least game param
    game = paramsList[0]
    # try for optional params for external platform info
    try:
        platform = paramsList[1]
    except IndexError:
        platform = None
    try:
        worker_id = paramsList[2]
    except IndexError:
        worker_id = None
    try:
        completion_url = paramsList[3]
    except IndexError:
        completion_url = None

    session_id = message.channel_session.session_key
    reply_channel = message.reply_channel

    # log name of game
    logger.info('path: ' + message.content['path'])
    logger.info('game name: ' + game)
    if platform is not None:
        # some extra logging for external platform
        logger.info('external platform: ' + platform)
        logger.info('worker-id: ' + worker_id)

    # Save game in session and add us to the group
    message.channel_session['game'] = game

    enqueue_player({'session': session_id,
                    'game': game,
                    'reply_channel': reply_channel,
                    'platform': platform,
                    'worker_id': worker_id,
                    'completion_url': completion_url})

    message = json.dumps({'status': 'QUEUE_JOINED', 'message': 'You have joined the queue for ' + game})
    reply_channel.send({'text': message})

    # whenever a user connects, check if we have 2 users to start a given game
    matching_players = []
    for player in settings.MATCH_MAKING_QUEUE:
        if player['game'] == game:
            matching_players.append(player)

    if len(matching_players) >= 2:
        make_match(matching_players, game)


# threaded function to call play on bot_runner
def bot_runner_play(bot_runner=None):
    bot_runner.play_until_end()


@enforce_ordering(slight=True)
@channel_session
def ws_matchmaking_message(message, params):
    # only do something if status is polling
    payload = json.loads(message['text'])
    if payload['status'] == 'POLLING':
        # we use this exclusively for polling and starting bot opponent sessions
        session_id = message.channel_session.session_key
        reply_channel = message.reply_channel
        # Work out game name from path (ignore slashes)
        game = params.split(',')[0]

        # check if we have another user other than us that wants to play the same game
        matching_players = []
        for player in settings.MATCH_MAKING_QUEUE:
            if player['game'] == game:
                matching_players.append(player)

        if len(matching_players) >= 2:
            # if so, make match (same as connect logic)
            # NOTE: this should never happen because match is made on connect
            make_match(matching_players, game)
        elif len(matching_players) == 1:
            # if not, dequeue user immediately and then create a session with bot_opponent
            try:
                dequeue_player({'session': session_id, 'game': message.channel_session['game'], 'reply_channel': reply_channel})
            except:
                # the user might have been removed because the game started
                logger.info('User already removed from matchmaking queue')

            # create a session with bot_opponent
            session = manually_create_session_for_matchmaking(game, 2, True)
            session_start_urls = [
                participant._start_url()
                for participant in session.get_participants()
            ]

            # setup user
            # log some stuff
            logger.info('Session (with bot opponent) created for: ' + game)
            logger.info('P1 URL (bot): ' + session_start_urls[0])
            logger.info('P2 URL: ' + session_start_urls[1])

            player = matching_players[0]
            message_back = json.dumps({
                'status': 'SESSION_CREATED',
                'message': 'Opponent found! Your game is about to start',
                # in case of bots user url is always the one with idx == 1
                'url': session_start_urls[1]
            })
            # send game starting message that will cause redirection
            player['reply_channel'].send({"text": message_back})

            # create bot runner
            bot_runner = create_bot_runner(session, game)
            # spawn thread and call play_until_end on bot
            thread = Thread(target=bot_runner_play, kwargs={'bot_runner': bot_runner})
            thread.start()


@enforce_ordering(slight=True)
@channel_session
def ws_matchmaking_disconnect(message, params):
    # remove user from matchmaking queue in case of disconnect
    session_id = message.channel_session.session_key
    reply_channel = message.reply_channel
    dequeue_player({'session': session_id, 'game': message.channel_session['game'], 'reply_channel': reply_channel})


def manually_create_session_for_matchmaking(game, participants, bot_opponent):
    session_kwargs = {}
    session_kwargs['session_config_name'] = game
    session_kwargs['num_participants'] = participants
    pre_create_id = uuid.uuid4().hex
    session_kwargs['_pre_create_id'] = pre_create_id
    session_kwargs['bot_opponent'] = bot_opponent

    session = None
    try:
        session = otree.session.create_session(**session_kwargs)
    except Exception as e:
        # full error message is printed to console (though sometimes not?)
        error_message = 'Failed to create session: "{}"'.format(e)
        logger.error(error_message)
        raise

    return session


# declare synchronized decorator
def synchronized(func):
    func.__lock__ = threading.Lock()

    def synced_func(*args, **kws):
        with func.__lock__:
            return func(*args, **kws)

    return synced_func


# add to matchmaking queue
@synchronized
def enqueue_player(meta):
    settings.MATCH_MAKING_QUEUE.append(meta)


# add to session bots map
@synchronized
def add_session_bot(session_id, bot):
    settings.SESSION_BOTS_MAP[session_id] = bot


#remove from matchmaking queue
@synchronized
def dequeue_player(player_meta):
    for player in settings.MATCH_MAKING_QUEUE:
        if player['session'] == player_meta['session']:
            settings.MATCH_MAKING_QUEUE.remove(player)
            logger.info('Removed player from queue - ws session:' + player_meta['session'])


# remove entry from session bots map queue
@synchronized
def remove_session_bot(session_id):
    if session_id in settings.SESSION_BOTS_MAP:
        del settings.SESSION_BOTS_MAP[session_id]


# synchronized match making function - create session and pop players from queue
@synchronized
def make_match(matching_players, game):
    # double check we have at least 2 matches before doing anything
    if len(matching_players) >= 2:
        # create session and get url for redirect
        session = manually_create_session_for_matchmaking(game, 2, False)
        session_start_urls = [
            participant._start_url()
            for participant in session.get_participants()
        ]

        # log some stuff
        logger.info('Session create for: ' + game)
        logger.info('P1 URL: ' + session_start_urls[0])
        logger.info('P2 URL: ' + session_start_urls[1])

        for indx, player in enumerate(matching_players):
            if indx < 2:
                # take first 2 matching players out of queue
                settings.MATCH_MAKING_QUEUE.remove(player)

                message = json.dumps({
                    'status': 'SESSION_CREATED',
                    'message': 'Opponent found! Your game is about to start',
                    'url': session_start_urls[indx]
                })

                # send game starting message
                player['reply_channel'].send({"text": message})


# create participant bots and bot runner
def create_bot_runner(session, game):
    bots= []
    for participant in session.get_participants().filter(_is_bot=True):
        bot = ParticipantBot(participant)
        bots.append(bot)
        bot.open_start_url()
        # track bot participant in session bots map for chat
        if game.lower() == 'chat':
            add_session_bot(session.id, bot)

    return SessionBotRunner(bots, session.code)