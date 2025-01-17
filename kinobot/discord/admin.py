#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# License: GPL
# Author : Vitiko <vhnz98@gmail.com>

# Discord bot for admin tasks.

import asyncio
import logging

from discord import Member
from discord.ext import commands
import pysubs2

from ..constants import DISCORD_ANNOUNCER_WEBHOOK
from ..constants import KINOBASE
from ..db import Execute
from ..exceptions import InvalidRequest
from ..jobs import register_media
from ..media import Episode
from ..media import Movie
from ..metadata import Category
from ..request import get_cls
from ..user import User
from ..utils import is_episode
from ..utils import send_webhook
from ..utils import sync_local_subtitles
from .chamber import Chamber
from .chamber import CollaborativeChamber
from .common import get_req_id_from_ctx
from .common import handle_error
from .extras.curator import MovieView
from .extras.curator import RadarrClient
from .extras.curator import register_movie_addition
from .extras.curator import register_tv_show_season_addition
from .extras.curator import ReleaseModel
from .extras.curator import ReleaseModelSonarr
from .extras.curator import SonarrClient
from .extras.curator import SonarrTVShowModel
from .extras.curator_user import Curator

logging.getLogger("discord").setLevel(logging.INFO)

logger = logging.getLogger(__name__)

bot = commands.Bot(command_prefix="!")


def _get_cls_from_ctx(ctx):
    return get_cls(get_req_id_from_ctx(ctx))


@bot.command(name="verify", help="Verify a request by ID.")
@commands.has_any_role("botmin", "verifier")
async def verify(ctx: commands.Context, id_: str):
    req = _get_cls_from_ctx(ctx).from_db_id(id_)
    req.verify()

    await ctx.send(f"Verified: {req.pretty_title}")

    req.user.load()

    if str(req.user.id) == str(ctx.author.id) and not any(
        "botmin" == str(role) for role in ctx.author.roles
    ):
        await ctx.reply(
            f"WARNING: verifying your own requests "
            "is FORBIDDEN unless you have direct admin permission."
        )
        send_webhook(
            DISCORD_ANNOUNCER_WEBHOOK,
            f"**WARNING:** {ctx.author.display_name} verified their own request: **{req.pretty_title}**",
        )


@bot.command(name="delete", help="Mark as used a request by ID.")
@commands.has_any_role("botmin", "verifier")
async def delete(ctx: commands.Context, id_: str):
    req = _get_cls_from_ctx(ctx).from_db_id(id_)
    req.mark_as_used()
    await ctx.send(f"Marked as used: {req.pretty_title}")


@commands.has_any_role("botmin", "verifier")
@bot.command(name="chamber", help="Enter the verification chamber.")
async def chamber(ctx: commands.Context, *args):
    chamber = await CollaborativeChamber.from_bot(bot, ctx, args)
    await chamber.start()


@commands.has_any_role("botmin")
@bot.command(name="schamber", help="Enter the verification chamber.")
async def schamber(ctx: commands.Context):
    chamber = Chamber(bot, ctx)
    await chamber.start()


@bot.command(name="count", help="Show the count of verified requests.")
async def count(ctx: commands.Context):
    req_cls = _get_cls_from_ctx(ctx)
    await ctx.send(
        f"Verified requests: {Execute().queued_requets(table=req_cls.table)}"
    )


@commands.has_any_role("botmin")
@bot.command(name="blacklist", help="Blacklist a movie or an episode")
async def blacklist(ctx: commands.Context, *args):
    query = " ".join(args)
    if is_episode(query):
        item = Episode.from_query(query)
    else:
        item = Movie.from_query(query)

    item.hidden = True
    item.update()
    await ctx.send(f"Blacklisted: {item.simple_title}.")


@commands.has_any_role("botmin")
@bot.command(name="media", help="Register media")
async def media(ctx: commands.Context):
    await ctx.send("Registering media")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, register_media)
    await ctx.send("Ok")


@commands.has_any_role("botmin")
@bot.command(name="syncsubs", help="Sync local subtitles")
async def syncsubs(ctx: commands.Context):
    await ctx.send("Syncing local subtitles")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, sync_local_subtitles)
    await ctx.send("Ok")


