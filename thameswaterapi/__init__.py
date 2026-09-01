import os
import uuid
import base64
import hashlib
import datetime
import json
import re
from typing import Callable, Optional, Literal, TypeVar
import logging
from dataclasses import dataclass, field, fields
from urllib.parse import urlparse, parse_qs, unquote

import requests


class AuthenticationError(Exception):
    """Raised when authentication with Thames Water fails."""


class TariffError(Exception):
    """Raised when the tariff page cannot be fetched or parsed."""


class RateLimitError(Exception):
    """Raised when Thames Water responds 429."""

    def __init__(self, retry_after: Optional[int] = None):
        #: Seconds to wait, when the Retry-After header gave a delay in
        #: seconds. None when the header was absent or in HTTP-date form.
        self.retry_after = retry_after
        super().__init__(
            "Rate limited by Thames Water"
            + (f"; retry after {retry_after}s" if retry_after is not None else "")
        )


class MalformedResponse(Exception):
    """Raised when a response is not what the endpoint is supposed to return.

    Covers a non-2xx status, a non-JSON body, an HTML error page, and JSON
    whose shape does not match the expected dataclass, as one class. It is
    never retried and never triggers re-authentication: the caller decides.
    """

    #: How much of the body to attach, enough to identify what came back.
    BODY_SNIPPET_LEN = 200

    def __init__(self, response: requests.Response, reason: str):
        self.status_code = response.status_code
        self.content_type = response.headers.get("content-type", "")
        self.body = response.text[: self.BODY_SNIPPET_LEN]
        super().__init__(
            f"{reason} (HTTP {self.status_code}, content-type "
            f"{self.content_type!r}, body starts {self.body!r})"
        )


#: requests has no default timeout of its own, so every call sets one.
DEFAULT_TIMEOUT = 30.0

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)


B2C_USER_FLOW_URL = (
    "https://login.thameswater.co.uk/identity.thameswater.co.uk/"
    "b2c_1_tw_website_signin/oauth2/v2.0"
)
AUTHORIZATION_ENDPOINT = f"{B2C_USER_FLOW_URL}/authorize"
TOKEN_ENDPOINT = f"{B2C_USER_FLOW_URL}/token"
END_SESSION_ENDPOINT = f"{B2C_USER_FLOW_URL}/logout"


# Public help page carrying the current metered-household Scheme of Charges.
# The figures are region-wide (identical for every customer) and need no auth.
TARIFF_URL = (
    "https://www.thameswater.co.uk/help/account-and-billing/"
    "understand-your-bill/metered-customers"
)


@dataclass
class Line:
    Label: str
    Usage: float
    Read: float
    IsEstimated: bool
    MeterSerialNumberHis: str


@dataclass
class DateRangeKey:
    Key: str
    Value: str


@dataclass
class MeterUsage:
    IsError: bool
    IsDataAvailable: bool
    IsConsumptionAvailable: bool
    TargetUsage: float
    AverageUsage: float
    ActualUsage: float
    MyUsage: Optional[str]  # so far have only seen 'NA', 'High', or None
    AverageUsagePerPerson: float
    IsMO365Customer: bool
    IsMOPartialCustomer: bool
    IsMOCompleteCustomer: bool
    IsExtraMonthConsumptionMessage: bool
    Lines: list[Line] = field(default_factory=list)
    AlertsValues: Optional[dict] = field(default_factory=dict)


@dataclass
class MetersResponse(MeterUsage):
    """Response from getMeters, which includes date range options and meter list
    in addition to the standard MeterUsage fields."""

    Meters: list[str] = field(default_factory=list)
    Yearly: list[DateRangeKey] = field(default_factory=list)
    HalfYearly: list[DateRangeKey] = field(default_factory=list)
    Monthly: list[DateRangeKey] = field(default_factory=list)
    Daily: list[DateRangeKey] = field(default_factory=list)
    IsRecentCustomer: bool = False
    IsPremiseAddressSameAsMailingAddress: bool = True


