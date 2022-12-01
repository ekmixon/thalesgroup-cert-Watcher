# coding=utf-8
from .models import Keyword, Alert, PasteId, Subscriber
import requests
from django.db import close_old_connections
from django.utils import timezone
from datetime import timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from django.conf import settings
from django.db.models.functions import Length
from .mail_template.default_template import get_template
from .mail_template.group_template import get_group_template
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from json.decoder import JSONDecodeError
import smtplib


def start_scheduler():
    """
    Launch multiple planning tasks in background:
        - Fire main every 5 minutes from Monday to Sunday
        - Fire cleanup every 2 hours
    """
    scheduler = BackgroundScheduler()

    scheduler.add_job(main_data_leak, 'cron', day_of_week='mon-sun', minute='*/5', id='week_job', max_instances=10,
                      replace_existing=True)

    scheduler.add_job(cleanup, 'cron', day_of_week='mon-sun', hour='*/2', id='clean', replace_existing=True)
    scheduler.start()


def cleanup():
    """
    Remove 2 hours old, useless, pasteIDs.
    """
    close_old_connections()
    print(f"{str(timezone.now())} - CRON TASK : Remove 2 hours old pasteIDs.")
    count = 0

    paste_ids = PasteId.objects.all()
    for paste_id in paste_ids:
        # A paste stay approximately 20-25 minutes in the same API 250 pastes request, if the paste is 2 hours old,
        # we can delete it.
        if (timezone.now() - paste_id.created_at) >= timedelta(hours=2):
            count += 1
            paste_id.delete()
    print(f"{str(timezone.now())} - Deleted ", count, " useless pasties ID.")


def main_data_leak():
    """
        Main function:
            - close_old_connections()
            - read in our list of keywords
            - check_keywords(keywords)
    """
    close_old_connections()
    print(f"{str(timezone.now())} - CRON TASK : Fetch searx & pastebin")
    # read in our list of keywords
    keywords = Keyword.objects.all().order_by(Length('name').desc())

    check_keywords(keywords)


def check_urls(keyword, urls):
    """
    Check if the URL is new.

    :param keyword: Keyword stored in database.
    :param urls: Fresh searx urls.
    :return: Urls not already in alert database column.
    :rtype: list
    """
    new_urls = []
    if Alert.objects.all():

        stored_urls = [alerts.url for alerts in Alert.objects.all()]

        for url in urls:

            if url not in stored_urls:
                print(f"{str(timezone.now())} - New URL for", keyword, " discovered: ", url)

                new_urls.append(url)
    else:
        new_urls = urls

    return new_urls


def check_searx(keyword):
    """
    Pull Searx instance for keyword.

    :param keyword: Keyword stored in database.
    :return: Matched urls.
    :rtype: list
    """
    hits = []
    urls = []

    # double quote for exact match
    keyword = '"' + str(keyword) + '"'
    params = {'q': keyword, 'engines': 'gitlab,github,bitbucket,apkmirror,gentoo,npm,stackoverflow,hoogle',
              'format': 'json'}

    print(f"{str(timezone.now())} - Querying Searx for: ", keyword)

    # send the request off to searx
    try:
        response = requests.get(settings.DATA_LEAK_SEARX_URL, params=params)
    except requests.exceptions.RequestException as e:
        print(f"{str(timezone.now())} - ", e)
        return hits

    try:
        results = response.json()
        # if we have results we want to check them against our stored URLs
        if len(results['results']):

            for result in results['results']:

                if result['url'] not in urls:
                    urls.append(result['url'])

            hits = check_urls(keyword, urls)
    except JSONDecodeError as e:
        # no JSON returned
        print(f"{str(timezone.now())} - ", e)
        return hits

    return hits


