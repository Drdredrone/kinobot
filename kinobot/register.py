#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# License: GPL
# Author : Vitiko <vhnz98@gmail.com>

import json
import logging
from typing import List, Optional

import pylast
import requests
import tmdbsimple as tmdb
from discord_webhook import DiscordEmbed, DiscordWebhook
from facepy import GraphAPI

from kinobot.cache import MEDIA_LIST_TIME, region
from kinobot.media import Episode, Movie, TVShow

from .constants import (
    FACEBOOK_TOKEN,
    LAST_FM_KEY,
    RADARR_TOKEN,
    RADARR_URL,
    RECENTLY_ADDED_HOOK,
    SONARR_TOKEN,
    SONARR_URL,
    TMDB_KEY,
)
from .db import Kinobase
from .exceptions import InvalidRequest, KinoException
from .request import Request
from .user import User

tmdb.API_KEY = TMDB_KEY

logger = logging.getLogger(__name__)

_FB_REQ_TYPES = (
    "!req",
    "!parallel",
    "!palette",
)


class FacebookRegister:
    def __init__(self, page_limit: int = 20, page_token: Optional[str] = None):
        self.page_limit = page_limit
        self.page_token = page_token or FACEBOOK_TOKEN
        self._comments: List[dict] = []
        self.__collected = False

    def requests(self):
        " Register requests. "
        logger.info("Registering requests")
        self._collect()
        for request in self._comments:
            self._register_request(request)

    def ratings(self):
        " Register ratings. "
        logger.info("Registering ratings")
        self._collect()
        for comment in self._comments:
            try:
                self._rate_movie(comment)
            except KinoException as error:
                logger.error(error)

    def _collect(self):
        " Collect 'requests' from Kinobot's last # posts. "
        if self.__collected:
            logger.info("Already collected")
            return

        kinobot = GraphAPI(self.page_token)
        # kinobot_tv = GraphAPI(FACEBOOK_TV)
        # kinobot_music = GraphAPI(FACEBOOK_MUSIC)

        logger.info("About to scan %d posts", self.page_limit)

        for post in kinobot.get("me/posts", limit=self.page_limit).get("data", []):  # type: ignore
            comments = kinobot.get(str(post.get("id")) + "/comments")
            for comment in comments.get("data", []):  # type: ignore
                self._comments.append(comment)

        self.__collected = True

    @staticmethod
    def _register_request(comment: dict):
        msg = comment.get("message", "n/a")

        for type_ in _FB_REQ_TYPES:

            if not msg.startswith(type_):
                continue

            request = Request.from_fb(comment)
            request.type = type_  # Workaround

            request.register()
            break

    @staticmethod
    def _rate_movie(comment: dict):
        """
        :param comment:
        :type comment: dict
        :raises:
            exceptions.InvalidRequest
            exceptions.MovieNotFound
        """
        msg = comment.get("message", "n/a").strip()
        if msg.startswith("!rate"):
            clean = msg.replace("!rate", "").strip().split()  # ["xx", "rate"]

            rating = clean[-1].split("/")[0]

            try:
                rating = float(rating)
            except ValueError:
                raise InvalidRequest(f"Invalid rating: {rating}")

            user = User.from_fb(**comment.get("from", {}))
            user.register()

            movie = Movie.from_query(" ".join(clean))

            user.rate_media(movie, rating)


class MediaRegister(Kinobase):
    type = "movies"

    def __init__(self, interactive: bool = True):
        self.interactive = interactive
        self.external_items = []
        self.local_items = []
        self.new_items = []
        self.deleted_items = []
        self.modified_items = []

    def load_new_and_deleted(self):
        self._load_local()
        self._load_external()

        for external in self.external_items:
            if not any(str(item.id) == str(external.id) for item in self.local_items):
                logger.info("Appending missing item: %s", external)
                self.new_items.append(external)

        for local in self.local_items:
            if not any(str(item.id) == str(local.id) for item in self.external_items):
                logger.info("Appending deleted item: %s", local)
                self.deleted_items.append(local)

        # Modified paths
        for local in self.local_items:
            if not any(item.path == local.path for item in self.external_items):
                local.path = next(
                    item.path
                    for item in self.external_items
                    if str(local.id) == str(item.id)
                )
                logger.info("Appending item with new path: %s", local.path)
                self.modified_items.append(local)

    def handle(self):
        self._handle_deleted()
        self._handle_new()
        self._handle_modified()

    def _handle_new(self):
        if not self.new_items:
            logger.info("No new items to add")
        else:
            for new in self.new_items:
                new.load_meta()
                if self.interactive:
                    new.category = input("Category:\n- ") or "Certified Kino"
                    new.category = new.category.title()

                new.register()
                if self.type == "movies":
                    self._notify(new.webhook_embed)

    @staticmethod
    def _notify(embed: DiscordEmbed):
        webhook = DiscordWebhook(url=RECENTLY_ADDED_HOOK)
        webhook.add_embed(embed)
        webhook.execute()

    def _handle_deleted(self):
        if not self.deleted_items:
            logger.info("No items to delete")
        else:
            for deleted in self.deleted_items:
                deleted.hidden = True
                deleted.update()

    def _handle_modified(self):
        if not self.modified_items:
            logger.info("No items to modify")
        else:
            [item.update() for item in self.modified_items]

    def _load_local(self):
        class_ = Movie if self.type == "movies" else Episode
        items = self._db_command_to_dict(f"select * from {self.type} where hidden=0")
        self.local_items = [class_(**item) for item in items]

    def _load_external(self):
        self.external_items = [
            Movie.from_radarr(item) for item in _get_radarr_list("cache")
        ]


