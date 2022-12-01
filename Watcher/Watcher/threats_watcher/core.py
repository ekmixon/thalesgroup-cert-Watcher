# coding=utf-8
from .models import BannedWord, Source, TrendyWord, PostUrl, Subscriber
from django.utils import timezone
from django.conf import settings
from datetime import timedelta
from datetime import datetime
import calendar
from apscheduler.schedulers.background import BackgroundScheduler
from nltk.tokenize import word_tokenize
from .mail_template.default_template import get_template
import feedparser
import requests
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import smtplib
from django.db import close_old_connections


def start_scheduler():
    """
    Launch multiple planning tasks in background:
        - Fire main_watch every 30 minutes from Monday to Friday (daylight only)
        - Fire main_watch at 18h00 on Saturday
        - Fire cleanup every day at 8 am
    """
    scheduler = BackgroundScheduler()

    scheduler.add_job(main_watch, 'cron', day_of_week='mon-sun', minute='*/30', id='main_watch_job',
                      max_instances=10,
                      replace_existing=True)

    scheduler.add_job(cleanup, 'cron', day_of_week='mon-sun', hour=8, minute=00, id='day_clean', replace_existing=True)
    scheduler.start()


def cleanup():
    """
    Remove words with a creation date greater than 30 days.
    """
    close_old_connections()
    print(
        f"{str(timezone.now())} - CRON TASK : Remove words with a creation date greater than 30 days"
    )

    words = TrendyWord.objects.all()
    for word in words:
        if (timezone.now() - word.created_at) >= timedelta(days=30):
            print(f"{str(timezone.now())} - Delete this trendy word -> ", word.name)
            word.delete()


def main_watch():
    """
    Main function:
        - close_old_connections()
        - load_feeds()
        - fetch_last_posts(settings.POSTS_DEPTH)
        - tokenize_count_urls()
        - remove_banned_words()
        - focus_five_letters()
        - focus_on_top(settings.WORDS_OCCURRENCE)
        - send_email()
    """
    close_old_connections()
    print(f"{str(timezone.now())} - CRON TASK : Main function")
    load_feeds()
    print(f"{str(timezone.now())} - Loaded feeds.")
    fetch_last_posts(settings.POSTS_DEPTH)
    print(f"{str(timezone.now())} - Fetched last posts.")
    tokenize_count_urls()
    print(f"{str(timezone.now())} - Tokenized words.")
    # print("POSTS Before Banned : ", posts_words)
    remove_banned_words()
    print(f"{str(timezone.now())} - Removed banned words.")
    # print("POSTS Without Banned Words : ", posts_without_banned)

    focus_five_letters()
    focus_on_top(settings.WORDS_OCCURRENCE)
    send_email()


def load_feeds():
    """
    Load feeds.
    """
    global rss_urls
    global feeds
    sources = Source.objects.all().order_by('id')
    feeds = []
    rss_urls = [source.url for source in sources]
    # print("RSS : ", rss_urls)


def fetch_last_posts(nb_max_post):
    """
    Fetch the nb last posts for each feed.

    :param nb_max_post: The deepness of the search on each feed.
    """
    global posts
    global posts_published
    posts = {}
    tmp_posts = {}
    posts_published = {}
    for url in rss_urls:
        try:
            feed_content = requests.get(url)
            feeds.append(feedparser.parse(feed_content.text))
        except requests.exceptions.RequestException as e:
            print(f"{str(timezone.now())} - ", e)
    for feed in feeds:
        count = 1
        for post in feed.entries:
            if count <= nb_max_post:
                count += 1
                if 'published_parsed' in post:
                    dt = datetime.fromtimestamp(calendar.timegm(post.published_parsed))
                else:
                    dt = "no-date"
                if 'link' in post and 'title' in post:
                    tmp_posts[str(post.title)] = post.link
                    posts_published[str(post.link)] = dt

    for title, url in tmp_posts.items():
        string = title.replace(u'\xa0', u' ')
        posts[string.lower()] = url
        # print("title lower : " + string.lower() + " url: " + url)


def tokenize_count_urls():
    """
    Tokenize phrases to words, Count word occurences and keep the word post source urls.
    """
    global posts_words
    global wordurl
    posts_words = {}
    wordurl = {}

    for title, url in posts.items():
        word_tokens = word_tokenize(title)
        for word in word_tokens:
            if word in posts_words:
                posts_words[word] += 1
                wordurl[f"{word}_url"] = f'{wordurl[f"{word}_url"]}, {url}'
            else:
                posts_words[word] = 1
                wordurl[f"{word}_url"] = url


