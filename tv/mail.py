import datetime
import email
import imaplib
import json
import os
import re
import smtplib
import ssl
import time
from email import encoders
from email import policy
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import unquote
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests
import yaml
from bs4 import BeautifulSoup

from kairos import tools
from tv import tv
import http.client as http_client
# -------------------------------------------------
#
# Utility to read email from Gmail Using Python
#
# ------------------------------------------------

TEST = False
BASE_DIR = r"" + os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CURRENT_DIR = os.path.curdir

config = tools.get_config(CURRENT_DIR)
log = tools.create_log()
log.setLevel(20)
log.setLevel(config.getint('logging', 'level'))

uid = str(config.get('mail', 'uid'))
pwd = str(config.get('mail', 'pwd'))
imap_server = config.get("mail", "imap_server")
imap_port = 993
smtp_server = config.get("mail", "smtp_server")
smtp_port = 465

charts = dict()


def create_browser(run_in_background=True):
    return tv.create_browser(run_in_background)


def destroy_browser(browser):
    tv.destroy_browser(browser)


def login(browser):
    tv.login(browser)


def take_screenshot(browser, symbol, interval, retry_number=0):
    return tv.take_screenshot(browser, symbol, interval, retry_number)


def process_data(data, browser):
    for response_part in data:
        if isinstance(response_part, tuple):
            msg = email.message_from_string(response_part[1].decode('utf-8'), policy=policy.default)
            email_subject = str(msg['subject'])
            if email_subject.find('TradingView Alert') >= 0:
                log.info('Processing: {} - {}'.format(msg['date'], email_subject))
                # get email body
                if msg.is_multipart():
                    for part in msg.walk():
                        ctype = part.get_content_type()
                        cdispo = str(part.get('Content-Disposition'))
                        # only use parts that are text/plain and not an attachment
                        if ctype == 'text/plain' and 'attachment' not in cdispo:
                            process_body(part, browser)
                            break
                else:
                    process_body(msg, browser)


def process_body(msg, browser):
    try:
        url = ''
        screenshot_url = ''
        date = msg['date']
        body = msg.get_content()
        soup = BeautifulSoup(body, features="lxml")
        links = soup.find_all('a', href=True)
        screenshot_charts = []

        tv_generated_url = ''
        for link in links:
            if link['href'].startswith('https://www.tradingview.com/chart/?') and tv_generated_url == '':
                tv_generated_url = link['href']
            if link['href'].startswith('https://www.tradingview.com/chart/') and url == '':
                # first chart found that is generated by Kairos should be the url to the chart, either %CHART or from include_screenshots_of_charts (see _example.yaml)
                url = link['href']
            elif link['href'].startswith('https://www.tradingview.com/x/'):
                screenshot_url = link['href']
        if url == '':
            url = tv_generated_url

        # search_screenshots =
        match = re.search("screenshots_to_include: \\[(.*)\\]", body)
        if match:
            screenshot_charts = match.group(1).split(',')
            log.debug('charts to include:' + str(screenshot_charts))

        log.debug("chart's url: " + url)
        if url == '':
            return False

        symbol = ''
        match = re.search("\\w+[%3A|:]\\w+$", url, re.M)
        try:
            symbol = match.group(0)
            symbol = symbol.replace('%3A', ':')
        except re.error as match_error:
            log.exception(match_error)
        for script in soup(["script", "style"]):
            script.extract()  # rip it out

        text = soup.get_text()
        lines = (line.strip() for line in text.splitlines())  # break into lines and remove leading and trailing space on each
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))  # break multi-headlines into a line each
        # drop blank lines
        j = 0
        alert = ''
        for chunk in chunks:
            chunk = str(chunk).replace('\u200c', '')
            chunk = str(chunk).replace('&zwn', '')
            if j == 0:
                if chunk:
                    alert = str(chunk).split(':')[1].strip()
                    j = 1
            elif not chunk:
                break
            elif str(chunk).startswith('https://www.tradingview.com/chart/'):
                url = str(chunk)
            elif str(chunk).startswith('https://www.tradingview.com/x/'):
                screenshot_url = str(chunk)
            else:
                alert += ', ' + str(chunk)
        alert = alert.replace(',,', ',')
        alert = alert.replace(':,', ':')

        interval = ''
        match = re.search("(\\d+)\\s(\\w\\w\\w)", alert)
        if match:
            interval = match.group(1)
            unit = match.group(2)
            if unit == 'day':
                interval += 'D'
            elif unit == 'wee':
                interval += 'W'
            elif unit == 'mon':
                interval += 'M'
            elif unit == 'hou':
                interval += 'H'
            elif unit == 'min':
                interval += ''

        if len(screenshot_charts) == 0:
            if screenshot_url:
                screenshot_charts.append(screenshot_url)
            else:
                screenshot_charts.append(url)

        screenshots = dict()
        filenames = dict()
        # Open the chart and make a screenshot
        if config.has_option('logging', 'screenshot_timing') and config.get('logging', 'screenshot_timing') == 'summary':
            for i, screenshot_chart in enumerate(screenshot_charts):
                screenshot_chart = unquote(screenshot_charts[i])
                # screenshot_chart = screenshot_charts[i]
                # log.info(screenshot_chart)
                browser.execute_script("window.open('{}');".format(screenshot_chart))
                for handle in browser.window_handles[1:]:
                    browser.switch_to.window(handle)
                # page is loaded when we are done waiting for an clickable element
                tv.wait_and_click(browser, tv.css_selectors['btn_calendar'])
                tv.wait_and_click(browser, tv.css_selectors['btn_watchlist_menu'])
                [screenshot_url, filename] = take_screenshot(browser, symbol, interval)
                if screenshot_url != '':
                    screenshots[screenshot_chart] = screenshot_url
                if filename != '':
                    filenames[screenshot_chart] = filename
                tv.close_all_popups(browser)
        charts[url] = [symbol, alert, date, screenshots, filenames]
    except Exception as e:
        log.exception(e)