class EpisodeRegister(MediaRegister):
    type = "episodes"

    def _load_external(self):
        self.external_items = [
            Episode.from_register_dict(item) for item in _get_episodes("cache")
        ]


# Cached functions


# @region.cache_on_arguments(expiration_time=MEDIA_LIST_TIME)
def _get_episodes(cache_str: str) -> List[dict]:
    assert cache_str is not None

    session = requests.Session()

    response = session.get(f"{SONARR_URL}/api/series?apiKey={SONARR_TOKEN}")

    response.raise_for_status()

    series = response.json()

    episode_list = []
    for serie in series:
        if not serie.get("sizeOnDisk", 0):
            continue

        found_ = _get_tmdb_imdb_find(serie["imdbId"])

        tmdb_serie = _get_tmdb_tv_show(found_[0]["id"])

        tv_show = TVShow(imdb=serie["imdbId"], tvdb=serie["tvdbId"], **tmdb_serie)
        tv_show.register()

        tv_show_id = tmdb_serie["id"]

        episodes_r = session.get(
            f"{SONARR_URL}/api/episode",
            params={"apiKey": SONARR_TOKEN, "seriesId": serie.get("id")},
        )

        episodes_r.raise_for_status()

        episodes = [item for item in episodes_r.json() if item["hasFile"]]

        season_ns = [
            season["seasonNumber"]
            for season in serie["seasons"]
            if season["statistics"]["sizeOnDisk"]
        ]

        episode_list += _gen_episodes(season_ns, tv_show_id, episodes)

    return episode_list


def _gen_episodes(season_ns: List[int], tmdb_id: int, radarr_eps: List[dict]):
    for season in season_ns:
        tmdb_season = _get_tmdb_season(tmdb_id, season)

        for episode in tmdb_season["episodes"]:
            try:
                episode["path"] = next(
                    item["episodeFile"]["path"]
                    for item in radarr_eps
                    if item["episodeNumber"] == episode["episode_number"]
                    and season == item["seasonNumber"]
                )
                episode["tv_show_id"] = tmdb_id
                yield episode
            except StopIteration:
                pass


@region.cache_on_arguments(expiration_time=MEDIA_LIST_TIME)
def _get_radarr_list(cache_str: str) -> List[dict]:
    assert cache_str is not None

    response = requests.get(f"{RADARR_URL}/api/v3/movie?apiKey={RADARR_TOKEN}")

    response.raise_for_status()

    return [i for i in json.loads(response.content) if i.get("hasFile")]


@region.cache_on_arguments()
def _get_tmdb_imdb_find(imdb_id):
    find_ = tmdb.find.Find(id=imdb_id)
    results = find_.info(external_source="imdb_id")["tv_results"]
    return results


@region.cache_on_arguments()
def _get_tmdb_tv_show(show_id) -> dict:
    tmdb_show = tmdb.TV(show_id)
    return tmdb_show.info()


@region.cache_on_arguments()
def _get_tmdb_season(serie_id, season_number) -> dict:
    tmdb_season = tmdb.TV_Seasons(serie_id, season_number)
    return tmdb_season.info()


def _clean_garbage(text):
    """
    Remove garbage from a track title (remastered tags and alike).
    """
    pass
    # return re.sub(_TAGS_RE, "", text)


def _search_tracks(query, limit=3, remove_extra=True):
    """
    Search for tracks on last.fm.
    """
    client = pylast.LastFMNetwork(LAST_FM_KEY)

    results = client.search_for_track("", query)

    for result in results.get_next_page()[:limit]:
        artist = str(result.artist)
        title = ""
        #        title = clean_garbage(result.title) if remove_extra else result.title
        complete = f"*{artist}* - **{title}**"

        yield {"artist": artist, "title": title, "complete": complete}