@dataclass
class Address:
    addressLine1: Optional[str]
    addressLine2: Optional[str]
    town: Optional[str]
    administrativeArea: Optional[str]
    country: Optional[str]
    postcode: Optional[str]
    fullAddress: Optional[str]


@dataclass
class PrimaryAccountHolder:
    businessPartnerId: Optional[str]
    dateOfBirth: Optional[str]
    firstName: Optional[str]
    secondName: Optional[str]
    lastName: Optional[str]
    fullName: Optional[str]


@dataclass
class Property:
    propertyId: Optional[str]
    address: Optional[Address]
    meterType: Optional[int]


@dataclass
class ContactDetails:
    primaryLandlineNumber: Optional[str]
    primaryMobileNumber: Optional[str]
    primaryEmail: Optional[str]
    isPrimaryLandlineNumberValid: Optional[bool]
    isPrimaryMobileNumberValid: Optional[bool]


@dataclass
class Correspondence:
    address: Optional[Address]


@dataclass
class Account:
    """Account details from the account-management-api /Accounts endpoint."""

    contractAccountNumber: str
    billingPreference: Optional[int] = None
    moveInDate: Optional[str] = None
    paymentDueAmount: float = 0.0
    currentBalance: float = 0.0
    moveOutDate: Optional[str] = None
    primaryAccountHolder: Optional[PrimaryAccountHolder] = None
    property: Optional[Property] = None
    isProgressiveMeterProgram: Optional[bool] = None
    status: Optional[int] = None
    isMetered: Optional[bool] = None
    isFutureMoveIn: Optional[bool] = None
    isActiveAccount: Optional[bool] = None
    isInCredit: Optional[bool] = None
    dunningLock: Optional[bool] = None
    contactDetails: Optional[ContactDetails] = None
    isStandard: Optional[bool] = None
    isCollective: Optional[bool] = None
    correspondence: Optional[Correspondence] = None
    isMovedOutStillActive: Optional[bool] = None


@dataclass
class TokenResponse:
    """A B2C token endpoint response, limited to the fields anything reads."""

    id_token: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expires_in: Optional[int] = None


@dataclass
class Tariff:
    """Metered-household tariff for the Thames Water region.

    Thames Water has no tariff API; metered charges are a fixed annual
    "Scheme of Charges" published per region, so the same figures apply to
    every customer. They are scraped from the public help page (see
    :func:`get_tariff`).
    """

    clean_water_rate_per_m3: float
    wastewater_rate_per_m3: float
    water_fixed_per_year: float
    wastewater_fixed_per_year: float

    @property
    def volumetric_rate_per_m3(self) -> float:
        """Combined clean water + wastewater volumetric rate (GBP/m3)."""
        return round(self.clean_water_rate_per_m3 + self.wastewater_rate_per_m3, 4)

    @property
    def unit_rate_per_litre(self) -> float:
        """Combined volumetric rate expressed per litre (GBP/L)."""
        return (self.clean_water_rate_per_m3 + self.wastewater_rate_per_m3) / 1000

    @property
    def standing_charge_per_day(self) -> float:
        """Combined fixed/standing charge expressed per day (GBP/day)."""
        return round(
            (self.water_fixed_per_year + self.wastewater_fixed_per_year) / 365, 4
        )


@dataclass
class Measurement:
    start: datetime.date
    usage: int  # Usage
    total: int  # Read


@dataclass
class HourlyMeasurement:
    hour_start: datetime.datetime
    usage: int  # Usage
    total: int  # Read


_logger = logging.getLogger(__name__)

T = TypeVar("T")

# Audience (resource app id) for the account-management-api. The app id is
# specific to Thames Water and is used to scope access tokens for the
# account-management-api host.
ACCOUNT_MANAGEMENT_API_RESOURCE_ID = "8a63d7f3-8ff8-4be6-b4cd-c5957e68a9bb"