@commands.has_any_role("botmin")
@bot.command(name="fsub", help="Change subtitles timestamp")
async def fsub(ctx: commands.Context, *args):
    time = args[-1].strip()
    try:
        sec, mss = [int(item) for item in time.split(".")]
    except ValueError:
        raise InvalidRequest(f"Invalid timestamps: {time}")

    query = " ".join(args).replace(time, "")
    if is_episode(query):
        item = Episode.from_query(query)
    else:
        item = Movie.from_query(query)

    subs = pysubs2.load(item.subtitle)
    subs.shift(s=sec, ms=mss)

    await ctx.send(f"Shifted `{sec}s:{mss}ms`. Type `reset` to restore it.")

    try:
        msg = await bot.wait_for("message", timeout=60, check=_check_botmin)

        if "reset" in msg.content.lower().strip():
            subs.shift(s=-sec, ms=-mss)
            await ctx.send("Restored.")

    except asyncio.TimeoutError:
        pass

    subs.save(item.subtitle)

    await ctx.send(f"Subtitles updated for `{item.pretty_title}`.")


@commands.has_any_role("botmin")
@bot.command(name="cat", help="Add category to a random untagged movie.")
async def cat(ctx: commands.Context, *args):
    if not args:
        movie = Movie(**Category.random_untagged_movie())
    else:
        movie = Movie.from_query(" ".join(args))

    await ctx.send(f"Tell me the new category for {movie.simple_title}:")

    try:
        msg = await bot.wait_for("message", timeout=60, check=_check_botmin)

        if "pass" not in msg.content.lower().strip():
            category = Category(name=msg.content.strip().title())
            category.register_for_movie(movie.id)
            await ctx.send(embed=movie.embed)
        else:
            await ctx.send("Ignored.")

    except asyncio.TimeoutError:
        await ctx.send("Bye")


def _check_author(author):
    return lambda message: message.author == author


async def _interactive_index(ctx, items):
    chosen_index = 0

    try:
        msg = await bot.wait_for(
            "message", timeout=120, check=_check_author(ctx.author)
        )
        try:
            chosen_index = int(msg.content.lower().strip()) - 1
            items[chosen_index]
        except (ValueError, IndexError):
            await ctx.send("Invalid index! Bye")
            return None

    except asyncio.TimeoutError:
        await ctx.send("Timeout! Bye")
        return None

    return chosen_index


async def _interactive_int_index(ctx, items):
    try:
        msg = await bot.wait_for(
            "message", timeout=120, check=_check_author(ctx.author)
        )
        try:
            selected = int(msg.content.lower().strip())
            if selected not in items:
                raise ValueError

            return selected
        except ValueError:
            await ctx.send("Invalid index! Bye")
            return None

    except asyncio.TimeoutError:
        await ctx.send("Timeout! Bye")
        return None


async def _interactive_y_n(ctx):
    try:
        msg = await bot.wait_for(
            "message", timeout=120, check=_check_author(ctx.author)
        )
        return msg.content.lower().strip() == "y"
    except asyncio.TimeoutError:
        return await ctx.send("Timeout! Bye")


async def _pretty_title_list(ctx, items, append=None):
    str_list = "\n".join(f"{n}. {m.pretty_title()}" for n, m in enumerate(items, 1))
    msg = f"Choose the item you want to add ('n' to ignore):\n\n{str_list}"

    if append is not None:
        msg = f"{msg}\n\n{append}"

    await ctx.send(msg)


async def call_with_typing(ctx, loop, *args):
    result = None
    async with ctx.typing():
        result = await loop.run_in_executor(*args)

    return result


_MIN_BYTES = 1e9


def _pretty_gbs(bytes_):
    return f"{bytes_/float(1<<30):,.1f} GBs"


