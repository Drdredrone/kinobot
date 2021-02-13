import logging
import re
import sqlite3

import asyncio
import click

from random import randint, shuffle

from discord import Embed, User
from discord.ext import commands

import kinobot.db as db
from kinobot import DISCORD_TOKEN
from kinobot.comments import dissect_comment
from kinobot.music import search_tracks, extract_id_from_url
from kinobot.exceptions import (
    EpisodeNotFound,
    MovieNotFound,
    OffensiveWord,
    InvalidRequest,
)
from kinobot.request import search_episode, search_movie
from kinobot.utils import get_id_from_discord, is_episode

db.create_discord_db()
db.create_music_db()

bot = commands.Bot(command_prefix="!")

REQUEST_RE = re.compile(r"[^[]*\[([^]]*)\]")
BASE = "https://kino.caretas.club"
RANGE_DICT = {"1️⃣": 0, "2️⃣": 1, "3️⃣": 2, "4️⃣": 3, "5️⃣": 4}
GOOD_BAD = ("👍", "💩")
EMOJI_STRS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣")


async def handle_discord_request(ctx, command, args, music=False):
    request = " ".join(args)
    user_disc = ctx.author.name + ctx.author.discriminator
    username = db.get_name_from_discriminator(user_disc)

    if not username:
        return await ctx.send("You are not registered. Use `!register <YOUR NAME>`.")

    try:
        request_dict = dissect_comment(f"!{command} {request}", music)
        if not request_dict:
            return await ctx.send("Invalid syntax.")
    except (MovieNotFound, EpisodeNotFound, OffensiveWord, InvalidRequest) as kino_exc:
        return await ctx.send(f"Nope: {type(kino_exc).__name__}.")

    request_id = str(randint(2000000, 5000000))

    try:
        db.insert_request(
            (
                username[0],
                request_dict["comment"],
                request_dict["command"],
                request_dict["title"],
                "|".join(request_dict["content"]),
                request_id,
                1,
            )
        )
        db.verify_request(request_id)
        return await ctx.send(f"Added. ID: {request_id}; user: {username[0]}.")
    except sqlite3.IntegrityError:
        return await ctx.send("Duplicate request.")


async def handle_queue(ctx, queue, title):
    if queue:
        shuffle(queue)
        description = "\n".join(queue[:10])
        return await ctx.send(embed=Embed(title=title, description=description))

    await ctx.send(embed=Embed(title=title, description="apoco si pa"))


def check_botmin(message):
    return str(message.author.top_role) == "botmin"


def enumerate_requests(requests):
    return [
        f"{n}. **{req[0]}** ~ *{req[2]}* - {req[1]}"
        for n, req in enumerate(requests, start=1)
    ]


@bot.command(name="req", help="make a regular request")
async def request(ctx, *args):
    message = await handle_discord_request(ctx, "req", args)
    [await message.add_reaction(emoji) for emoji in GOOD_BAD]


@bot.command(name="parallel", help="make a parallel request")
async def parallel(ctx, *args):
    message = await handle_discord_request(ctx, "parallel", args)
    [await message.add_reaction(emoji) for emoji in GOOD_BAD]


@bot.command(name="palette", help="make a palette request")
async def palette(ctx, *args):
    message = await handle_discord_request(ctx, "palette", args)
    [await message.add_reaction(emoji) for emoji in GOOD_BAD]


@bot.command(name="mreq", help="make a music request")
async def mreq(ctx, *args):
    await handle_discord_request(ctx, "req", args, True)


@bot.command(name="register", help="register yourself")
async def register(ctx, *args):
    name = " ".join(args).title()
    discriminator = ctx.author.name + ctx.author.discriminator
    if not name:
        return await ctx.send("Usage: `!register <YOUR NAME>`")

    if not "".join(args).isalpha():
        return await ctx.send("Invalid name.")

    try:
        db.register_discord_user(name, discriminator)
        return await ctx.send("You were registered as '{name}'.")
    except sqlite3.IntegrityError:
        old_name = db.get_name_from_discriminator(discriminator)[0]
        try:
            db.update_discord_name(name, discriminator)
            db.update_name_from_requests(old_name, name)
            return await ctx.send(f"Your name was updated: '{name}'.")
        except sqlite3.IntegrityError:
            return await ctx.send("Duplicate name.")


