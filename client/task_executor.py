import random
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
import capsolver
from urllib.parse import quote


def format_proxy(proxy_str):
    ip, port, user, password = proxy_str.split(':')
    return {
        'http': f'http://{user}:{password}@{ip}:{port}',
        'https': f'http://{user}:{password}@{ip}:{port}',
    }

def reservation_dates(start_date, end_date):
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if start.isoformat() != start_date or end.isoformat() != end_date:
        raise ValueError('Reservation dates must use YYYY-MM-DD.')
    day_count = (end - start).days + 1
    if not 1 <= day_count <= 31:
        raise ValueError('Reservation date range must contain between 1 and 31 days.')
    return [(start + timedelta(days=offset)).isoformat() for offset in range(day_count)]


def execute_task(task, capsolver_key, capmonster_key, proxies, webhook_url):
    auth_token = task['auth_token']
    payment_id = task['payment_id']
    restaurant_id = task['restaurant_id']
    party_sz = task['party_sz']
    start_date = task['start_date']
    end_date = task['end_date']
    start_time = task['start_time']
    end_time = task['end_time']
    delay = task['delay']
    days = reservation_dates(start_date, end_date)
    pause_seconds = max(delay / 1000, 1.0)
    #captcha_service = task['captcha_service']

    headers = {
            'X-Resy-Auth-Token': auth_token,
            'Authorization': 'ResyAPI api_key="VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'X-Resy-Universal-Auth': auth_token,
            'Accept-Encoding': 'gzip, deflate, br',
            'Host': 'api.resy.com',
            'Accept': 'application/json, text/plain, */*',
            'Referer': 'https://resy.com/',
    }

    #captcha_key = capsolver_key if captcha_service == 'CAPSolver' else capmonster_key
    #capsolver.api_key = captcha_key

    while True:
        try:
            for day in days:
                select_proxy = format_proxy(random.choice(proxies)) if proxies else {}
                response = requests.get(
                    'https://api.resy.com/4/find',
                    params={'lat': 0, 'long': 0, 'day': day, 'party_size': party_sz, 'venue_id': restaurant_id},
                    headers=headers,
                    proxies=select_proxy,
                    timeout=(5, 15),
                )
                if response.status_code != 200:
                    send_discord_notification(webhook_url, f'Failed to get availability for restaurant {restaurant_id} - {response.status_code}', summary=f'Availability check failed (HTTP {response.status_code}); no booking was submitted by this task.')
                    return

                data = response.json()
                results = data.get('results') if isinstance(data, dict) else None
                venues = results.get('venues') if isinstance(results, dict) else None
                if not isinstance(venues, list):
                    send_discord_notification(webhook_url, f'Unexpected availability response for restaurant {restaurant_id}', summary='Slot response was unexpected; no booking was submitted by this task.')
                    return

                for venue in venues[:1]:
                    for slot in venue['slots']:
                        config_token = slot['config']['token']
                        parts = config_token.split('/')
                        time_part = parts[8].split(':')[0]
                        if int(time_part) >= int(start_time) and int(time_part) <= int(end_time):
                            book_token = get_details(day, party_sz, config_token, restaurant_id, headers, select_proxy)
                            reservationVal = book_reservation(book_token, auth_token, payment_id, day, party_sz, restaurant_id, config_token, headers, select_proxy)

                            if 'reservation_id' in reservationVal or ('specs' in reservationVal and 'reservation_id' in reservationVal['specs']):
                                send_discord_notification(webhook_url, f'Reservation booked for restaurant {restaurant_id} - {reservationVal}', summary='Booking API returned a reservation ID. Verify the reservation in your Resy account before retrying.')
                            else:
                                send_discord_notification(webhook_url, f'Failed to book reservation for restaurant {restaurant_id} - {reservationVal}', summary='Booking was not confirmed. Check your Resy account before retrying; submission may have occurred.')
                            return

                print(f'No matching slot for {day}; waiting {pause_seconds:g} seconds before the next check.')
                time.sleep(pause_seconds)
        except Exception:
            import traceback
            print('failed to execute task')
            traceback.print_exc()
            break


def get_captcha_token(captcha_key, site_key, url, proxy):
    solution = capsolver.solve({
        "type": "RecaptchaV2Task",
        "websiteKey": site_key,
        "websiteURL": url,
        "proxy": proxy['http']
    })
    gRecaptchaResponse = solution['gRecaptchaResponse']
    return gRecaptchaResponse
    
def get_details(day, party_size, config_token, restaurant_id, headers, select_proxy):
    url = 'http://127.0.0.1:8000/api/get-details'
    payload = {
        'day': day,
        'party_size': party_size,
        'config_token': config_token,
        'restaurant_id': restaurant_id,
        'headers': headers,
        'select_proxy': select_proxy
    }

    response = requests.post(url, json=payload)
    
    if response.status_code != 200:
        print(f'Failed to get details for restaurant {restaurant_id} - {response.text} - {response.status_code}')
        return

    data = response.json()
    return data['response_value']

def book_reservation(book_token, auth_token, payment_id, day, party_size, restaurant_id, config_token, headers, select_proxy):
    url = 'http://127.0.0.1:8000/api/book-reservation'
    payload = {
        'book_token': book_token,
        'auth_token': auth_token,
        'payment_id': payment_id,
        'day': day,
        'party_size': party_size,
        'restaurant_id': restaurant_id,
        'config_token': config_token,
        'headers': headers,
        'select_proxy': select_proxy
    }

    response = requests.post(url, json=payload)

    return response.json()
        
def send_discord_notification(webhook_url, message, *, summary='Task ended; check reservation status in Resy before retrying.'):
    print(summary)
    if not webhook_url:
        print('Discord notification skipped (no webhook configured). Check reservation status in Resy.')
        return
    data = {"content": message}
    requests.post(webhook_url, json=data)

def run_tasks_concurrently(tasks, capsolver_key, capmonster_key, proxies, webhook_url):
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = [executor.submit(execute_task, task, capsolver_key, capmonster_key, proxies, webhook_url) for task in tasks]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print('Failed to execute task')
                print(e)