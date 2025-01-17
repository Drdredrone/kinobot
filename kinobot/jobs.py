#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# License: GPL
# Author : Vitiko <vhnz98@gmail.com>

import logging
import os
import subprocess

from apscheduler.events import EVENT_JOB_ERROR
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .constants import FACEBOOK_URL, FACEBOOK_URL_ES, FACEBOOK_URL_MAIN, FACEBOOK_URL_PT
from .db import Execute
from .exceptions import KinoException, NothingFound, RecentPostFound
from .poster import FBPoster, FBPosterEs, FBPosterPt
from .register import EpisodeRegister, FacebookRegister, MediaRegister
from .request import Request, RequestEs, RequestMain, RequestPt
from .utils import handle_general_exception, sync_local_subtitles

logger = logging.getLogger(__name__)

sched = BlockingScheduler()

sched.add_job(sync_local_subtitles, CronTrigger.from_crontab("*/30 * * * *"))


@sched.scheduled_job(CronTrigger.from_crontab("*/30 * * * *"))  # every 30 min
def collect_from_facebook(posts: int = 40):
    """Collect new requests and ratings from the Facebook page.

    :param posts:
    :type posts: int
    """
    for identifier in ("en", "es", "pt"):
        register = FacebookRegister(posts, identifier)
        register.requests()
        register.ratings()


@sched.scheduled_job(CronTrigger.from_crontab("0 0 * * *"))  # every midnight
def reset_discord_limits():
    "Reset role limits for Discord users."
    Execute().reset_limits()


def _post_to_facebook(identifier="en"):
    request_cls = _req_cls_map.get(identifier, Request)

    try:
        fb_url = _fb_url_map[identifier]
    except KeyError:
        raise ValueError(f"{identifier} not found in registry")

    poster_cls = _request_poster_map.get(request_cls, FBPoster)  # type: ignore

    count = 0
    while True:
        count += 1

        try:
            request = request_cls.random_from_queue(verified=True)
        except NothingFound:
            logger.info("No new requests found")
            break

        try:
            poster = poster_cls(request, fb_url)
            poster.handle()
            poster.comment()
            break

        except RecentPostFound as error:
            logger.error(error)
            break

        except KinoException as error:
            logger.error(error)
            if count < 4:
                continue

            logger.debug("KinoException limit exceeded")
            break


_request_poster_map = {RequestEs: FBPosterEs, RequestPt: FBPosterPt}
_req_cls_map = {"es": RequestEs, "pt": RequestPt, "main": RequestMain}
_fb_url_map = {
    "en": FACEBOOK_URL,
    "es": FACEBOOK_URL_ES,
    "pt": FACEBOOK_URL_PT,
    "main": FACEBOOK_URL_MAIN,
}


@sched.scheduled_job(CronTrigger.from_crontab("*/30 * * * *"))  # every 30 min
def post_to_facebook():
    "Find a valid request and post it to Facebook."
    for identifier in ("en", "es", "pt", "main"):
        _post_to_facebook(identifier)


@sched.scheduled_job(CronTrigger.from_crontab("0 * * * *"))  # every hour
def register_media():
    "Register new media in the database."
    for media in (MediaRegister, EpisodeRegister):
        handler = media(only_w_subtitles=False)

        try:
            handler.load_new_and_deleted()
            handler.handle()
        except Exception as error:
            logger.debug("%s raised for %s. Ignoring", error, media)
            continue


def error_listener(event):
    exception = event.exception

    if not isinstance(exception, KinoException):
        handle_general_exception(exception)


sched.add_listener(error_listener, EVENT_JOB_ERROR)