def _parse_retry_after(header: Optional[str]) -> Optional[int]:
    """Return the Retry-After delay in seconds, or None if not in that form."""
    if header is None:
        return None
    try:
        return int(header)
    except ValueError:
        # The HTTP-date form is legal but has never been observed here.
        return None


def _filter_known_fields(cls: type, data: dict) -> dict:
    """Filter a dict to only known dataclass fields, warning about unknown ones."""
    known = {f.name for f in fields(cls)}
    unknown = data.keys() - known
    if unknown:
        _logger.warning(
            "Unknown fields in %s response: %s",
            cls.__name__,
            ", ".join(sorted(unknown)),
        )
    return {k: v for k, v in data.items() if k in known}


def parse_meter_usage(data: dict) -> MeterUsage:
    """Parse a raw JSON dict from the meter usage API into a MeterUsage object."""
    data = dict(data)
    data["Lines"] = [Line(**line) for line in data["Lines"] or []]
    return MeterUsage(**_filter_known_fields(MeterUsage, data))


def parse_token_response(data: dict) -> TokenResponse:
    """Parse a token endpoint response.

    The endpoint returns a dozen MSAL telemetry and client_info fields that
    nothing here reads, so the wanted ones are picked out rather than filtered.
    """
    if "error" in data:
        raise ValueError(
            f"token endpoint returned {data['error']}: "
            f"{str(data.get('error_description', ''))[:200]}"
        )
    return TokenResponse(
        id_token=data.get("id_token"),
        access_token=data.get("access_token"),
        refresh_token=data.get("refresh_token"),
        expires_in=data.get("expires_in"),
    )


def _parse_self_asserted_response(data: dict) -> None:
    """Raise :class:`AuthenticationError` if the credentials were rejected.

    B2C answers a rejected credential with HTTP 200 and a body whose own
    status field carries the failure, so nothing in the transport layer sees
    it and it would otherwise surface several requests later as a missing
    'code' in the redirect fragment.
    """
    status = str(data.get("status", ""))
    if status != "200":
        raise AuthenticationError(
            data.get("message") or f"the sign-in step returned status {status!r}"
        )


def _parse_id_token_response(data: dict) -> TokenResponse:
    """Parse a token response that is only useful if it carries an id token."""
    tokens = parse_token_response(data)
    if tokens.id_token is None:
        raise ValueError("no id_token in the token response")
    return tokens


def _parse_access_token_response(data: dict) -> TokenResponse:
    """Parse a token response that is only useful if it carries an access token."""
    tokens = parse_token_response(data)
    if tokens.access_token is None:
        raise ValueError("no access_token in the token response")
    return tokens


def _parse_address(data: Optional[dict]) -> Optional[Address]:
    if data is None:
        return None
    return Address(**_filter_known_fields(Address, data))


def parse_account(data: dict) -> Account:
    """Parse a raw JSON dict from the account-management-api /Accounts endpoint."""
    data = dict(data)

    if (holder := data.get("primaryAccountHolder")) is not None:
        data["primaryAccountHolder"] = PrimaryAccountHolder(
            **_filter_known_fields(PrimaryAccountHolder, holder)
        )

    if (prop := data.get("property")) is not None:
        prop = dict(prop)
        prop["address"] = _parse_address(prop.get("address"))
        data["property"] = Property(**_filter_known_fields(Property, prop))

    if (contact := data.get("contactDetails")) is not None:
        data["contactDetails"] = ContactDetails(
            **_filter_known_fields(ContactDetails, contact)
        )

    if (corr := data.get("correspondence")) is not None:
        corr = dict(corr)
        corr["address"] = _parse_address(corr.get("address"))
        data["correspondence"] = Correspondence(
            **_filter_known_fields(Correspondence, corr)
        )

    return Account(**_filter_known_fields(Account, data))