def read_mail(browser):
    # noinspection PyBroadException
    try:
        mail = imaplib.IMAP4_SSL(imap_server)
        mail.login(uid, pwd)
        result, data = mail.list()
        if result != 'OK':
            log.error(result)
            return False

        mailbox = 'inbox'
        if config.has_option('mail', 'mailbox') and config.get('mail', 'mailbox') != '':
            mailbox = str(config.get('mail', 'mailbox'))
        mail.select(mailbox)

        search_area = "UNSEEN"
        if config.has_option('mail', 'search_area') and config.get('mail', 'search_area') != '':
            search_area = str(config.get('mail', 'search_area'))
        if search_area != "UNSEEN" and config.has_option('mail', 'search_term') and config.get('mail', 'search_term') != '':
            search_term = u"" + str(config.get('mail', 'search_term'))
            log.debug('search_term: ' + search_term)
            mail.literal = search_term.encode("UTF-8")

        log.debug('search_area: ' + search_area)
        try:
            result, data = mail.search("utf-8", search_area)
            mail_ids = data[0]
            id_list = mail_ids.split()
            if len(id_list) == 0:
                log.info('no TradingView alerts found in mailbox ' + mailbox)
            else:
                for mail_id in id_list:
                    result, data = mail.fetch(mail_id, '(RFC822)')
                    try:
                        process_data(data, browser)
                    except Exception as e:
                        log.exception(e)

        except imaplib.IMAP4.error as mail_error:
            log.error("Search failed. Please verify you have a correct search_term and search_area defined.")
            log.exception(mail_error)

        mail.close()
        mail.logout()
    except Exception as e:
        log.exception(e)


def save_watchlist_to_file(csv, filename=''):
    filepath = ''
    if config.has_option('logging', 'watchlist_path'):
        watchlist_dir = config.get('logging', 'watchlist_path')
        if watchlist_dir != '':
            if not os.path.exists(watchlist_dir):
                # noinspection PyBroadException
                try:
                    os.mkdir(watchlist_dir)
                except Exception as e:
                    log.info('No watchlist directory specified or unable to create it.')
                    log.exception(e)

            if os.path.exists(watchlist_dir):
                if filename != '':
                    filename = filename.replace('%DATE', datetime.datetime.today().strftime('%Y-%m-%d'))
                    filename = filename.replace('%TIME', datetime.datetime.today().strftime('%H%M'))
                else:
                    filename = datetime.datetime.today().strftime('%Y-%m-%d_%H%M')

                # TradingView's upload dialog expects the file to end with .txt
                filename += '.txt'
                filepath = os.path.join(watchlist_dir, filename)
                f = open(filepath, "w")
                f.write(csv)
                f.close()

    return [filepath, filename]