@bot.command(name="queue", help="get user queue")
async def queue(ctx, user: User = None):
    try:
        if user:
            name = db.get_name_from_discriminator(user.name + user.discriminator)[0]
            queue = db.get_user_queue(name)
        else:
            name = db.get_name_from_discriminator(
                ctx.author.name + ctx.author.discriminator
            )[0]
            queue = db.get_user_queue(name)
    except TypeError:
        return await ctx.send("User not registered.")

    await handle_queue(ctx, queue, f"{name}' queue")


@bot.command(name="pq", help="get priority queue")
async def priority_queue(ctx):
    queue = db.get_priority_queue()
    await handle_queue(ctx, queue, "Priority queue")


@bot.command(name="sr", help="search requests")
async def search_request_(ctx, *args):
    query = " ".join(args)

    if query.endswith("!1"):
        requests = db.search_requests(query.replace("!1", ""))

        if requests:
            final = []
            for request in requests:
                content = REQUEST_RE.findall(request[0])
                if len(content) == 1:
                    final.append(request)

            requests = final[:5]
            message = await ctx.send("\n".join(enumerate_requests(requests)))
    else:
        requests = db.search_requests(query)[:5]
        if requests:
            message = await ctx.send("\n".join(enumerate_requests(requests)))

    if requests:
        return [
            await message.add_reaction(emoji) for emoji in EMOJI_STRS[: len(requests)]
        ]

    await ctx.send("Nothing found.")


@bot.command(name="music", help="add a music video to the database", usage="URL QUERY")
async def music(ctx, *args):
    url = args[0]
    query = " ".join(args[1:])

    video_id = extract_id_from_url(url)
    if not video_id:
        return await ctx.reply("Video ID not found.")

    results = list(search_tracks(query))
    if not results:
        return await ctx.reply("Track not found.")

    tracks = [f"{n}. {item['complete']}" for n, item in enumerate(results, 1)]

    message = await ctx.reply("Select the track to save:\n\n" + "\n".join(tracks))

    [await message.add_reaction(emoji) for emoji in EMOJI_STRS[: len(results)]]

    def check_react(reaction_, user_):
        return user_ == ctx.author

    try:
        reaction, user = await bot.wait_for(
            "reaction_add", timeout=20, check=check_react
        )
    except asyncio.TimeoutError:
        return await ctx.reply("Timeout. Choose one next time, dumbass.")

    index = RANGE_DICT[str(reaction)]
    selected = results[index]

    try:
        if str(ctx.author.top_role) == "botmin":
            await ctx.reply("Tell me the category.")
            msg = await bot.wait_for("message", timeout=30, check=check_botmin)
            category = msg.content
        else:
            category = "Unknown"

        db.insert_music_video(video_id, selected["artist"], selected["title"], category)
        message_ = await ctx.reply(
            f"Music added to the database: {tracks[index]} with '{category}' "
            "category. React :poop: to delete it."
        )

        await message_.add_reaction(GOOD_BAD[1])

        reaction, user = await bot.wait_for(
            "reaction_add", timeout=20, check=check_react
        )

        if str(reaction) == GOOD_BAD[1]:
            db.delete_music_video(video_id)
            await ctx.reply("Deleted.")

    except sqlite3.IntegrityError:
        await ctx.reply("Already added.")


@bot.command(name="sm", help="search for music videos in the db", usage="QUERY")
async def search_m(ctx, *args):
    query = " ".join(args)
    results = db.search_db_tracks(query)
    if not results:
        return await ctx.reply("Nothing found.")

    tracks = [
        f"{n}. {item[1]} - {item[2]} ({item[3]})" for n, item in enumerate(results)
    ]
    await ctx.reply("Results:\n\n" + "\n".join(tracks))

    if str(ctx.author.top_role) != "botmin":
        return

    await ctx.reply("`{INDEXES,...}:TEXT`")

    msg = await bot.wait_for("message", timeout=45, check=check_botmin)
    commands_ = str(msg.content).split()

    for command_ in commands_:
        try:
            numbers, text = command_.split(":")
        except ValueError:
            continue

        for number in numbers.split(","):
            tmp_index = int(number)

            if "delete" in text:
                db.delete_music_video(results[tmp_index][0])
                await ctx.reply(f"Deleted: {tracks[tmp_index]}.")
            else:
                db.update_music_category(results[tmp_index][0], text)
                await ctx.reply(
                    f"Updated category: {text} for {results[tmp_index][2]}."
                )