def _search_tariff_float(pattern: str, text: str, description: str) -> float:
    """Return the first captured group of ``pattern`` in ``text`` as a float."""
    match = re.search(pattern, text)
    if match is None:
        raise TariffError(
            f"Could not find {description} on the Thames Water tariff page "
            "(the page markup may have changed)"
        )
    return float(match.group(1))


def parse_tariff(html: str) -> Tariff:
    """Parse the metered-customers help page HTML into a :class:`Tariff`.

    The figures live inside markup (``<strong>`` tags and a table); stripping
    tags and collapsing whitespace leaves each value adjacent to its label,
    which the regexes below anchor on.
    """
    text = re.sub(r"<[^>]+>", " ", html).replace('\\"', '"')
    text = re.sub(r"\s+", " ", text)

    return Tariff(
        clean_water_rate_per_m3=_search_tariff_float(
            r"£([0-9]+\.[0-9]+) per m3 for clean water",
            text,
            "the clean water volumetric rate",
        ),
        wastewater_rate_per_m3=_search_tariff_float(
            r"£([0-9]+\.[0-9]+) per m3 for wastewater",
            text,
            "the wastewater volumetric rate",
        ),
        water_fixed_per_year=_search_tariff_float(
            r"Water £([0-9]+\.[0-9]+) Not applicable",
            text,
            "the water fixed charge",
        ),
        # The wastewater row lists the standard fixed charge first and the
        # (lower) surface-water-drainage rebate charge second; take the standard.
        wastewater_fixed_per_year=_search_tariff_float(
            r"Wastewater £([0-9]+\.[0-9]+) £",
            text,
            "the wastewater fixed charge",
        ),
    )