def update_watchlist(browser, filename, markets, delay_after_update):
    cleanup_browser = False
    if not browser:
        browser = tv.create_browser(tv.RUN_IN_BACKGROUND)
        login(browser)
        cleanup_browser = True

    result = tv.update_watchlist(browser, filename, markets, delay_after_update)
    if cleanup_browser:
        tv.destroy_browser(browser)
    return result


def send_mail(summary_config):
    try:
        text = ''
        list_html = ''
        html = ''
        csv = ''
        to = [uid]
        cc = []
        bcc = []
        mime_images = []
        watchlist_att = None

        headers = dict()
        headers['Subject'] = 'TradingView Alert Summary'
        headers['From'] = uid
        headers['To'] = uid

        email_config = None
        if summary_config and 'email' in summary_config:
            email_config = summary_config['email']
        if 'to' in email_config and len(email_config['to']) > 0:
            to = email_config['to']
            if 'one-mail-per-recipient' in email_config and not email_config['one-mail-per-recipient']:
                headers['To'] = ",".join(to)
        if 'cc' in email_config and len(email_config['cc']) > 0:
            cc = email_config['cc']
            if 'one-mail-per-recipient' in email_config and not email_config['one-mail-per-recipient']:
                headers['Cc'] = ",".join(cc)
        if 'bcc' in email_config and len(email_config['bcc']) > 0:
            bcc = email_config['bcc']
        if 'subject' in email_config and email_config['subject'] != '':
            headers['Subject'] = '' + email_config['subject']

        count = 0
        if config.has_option('mail', 'format') and config.get('mail', 'format') == 'table':
            html += '<table><thead><tr><th>Date</th><th>Symbol</th><th>Alert</th><th>Screenshot</th><th>Chart</th></tr></thead><tbody>'

        for url in charts:
            symbol = charts[url][0]
            alert = charts[url][1]
            date = charts[url][2]
            screenshots = charts[url][3]
            filenames = []
            if len(charts[url]) >= 4:
                filenames = charts[url][4]

            if config.has_option('mail', 'format') and config.get('mail', 'format') == 'table':
                html += generate_table_row(date, symbol, alert, screenshots, url)
            else:
                list_html += generate_list_entry(mime_images, alert, screenshots, filenames, url, count)

            text += generate_text(date, symbol, alert, screenshots, url)

            if csv == '':
                csv += symbol
            else:
                csv += ',' + symbol
            count += 1

        # send alerts to webhooks
        if summary_config and 'webhooks' in summary_config:
            webhooks_config = summary_config['webhooks']
            if type(webhooks_config) is list:
                for config_item in webhooks_config:
                    webhooks = config_item['url']
                    enabled = True
                    if 'enabled' in config_item:
                        enabled = config_item['enabled']
                    if enabled:
                        search_criteria = []
                        batch_size = 0
                        headers = None
                        headers_by_request = None
                        if 'search_criteria' in config_item:
                            search_criteria = config_item['search_criteria']
                        if 'batch_size' in config_item:
                            batch_size = config_item['batch_size']
                        if 'batch' in config_item:
                            batch_size = config_item['batch']
                        if 'headers' in config_item:
                            headers = config_item['headers']
                        if 'set_headers_by_request' in config_item:
                            if not headers:
                                headers = {}
                            headers_by_request = config_item['set_headers_by_request']
                            headers = set_headers_by_request(headers, headers_by_request)
                        send_alert_to_webhooks(charts, webhooks, search_criteria, batch_size, headers, headers_by_request)
        elif config.has_option('webhooks', 'search_criteria') and config.has_option('webhooks', 'webhook'):
            webhooks = config.getlist('webhooks', 'webhook')
            search_criteria = []
            if config.has_option('webhooks', 'search_criteria'):
                search_criteria = config.getlist('webhooks', 'search_criteria')
            batch_size = 0
            if config.has_option('webhooks', 'batch_size'):
                batch_size = config.getint('webhooks', 'batch_size')
            send_alert_to_webhooks(charts, webhooks, search_criteria, batch_size)

        # send alerts to Google Spreadsheet
        if config.has_option('api', 'google') and summary_config and 'google_sheets' in summary_config:
            google_api_creds = config.get('api', 'google')
            google_sheets_config = summary_config['google_sheets']
            if type(google_sheets_config) is list:
                for config_item in google_sheets_config:
                    name = config_item['name']
                    sheet = ''
                    search_criteria = []
                    enabled = True
                    index = 1
                    if 'sheet' in config_item:
                        sheet = config_item['sheet']
                    if 'index' in config_item:
                        index = config_item['index']
                    if 'search_criteria' in config_item:
                        search_criteria = config_item['search_criteria']
                    if 'enabled' in config_item:
                        enabled = config_item['enabled']
                    if enabled:
                        send_alert_to_google_sheet(google_api_creds, charts, name, sheet, index, search_criteria)

        if config.has_option('mail', 'format') and config.get('mail', 'format') == 'table':
            html += '</tbody></tfooter><tr><td>Number of alerts:' + str(count) + '</td></tr></tfooter></table>'
        else:
            html += '<h2>TradingView Alert Summary</h2><h3>Number of alerts: ' + str(count) + '</h3>' + list_html

        if email_config and 'text' in email_config and email_config['text'] != '':
            text = email_config['text'].replace('%SUMMARY', '' + text)
        if email_config and 'html' in email_config and email_config['html'] != '':
            html = email_config['html'].replace('%SUMMARY', '' + html)

        if html[:6].lower() != '<html>':
            html = '<html><body>' + html + '</body></html>'

        delay_after_update = 5
        # create watchlist
        if summary_config and 'watchlist' in summary_config:
            watchlist_config = summary_config['watchlist']
            filename = watchlist_config['name']
            [filepath, filename] = save_watchlist_to_file(csv, filename)
            filepath = os.path.join(os.getcwd(), filepath)
            log.info('watchlist ' + filepath + ' created')
            if 'delay_after_update' in watchlist_config:
                delay_after_update = watchlist_config['delay_after_update']
            if watchlist_config['import']:
                watchlist_name = filename.replace('.txt', '')
                if update_watchlist(None, watchlist_name, csv, delay_after_update):
                    log.info("watchlist imported into TradingView as '" + watchlist_name + "'")
            if watchlist_config['attach-to-email']:
                watchlist_att = MIMEBase('application', "octet-stream")
                watchlist_att.set_payload(open(filepath, "rb").read())
                encoders.encode_base64(watchlist_att)
                watchlist_att.add_header('Content-Disposition', 'attachment; filename="' + filename + '"')
        else:
            result = save_watchlist_to_file(csv)
            filepath = os.path.join(os.getcwd(), result[0])
            log.info('watchlist ' + filepath + ' created')

        recipients = to + cc + bcc

        if (not email_config) or ('send' in email_config and email_config['send']):
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_server, context=context) as server:
                server.login(uid, pwd)

                if 'one-mail-per-recipient' in email_config and email_config['one-mail-per-recipient']:
                    for recipient in recipients:
                        headers['To'] = str(recipient)
                        msg = MIMEMultipart('alternative')
                        for mime_image in mime_images:
                            msg.attach(mime_image)
                        msg.attach(MIMEText(text, 'plain'))
                        msg.attach(MIMEText(html, 'html'))
                        if watchlist_att:
                            msg.attach(watchlist_att)
                        for key in headers:
                            msg[key] = headers[key]

                        server.sendmail(uid, [recipient], msg.as_string())
                        log.info("Mail send to: " + str(recipient))
                else:
                    msg = MIMEMultipart('alternative')
                    for mime_image in mime_images:
                        msg.attach(mime_image)
                    msg.attach(MIMEText(text, 'plain'))
                    msg.attach(MIMEText(html, 'html'))
                    if watchlist_att:
                        msg.attach(watchlist_att)
                    for key in headers:
                        msg[key] = headers[key]
                    server.sendmail(uid, recipients, msg.as_string())
                    log.info("Mail send to: " + str(recipients))

                server.quit()

    except Exception as e:
        log.exception(e)


