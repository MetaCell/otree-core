#!/usr/bin/env python
# -*- coding: utf-8 -*-

from channels.routing import route

from otree.channels import consumers


channel_routing = [
    # questions
    route('websocket.connect', consumers.question_connect,
          path=r'^/question/(?P<params>[\w,]+)/$'),
    route('websocket.disconnect', consumers.question_disconnect,
          path=r'^/question/(?P<params>[\w,]+)/$'),
    route('websocket.receive', consumers.question_receive,
          path=r'^/question/(?P<params>[\w,]+)/$'),
    # end_chat
    #chat
    route('websocket.connect', consumers.chat_connect,
          path=r'^/chat/(?P<params>[\w,]+)/$'),
    route('websocket.disconnect', consumers.chat_disconnect,
          path=r'^/chat/(?P<params>[\w,]+)/$'),
    route('websocket.receive', consumers.chat_receive,
          path=r'^/chat/(?P<params>[\w,]+)/$'),
    #end_chat
    route('websocket.connect', consumers.ws_matchmaking_connect,
          path=r'^/matchmaking/(?P<params>[\w,]+)/$'),
    route('websocket.receive', consumers.ws_matchmaking_message,
          path=r'^/matchmaking/(?P<params>[\w,]+)/$'),
    route('websocket.disconnect', consumers.ws_matchmaking_disconnect,
          path=r'^/matchmaking/(?P<params>[\w,]+)/$'),
    route(
        'websocket.connect', consumers.connect_wait_page,
        path=r'^/wait_page/(?P<params>[\w,]+)/$'),
    route(
        'websocket.disconnect', consumers.disconnect_wait_page,
        path=r'^/wait_page/(?P<params>[\w,]+)/$'),
    route(
        'websocket.connect', consumers.connect_auto_advance,
        path=r'^/auto_advance/(?P<params>[\w,]+)/$'),
    route('websocket.disconnect', consumers.disconnect_auto_advance,
          path=r'^/auto_advance/(?P<params>[\w,]+)/$'),
    route('websocket.connect', consumers.connect_wait_for_session,
          path=r'^/wait_for_session/(?P<pre_create_id>\w+)/$'),
    route('websocket.disconnect', consumers.disconnect_wait_for_session,
          path=r'^/wait_for_session/(?P<pre_create_id>\w+)/$'),
    route('otree.create_session',
          consumers.create_session),
    route('websocket.connect',
          consumers.connect_room_participant,
          path=r'^/wait_for_session_in_room/(?P<params>[\w,]+)/$'),
    route('websocket.disconnect',
          consumers.disconnect_room_participant,
          path=r'^/wait_for_session_in_room/(?P<params>[\w,]+)/$'),
    route('websocket.connect',
          consumers.connect_room_admin,
          path=r'^/room_without_session/(?P<room>\w+)/$'),
    route('websocket.disconnect',
          consumers.disconnect_room_admin,
          path=r'^/room_without_session/(?P<room>\w+)/$'),
    route('websocket.connect',
          consumers.connect_browser_bots_client,
          path=r'^/browser_bots_client/(?P<session_code>\w+)/$'),
    route('websocket.disconnect',
          consumers.disconnect_browser_bots_client,
          path=r'^/browser_bots_client/(?P<session_code>\w+)/$'),
    route('websocket.connect',
          consumers.connect_browser_bot,
          path=r'^/browser_bot_wait/$'),
    route('websocket.disconnect',
          consumers.disconnect_browser_bot,
          path=r'^/browser_bot_wait/$')
]
