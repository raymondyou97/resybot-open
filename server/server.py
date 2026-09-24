"""Authenticated loopback bridge; preserves the inherited upstream request methods."""

import json
import logging
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
import httpx  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402

from client.booking_state import BookingState, CampaignBlocked  # noqa: E402
from client.local_auth import local_token  # noqa: E402
from client.http_headers import CLIENT_USER_AGENT  # noqa: E402
from client.fees import FeePolicyError, validate_quote  # noqa: E402


logger = logging.getLogger(__name__)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)


class DetailsRequest(BaseModel):
    day: str
    party_size: int = Field(ge=1, le=20)
    config_token: str
    claim_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    restaurant_id: str
    headers: dict[str, str]
    select_proxy: dict[str, str] = Field(default_factory=dict)


class ReservationRequest(BaseModel):
    book_token: str
    payment_id: int
    day: str
    party_size: int = Field(ge=1, le=20)
    restaurant_id: str
    claim_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    headers: dict[str, str]
    select_proxy: dict[str, str] = Field(default_factory=dict)


def proxy_url(proxies):
    proxy = proxies.get('https') or proxies.get('http')
    if proxy and not proxy.startswith(('http://', 'https://')):
        raise HTTPException(400, 'Unsupported proxy scheme.')
    return proxy


def upstream_headers(headers):
    if not headers.get('Authorization') or not headers.get('X-Resy-Auth-Token'):
        raise HTTPException(400, 'Required authentication fields are missing.')
    return {
        'Authorization': headers['Authorization'],
        'X-Resy-Auth-Token': headers['X-Resy-Auth-Token'],
        'X-Resy-Universal-Auth': headers['X-Resy-Auth-Token'],
        'User-Agent': CLIENT_USER_AGENT,
        'Accept': 'application/json',
        'Referer': 'https://widgets.resy.com/',
    }


def create_app(token=None, booking_state=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', '[::1]'])

    @app.middleware('http')
    async def protect(request: Request, call_next):
        if request.headers.get('origin'):
            return JSONResponse({'detail': 'Browser-origin requests are not supported.'}, status_code=403)
        if request.url.path != '/':
            expected = token or local_token()
            supplied = request.headers.get('authorization', '')
            if not secrets.compare_digest(supplied.encode(), f'Bearer {expected}'.encode()):
                return JSONResponse({'detail': 'Local authentication required.'}, status_code=401)
        body = b''
        async for chunk in request.stream():
            body += chunk
            if len(body) > 32768:
                return JSONResponse({'detail': 'Request too large.'}, status_code=413)
        request._body = body
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse({'detail': 'Invalid request fields; values are withheld.'}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        return JSONResponse(
            {'detail': 'Local server failed; verify any submitted reservation.'}, status_code=500
        )

    @app.get('/')
    async def index():
        return {'message': 'Server is live!'}

    @app.post('/api/get-details')
    async def get_details(data: DetailsRequest):
        headers = upstream_headers(data.headers)
        state = booking_state or BookingState()
        claim = state.get(data.claim_id)
        if (
            not claim
            or claim['status'] != 'claimed'
            or (claim['venue'], claim['day'], claim['party'])
            != (data.restaurant_id, data.day, data.party_size)
        ):
            raise HTTPException(409, 'Matching pre-submission claim required.')
        try:
            async with httpx.AsyncClient(
                proxy=proxy_url(data.select_proxy), timeout=5, follow_redirects=False
            ) as client:
                response = await client.get(
                    'https://api.resy.com/3/details',
                    params={
                        'day': data.day,
                        'party_size': data.party_size,
                        'x-resy-auth-token': data.headers['X-Resy-Auth-Token'],
                        'venue_id': data.restaurant_id,
                        'config_id': data.config_token,
                    },
                    headers=headers,
                )
            if response.status_code != 200:
                raise HTTPException(502, 'Upstream details request failed.')
            raw = response.json()
            payment = raw.get('payment', {})
            details = {
                'payment': {
                    'amounts': payment.get('amounts', {}),
                    'config': {
                        'currency': payment.get('config', {}).get('currency'),
                    },
                },
                'cancellation': {'fee': {'amount': raw.get('cancellation', {}).get('fee', {}).get('amount')}},
                'currency': raw.get('currency'),
            }
            validate_quote(json.loads(claim['policy'] or '{}'), details)
            book_token = raw['book_token']['value']
            state.bind_quote(data.claim_id, book_token)
            return {'response_value': book_token, 'details': details}
        except (FeePolicyError, CampaignBlocked):
            raise HTTPException(409, 'Quote does not satisfy the approved policy or claim state.') from None
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(502, 'Upstream details unavailable; no booking submitted.') from None

    @app.post('/api/book-reservation')
    async def book_reservation(data: ReservationRequest):
        headers = upstream_headers(data.headers)
        proxy = proxy_url(data.select_proxy)
        state = booking_state or BookingState()
        claim = state.get(data.claim_id)
        if not claim or (claim['venue'], claim['day'], claim['party']) != (
            data.restaurant_id,
            data.day,
            data.party_size,
        ):
            raise HTTPException(409, 'A matching submission claim is required.')
        try:
            state.dispatch(data.claim_id, data.book_token)
        except CampaignBlocked:
            raise HTTPException(409, 'Submission already dispatched or claim is not ready.') from None
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=5, follow_redirects=False) as client:
                response = await client.post(
                    'https://api.resy.com/3/book',
                    data={
                        'book_token': data.book_token,
                        'struct_payment_method': json.dumps({'id': data.payment_id}),
                        'source_id': 'resy.com-venue-details',
                    },
                    headers=headers,
                )
            logger.info(
                'Reservation dispatch completed with HTTP %d; account verification required.',
                response.status_code,
            )
            if response.status_code not in (200, 201):
                raise HTTPException(502, 'Upstream booking not confirmed; hold retained.')
            return JSONResponse({'submitted': True, 'verified': False}, status_code=201)
        except httpx.HTTPError:
            raise HTTPException(502, 'Submission outcome uncertain; hold retained.') from None

    return app


app = create_app()

if __name__ == '__main__':
    import uvicorn

    uvicorn.run(app, host='127.0.0.1', port=8000, log_level='warning', access_log=False)