def generate_text(date, symbol, alert, screenshots, url):
    result = url + "\n" + alert + "\n" + symbol + "\n" + date + "\n"
    for chart in screenshots:
        result += screenshots[chart] + "\n"
    return result


def generate_list_entry(mime_images, alert, screenshots, filenames, url, count):
    result = '<hr><h3>' + alert + '</h3><h4>Alert generated on chart: <a href="' + url + '">' + url + '<a></h4>'
    if len(screenshots) > 0:
        for chart in screenshots:
            result += '<p><a href="' + chart + '"><img src="' + screenshots[chart] + '"/></a><br/><a href="'+screenshots[chart]+'">' + screenshots[chart] + '</a></p>'
    elif len(filenames) > 0:
        for chart in filenames:
            try:
                screenshot_id = str(count + 1)
                fp = open(filenames[chart], 'rb')
                mime_image = MIMEImage(fp.read())
                fp.close()
                mime_image.add_header('Content-ID', '<screenshot' + screenshot_id + '>')
                mime_images.append(mime_image)
                result += '<p><a href="' + chart + '"><img src="cid:screenshot' + screenshot_id + '"/></a><br/>' + filenames[chart] + '</p>'
            except Exception as send_mail_error:
                log.exception(send_mail_error)
                result += '<p><a href="' + url + '">Error embedding screenshot: ' + filenames[chart] + '</a><br/>' + filenames[chart] + '</p>'
    return result


