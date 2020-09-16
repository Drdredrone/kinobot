from facepy import GraphAPI
from functools import reduce

import cv2
import os
import random
import re
import kinobot_utils.comments as check_comments
import kinobot_utils.subs as subs
import kinobot_utils.random_picks as random_picks
import normal_kino
import facepy
import sys
import json
import datetime


FACEBOOK = os.environ.get("FACEBOOK")
FILM_COLLECTION = os.environ.get("FILM_COLLECTION")
TV_COLLECTION = os.environ.get("TV_COLLECTION")
MOVIE_JSON = os.environ.get("MOVIE_JSON")
TV_JSON = os.environ.get("TV_JSON")
COMMENTS_JSON = os.environ.get("COMMENTS_JSON")
MONKEY_PATH = os.environ.get("MONKEY_PATH")
FB = GraphAPI(FACEBOOK)

tiempo = datetime.datetime.now()
tiempo_str = tiempo.strftime("Automatically executed at %H:%M:%S GMT-4")


def get_normal():
    id_normal = normal_kino.main(FILM_COLLECTION, TV_COLLECTION, FB, tiempo_str)
    comment_post(id_normal)


def cleansub(text):
    cleanr = re.compile("<.*?>")
    cleantext = re.sub(cleanr, "", text)
    return cleantext


def save_images(pil_list):
    nums = random.sample(range(10000), len(pil_list))
    names = ['/tmp/{}.png'.format(i) for i in nums]
    for im, nam in zip(pil_list, names):
        im.save(nam)
    return names


def post_multiple(images, message):
    IDs = []
    for image in images:
        IDs.append(
            {
                "media_fbid": FB.post(
                    path="me/photos", source=open(image, "rb"), published=False
                )["id"]
            }
        )
    final = FB.post(
        path="me/feed",
        attached_media=json.dumps(IDs),
        message=message,
        published=True,
    )
    return final["id"]


def post_request(file, movie_info, discriminator, request, tiempo, is_episode=False, is_multiple=True):
    if is_episode:
        title = "{} - {}{}".format(
            movie_info["title"], movie_info["season"], movie_info["episode"]
        )
    else:
        if movie_info["title"] != movie_info["original_title"] and len(movie_info["original_title"]) < 45:
            pretty_title = '{} [{}]'.format(movie_info["original_title"], movie_info["title"])
        else:
            pretty_title = movie_info["title"]
        title = "{} by {} ({})".format(pretty_title, movie_info["director(s)"], movie_info["year"])

    print("Posting")
    disc = cleansub(discriminator)
    mes = (
        "{}\n{}\n\nRequested by {} (!req {})\n\n"
        "{}\nThis bot is open source: https://github.com/"
        "vitiko98/Certified-Kino-Bot".format(
            title, disc, request["user"], request["comment"], tiempo_str
        )
    )
    if len(file) > 1:
        return post_multiple(file, mes)
    else:
        id2 = FB.post(
            path="me/photos", source=open(file[0], "rb"), published=True, message=mes
        )
        return id2["id"]


def comment_post(postid):
    desc = random_picks.get_rec(MOVIE_JSON)
    desc.save("/tmp/tmp_collage.png")
    com = (
        "Complete list: https://kino.caretas.club\n"
        '\nRequest examples:\n"!req Taxi Driver [you talking to me?]"\n"!req Stalker [20:34]"\n'
        '"!req The Wire s01e01 [this america, man] [40:30]"'
    )
    FB.post(
        path=postid + "/comments",
        source=open("/tmp/tmp_collage.png", "rb"),
        message=com,
    )
    print(postid)


def notify(comment_id, content, fail=False):
    if not fail:
        noti = (
            "202: Your request was successfully executed."
            "\n\nI haven't added over 450 movies in vain! If you "
            "request the SAME MOVIE too many times, your requests will be disabled."
            " Check the list of available films"
            " and episodes: https://kino.caretas.club".format(content)
        )
    else:
        noti = (
            "404: Something went wrong with your request. Please, don't forget "
            "to check the list of available films, episodes and instructions befo"
            "re embarrassing the bot: https://kino.caretas.club"
        )
    try:
        FB.post(path=comment_id + "/comments", message=noti)
    except facepy.exceptions.FacebookError:
        pass


def write_js(slctd):
    with open(COMMENTS_JSON, "w") as c:
        json.dump(slctd, c)


def handle_requests(slctd):
    inc = 0
    while True:
        m = slctd[inc]
        if not m["used"]:
            m["used"] = True
            print("Request: " + m["movie"])
            try:
                if len(m["content"]) > 6:
                    raise AttributeError
                is_episode = True if m["episode"] else False
                is_multiple = True if len(m["content"]) > 1 else False
                Frames = []
                for frame in m["content"]:
                    Frames.append(
                        subs.Subs(
                            m["movie"],
                            frame,
                            MOVIE_JSON,
                            TV_JSON,
                            is_episode=is_episode,
                            multiple=is_multiple,
                        )
                    )

                if is_multiple:
                    quote_list = [word.discriminator for word in Frames]
                    if Frames[0].isminute:
                        discriminator = "Minutes: " + ", ".join(quote_list)
                    else:
                        discriminator = ", ".join(quote_list)
                else:
                    if Frames[0].isminute:
                        discriminator = "Minute: " + Frames[0].discriminator
                    else:
                        discriminator = Frames[0].discriminator
                """final_image_list = [im.pill for im in Frames]
                to_save = random_picks.get_collage(final_image_list, resize=False)
                output = "/tmp/" + m["id"] + ".png"
                to_save.save(output) """
                final_image_list = [im.pill for im in Frames]
                single_image_list = reduce(lambda x, y: x + y, final_image_list)
                print(single_image_list)
                output_list = save_images(single_image_list)

                post_id = post_request(
                    output_list,
                    Frames[0].movie,
                    discriminator,
                    m,
                    tiempo,
                    is_episode,
                )

                write_js(slctd)
                comment_post(post_id)
                notify(m['id'], m['comment'])
                break
            except (TypeError, UnicodeDecodeError, NameError, IndexError,
                    cv2.error, AttributeError, subs.DuplicateRequest,
                    facepy.exceptions.FacebookError):
                notify(m['id'], m['comment'], fail=True)
                write_js(slctd)
                pass

        inc += 1
        if inc == len(slctd):
            get_normal()
            break


def main():
    slctd = check_comments.main(COMMENTS_JSON, FB)
    if slctd:
        handle_requests(slctd)
    else:
        get_normal()


if __name__ == "__main__":
    sys.exit(main())