"""Phone numbers, parsed offline with Google's libphonenumber metadata.

The python `phonenumbers` package (Apache-2.0) carries libphonenumber's
numbering plans, geocoding, original-carrier and time-zone data. Nothing is
dialled and nothing leaves this node: the answer is what the number itself
says about where and how it was issued, never who owns it.
"""

import phonenumbers
from phonenumbers import PhoneNumberType, carrier, geocoder
from phonenumbers import timezone as number_timezone

from .. import runtime

LINE_TYPES = {
    PhoneNumberType.FIXED_LINE: "fixed_line", PhoneNumberType.MOBILE: "mobile",
    PhoneNumberType.FIXED_LINE_OR_MOBILE: "fixed_line_or_mobile", PhoneNumberType.TOLL_FREE: "toll_free",
    PhoneNumberType.PREMIUM_RATE: "premium_rate", PhoneNumberType.SHARED_COST: "shared_cost",
    PhoneNumberType.VOIP: "voip", PhoneNumberType.PERSONAL_NUMBER: "personal_number",
    PhoneNumberType.PAGER: "pager", PhoneNumberType.UAN: "uan", PhoneNumberType.VOICEMAIL: "voicemail",
    PhoneNumberType.UNKNOWN: "unknown",
}


def parse_number(number: str, region=None):
    """The parsed number, or raises runtime.InvalidRequest when the text is
    not a phone number at all (a free refusal, before any payment)."""
    try:
        return phonenumbers.parse(number, region)
    except phonenumbers.NumberParseException as exc:
        raise runtime.InvalidRequest(
            f"`number` is not a phone number ({exc._msg}). Give it in international form (+44 20 7946 0958) "
            "or pass `region` (GB) for a national one.") from exc


class _Phone:
    id = "libphonenumber"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def describe(self, number: str, region=None) -> runtime.ProviderResult:
        n = parse_number(number, region)
        valid = phonenumbers.is_valid_number(n)
        region_code = phonenumbers.region_code_for_number(n)
        value = {
            "valid": valid,
            "possible": phonenumbers.is_possible_number(n),
            "e164": phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164),
            "international": phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.INTERNATIONAL),
            "national": phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.NATIONAL),
            "country_code": n.country_code,
            "region": region_code if region_code and region_code != "ZZ" else None,
            "country": geocoder.country_name_for_number(n, "en") or None,
            "location": (geocoder.description_for_number(n, "en") or None) if valid else None,
            "carrier": (carrier.name_for_number(n, "en") or None) if valid else None,
            "line_type": LINE_TYPES.get(phonenumbers.number_type(n), "unknown") if valid else "unknown",
            "time_zones": [z for z in number_timezone.time_zones_for_number(n) if z != "Etc/Unknown"] if valid else [],
            "metadata_version": phonenumbers.__version__,
        }
        return runtime.ProviderResult(value=value, cost_micros=0, cost_measured=True,
                                      usage=f"valid={valid} region={value['region']}")


PHONE = _Phone()
PROVIDERS = [PHONE]