def search_item(query, return_dict=False):
    if is_episode(query):
        EPISODE_LIST = db.get_list_of_episode_dicts()
        result = search_episode(EPISODE_LIST, query, raise_resting=False)
        if not return_dict:
            return f"{BASE}/episode/{result['id']}"
    else:
        MOVIE_LIST = db.get_list_of_movie_dicts()
        result = search_movie(MOVIE_LIST, query, raise_resting=False)
        if not return_dict:
            return f"{BASE}/movie/{result['tmdb']}"

    return result


@bot.command(name="search", help="search for a movie or an episode")
async def search(ctx, *args):
    query = " ".join(args)
    try:
        await ctx.send(search_item(query))
    except (MovieNotFound, EpisodeNotFound):
        await ctx.send("apoco si pa")


@bot.command(name="key", help="return a key value from a movie or an episode")
async def key(ctx, *args):
    key = args[0].strip()
    query = " ".join(args[1:])
    try:
        item = search_item(query, True)
        try:
            await ctx.send(f"{item['title']}'s {key}: {item[key]}")
        except KeyError:
            await ctx.send(f"Invalid key. Choose between: {', '.join(item.keys())}")

    except (MovieNotFound, EpisodeNotFound):
        await ctx.send("apoco si pa")


@bot.command(name="delete", help="delete a request by ID")
@commands.has_any_role("botmin", "verifier")
async def delete(ctx, arg):
    await ctx.send(db.remove_request(arg.strip()))


@bot.command(name="verify", help="verify a request by ID")
@commands.has_any_role("botmin", "verifier")
async def verify(ctx, arg):
    await ctx.send(db.verify_request(arg.strip()))


@bot.command(name="block", help="block an user by name")
@commands.has_any_role("botmin", "verifier")
async def block(ctx, *args):
    user = " ".join(args)
    db.block_user(user.strip())
    db.purge_user_requests(user.strip())
    await ctx.send("Ok.")


@bot.command(name="list", help="get user list (admin-only)")
@commands.has_permissions(administrator=True)
async def user_list(ctx, *args):
    users = db.get_discord_user_list()
    embed = Embed(title="List of users", description=", ".join(users))
    await ctx.send(embed=embed)


@bot.command(name="sql", help="run a sql command on Kinobot's DB (admin-only)")
@commands.has_permissions(administrator=True)
async def sql(ctx, *args):
    command = " ".join(args)
    try:
        db.execute_sql_command(command)
        message = f"Command OK: {command}."
    except sqlite3.Error as sql_exc:
        message = f"Error: {sql_exc}."

    await ctx.send(message)


@bot.command(name="purge", help="purge user requests by user (admin-only)")
@commands.has_permissions(administrator=True)
async def purge(ctx, user: User):
    try:
        user = db.get_name_from_discriminator(user.name + user.discriminator)[0]
    except TypeError:
        return await ctx.send("No requests found for given user")

    db.purge_user_requests(user)
    await ctx.send(f"Purged: {user}.")


@bot.event
async def on_reaction_add(reaction, user):
    content = reaction.message.content

    if user.bot:
        return

    if not str(reaction) in GOOD_BAD + EMOJI_STRS:
        return

    if not str(user.top_role) in "botmin verifier":
        return

    if content.startswith(("Results", "Select", "Tell", "Music")):
        return

    channel = bot.get_channel(reaction.message.channel.id)

    if content.startswith("1. "):
        split_ = content.split("\n")
        try:
            index = split_[RANGE_DICT[str(reaction)]]
        except IndexError:
            return await channel.send("apoco si pa")

        request_id = index.split("-")[-1].strip()
        return await channel.send(db.verify_request(request_id))

    item_id = get_id_from_discord(content)

    if content.startswith("Added") and str(reaction) == GOOD_BAD[1]:
        return await channel.send(db.remove_request(item_id))

    if content.startswith("Possible NSFW") and str(reaction) == GOOD_BAD[0]:
        return await channel.send(db.verify_request(item_id))


@click.command("discord")
def discord_bot():
    " Run discord Bot. "
    logging.basicConfig(level=logging.INFO)
    bot.run(DISCORD_TOKEN)