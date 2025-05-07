import os
import json
from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
import firebase_admin
from firebase_admin import credentials, firestore
import uvicorn
import requests
import aiohttp
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Firebase Firestore
try:
    firebase_creds = json.loads(os.getenv("FIREBASE_CREDENTIALS"))
    logger.debug(f"Firebase credentials keys: {firebase_creds.keys()}")
    cred = credentials.Certificate(firebase_creds)
    firebase_admin.initialize_app(cred)
    logger.debug("Firebase initialized successfully")
except Exception as e:
    logger.error(f"Firebase initialization failed: {str(e)}")
    raise
db = firestore.client()

app = FastAPI()

# HTML template with Tailwind CSS
BASE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Token Validation</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body {
            background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
    </style>
</head>
<body>
    <div class="max-w-md w-full mx-auto p-6 bg-white rounded-xl shadow-lg">
        {content}
    </div>
</body>
</html>"""

BASE_API_URL = "https://beigeparrotfish.onpella.app"  # Your pella.app URL
PING_URL = "https://fastapi-render-8jly.onrender.com/checkpoint_start?token=ping-dummy-token"

# CPM rates from ShrinkMe.io
CPM_RATES = {
    "GL": 22.00, "IE": 16.00, "US": 11.00, "BE": 7.50, "GB": 7.00,
    "CA": 6.50, "BR": 6.50, "NZ": 6.00, "AU": 6.00, "SE": 5.50,
    "FR": 5.00, "ES": 5.00, "DE": 5.00, "IN": 4.10, "ID": 4.00,
    "PH": 4.00, "IT": 4.00, "TH": 4.00, "SA": 4.00, "MX": 4.00,
}
DEFAULT_CPM = 3.50

def get_token_reward(country_code):
    cpm = CPM_RATES.get(country_code, DEFAULT_CPM)
    if cpm >= 16:
        return 10
    elif cpm >= 7:
        return 7.5
    elif cpm > 3.50:
        return 5
    else:
        return 5

async def get_ip_info(ip):
    async with aiohttp.ClientSession() as session:
        async with session.get(f'http://ip-api.com/json/{ip}?fields=status,message,country,countryCode,query') as response:
            data = await response.json()
            logger.info(f"IP API Response for {ip}: {data}")
            return data

def shorten_with_shrinkme(long_url):
    SHRINKME_API_KEY = "d1e14519207609e39e742a29ffc2d88782800083"  # Your key
    try:
        response = requests.get(f'https://shrinkme.io/api?api={SHRINKME_API_KEY}&url={long_url}', timeout=10)
        response.raise_for_status()
        data = response.json()
        logger.info(f"ShrinkMe API Response: {data}")
        if data.get('status') == 'success':
            return data['shortenedUrl']
        else:
            raise Exception(f"Failed to shorten URL: {data.get('error', 'Unknown error')}")
    except requests.RequestException as e:
        logger.error(f"ShrinkMe API request failed: {str(e)}")
        raise Exception(f"Failed to shorten URL: Request error - {str(e)}")

def ping_api():
    """Send a ping request to keep the Render server active."""
    try:
        response = requests.get(PING_URL, timeout=10)
        logger.info(f"Ping request to {PING_URL}: Status {response.status_code}")
    except requests.RequestException as e:
        logger.error(f"Ping request failed: {str(e)}")

# Set up scheduler for pinging every 15 minutes
scheduler = BackgroundScheduler()
scheduler.add_job(
    ping_api,
    trigger=IntervalTrigger(minutes=15),
    id='keep_alive_ping',
    name='Ping API every 15 minutes',
    replace_existing=True
)
scheduler.start()
logger.info("Started scheduler for API ping every 15 minutes")

@app.get("/checkpoint_start", response_class=HTMLResponse)
async def checkpoint_start(request: Request, token: str = Query(...)):
    # Get the client's real IP from cf-connecting-ip
    user_ip = request.headers.get("cf-connecting-ip")
    if not user_ip:
        content = "<p class='text-center text-red-600'>Could not detect your IP. Are you using a proxy?</p>"
        return HTMLResponse(content=BASE_HTML.replace("{content}", content), status_code=400)
    logger.info(f"Detected client IP: {user_ip}")

    ip_info = await get_ip_info(user_ip)
    if ip_info['status'] != 'success':
        content = f"<p class='text-center text-red-600'>Failed to get IP info: {ip_info.get('message', 'Unknown error')}</p>"
        return HTMLResponse(content=BASE_HTML.replace("{content}", content), status_code=400)

    user_ip = ip_info['query']  # Confirm the IP from the API
    country = ip_info['country']
    country_code = ip_info['countryCode']
    token_reward = get_token_reward(country_code)

    # Skip token processing for ping requests
    if token == "ping-dummy-token":
        content = "<p class='text-center text-gray-600'>Ping request received</p>"
        return HTMLResponse(content=BASE_HTML.replace("{content}", content), status_code=200)

    token_ref = db.collection('tokens').where('token', '==', token).limit(1).stream()
    token_doc = next(token_ref, None)
    if token_doc:
        db.collection('tokens').document(token_doc.id).update({
            'initial_ip': user_ip,
            'initial_country': country,
            'token_reward': token_reward
        })
    else:
        content = "<p class='text-center text-red-600'>Invalid token.</p>"
        return HTMLResponse(content=BASE_HTML.replace("{content}", content), status_code=400)

    checkpoint_end_url = f'{BASE_API_URL}/checkpoint_end?token={token}'
    try:
        shrinkme_link = shorten_with_shrinkme(checkpoint_end_url)
    except Exception as e:
        logger.error(f"Failed to generate ShrinkMe link: {str(e)}")
        content = "<p class='text-center text-red-600'>Failed to generate ad link. Please try again later.</p>"
        return HTMLResponse(content=BASE_HTML.replace("{content}", content), status_code=500)

    content = f"""
    <div class="text-center">
        <h3 class="text-lg font-semibold text-gray-900">Your IP Information</h3>
        <p class="mt-2 text-gray-600">Your IP Address is: {user_ip}</p>
        <p class="text-gray-600">Country: {country}</p>
        <p class="text-gray-600">Balance Target: {token_reward} tokens</p>
        <p class="mt-4 text-gray-600">Redirecting in 5 seconds...</p>
    </div>
    <script>
        setTimeout(function() {{
            window.location.href = "{shrinkme_link}";
        }}, 5000);
    </script>
    """
    return HTMLResponse(content=BASE_HTML.replace("{content}", content), status_code=200)

@app.get("/checkpoint_end", response_class=HTMLResponse)
async def checkpoint_end(request: Request, token: str = Query(...)):
    token_ref = db.collection('tokens').where('token', '==', token).limit(1).stream()
    token_doc = next(token_ref, None)

    if not token_doc:
        content = """<div class="text-center">Invalid Token</div>"""
        return HTMLResponse(content=BASE_HTML.replace("{content}", content), status_code=400)

    token_data = token_doc.to_dict()
    if token_data['status'] != 'pending':
        content = """<div class="text-center">Token Already Used</div>"""
        return HTMLResponse(content=BASE_HTML.replace("{content}", content), status_code=400)

    # Get current IP from cf-connecting-ip
    current_ip = request.headers.get("cf-connecting-ip")
    if not current_ip:
        content = "<p class='text-center text-red-600'>Could not detect your IP. Are you using a proxy?</p>"
        return HTMLResponse(content=BASE_HTML.replace("{content}", content), status_code=400)
    logger.info(f"Detected client IP at checkpoint_end: {current_ip}")

    ip_info = await get_ip_info(current_ip)
    if ip_info['status'] != 'success':
        content = f"<p class='text-center text-red-600'>Failed to get IP info: {ip_info.get('message', 'Unknown error')}</p>"
        return HTMLResponse(content=BASE_HTML.replace("{content}", content), status_code=400)

    current_ip = ip_info['query']
    current_country = ip_info['country']

    # Verify IP and country consistency
    if current_ip != token_data.get('initial_ip') or current_country != token_data.get('initial_country'):
        content = """<div class="text-center text-red-600">IP or Country Mismatch</div>"""
        return HTMLResponse(content=BASE_HTML.replace("{content}", content), status_code=400)

    # Add tokens and update status
    token_reward = token_data['token_reward']
    db.collection('tokens').document(token_doc.id).update({'status': 'used'})
    user_id = token_data['userId']
    db.collection('users').document(user_id).update({'balance': firestore.Increment(token_reward)})

    content = f"""<div class="text-center">
        <h3 class="text-lg font-semibold text-gray-900">Success!</h3>
        <p class="mt-1 text-gray-600">{token_reward} tokens have been added to your balance.</p>
        <a href="https://t.me/Boraksms_bot" class="mt-4 inline-flex items-center px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700">Return to Bot</a>
    </div>"""
    return HTMLResponse(content=BASE_HTML.replace("{content}", content), status_code=200)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)