def remove_banned_words():
    """
    Clean the posts for specific patterns: BannedWord, then english + french common words.
    """
    banned_words = BannedWord.objects.all().order_by('id')
    global posts_without_banned

    posts_without_banned = {}

    french = open('threats_watcher/datas/french.txt', 'r')
    french = french.read().splitlines()
    english = open('threats_watcher/datas/english.txt', 'r')
    english = english.read().splitlines()

    for word, count in posts_words.items():
        for word1 in banned_words:
            if word1.name == word:
                word = ""
        for word2 in english:
            word = re.sub(f'^{word2}$', "", word)
        for word3 in french:
            word = re.sub(f'^{word3}$', "", word)
        if word == "https":
            word = ""

        # Remove all special characters
        word = re.sub(r"[^a-zA-Z0-9]+" + r'$', '', word)
        # Remove version numbers
        word = re.sub(r"^(\d+\.)?(\d+\.)?(\*|\d+)?(\.\d+)?(\.\d+)$" + r'$', '', word)
        # Remove ' (sometimes regular expression don't catch this character)
        word = word.replace("'", "")
        if word := word.replace("/", ""):
            posts_without_banned[word] = count


def focus_five_letters():
    """
    Focus on 5 letters long words.
    """
    global posts_five_letters
    n = 4
    posts_five_letters = {
        word: count
        for word, count in posts_without_banned.items()
        if len(word) > n
    }


def focus_on_top(words_occurrence):
    """
    Focus on top words.
    Populated the database with only words with a minimum occurrence of  "words_occurence" in feeds.

    :param words_occurrence: Word occurence in feeds.
    """
    global email_words
    email_words = []
    new_posts = {}

    for word, occurrences in posts_five_letters.items():
        if occurrences >= words_occurrence:

            # If word is already created, update occurences number
            if TrendyWord.objects.filter(name=word):
                print(f"{str(timezone.now())} - {word} : ", occurrences, " (in database)")
                try:
                    for posturl in wordurl[f"{word}_url"].split(', '):
                        for url_, date in posts_published.items():
                            if posturl == url_ and not PostUrl.objects.filter(
                                url=posturl
                            ):
                                print(f"{str(timezone.now())} - {word}", " appeared in a new post!")
                                # Increase occurences number of 1
                                TrendyWord.objects.filter(name=word).update(
                                    occurrences=(TrendyWord.objects.get(name=word).occurrences + 1))

                                if date != "no-date":
                                    # Add new post
                                    PostUrl.objects.create(url=posturl, created_at=date)
                                else:
                                    PostUrl.objects.create(url=posturl)

                                # Link created word with new posts
                                TrendyWord.objects.get(name=word).posturls.add(PostUrl.objects.get(url=posturl))
                                new_posts[word] = new_posts.get(word, 0) + 1
                except KeyError:
                    pass
            else:
                print(f"{str(timezone.now())} - {word} : ", occurrences)
                # Add urls in DB
                try:
                    for url in wordurl[f"{word}_url"].split(', '):
                        for url_, date in posts_published.items():
                            if url == url_ and not PostUrl.objects.filter(
                                url=url
                            ):
                                if date != "no-date":
                                    # Add new post
                                    PostUrl.objects.create(url=url, created_at=date)
                                else:
                                    PostUrl.objects.create(url=url)

                    word_db = TrendyWord.objects.create(name=word, occurrences=occurrences)

                    # Link created words with new posts
                    for url in wordurl[f"{word}_url"].split(', '):
                        word_db.posturls.add(PostUrl.objects.get(url=url))

                    email_words.append(
                        f"<a href={settings.WATCHER_URL}>{word}</a> :<b> {str(occurrences)}</b>"
                    )

                except KeyError:
                    pass


def send_email():
    """
    Send e-mail alert.
    """
    if len(email_words) > 0:
        if emails_to := [
            subscriber.user_rec.email
            for subscriber in Subscriber.objects.all()
        ]:
            try:
                msg = MIMEMultipart()
                msg['From'] = settings.EMAIL_FROM
                msg['To'] = ','.join(emails_to)
                msg['Subject'] = "[WARNING] Threats Watcher buzzword detected"
                body = get_template(settings.WORDS_OCCURRENCE, email_words)
                msg.attach(MIMEText(body, 'html', _charset='utf-8'))
                text = msg.as_string()
                smtp_server = smtplib.SMTP(settings.SMTP_SERVER)
                smtp_server.sendmail(settings.EMAIL_FROM, emails_to, text)
                smtp_server.quit()

            except Exception as e:
                # Print any error messages to stdout
                print(f"{str(timezone.now())} - Email Error : ", e)
            finally:
                for email in emails_to:
                    print(f"{str(timezone.now())} - Email sent to ", email)
        else:
            print(f"{str(timezone.now())} - No subscriber, no email sent.")
    else:
        print(f"{str(timezone.now())} - No new word detected, no email sent.")
