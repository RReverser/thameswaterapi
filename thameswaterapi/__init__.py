from __future__ import annotations

import base64
import datetime
import hashlib
import json
import logging
import os
import re
import uuid
import zoneinfo
from dataclasses import dataclass, field, fields
from typing import Literal
from urllib.parse import parse_qs, unquote, urlparse

import requests


class AuthenticationError(Exception):
    """Raised when authentication with Thames Water fails."""


class TariffError(Exception):
    """Raised when the tariff page cannot be fetched or parsed."""


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
    MyUsage: str | None  # so far have only seen 'NA', 'High', or None
    AverageUsagePerPerson: float
    IsMO365Customer: bool
    IsMOPartialCustomer: bool
    IsMOCompleteCustomer: bool
    IsExtraMonthConsumptionMessage: bool
    Lines: list[Line] = field(default_factory=list)
    AlertsValues: dict | None = field(default_factory=dict)


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
    addressLine1: str | None
    addressLine2: str | None
    town: str | None
    administrativeArea: str | None
    country: str | None
    postcode: str | None
    fullAddress: str | None


@dataclass
class PrimaryAccountHolder:
    businessPartnerId: str | None
    dateOfBirth: str | None
    firstName: str | None
    secondName: str | None
    lastName: str | None
    fullName: str | None


@dataclass
class Property:
    propertyId: str | None
    address: Address | None
    meterType: int | None


@dataclass
class ContactDetails:
    primaryLandlineNumber: str | None
    primaryMobileNumber: str | None
    primaryEmail: str | None
    isPrimaryLandlineNumberValid: bool | None
    isPrimaryMobileNumberValid: bool | None


@dataclass
class Correspondence:
    address: Address | None


@dataclass
class Account:
    """Account details from the account-management-api /Accounts endpoint."""

    contractAccountNumber: str
    billingPreference: int | None = None
    moveInDate: str | None = None
    paymentDueAmount: float = 0.0
    currentBalance: float = 0.0
    moveOutDate: str | None = None
    primaryAccountHolder: PrimaryAccountHolder | None = None
    property: Property | None = None
    isProgressiveMeterProgram: bool | None = None
    status: int | None = None
    isMetered: bool | None = None
    isFutureMoveIn: bool | None = None
    isActiveAccount: bool | None = None
    isInCredit: bool | None = None
    dunningLock: bool | None = None
    contactDetails: ContactDetails | None = None
    isStandard: bool | None = None
    isCollective: bool | None = None
    correspondence: Correspondence | None = None
    isMovedOutStillActive: bool | None = None


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


#: Meter readings are labelled in local clock time.
LONDON = zoneinfo.ZoneInfo("Europe/London")

_logger = logging.getLogger(__name__)

# Audience (resource app id) for the account-management-api. The app id is
# specific to Thames Water and is used to scope access tokens for the
# account-management-api host.
ACCOUNT_MANAGEMENT_API_RESOURCE_ID = "8a63d7f3-8ff8-4be6-b4cd-c5957e68a9bb"


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


def _parse_address(data: dict | None) -> Address | None:
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