def check_pastebin(keywords):
    """
    Check Pastebin for keyword list.

    :param keywords: Keywords stored in database.
    :return: Matched urls & Corresponding keyword.
    :rtype: dictionary
    """
    global paste_content_hits
    paste_content_hits = {}
    paste_hits = {}

    # Fetch the Pastebin API
    try:
        response = requests.get("https://scrape.pastebin.com/api_scraping.php?limit=250")
    except requests.exceptions.RequestException as e:
        print(f"{str(timezone.now())} - ", e)
        return paste_hits

    if len(response.text) > 0:
        # Check if it is a json, cannot check 'application/json' in  content-type because it is always the same...
        if response.text[0] == "[":
            # parse the JSON
            result = response.json()

            # load up our list of stored paste ID's and only check the new ones
            pastebin = PasteId.objects.all()
            pastebin_ids = [paste.paste_id for paste in pastebin]
            new_ids = []
            for paste in result:

                if paste['key'] not in pastebin_ids:

                    new_ids.append(paste['key'])

                    # this is a new paste so send a secondary request to retrieve
                    # it and then check it for our keywords
                    try:
                        headers = {
                            'Referer': 'https://scrape.pastebin.com',
                            'User-Agent': 'Chrome/75.0.3770.142'
                        }

                        paste_response = requests.get(paste['scrape_url'], headers)
                        paste_body_lower = paste_response.content.lower()

                        keyword_hits = [
                            keyword
                            for keyword in keywords
                            if bytes(str(keyword).lower(), 'utf8')
                            in paste_body_lower
                        ]


                        if len(keyword_hits):
                            # We stored the first matched keyword, others are pointless
                            paste_hits[paste['full_url']] = keyword_hits[0]
                            paste_content_hits[paste['full_url']] = str(paste_body_lower.decode("utf-8"))

                            print(
                                f"{str(timezone.now())} - Hit on Pastebin for ",
                                keyword_hits,
                                ": ",
                                paste['full_url'],
                            )

                    except requests.exceptions.RequestException as e:
                        print(f"{str(timezone.now())} - ", e)

            # store the newly checked IDs
            for pastebin_id in new_ids:
                PasteId.objects.create(paste_id=pastebin_id)

            print(
                f"{str(timezone.now())} - Successfully processed ",
                len(new_ids),
                " Pastebin posts.",
            )

        else:
            print(str(
                timezone.now()) + " - Cannot Pull https://scrape.pastebin.com API. You need a Pastebin Pro Account. "
                                  "Please verify that the software IP is whitelisted: https://pastebin.com/doc_scraping_api")
    else:
        print(
            f"{str(timezone.now())} - Cannot Pull https://scrape.pastebin.com API. API is in maintenance"
        )


    return paste_hits


def check_keywords(keywords):
    """
    Check keywords in Searx Instance & Pastebin.

    :param keywords: Keywords stored in database.
    """
    # use the list of keywords and check each against searx
    for keyword in keywords:
        # query searx for the keyword
        results = check_searx(keyword)

        if len(results):
            for result in results:
                print(f"{str(timezone.now())} - Create alert for: ", keyword, "url: ", result)
                alert = Alert.objects.create(keyword=Keyword.objects.get(name=keyword), url=result)
                # limiting the number of specific email per alert
                if len(results) < 6:
                    send_email(alert)
            # if there is too many alerts, we send a group email
            if len(results) >= 6:
                send_group_email(keyword, len(results))

    # now we check Pastebin for new pastes
    result = check_pastebin(keywords)

    if len(result.keys()):
        for url, keyword in result.items():
            print(f"{str(timezone.now())} - Create alert for: ", keyword, "url: ", url)
            alert = Alert.objects.create(keyword=Keyword.objects.get(name=keyword), url=url,
                                         content=paste_content_hits[url])
            send_email(alert)


def send_email(alert):
    """
    Send e-mail alert.

    :param alert: Alert object.
    """
    if emails_to := [
        subscriber.user_rec.email for subscriber in Subscriber.objects.all()
    ]:
        try:
            msg = MIMEMultipart()
            msg['From'] = settings.EMAIL_FROM
            msg['To'] = ','.join(emails_to)
            msg['Subject'] = str(f"[ALERT #{str(alert.pk)}] Data Leak")
            body = get_template(alert)
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


def send_group_email(keyword, alerts_number):
    """
    Send group e-mail for a specific keyword.

    :param keyword: Matched Keyword.
    :param alerts_number: Number of alerts.
    """
    if emails_to := [
        subscriber.user_rec.email for subscriber in Subscriber.objects.all()
    ]:
        try:
            msg = MIMEMultipart()
            msg['From'] = settings.EMAIL_FROM
            msg['To'] = ','.join(emails_to)
            msg['Subject'] = str(f"[{str(alerts_number)} ALERTS] Data Leak")
            body = get_group_template(keyword, alerts_number)
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
