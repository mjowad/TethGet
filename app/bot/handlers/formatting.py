"""
Text formatting, label tables, and input-parsing helpers shared across
handler modules: Persian/Arabic digit translation, Tehran-timezone
timestamp formatting, condition/frequency display labels, target-price
input parsing and validation, and the market-price sanity bound used
before displaying any price to a user.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.bot.bot_state import poller_status

_TEHRAN_TZ = ZoneInfo("Asia/Tehran")

_CONDITION_LABELS = {
    "above":           "📈 بالاتر از قیمت",
    "below":           "📉 پایین‌تر از قیمت",
    "percentage_up":   "🚀 رشد درصدی",
    "percentage_down": "🔻 افت درصدی",
}

_FREQUENCY_LABELS = {
    "once":       "فقط یک‌بار",
    "every_time": "هر بار",
    "daily":      "روزی یک‌بار",
}

_EXCHANGE_NAMES_FA = {
    "wallex": "والکس",
    "bitpin": "بیت‌پین",
    "exir": "اکسیر",
    "zipodo": "اومپی فینکس",
    "index_median": "شاخص قیمت بازار",
}

_DIGIT_TRANSLATION = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹" "٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)


def _format_exchange_name(source: str | None) -> str:
    if not source:
        return "—"
    return _EXCHANGE_NAMES_FA.get(source.lower(), source)


def _format_age(seconds: int) -> str:
    if seconds < 60:
        return "کمتر از یک دقیقه"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} دقیقه"
    hours = minutes // 60
    return f"{hours} ساعت"


def _to_persian_digits(text: str) -> str:
    table = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
    return text.translate(table)


def _format_tehran_hhmm(unix_ts: int) -> str:
    dt_utc = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    dt_tehran = dt_utc.astimezone(_TEHRAN_TZ)
    hhmm = dt_tehran.strftime("%H:%M")
    return _to_persian_digits(hhmm)


def _parse_number(raw_text: str) -> float | None:
    cleaned = raw_text.strip().translate(_DIGIT_TRANSLATION)
    cleaned = cleaned.replace(",", "").replace("٬", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None


def _is_market_price_valid(price: any) -> bool:
    """
    اعتبارسنجی قیمت دریافتی از صرافی/دیتابیس.
    بررسی محدوده منطقی تتر (مثلاً کف ۳۰,۰۰۰ تومان و سقف ۱,۰۰۰,۰۰۰ تومان).
    """
    if price is None:
        return False
    try:
        price_float = float(price)
    except (ValueError, TypeError):
        return False

    if price_float <= 30000.0 or price_float >= 1000000.0:
        return False

    return True


def _format_24h_change_label(change_val: float | str | None) -> str:
    """
    فرمت‌دهی مقدار تغییرات ۲۴ ساعته بر اساس شرایط:
    - عدد مثبت: 🟢 +0.92٪
    - عدد منفی: 🔴 -0.92٪
    - صفر: ⚪ 0.00٪
    - دیتای ناقص / N/A / None: -
    """
    if change_val is None or change_val == "N/A" or change_val == "":
        return "-"

    try:
        val = float(change_val)
    except (ValueError, TypeError):
        return "-"

    if val > 0:
        return f"🟢 +{val:.2f}٪"
    elif val < 0:
        return f"🔴 {val:.2f}٪"
    else:
        return "⚪ ۰.۰۰٪"


def _format_partial_value(val: any, formatter=None) -> str:
    """
    اگر مقدار وجود داشته باشد، آن را فرمت کرده و برمی‌گرداند؛
    در غیر این صورت یک خط تیره ساده ('—') پس می‌دهد.
    """
    if val is None or val == "":
        return "—"
    if formatter:
        try:
            return formatter(val)
        except Exception:
            return "—"
    return str(val)


def _validate_target_price(condition: str, target_price: float) -> tuple[bool, str]:
    """
    اعتبارسنجی قیمت هدف کاربر نسبت به آخرین قیمت بازار (ذخیره شده در رم)
    خروجی: (is_valid, error_message)
    """
    if condition not in ["above", "below"]:
        return True, ""

    current_price = poller_status.last_price

    if current_price is None:
        return False, "⚠️ در حال حاضر دیتای قیمتی دریافت نشده است. لطفا چند ثانیه دیگر تلاش کنید."

    if condition == "above" and target_price <= current_price:
        return False, (
            f"⚠️ خطای قیمت هدف!\n\n"
            f"شما شرط «📈 بالاتر از قیمت» را انتخاب کرده‌اید، بنابراین قیمت هدف شما باید از قیمت فعلی بازار بیشتر باشد.\n\n"
            f"📊 شاخص قیمت فعلی تتر: {current_price:,.0f} تومان\n"
            f"❌ قیمت وارد شده: {target_price:,.0f} تومان\n\n"
            f"💡 لطفاً یک عدد بزرگتر وارد کنید:"
        )

    if condition == "below" and target_price >= current_price:
        return False, (
            f"⚠️ خطای قیمت هدف!\n\n"
            f"شما شرط «📉 پایین‌تر از قیمت» را انتخاب کرده‌اید، بنابراین قیمت هدف شما باید از قیمت فعلی بازار کمتر باشد.\n\n"
            f"📊 شاخص قیمت فعلی تتر: {current_price:,.0f} تومان\n"
            f"❌ قیمت وارد شده: {target_price:,.0f} تومان\n\n"
            f"💡 لطفاً یک عدد کوچکتر وارد کنید:"
        )

    return True, ""