@bot.command(name="addm", help="Add a movie to the database.")
async def addmovie(ctx: commands.Context, *args):
    with Curator(ctx.author.id, KINOBASE) as curator:
        size_left = curator.size_left()

    if size_left < _MIN_BYTES:
        return await ctx.send(
            f"You need at least a quota of 1 GB to use this feature. You have {_pretty_gbs(size_left)}."
        )

    query = " ".join(args)

    user = User.from_discord(ctx.author)
    user.load()

    loop = asyncio.get_running_loop()

    client = await call_with_typing(ctx, loop, None, RadarrClient.from_constants)
    movies = await call_with_typing(ctx, loop, None, client.lookup, query)
    movies = movies[:10]

    movie_views = [MovieView(movie) for movie in movies]

    await _pretty_title_list(ctx, movie_views)

    chosen_index = await _interactive_index(ctx, movies)

    if chosen_index is None:
        return None

    chosen_movie_view = movie_views[chosen_index]
    if chosen_movie_view.already_added():  # or chosen_movie_view.to_be_added():
        return await ctx.send("This movie is already in the database.")

    await ctx.send(embed=chosen_movie_view.embed())
    await ctx.send("Are you sure? (y/n)")

    sure = await _interactive_y_n(ctx)
    if sure is None:
        return None

    if not sure:
        return await ctx.send("Dumbass (jk)")

    result = await call_with_typing(
        ctx, loop, None, client.add, movies[chosen_index], False
    )

    pretty_title = f"**{chosen_movie_view.pretty_title()}**"

    await ctx.send("Looking for releases")
    manual_r = await call_with_typing(
        ctx, loop, None, client.manual_search, result["id"]
    )

    models = [ReleaseModel(**item) for item in manual_r]
    models = [
        model for model in models if model.seeders and "Unknown" != model.quality.name
    ]
    if not models:
        return await ctx.send("No releases found.")

    models.sort(key=lambda x: x.size, reverse=False)

    append_txt = (
        "Expected quality: **Blu-ray > WEB-DL > WEBrip/DVD > Others**.\n**Bitrate > Resolution** "
        "(most cases).\nAsk admin if you are not sure about releases "
        "that require manual import; your GBs won't be recovered."
    )
    await _pretty_title_list(ctx, models[:20], append_txt)

    chosen_index = await _interactive_index(ctx, models)
    if chosen_index is None:
        return None

    await ctx.send("Are you sure? (y/n)")

    model_1 = models[chosen_index]

    if model_1.size > size_left:
        return await ctx.send("You don't have enough GBs available.")

    sure = await _interactive_y_n(ctx)
    if sure is None:
        return None

    await loop.run_in_executor(
        None,
        client.add_to_download_queue,
        model_1.movie_id,
        model_1.guid,
        model_1.indexer_id,
    )

    register_movie_addition(user.id, chosen_movie_view.tmdb_id)

    with Curator(ctx.author.id, KINOBASE) as curator:
        curator.register_addition(model_1.size, "Made via curator command")
        new_size_left = curator.size_left()

    await ctx.send(
        f"Getting the release. Let's wait.\nGBs left: {_pretty_gbs(new_size_left)}"
    )

    await asyncio.sleep(10)

    retries = 0
    grabbed_event_sent = False

    while 45 > retries:
        events = await loop.run_in_executor(
            None, client.events_in_history, result["id"]
        )
        for event in events:
            if event == "downloadFolderImported":
                return await ctx.reply(f"{pretty_title} is ready!")

            if event == "grabbed" and not grabbed_event_sent:
                grabbed_event_sent = True
                # await ctx.reply(
                #    f"Good news: {pretty_title} is being imported. Let's wait..."
                # )
            else:
                logger.debug("Unknown event: %s", event)

        retries += 1
        await asyncio.sleep(60)

    if grabbed_event_sent:
        await ctx.reply(
            f"{pretty_title} is taking too much time to import. Botmin will "
            "have a look if the issue persists."
        )
    else:
        await ctx.reply(
            f"Impossible to add {pretty_title} automatically. Botmin will check it manually."
        )