def generate_table_row(date, symbol, alert, screenshots, url):
    result = '<tr><td>' + date + '</td><td>' + symbol + '</td><td>' + alert + '</td><td>'
    for chart in screenshots:
        result += '<a href="' + screenshots[chart] + '">' + screenshots[chart] + '</a>'
    result += '</td><td>' + '<a href="' + url + '">' + url + '</a>' + '</td></tr>'
    return result


def send_alert_to_webhooks(data, webhooks, search_criteria='', batch_size=0, headers=None, headers_by_request=None):
    result = False
    try:
        batches = []
        batch = []
        for url in data:

            if len(batch) >= batch_size > 0:
                batches.append(batch)
                batch = []

            symbol = data[url][0]
            alert = data[url][1]
            date = data[url][2]
            screenshots = data[url][3]

            screenshot = ''
            for chart in screenshots:
                if screenshot == '':
                    screenshot = screenshots[chart]

            if len(search_criteria) == 0:
                batch.append({'date': date, 'symbol': symbol, 'alert': alert, 'chart_url': url, 'screenshot_url': screenshot, 'screenshots': screenshots})
            else:
                for search_criterium in search_criteria:
                    if str(alert).find(str(search_criterium)) >= 0:
                        batch.append({'date': date, 'symbol': symbol, 'alert': alert, 'chart_url': url, 'screenshot_url': screenshot, 'screenshots': screenshots})
                        break

        # append the final batch
        if len(batch) > 0:
            batches.append(batch)
        # send batches to webhooks
        if len(batches) > 0:
            send_webhooks(webhooks, batches, headers, headers_by_request)
    except Exception as e:
        log.exception(e)
    return result


def send_webhooks(webhooks, batches, headers=None, headers_by_request=None):
    # http_client.HTTPConnection.debuglevel = 1
    try:
        i = 0
        count_batches = 0
        total_batches = str(len(batches))
        while len(batches) > 0:
            count_batches += 1
            for webhook in webhooks:
                if webhook:
                    json_data = {'signals': batches[i]}
                    data = json.dumps(json_data)
                    if TEST:
                        print(data)
                        print(headers)
                        result = [200, 'OK', '{"TEST (no actual request send)"}', 'TEST (no actual request send)']
                    else:
                        r = requests.post(str(webhook), data=data, headers=headers)
                        # unfortunately, we cannot always send a raw image (e.g. zapier)
                        # elif filename:
                        #     screenshot_bytestream = ''
                        #     try:
                        #         fp = open(filename, 'rb')
                        #         screenshot_bytestream = MIMEImage(fp.read())
                        #         fp.close()
                        #     except Exception as send_webhook_error:
                        #         log.exception(send_webhook_error)
                        #     r = requests.post(webhook_url, json={'date': date, 'symbol': symbol, 'alert': alert, 'chart_url': url, 'screenshot_url': screenshot, 'screenshot_bytestream': screenshot_bytestream})
                        result_json = ""
                        try:
                            result_json = repr(r.json())
                        except Exception as e:
                            log.debug(e)
                        result = [r.status_code, r.reason, result_json, r.text]
                    if 200 <= result[0] <= 226:
                        log.info('{} {}/{} {} {} {}'.format(str(webhook),  str(count_batches), str(total_batches), str(result[0]), str(result[1]), str(result[2])))
                    elif (result[0] == 401 or result[0] == 403) and headers_by_request:
                        log.info('{} {}/{} {} {} {}'.format(str(webhook), str(count_batches), str(total_batches), str(result[0]), str(result[1]), str(result[3])))
                        log.info("authorization failed, updating headers")
                        headers = set_headers_by_request(headers, headers_by_request)
                        return send_webhooks(webhooks, batches, headers)
                    else:
                        log.info('{} {}/{} {} {} {} {}'.format(str(webhook), str(count_batches), str(total_batches), str(result[0]), str(result[1]), str(result[2]), str(result[3])))

            batches.remove(batches[i])
    except Exception as e:
        log.exception(e)
    finally:
        http_client.HTTPConnection.debuglevel = 0