def get_tariff(
    session: Optional[requests.Session] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Tariff:
    """Fetch and parse the current metered-household tariff.

    Needs no authentication (the figures are region-wide), so it can be called
    without a :class:`ThamesWater` instance. A ``requests.Session`` may be
    passed to reuse an existing connection.
    """
    getter = session.get if session is not None else requests.get
    try:
        r = getter(
            TARIFF_URL,
            headers={"user-agent": USER_AGENT},
            timeout=timeout,
        )
        r.raise_for_status()
    except requests.RequestException as err:
        raise TariffError(
            f"Failed to fetch the Thames Water tariff page: {err}"
        ) from err
    return parse_tariff(r.text)


def parse_meters_response(data: dict) -> MetersResponse:
    """Parse a raw JSON dict from the getMeters API into a MetersResponse object."""
    data = dict(data)
    data["Lines"] = [Line(**line) for line in data["Lines"] or []]
    data["Yearly"] = [DateRangeKey(**k) for k in data.get("Yearly") or []]
    data["HalfYearly"] = [DateRangeKey(**k) for k in data.get("HalfYearly") or []]
    data["Monthly"] = [DateRangeKey(**k) for k in data.get("Monthly") or []]
    data["Daily"] = [DateRangeKey(**k) for k in data.get("Daily") or []]
    return MetersResponse(**_filter_known_fields(MetersResponse, data))


def _decode_jwt_payload(token: str) -> dict:
    """Decode the payload of a JWT without verifying the signature.

    The signature is not checked against the user flow's jwks_uri because
    the token is fetched over TLS directly from the issuer being
    authenticated to, and the only claims read are the caller's own account
    numbers.
    """
    payload = token.split(".")[1]
    # Add padding for base64
    payload += "=" * (4 - len(payload) % 4)
    return json.loads(base64.b64decode(payload))


class ThamesWater:
    def __init__(
        self,
        email: str,
        password: str,
        account_number: Optional[int] = None,
        client_id: str = "cedfde2d-79a7-44fd-9833-cae769640d3d",  # specific to Thames Water
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.s = requests.session()
        # Every request wants it, so the session carries it rather than each
        # call site or the helper merging it in.
        self.s.headers["user-agent"] = USER_AGENT
        self.client_id = client_id
        self.timeout = timeout
        self._refresh_token: Optional[str] = None

        self._authenticate(email, password)

        if account_number is None:
            account_number = int(
                self._id_token_claims["extension_DefaultContractAccountNumber"]
            )
        self.account_number = account_number

        self._visit_meter_page()

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Issue a request with the client timeout and classify the outcome.

        3xx is allowed through: the authentication chain reads codes and
        tokens out of Location headers with ``allow_redirects=False``.
        """
        r = self.s.request(method, url, timeout=self.timeout, **kwargs)

        if r.status_code == 429:
            raise RateLimitError(_parse_retry_after(r.headers.get("Retry-After")))
        if r.status_code >= 400:
            raise MalformedResponse(r, "unexpected HTTP status")
        return r

    def _request_json(
        self,
        method: str,
        url: str,
        parse: Callable[[dict], T],
        **kwargs,
    ) -> T:
        """Issue a request and parse the body into its expected dataclass."""
        r = self._request(method, url, **kwargs)
        try:
            payload = r.json()
        except ValueError as err:
            raise MalformedResponse(r, "response body is not JSON") from err
        try:
            return parse(payload)
        except (AttributeError, KeyError, TypeError, ValueError) as err:
            raise MalformedResponse(r, f"unexpected response body: {err}") from err

    @property
    def refresh_token(self) -> Optional[str]:
        """The refresh token currently held, for a caller that persists it.

        The grant rotates it on every use, so read it back after every
        authentication and store whatever it now is.
        """
        return self._refresh_token

    def _store_tokens(self, tokens: TokenResponse) -> None:
        """Keep the rotated refresh token; the previous one is spent."""
        if tokens.refresh_token is not None:
            self._refresh_token = tokens.refresh_token

    def logout(self) -> None:
        """End the B2C session.

        The response is a redirect to the post-logout page, which nothing
        reads; only the server-side session teardown matters.
        """
        self._request("GET", END_SESSION_ENDPOINT, allow_redirects=False)

    def _generate_pkce(self):
        self.pkce_verifier = (
            base64.urlsafe_b64encode(os.urandom(32)).decode("utf-8").rstrip("=")
        )
        self.pkce_challenge = (
            base64.urlsafe_b64encode(
                hashlib.sha256(self.pkce_verifier.encode()).digest()
            )
            .decode("utf-8")
            .rstrip("=")
        )

    def _authorize_b2c_1_tw_website_signin(self) -> tuple[str, str]:
        url = AUTHORIZATION_ENDPOINT

        params = {
            "client_id": self.client_id,
            "scope": "openid profile offline_access",
            "response_type": "code",
            "redirect_uri": "https://www.thameswater.co.uk/login",
            "response_mode": "fragment",
            "code_challenge": self.pkce_challenge,
            "code_challenge_method": "S256",
            "nonce": str(uuid.uuid4()),
            "state": str(uuid.uuid4()),
        }

        r = self._request("GET", url, params=params)

        cookies = dict(self.s.cookies)
        try:
            return cookies["x-ms-cpim-trans"], cookies["x-ms-cpim-csrf"]
        except KeyError as err:
            raise MalformedResponse(
                r, f"the authorize response set no {err} cookie"
            ) from err

    def _self_asserted_b2c_1_tw_website_signin(
        self, email: str, password: str, trans_token: str, csrf_token: str
    ):
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/B2C_1_tw_website_signin/SelfAsserted"

        params = {
            "tx": f"StateProperties={trans_token}",
            "p": "B2C_1_tw_website_signin",
        }

        data = {"request_type": "RESPONSE", "email": email, "password": password}

        self._request_json(
            "POST",
            url,
            _parse_self_asserted_response,
            params=params,
            data=data,
            headers={"x-csrf-token": csrf_token},
        )

    def _confirmed_b2c_1_tw_website_signin(self, trans_token: str, csrf_token: str):
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/B2C_1_tw_website_signin/api/CombinedSigninAndSignup/confirmed"

        params = {
            "rememberMe": "false",
            "tx": f"StateProperties={trans_token}",
            "csrf_token": csrf_token,
            "p": "B2C_1_tw_website_signin",
        }

        # /confirmed emits a single hop carrying the code, and the reply URL
        # it points at is a page whose body nothing reads, so do not follow it.
        r = self._request("GET", url, params=params, allow_redirects=False)

        location = r.headers.get("Location", "")
        fragment_params = parse_qs(urlparse(location).fragment)
        if "code" not in fragment_params:
            raise MalformedResponse(
                r, f"no 'code' in the redirect fragment; Location was {location!r}"
            )
        return fragment_params["code"][0]

    def _get_oauth2_code_b2c_1_tw_website_signin(
        self, confirmation_code: str
    ) -> TokenResponse:
        url = TOKEN_ENDPOINT

        headers = {"content-type": "application/x-www-form-urlencoded;charset=utf-8"}

        data = {
            "client_id": self.client_id,
            "redirect_uri": "https://www.thameswater.co.uk/login",
            "scope": "openid offline_access profile",
            "grant_type": "authorization_code",
            "client_info": "1",
            "x-client-SKU": "msal.js.browser",
            "x-client-VER": "3.1.0",
            "x-ms-lib-capability": "retry-after, h429",
            "x-client-current-telemetry": "5|865,0,,,|,",
            "x-client-last-telemetry": "5|0|||0,0",
            "code_verifier": self.pkce_verifier,
            "code": confirmation_code,
        }

        tokens = self._request_json(
            "POST", url, _parse_id_token_response, headers=headers, data=data
        )
        self._store_tokens(tokens)
        return tokens

    def _refresh_token_grant(
        self,
        scope: str = "openid profile offline_access",
        parse: Callable[[dict], TokenResponse] = parse_token_response,
    ) -> TokenResponse:
        """Exchange the held refresh token for fresh tokens.

        The grant rotates the refresh token, so the new one is stored the
        moment it arrives: the previous one is spent and losing the new one
        costs a password login.
        """
        if self._refresh_token is None:
            raise ValueError("no refresh token held")

        data = {
            "client_id": self.client_id,
            "scope": scope,
            "grant_type": "refresh_token",
            "client_info": "1",
            "x-client-SKU": "msal.js.browser",
            "x-client-VER": "3.1.0",
            "x-ms-lib-capability": "retry-after, h429",
            "x-client-current-telemetry": "5|61,0,,,|@azure/msal-react,2.0.3",
            "x-client-last-telemetry": "5|0|||0,0",
            "refresh_token": self._refresh_token,
        }

        headers = {"content-type": "application/x-www-form-urlencoded;charset=utf-8"}

        tokens = self._request_json(
            "POST", TOKEN_ENDPOINT, parse, headers=headers, data=data
        )
        self._store_tokens(tokens)
        return tokens

    def _login(self, state: str, id_token: str):
        url = "https://myaccount.thameswater.co.uk/login"

        data = {
            "state": state,
            "id_token": id_token,
        }

        headers = {"content-type": "application/x-www-form-urlencoded"}

        self._request("POST", url, data=data, headers=headers)

    def _authenticate(
        self,
        email: str,
        password: str,
    ):
        self._generate_pkce()
        trans_token, csrf_token = self._authorize_b2c_1_tw_website_signin()
        self._self_asserted_b2c_1_tw_website_signin(
            email, password, trans_token, csrf_token
        )
        confirmation_code = self._confirmed_b2c_1_tw_website_signin(
            trans_token, csrf_token
        )
        tokens = self._get_oauth2_code_b2c_1_tw_website_signin(confirmation_code)

        id_token = tokens.id_token
        assert id_token is not None  # _parse_id_token_response guarantees it
        self._id_token_claims = _decode_jwt_payload(id_token)

        # First POST to /login with the id_token to establish a session on
        # myaccount.thameswater.co.uk. The server redirects through
        # /twservice/Account/SignIn and then to a second B2C authorize page
        # that carries a new state value and contains a fresh id_token in the
        # page body.
        r = self._request(
            "POST",
            "https://myaccount.thameswater.co.uk/login",
            data={"id_token": id_token, "state": ""},
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

        parsed = urlparse(r.url)
        query_params = parse_qs(parsed.query)
        if "state" not in query_params:
            raise AuthenticationError(
                f"Authentication failed: 'state' not found in redirect URL after first login POST. "
                f"URL was: {r.url!r}"
            )
        state = unquote(query_params["state"][0])
        if "id='id_token' value='" not in r.text:
            raise AuthenticationError(
                "Authentication failed: 'id_token' not found in page after first login POST."
            )
        new_id_token = r.text.split("id='id_token' value='")[1].split("'/>")[0]

        # Second POST to /login with the state and id_token from the redirect page
        # to complete the session establishment.
        self._login(state, new_id_token)
        self.s.cookies.set(name="b2cAuthenticated", value="true")

    def _visit_meter_page(self) -> None:
        """Visit the meters usage page to establish server-side session context.

        This is required for the AJAX endpoints to return data rather than a 500 page.
        """
        self._request(
            "GET",
            "https://myaccount.thameswater.co.uk/mydashboard/my-meters-usage",
            params={"contractAccountNumber": self.account_number},
        )

    def get_account_numbers(self) -> list[int]:
        """Return the list of contract account numbers available for this login."""
        raw = self._id_token_claims.get("extension_AvailableContractAccounts", "")
        if not raw:
            return []
        return [int(n) for n in raw.split(",")]

    def get_meter_numbers(self) -> list[str]:
        """Return the list of meter serial numbers on the account."""
        return self.get_meters().Meters

    def get_meters(self) -> MetersResponse:
        """Return meter list and current usage data.

        This is the primary endpoint for daily consumption data.
        The Referer header with contractAccountNumber is required by the server.
        """
        url = "https://myaccount.thameswater.co.uk/ajax/waterMeter/getMeters"

        headers = {
            "Referer": f"https://myaccount.thameswater.co.uk/mydashboard/my-meters-usage?contractAccountNumber={self.account_number}",
            "X-Requested-With": "XMLHttpRequest",
        }

        return self._request_json("GET", url, parse_meters_response, headers=headers)

    def get_meter_usage(
        self,
        meter: int | str,
        start: datetime.datetime,
        end: datetime.datetime,
        granularity: Literal["H", "D", "M"] = "H",
    ) -> MeterUsage:
        url = "https://myaccount.thameswater.co.uk/ajax/waterMeter/getSmartWaterMeterConsumptions"

        params = {
            "meter": meter,
            "startDate": start.day,
            "startMonth": start.month,
            "startYear": start.year,
            "endDate": end.day,
            "endMonth": end.month,
            "endYear": end.year,
            "granularity": granularity,
            "premiseId": "",
            "isForC4C": "false",
        }

        headers = {
            "Referer": "https://myaccount.thameswater.co.uk/mydashboard/my-meters-usage",
            "X-Requested-With": "XMLHttpRequest",
        }

        return self._request_json(
            "GET", url, parse_meter_usage, params=params, headers=headers
        )

    def _acquire_account_management_api_access_token(self) -> str:
        """Exchange the refresh token for an access token scoped to the
        account-management-api resource."""
        scope = (
            f"https://identity.thameswater.co.uk/{ACCOUNT_MANAGEMENT_API_RESOURCE_ID}"
            "/default openid profile offline_access"
        )

        tokens = self._refresh_token_grant(scope, _parse_access_token_response)
        assert tokens.access_token is not None  # the parser guarantees it
        return tokens.access_token

    def get_account(self) -> Account:
        """Return account details for the current contract account number.

        Includes the outstanding balance (paymentDueAmount) and current
        balance, as well as account holder, property, and contact details.
        """
        access_token = self._acquire_account_management_api_access_token()

        url = "https://account-management-api.prod.p.webapp.thameswater.co.uk/account-management-api/Accounts"

        headers = {
            "Accept": "text/plain",
            "Authorization": f"Bearer {access_token}",
            "content-type": "application/json",
            "x-contract-account-number": str(self.account_number),
            "Origin": "https://www.thameswater.co.uk",
            "Referer": "https://www.thameswater.co.uk/",
        }

        return self._request_json("GET", url, parse_account, headers=headers)

    def get_tariff(self) -> Tariff:
        """Return the current metered-household tariff for the region.

        The figures are region-wide and need no authentication; this reuses the
        client's session for convenience. See the module-level
        :func:`get_tariff` for a credential-free alternative.
        """
        return get_tariff(self.s, timeout=self.timeout)


def _parse_line_label_as_date(label: str, today: datetime.date) -> datetime.date:
    """Parse a line label like '16-January' or '1-February' into a date.

    The year is inferred from today's date, with rollover handling so that
    e.g. a December label in a response fetched in January uses the prior year.
    """
    # Append the current year to avoid the Python 3.15 deprecation for yearless strptime.
    dt = datetime.datetime.strptime(f"{label}-{today.year}", "%d-%B-%Y")
    # If the label month is later than June and we're in the first half of the year,
    # the data belongs to the previous year.
    if dt.month > 6 and today.month <= 6:
        dt = dt.replace(year=today.year - 1)
    return dt.date()


def lines_to_timeseries(lines: list[Line]) -> list[Measurement]:
    """Convert meter usage lines to a time series of Measurement objects.

    The date of each measurement is parsed from the line's Label field
    (e.g. '16-January', '1-February').
    """
    today = datetime.date.today()
    return [
        Measurement(
            start=_parse_line_label_as_date(line.Label, today),
            usage=int(line.Usage),
            total=int(line.Read),
        )
        for line in lines
    ]


def _date_range(
    start: datetime.date,
    end: datetime.date,
    freq: datetime.timedelta = datetime.timedelta(hours=1),
    tz: str = "Europe/London",
) -> list[datetime.datetime]:
    import zoneinfo

    if isinstance(start, datetime.date) and not isinstance(start, datetime.datetime):
        start = datetime.datetime(start.year, start.month, start.day)
    if isinstance(end, datetime.date) and not isinstance(end, datetime.datetime):
        end = datetime.datetime(end.year, end.month, end.day)
    if start.tzinfo is not None or end.tzinfo is not None:
        raise ValueError(
            "Input datetimes must be timezone-naive. Convert them to naive before calling this function."
        )

    tzinfo = zoneinfo.ZoneInfo(tz)
    start = start.replace(tzinfo=tzinfo)
    end = end.replace(tzinfo=tzinfo)

    result = []
    current = start
    while current <= end:
        result.append(current)
        current += freq

    return result


def meter_usage_lines_to_timeseries(
    start: datetime.date,
    lines: list[Line],
) -> list[HourlyMeasurement]:
    """Convert hourly meter usage lines to a time series of HourlyMeasurement objects.

    Assumptions:
    * Lines is hourly
    * Lines is contiguous (no gaps)
    """
    if isinstance(start, datetime.date) and not isinstance(start, datetime.datetime):
        start = datetime.datetime(start.year, start.month, start.day)
    timestamps = _date_range(start, start + datetime.timedelta(hours=len(lines)))
    return [
        HourlyMeasurement(
            hour_start=timestamps[i],
            usage=int(line.Usage),
            total=int(line.Read),
        )
        for i, line in enumerate(lines)
    ]