@bot.command(name="addtv", help="Add a TV Show's season to the database.")
async def addtvshow(ctx: commands.Context, *args):
    with Curator(ctx.author.id, KINOBASE) as curator:
        size_left = curator.size_left()

    if size_left < _MIN_BYTES:
        return await ctx.send(
            f"You need at least a quota of 1 GB to use this feature. You have {_pretty_gbs(size_left)}."
        )

    query = " ".join(args)

    user = User.from_discord(ctx.author)
    user.load()

    loop = asyncio.get_running_loop()

    client = await call_with_typing(ctx, loop, None, SonarrClient.from_constants)
    items = await call_with_typing(ctx, loop, None, client.lookup, query)
    tv_models = [SonarrTVShowModel(**item) for item in items[:10]]

    await _pretty_title_list(ctx, tv_models)

    chosen_index = await _interactive_index(ctx, tv_models)

    if chosen_index is None:
        return None

    chosen_tv = tv_models[chosen_index]

    await ctx.send(embed=chosen_tv.embed())
    await ctx.send("Are you sure? (y/n)")

    sure = await _interactive_y_n(ctx)
    if sure is None:
        return None

    if not sure:
        return await ctx.send("Bye")

    result = await call_with_typing(
        ctx, loop, None, client.add, items[chosen_index], False
    )
    series_id = result["id"]

    valid_seasons = [i.season_number for i in chosen_tv.seasons if i.season_number]
    await ctx.send(f"Select the season: {', '.join(str(i) for i in valid_seasons)}")
    chosen_season = await _interactive_int_index(ctx, valid_seasons)
    if chosen_season is None:
        return None

    await ctx.send(
        f"Looking for releases [{chosen_tv.pretty_title()} Season {chosen_season}]"
    )
    manual_r = await call_with_typing(
        ctx,
        loop,
        None,
        client.manual_search,
        result["id"],
        chosen_season,
    )

    models = [ReleaseModelSonarr(**item, seriesId=series_id) for item in manual_r]  # type: ignore
    models = [model for model in models if model.seeders]
    if not models:
        return await ctx.send("No releases found.")

    models.sort(key=lambda x: x.size, reverse=False)

    append_txt = (
        "Expected quality: **Blu-ray > WEB-DL > WEBrip/DVD > Others**.\n**Bitrate > Resolution** "
        "(most cases). Subtitles are harder to get for HDTV releases.\nAsk admin if you are not "
        "sure about releases that require manual import."
    )
    await _pretty_title_list(ctx, models[:20], append_txt)

    chosen_index = await _interactive_index(ctx, models)
    if chosen_index is None:
        return None

    await ctx.send("Are you sure? (y/n)")

    model_1 = models[chosen_index]

    if model_1.size > size_left:
        return await ctx.send("You don't have enough GBs available.")

    sure = await _interactive_y_n(ctx)
    if sure is None:
        return None

    await loop.run_in_executor(
        None,
        client.add_to_download_queue,
        model_1.guid,
        model_1.indexer_id,
    )

    register_tv_show_season_addition(user.id, chosen_tv.tvdb_id, chosen_season)

    with Curator(ctx.author.id, KINOBASE) as curator:
        curator.register_addition(model_1.size, "Made via curator command")
        new_size_left = curator.size_left()

    await ctx.send(
        f"Getting the release. Let's wait.\nGBs left: {_pretty_gbs(new_size_left)} "
        "Check #announcements."
    )


_GB = float(1 << 30)


@bot.command(name="gkey", help="Give a curator key")
@commands.has_any_role("botmin")
async def gkey(ctx: commands.Context, user: Member, gbs, *args):
    bytes_ = int(_GB * float(gbs))
    note = " ".join(args)

    with Curator(user.id, KINOBASE) as curator:
        curator.register_key(bytes_, note)

    await ctx.send(f"Key of {gbs} GBs registered for user:{user.id}")


@bot.command(name="gbs", help="Get GBs free to use for curator tasks")
async def gbs(ctx: commands.Context):
    with Curator(ctx.author.id, KINOBASE) as curator:
        size_left = curator.size_left()

    await ctx.send(_pretty_gbs(size_left))


@bot.command(name="getid", help="Get an user ID by search query.")
@commands.has_any_role("botmin", "verifier")
async def getid(ctx: commands.Context, *args):
    user = User.from_query(" ".join(args))
    await ctx.send(f"{user.name} ID: {user.id}")


@bot.event
async def on_command_error(ctx: commands.Context, error):
    await handle_error(ctx, error)


def _check_botmin(message):
    return str(message.author.top_role) == "botmin"


def run(token: str, prefix: str):
    bot.command_prefix = prefix

    bot.run(token)