def send_alert_to_google_sheet(google_api_creds, data, name, sheet='', index=1, search_criteria=''):
    try:
        result = ''
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']
        credentials = ServiceAccountCredentials.from_json_keyfile_name(google_api_creds, scope)
        client = gspread.authorize(credentials)
        sheet = client.open(name).worksheet(sheet)

        limit = 100
        if config.has_option('api', 'google_write_requests_per_100_seconds_per_user'):
            limit = config.getint('api', 'google_write_requests_per_100_seconds_per_user')

        inserted = 0
        for url in data:
            log.info(data[url])
            symbol = data[url][0]
            alert = data[url][1]
            date = data[url][2]
            screenshots = data[url][3]
            [exchange, market] = symbol.split(':')

            screenshot = ''
            for chart in screenshots:
                if screenshot == '':
                    screenshot = screenshots[chart]

            row = [date, alert, url, screenshot, exchange, market]
            if TEST:
                log.info(row)
            else:
                if len(search_criteria) == 0:
                    result = sheet.insert_row(row, index, 'RAW')
                else:
                    for search_criterium in range(len(search_criteria)):
                        if str(alert).find(str(search_criterium)) >= 0:
                            result = sheet.insert_row(row, index)
                            break
                if result:
                    log.debug(str(result))
                inserted += 1
                if inserted == 100:
                    log.info('API limit reached. Waiting {} seconds before continuing...' + str(limit))
                    time.sleep(limit)
                    inserted = 0
    except Exception as e:
        log.exception(e)


def set_headers_by_request(headers, configs):

    yaml_ok = True

    for a_config in configs:
        mandatory = ['request']
        for header in mandatory:
            if not (header in a_config):
                log.warn("'" + str(header) + "' not declared in YAML")
                yaml_ok = False

        request_config = a_config['request']
        mandatory = ['url', 'type', 'headers', 'body', 'response_values']
        for header in mandatory:
            if not (header in request_config):
                log.warn("'" + str(header) + "' not declared in YAML")
                yaml_ok = False

        if not yaml_ok:
            log.info(str(a_config))
            log.warn("Incomplete YAML")
            return headers

        request_url = request_config['url']
        request_type = request_config['type']
        request_headers = request_config['headers']
        request_body = request_config['body']
        response_values = request_config['response_values']

        try:
            status = [501, 'Not Implemented: ' + str(request_type)]
            result = ""
            if request_type == 'POST':
                r = requests.post(request_url, data=json.dumps(request_body), headers=request_headers)
                status = [r.status_code, r.reason]
                for header in response_values:
                    if r.json:
                        result = r.json()
                        result = result[response_values[header]]
                    elif r.text:
                        result = r.text
                    headers[header] = result
            if 200 <= status[0] <= 226:
                log.info('{} {} {}'.format(str(request_url), str(status[0]), str(status[1])))
            else:
                log.warn('{} {} {}'.format(str(request_url), str(status[0]), str(status[1])))
        except Exception as e:
            log.exception(e)
    return headers


def run(delay, file):
    if TEST:
        log.info("RUNNING IN TEST MODE")
    log.info("Generating summary mail with a delay of {} minutes.".format(str(delay)))
    time.sleep(delay*60)

    run_in_background = config.getboolean('webdriver', 'run_in_background')
    summary_config = ''

    if file:
        file = r"" + os.path.join(config.get('tradingview', 'settings_dir'), file)
        if not os.path.exists(file):
            log.error("File {} does not exist. Did you setup your kairos.cfg and yaml file correctly?".format(str(file)))
            raise FileNotFoundError

        with open(file, 'r') as stream:
            try:
                data = yaml.safe_load(stream)
                if 'summary' in data:
                    summary_config = data['summary']
                if 'webdriver' in data and 'run-in-background' in data['webdriver']:
                    run_in_background = data['webdriver']['run-in-background']
            except Exception as err_yaml:
                log.exception(err_yaml)

    tv.RUN_IN_BACKGROUND = run_in_background
    browser = create_browser(run_in_background)
    login(browser)
    read_mail(browser)
    destroy_browser(browser)
    if len(charts) > 0:
        send_mail(summary_config)