def get_tariff(session: requests.Session | None = None) -> Tariff:
    """Fetch and parse the current metered-household tariff.

    Needs no authentication (the figures are region-wide), so it can be called
    without a :class:`ThamesWater` instance. A ``requests.Session`` may be
    passed to reuse an existing connection.
    """
    getter = session.get if session is not None else requests.get
    try:
        r = getter(
            TARIFF_URL,
            headers={
                "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
            },
            timeout=30,
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
    """Decode the payload of a JWT without verifying the signature."""
    payload = token.split(".")[1]
    # JWT payloads are base64url and carry no padding; b64decode wants it back.
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


class ThamesWater:
    def __init__(
        self,
        email: str,
        password: str,
        account_number: int | None = None,
        client_id: str = "cedfde2d-79a7-44fd-9833-cae769640d3d",  # specific to Thames Water
    ):
        self.s = requests.session()
        self.client_id = client_id

        self._authenticate(email, password)

        if account_number is None:
            account_number = int(
                self._id_token_claims["extension_DefaultContractAccountNumber"]
            )
        self.account_number = account_number

        self._visit_meter_page()

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
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/b2c_1_tw_website_signin/oauth2/v2.0/authorize"

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

        r = self.s.get(url, params=params)
        r.raise_for_status()
        return dict(self.s.cookies)["x-ms-cpim-trans"], dict(self.s.cookies)[
            "x-ms-cpim-csrf"
        ]

    def _self_asserted_b2c_1_tw_website_signin(
        self, email: str, password: str, trans_token: str, csrf_token: str
    ):
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/B2C_1_tw_website_signin/SelfAsserted"

        params = {
            "tx": f"StateProperties={trans_token}",
            "p": "B2C_1_tw_website_signin",
        }

        data = {"request_type": "RESPONSE", "email": email, "password": password}

        headers = {
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "x-csrf-token": csrf_token,
        }

        r = self.s.post(url, params=params, data=data, headers=headers)
        r.raise_for_status()

    def _confirmed_b2c_1_tw_website_signin(self, trans_token: str, csrf_token: str):
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/B2C_1_tw_website_signin/api/CombinedSigninAndSignup/confirmed"

        headers = {
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
        }

        params = {
            "rememberMe": "false",
            "tx": f"StateProperties={trans_token}",
            "csrf_token": csrf_token,
            "p": "B2C_1_tw_website_signin",
        }

        r = self.s.get(url, headers=headers, params=params)
        r.raise_for_status()

        parsed = urlparse(r.url)
        fragment_params = parse_qs(parsed.fragment)
        if "code" not in fragment_params:
            raise AuthenticationError(
                f"Authentication failed: 'code' not found in redirect URL fragment. "
                f"URL was: {r.url!r}"
            )
        return fragment_params["code"][0]

    def _get_oauth2_code_b2c_1_tw_website_signin(self, confirmation_code: str):
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/b2c_1_tw_website_signin/oauth2/v2.0/token"

        headers = {
            "content-type": "application/x-www-form-urlencoded;charset=utf-8",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        }

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

        r = self.s.post(url, headers=headers, data=data)
        r.raise_for_status()
        self.oauth_request_tokens = r.json()

    def _refresh_oauth2_token_b2c_1_tw_website_signin(self):
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/b2c_1_tw_website_signin/oauth2/v2.0/token"

        data = {
            "client_id": self.client_id,
            "scope": "openid profile offline_access",
            "grant_type": "refresh_token",
            "client_info": "1",
            "x-client-SKU": "msal.js.browser",
            "x-client-VER": "3.1.0",
            "x-ms-lib-capability": "retry-after, h429",
            "x-client-current-telemetry": "5|61,0,,,|@azure/msal-react,2.0.3",
            "x-client-last-telemetry": "5|0|||0,0",
            "refresh_token": self.oauth_request_tokens["refresh_token"],
        }

        headers = {"content-type": "application/x-www-form-urlencoded;charset=utf-8"}

        r = self.s.get(url, headers=headers, data=data)
        r.raise_for_status()
        self.oauth_response_tokens = r.json()

    def _login(self, state: str, id_token: str):
        url = "https://myaccount.thameswater.co.uk/login"

        data = {
            "state": state,
            "id_token": id_token,
        }

        headers = {
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "content-type": "application/x-www-form-urlencoded",
        }

        r = self.s.post(url, data=data, headers=headers)
        r.raise_for_status()

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
        self._get_oauth2_code_b2c_1_tw_website_signin(confirmation_code)
        self._refresh_oauth2_token_b2c_1_tw_website_signin()

        id_token = self.oauth_request_tokens["id_token"]
        self._id_token_claims = _decode_jwt_payload(id_token)

        # First POST to /login with the id_token to establish a session on
        # myaccount.thameswater.co.uk. The server redirects through
        # /twservice/Account/SignIn and then to a second B2C authorize page
        # that carries a new state value and contains a fresh id_token in the
        # page body.
        r = self.s.post(
            "https://myaccount.thameswater.co.uk/login",
            data={"id_token": id_token, "state": ""},
            headers={
                "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
                "content-type": "application/x-www-form-urlencoded",
            },
        )
        r.raise_for_status()

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
        r = self.s.get(
            f"https://myaccount.thameswater.co.uk/mydashboard/my-meters-usage?contractAccountNumber={self.account_number}",
            headers={
                "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            },
        )
        r.raise_for_status()

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
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "Referer": f"https://myaccount.thameswater.co.uk/mydashboard/my-meters-usage?contractAccountNumber={self.account_number}",
            "X-Requested-With": "XMLHttpRequest",
        }

        r = self.s.get(url, headers=headers)
        r.raise_for_status()

        return parse_meters_response(r.json())

    def get_meter_usage(
        self,
        meter: int | str,
        start: datetime.date,
        end: datetime.date,
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
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "Referer": "https://myaccount.thameswater.co.uk/mydashboard/my-meters-usage",
            "X-Requested-With": "XMLHttpRequest",
        }

        r = self.s.get(url, params=params, headers=headers)
        r.raise_for_status()

        return parse_meter_usage(r.json())

    def _acquire_account_management_api_access_token(self) -> str:
        """Exchange the refresh token for an access token scoped to the
        account-management-api resource."""
        url = "https://login.thameswater.co.uk/identity.thameswater.co.uk/b2c_1_tw_website_signin/oauth2/v2.0/token"

        scope = (
            f"https://identity.thameswater.co.uk/{ACCOUNT_MANAGEMENT_API_RESOURCE_ID}"
            "/default openid profile offline_access"
        )

        data = {
            "client_id": self.client_id,
            "scope": scope,
            "grant_type": "refresh_token",
            "client_info": "1",
            "x-client-SKU": "msal.js.browser",
            "x-client-VER": "3.1.0",
            "refresh_token": self.oauth_request_tokens["refresh_token"],
        }

        headers = {"content-type": "application/x-www-form-urlencoded;charset=utf-8"}

        r = self.s.post(url, headers=headers, data=data)
        r.raise_for_status()
        body = r.json()
        if "access_token" not in body:
            raise AuthenticationError(
                "No access_token in response from account-management-api token "
                f"exchange. Keys: {sorted(body.keys())}"
            )
        return body["access_token"]

    def get_account(self) -> Account:
        """Return account details for the current contract account number.

        Includes the outstanding balance (paymentDueAmount) and current
        balance, as well as account holder, property, and contact details.
        """
        access_token = self._acquire_account_management_api_access_token()

        url = "https://account-management-api.prod.p.webapp.thameswater.co.uk/account-management-api/Accounts"

        headers = {
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "Accept": "text/plain",
            "Authorization": f"Bearer {access_token}",
            "content-type": "application/json",
            "x-contract-account-number": str(self.account_number),
            "Origin": "https://www.thameswater.co.uk",
            "Referer": "https://www.thameswater.co.uk/",
        }

        r = self.s.get(url, headers=headers)
        r.raise_for_status()

        return parse_account(r.json())

    def get_tariff(self) -> Tariff:
        """Return the current metered-household tariff for the region.

        The figures are region-wide and need no authentication; this reuses the
        client's session for convenience. See the module-level
        :func:`get_tariff` for a credential-free alternative.
        """
        return get_tariff(self.s)


def _parse_line_label_as_date(label: str, today: datetime.date) -> datetime.date:
    """Parse a line label like '16-January' or '1-February' into a date.

    The year is inferred from today's date, with rollover handling so that
    e.g. a December label in a response fetched in January uses the prior year.
    """
    # Append the current year to avoid the Python 3.15 deprecation for yearless strptime.
    dt = datetime.datetime.strptime(f"{label}-{today.year}", "%d-%B-%Y")  # noqa: DTZ007
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
    today = datetime.datetime.now(tz=zoneinfo.ZoneInfo("Europe/London")).date()
    return [
        Measurement(
            start=_parse_line_label_as_date(line.Label, today),
            usage=int(line.Usage),
            total=int(line.Read),
        )
        for line in lines
    ]


def _parse_line_label_as_hour(label: str) -> datetime.time:
    """Parse an hourly line label like '0:00' or '23:00' into a clock time."""
    return datetime.datetime.strptime(label.strip(), "%H:%M").time()  # noqa: DTZ007


def meter_usage_lines_to_timeseries(
    start: datetime.date,
    lines: list[Line],
) -> list[HourlyMeasurement]:
    """Convert hourly meter usage lines to a time series.

    An hourly label is a clock time that repeats every day, so it carries the
    hour but not the day. The day comes from a cursor that starts at ``start``
    and advances every time a label reads 0:00, which is where one day ends
    and the next begins.

    Counting day boundaries this way is indifferent to how many rows a day
    has, so one rule covers every case the API produces: a window of any
    width, a spring 23-hour day, a day with hours missing from the middle,
    and a response truncated before the window ends. Deriving the day from
    the row's position instead would need all of those to be exactly 24 rows,
    and a window spanning a DST transition is not.

    An autumn 25-hour day repeats a label, because 1:00 happens twice. The
    second one is the repeat, which is what ``fold=1`` denotes, so the two
    rows land an hour apart as they should. On any other day the same
    ``fold`` is a no-op, so a label repeated for any other reason is left
    where it was.
    """
    day = start.date() if isinstance(start, datetime.datetime) else start
    seen: set[datetime.time] = set()

    measurements = []
    for index, line in enumerate(lines):
        hour = _parse_line_label_as_hour(line.Label)
        if index > 0 and hour == datetime.time.min:
            day += datetime.timedelta(days=1)
            seen.clear()

        hour_start = datetime.datetime.combine(day, hour, tzinfo=LONDON)
        if hour in seen:
            hour_start = hour_start.replace(fold=1)
        seen.add(hour)

        measurements.append(
            HourlyMeasurement(
                hour_start=hour_start,
                usage=int(line.Usage),
                total=int(line.Read),
            )
        )
    return measurements
