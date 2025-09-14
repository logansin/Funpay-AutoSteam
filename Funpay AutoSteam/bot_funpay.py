import os
import uuid
import logging
import re
import time
import threading
import requests
from dotenv import load_dotenv

from FunPayAPI import Account
from FunPayAPI.updater.runner import Runner
from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent

# ---------- ENV ----------
load_dotenv()

FUNPAY_AUTH_TOKEN = os.getenv("FUNPAY_AUTH_TOKEN")
STEAM_API_USER = os.getenv("STEAM_API_USER")
STEAM_API_PASS = os.getenv("STEAM_API_PASS")
MIN_BALANCE = float(os.getenv("MIN_BALANCE", "5"))


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")

AUTO_REFUND = _env_bool("AUTO_REFUND", True)
AUTO_DEACTIVATE = _env_bool("AUTO_DEACTIVATE", True)

CATEGORY_ID = 1086

# ---------- COLORFUL LOGGING ----------
try:
    from colorama import init as colorama_init, Fore, Style
    colorama_init(autoreset=True)
except Exception:
    class _Dummy:
        RESET_ALL = ""
    class _Fore(_Dummy):
        RED = GREEN = YELLOW = CYAN = MAGENTA = BLUE = WHITE = ""
    class _Style(_Dummy):
        BRIGHT = NORMAL = ""
    Fore, Style = _Fore(), _Style()

class ColorFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG: Fore.BLUE,
        logging.INFO: Fore.CYAN,
        logging.WARNING: Fore.YELLOW,
        logging.ERROR: Fore.RED,
        logging.CRITICAL: Fore.MAGENTA + Style.BRIGHT,
    }

    def format(self, record):
        color = self.LEVEL_COLORS.get(record.levelno, "")
        message = super().format(record)
        return f"{color}{message}{Style.RESET_ALL}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s:%(lineno)d | %(message)s"
)
for h in logging.getLogger().handlers:
    h.setFormatter(ColorFormatter(h.formatter._fmt if hasattr(h, "formatter") else "%(message)s"))

logger = logging.getLogger("SteamBot")

# ---------- CONSTANTS & STATE ----------
MIN_AMOUNTS = {
    "RUB": 15,
    "KZT": 80,
    "UAH": 7,
    "USD": 0.15
}

STEAM_BASE = "https://xn--h1aahgceagbyl.xn--p1ai/api"
REQUEST_TIMEOUT = 20

USER_STATES = {}

# ==================== AUTH / TOKEN ====================
STEAM_TOKEN: str | None = None
_STEAM_TOKEN_LOCK = threading.Lock()

def _set_token(token: str):
    global STEAM_TOKEN
    STEAM_TOKEN = token

def get_api_token() -> str:
    url = f"{STEAM_BASE}/token"
    payload = {"username": STEAM_API_USER, "password": STEAM_API_PASS}
    headers = {"accept": "application/json", "content-type": "application/json"}
    r = requests.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    token = r.json().get("access_token")
    if not token:
        raise RuntimeError("Ошибка авторизации в Steam API (нет access_token).")
    logger.info(Fore.GREEN + "✅ Успешно получили токен Steam API")
    return token

def _ensure_token():
    global STEAM_TOKEN
    if STEAM_TOKEN:
        return
    with _STEAM_TOKEN_LOCK:
        if not STEAM_TOKEN:
            _set_token(get_api_token())

def _refresh_token():
    with _STEAM_TOKEN_LOCK:
        _set_token(get_api_token())

def steam_headers() -> dict:
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {STEAM_TOKEN}"
    }

def _request_with_refresh(method: str, path: str, retry: bool = True, **kwargs) -> requests.Response:
    _ensure_token()
    headers = kwargs.pop("headers", {}) or {}
    url = f"{STEAM_BASE}{path}"

    resp = requests.request(method.upper(), url, headers={**headers, **steam_headers()},
                            timeout=REQUEST_TIMEOUT, **kwargs)

    if resp.status_code in (401, 403) and retry:
        logger.warning(Fore.YELLOW + f"[AUTH] {resp.status_code} на {path}. Обновляю токен и повторяю запрос.")
        _refresh_token()
        resp = requests.request(method.upper(), url, headers={**headers, **steam_headers()},
                                timeout=REQUEST_TIMEOUT, **kwargs)
    return resp

def _token_refresher_loop(interval_sec: int = 50 * 60):
    while True:
        try:
            time.sleep(interval_sec)
            logger.info(Fore.CYAN + "[AUTH] Плановый рефреш токена")
            _refresh_token()
        except Exception as e:
            logger.error(Fore.RED + f"[AUTH] Плановый рефреш не удался: {e}")

def start_token_refresher(interval_sec: int = 50 * 60):
    t = threading.Thread(target=_token_refresher_loop, args=(interval_sec,), daemon=True)
    t.start()

# ==================== STEAM API ====================
def _friendly_http_error(resp: requests.Response, default_msg: str = "Сервис временно недоступен."):
    try:
        data = resp.json()
    except Exception:
        data = {}
    text = data.get("message") or data.get("detail") or ""
    tech = text or resp.text[:500]
    logger.error(f"{default_msg} HTTP {resp.status_code}. Ответ: {tech}")
    if resp.status_code in (401, 403):
        return "Ошибка авторизации сервиса. Уже разбираемся — оформим возврат."
    if resp.status_code in (429,):
        return "Сервис перегружен. Попробуйте оформить заказ чуть позже — мы сделаем возврат."
    if resp.status_code >= 500:
        return "У сервиса технические неполадки. Мы вернём средства."
    if resp.status_code >= 400:
        return "Запрос отклонён сервисом. Мы вернём средства."
    return "Не удалось выполнить запрос. Мы вернём средства."

def check_login(login: str) -> bool:
    if not login:
        return False
    try:
        r = _request_with_refresh("POST", "/check", json={"login": login})
        ok = bool(r.json().get("result", False))
        logger.info((Fore.GREEN if ok else Fore.YELLOW) + f"🔎 Проверка логина: '{login}' -> {ok}")
        return ok
    except Exception as e:
        logger.error(Fore.RED + f"Ошибка проверки логина Steam: {e}")
        return False

def convert_to_usd(currency: str, amount: float) -> float | None:
    try:
        if currency.upper() == "USD":
            return float(amount)
        r = _request_with_refresh(
            "POST", "/rates",
            json={"primary_currency": currency.upper(), "amount": amount}
        )
        if r.status_code != 200:
            logger.warning(Fore.YELLOW + f"[RATES] Нехороший ответ: {r.status_code} {r.text[:200]}")
            return None
        return r.json().get("usd_price")
    except Exception as e:
        logger.error(Fore.RED + f"Ошибка конвертации: {e}")
        return None

def create_order(login: str, usd_amount: float):
    custom_id = str(uuid.uuid4())
    payload = {
        "service_id": 1,
        "quantity": round(float(usd_amount), 2),
        "custom_id": custom_id,
        "data": login
    }
    logger.debug(Fore.BLUE + f"[DEBUG] payload create_order: {payload}")
    r = _request_with_refresh("POST", "/create_order", json=payload)
    logger.info(
        (Fore.GREEN if r.status_code == 200 else Fore.RED)
        + f"🧾 Создание заказа {custom_id}: HTTP {r.status_code}"
    )
    return r, custom_id

def pay_order(custom_id: str):
    payload = {"custom_id": str(custom_id)}
    logger.debug(Fore.BLUE + f"[DEBUG] payload pay_order: {payload}")
    r = _request_with_refresh("POST", "/pay_order", json=payload)
    logger.info(
        (Fore.GREEN if r.status_code == 200 else Fore.RED)
        + f"💳 Оплата заказа {custom_id}: HTTP {r.status_code}"
    )
    return r

def check_balance() -> float:
    try:
        r = _request_with_refresh("POST", "/check_balance")
        r.raise_for_status()
        data = r.json()
        balance = float(data.get("balance", data) if isinstance(data, dict) else data)
        logger.info(Fore.MAGENTA + f"[BALANCE] Текущий баланс API: {balance} USD")
        return balance
    except Exception as e:
        logger.error(Fore.RED + f"[BALANCE] Ошибка проверки баланса: {e}")
        return 0.0

def deactivate_category(account: Account, category_id: int):
    try:
        my_lots = account.get_my_subcategory_lots(category_id)
    except Exception as e:
        logger.error(Fore.RED + f"[LOTS] Не удалось получить список лотов категории {category_id}: {e}")
        return

    if not my_lots:
        logger.info(Fore.YELLOW + f"[LOTS] В категории {category_id} нет лотов для деактивации.")
        return

    deactivated = 0
    skipped = 0
    failed = 0

    for lot in my_lots:
        lot_id = getattr(lot, "id", None)
        if lot_id is None:
            logger.warning(Fore.YELLOW + "[LOTS] Пропускаю элемент без lot.id")
            skipped += 1
            continue

        try:
            fields = account.get_lot_fields(lot_id)
        except Exception as e:
            logger.error(Fore.RED + f"[LOTS] Не удалось получить поля лота {lot_id}: {e}")
            failed += 1
            continue

        if fields is None:
            logger.warning(Fore.YELLOW + f"[LOTS] get_lot_fields вернул None для {lot_id} — пропускаю")
            skipped += 1
            continue

        try:
            if isinstance(fields, dict):
                is_active = fields.get("active", fields.get("is_active", True))
                if not is_active:
                    logger.debug(f"[LOTS] Лот {lot_id} уже деактивирован (dict).")
                    skipped += 1
                    continue
                fields["active"] = False
                account.save_lot(fields)
            else:
                active_val = (
                    getattr(fields, "active", None)
                    if hasattr(fields, "active")
                    else getattr(fields, "is_active", None)
                    if hasattr(fields, "is_active")
                    else getattr(fields, "enabled", None)
                    if hasattr(fields, "enabled")
                    else None
                )

                if active_val is False:
                    logger.debug(f"[LOTS] Лот {lot_id} уже деактивирован (obj).")
                    skipped += 1
                    continue

                if hasattr(fields, "active"):
                    fields.active = False
                elif hasattr(fields, "is_active"):
                    fields.is_active = False
                elif hasattr(fields, "enabled"):
                    fields.enabled = False
                else:
                    logger.warning(Fore.YELLOW + f"[LOTS] Не найден флаг активности у лота {lot_id} — пропускаю")
                    skipped += 1
                    continue

                account.save_lot(fields)

            deactivated += 1
            logger.info(Fore.YELLOW + f"[LOTS] Деактивирован лот {lot_id}")
            time.sleep(0.3)

        except Exception as e:
            logger.error(Fore.RED + f"[LOTS] Ошибка деактивации лота {lot_id}: {e}")
            failed += 1
            continue

    logger.warning(
        Fore.YELLOW
        + f"[LOTS] Итог: деактивировано={deactivated}, пропущено={skipped}, с ошибкой={failed} (категория {category_id})"
    )

def activate_category(account: Account, category_id: int):
    try:
        my_lots = account.get_my_subcategory_lots(category_id)
    except Exception as e:
        logger.error(Fore.RED + f"[LOTS] Не удалось получить список лотов категории {category_id}: {e}")
        return

    if not my_lots:
        logger.info(Fore.YELLOW + f"[LOTS] В категории {category_id} нет лотов для активации.")
        return

    activated = 0
    skipped = 0
    failed = 0

    for lot in my_lots:
        lot_id = getattr(lot, "id", None)
        if lot_id is None:
            skipped += 1
            continue

        try:
            fields = account.get_lot_fields(lot_id)
            if fields is None:
                skipped += 1
                continue

            if isinstance(fields, dict):
                is_active = fields.get("active", fields.get("is_active", False))
                if is_active:
                    skipped += 1
                    continue
                fields["active"] = True
                account.save_lot(fields)
            else:
                active_val = (
                    getattr(fields, "active", None)
                    if hasattr(fields, "active")
                    else getattr(fields, "is_active", None)
                    if hasattr(fields, "is_active")
                    else getattr(fields, "enabled", None)
                    if hasattr(fields, "enabled")
                    else None
                )
                if active_val is True:
                    skipped += 1
                    continue

                if hasattr(fields, "active"):
                    fields.active = True
                elif hasattr(fields, "is_active"):
                    fields.is_active = True
                elif hasattr(fields, "enabled"):
                    fields.enabled = True
                else:
                    skipped += 1
                    continue

                account.save_lot(fields)

            activated += 1
            logger.info(Fore.GREEN + f"[LOTS] Активирован лот {lot_id}")
            time.sleep(0.3)

        except Exception as e:
            logger.error(Fore.RED + f"[LOTS] Ошибка активации лота {lot_id}: {e}")
            failed += 1
            continue

    logger.warning(
        Fore.CYAN
        + f"[LOTS] Итог активации: активировано={activated}, пропущено={skipped}, с ошибкой={failed} (категория {category_id})"
    )

# ==================== HELPERS ====================
def _first_number_from_string(s: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)", s)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except:
            return None
    return None

def get_order_amount(order) -> tuple[float, str] | None:
    candidate_attrs = ["quantity", "qty", "amount", "sum", "count", "price", "quantity_value", "quantity_text"]
    for attr in candidate_attrs:
        val = getattr(order, attr, None)
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        if isinstance(val, (int, float)):
            logger.info(Fore.CYAN + f"[get_order_amount] Взято из атрибута '{attr}': {float(val)}")
            return float(val), f"attr:{attr}"
        try:
            s = str(val).replace(",", ".").strip()
            num = _first_number_from_string(s)
            if num is not None:
                logger.info(Fore.CYAN + f"[get_order_amount] Взято из атрибута '{attr}' (парсинг строки): {num} (raw='{val}')")
                return float(num), f"attr:{attr}"
        except Exception as e:
            logger.debug(f"[get_order_amount] Ошибка парсинга атрибута {attr}: {e}")

    text_fields = []
    for f in ("html", "title", "full_description", "short_description"):
        v = getattr(order, f, None)
        if v:
            text_fields.append(str(v))
    full_text = " ".join(text_fields).lower()
    if not full_text.strip():
        logger.warning(Fore.YELLOW + "[get_order_amount] Нет текстовых полей для поиска суммы.")
        return None

    patterns_priority = [
        r'(?:количеств(?:о|о:)|кол-во|кол:)\D{0,60}?(\d+(?:[.,]\d+)?)',
        r'(?:amount|quantity|qty)\D{0,60}?(\d+(?:[.,]\d+)?)',
        r'(?:пополнение|пополнен|wallet|steam_wallet)\D{0,60}?(\d+(?:[.,]\d+)?)',
        r'(\d+(?:[.,]\d+)?)\s*(?:uah|грн|uah|rub|руб|kzt|тенге|usd|\$|₽|₸)\b'
    ]
    for pat in patterns_priority:
        m = re.search(pat, full_text)
        if m:
            try:
                val = float(m.group(1).replace(",", "."))
                logger.info(Fore.CYAN + f"[get_order_amount] Найдено по приоритетному шаблону: {val} (pattern: {pat})")
                return val, f"pattern:{pat}"
            except Exception:
                continue

    first_num = _first_number_from_string(full_text)
    if first_num is not None:
        logger.info(Fore.CYAN + f"[get_order_amount] Взято первое число из текста: {first_num}")
        return float(first_num), "text:first_number"

    logger.warning(Fore.YELLOW + "[get_order_amount] Не удалось определить сумму пополнения.")
    return None

def get_description_text(order) -> str:
    for attr in ("full_description", "short_description", "html", "title"):
        v = getattr(order, attr, None)
        if v:
            return str(v).lower()
    return ""

def order_link(order_id) -> str:
    try:
        return f"https://funpay.com/orders/{int(order_id)}/"
    except Exception:
        return "https://funpay.com/orders/"

def _nice_refund(account: Account, chat_id, order_id, user_text: str):
    logger.info(Fore.YELLOW + f"↩️ Возврат по заказу {order_id}: {user_text}")
    if chat_id:
        if AUTO_REFUND:
            account.send_message(chat_id, user_text + "\n\nДеньги будут возвращены автоматически.")
        else:
            account.send_message(chat_id, user_text + "\n\n⚠️ Автоматический возврат выключен. Свяжитесь с админом для возврата.")
    if order_id and AUTO_REFUND:
        try:
            account.refund(order_id)
        except Exception as e:
            logger.error(Fore.RED + f"[REFUND] Ошибка возврата по заказу {order_id}: {e}")

# ==================== HANDLERS ====================
def handle_new_order(account: Account, order):
    try:
        _refresh_token()

        subcat_id, subcat = get_subcategory_id_safe(order, account)
        if subcat_id != CATEGORY_ID:
            logger.info(Fore.BLUE + f"[ORDER] Заказ {order.id} проигнорирован (subcategory {subcat_id}, требуется {CATEGORY_ID})")
            return

        chat_id = getattr(order, "chat_id", None)
        buyer_id = getattr(order, "buyer_id", None)

        title = getattr(order, "title", None)
        logger.info(Style.BRIGHT + Fore.WHITE + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        logger.info(Style.BRIGHT + Fore.CYAN + f"🆕 Новый заказ #{getattr(order, 'id', 'unknown')} | Покупатель: {buyer_id}")
        if title:
            logger.info(Fore.CYAN + f"📦 Товар: {title}")
        logger.info(Style.BRIGHT + Fore.WHITE + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        desc_text = get_description_text(order)
        if "steam_wallet:" not in desc_text:
            _nice_refund(
                account, chat_id, getattr(order, "id", None),
                "⚠️ Ошибка в заказе: не указана валюта (ожидалось `steam_wallet: rub|uah|kzt|usd`)."
            )
            return

        try:
            currency_raw = desc_text.split("steam_wallet:")[1].split()[0]
            currency = currency_raw.strip().upper()
        except Exception:
            currency = None

        if currency not in MIN_AMOUNTS:
            _nice_refund(
                account, chat_id, getattr(order, "id", None),
                f"⚠️ Неверная валюта: {currency or '—'}. Поддерживаются: RUB, UAH, KZT, USD."
            )
            return

        amt_info = get_order_amount(order)
        if not amt_info:
            _nice_refund(
                account, chat_id, getattr(order, "id", None),
                "⚠️ Не удалось определить сумму пополнения. Пожалуйста, укажите количество при оформлении заказа."
            )
            return

        amount, source = amt_info
        logger.info(Fore.CYAN + f"[ORDER] Сумма пополнения: {amount} {currency} (source={source})")

        if amount < MIN_AMOUNTS[currency]:
            _nice_refund(
                account, chat_id, getattr(order, "id", None),
                f"⚠️ Минимальная сумма пополнения — {MIN_AMOUNTS[currency]} {currency}."
            )
            return

        usd_amount = convert_to_usd(currency, amount)
        if usd_amount is None:
            _nice_refund(
                account, chat_id, getattr(order, "id", None),
                "❌ Не удалось преобразовать валюту. Повторите заказ позже."
            )
            return

        USER_STATES[buyer_id] = {
            "step": "waiting_login",
            "order_id": getattr(order, "id", None),
            "chat_id": chat_id,
            "amount": amount,
            "currency": currency,
            "usd_amount": usd_amount
        }
        account.send_message(
            chat_id,
            "👋 Спасибо за заказ!\n\n"
            f"Сумма пополнения: {amount} {currency} (≈ {usd_amount:.2f} USD).\n"
            "Пожалуйста, укажите ваш *Steam-логин* (без почты и телефона)."
        )
        logger.info(Fore.BLUE + f"⏳ Ожидаем логин от покупателя {buyer_id}...")

    except Exception:
        logger.exception(Fore.RED + "Ошибка обработки заказа (общая)")

def handle_new_message(account: Account, message):
    if not message:
        logger.debug("[MSG] Пустое сообщение (None) — пропускаю")
        return

    user_id = getattr(message, "author_id", None)
    chat_id = getattr(message, "chat_id", None)
    raw_text = (
        getattr(message, "text", None)
        or getattr(message, "body", None)
        or getattr(message, "content", None)
        or ""
    )
    if not isinstance(raw_text, str) or not raw_text.strip():
        logger.debug(f"[MSG] Нет текстового содержимого (type={type(raw_text).__name__}) — пропускаю")
        return
    text = raw_text.strip()

    if not user_id or user_id not in USER_STATES:
        return

    state = USER_STATES[user_id]

    if state["step"] == "waiting_login":
        login = text
        if not check_login(login):
            account.send_message(
                chat_id,
                f"⚠️ Логин *{login}* не найден. Проверьте правильность написания и отправьте ещё раз.\n\n"
                "Пример: `gabelogannewell`"
            )
            logger.info(Fore.YELLOW + f"🚫 Логин не найден: {login}")
            return

        state["login"] = login
        state["step"] = "confirm_login"
        account.send_message(
            chat_id,
            "✅ Логин найден!\n\n"
            f"Вы указали: *{login}*\n"
            f"Сумма: *{state['amount']} {state['currency']}* (≈ *{state['usd_amount']:.2f} USD*)\n\n"
            "Если всё верно — напишите `+`.\n"
            "Если нужно изменить логин — просто отправьте новый."
        )
        logger.info(Fore.GREEN + f"✅ Логин подтверждён существующий: {login}")
        return

    if state["step"] == "confirm_login":
        if text == "+":
            logger.info(Fore.BLUE + f"🧾 Запрос на создание заказа для {state['login']} на {state['usd_amount']:.2f} USD")
            r, custom_id = create_order(state["login"], state["usd_amount"])

            try:
                json_r = r.json()
            except Exception:
                json_r = {}

            if r.status_code != 200 or "error" in json_r:
                user_msg = _friendly_http_error(r, "Не удалось создать заказ в сервисе пополнения.")
                account.send_message(chat_id, f"❌ {user_msg}")

                balance = check_balance()
                if balance < MIN_BALANCE and AUTO_DEACTIVATE:
                    logger.warning(Fore.YELLOW + "💤 Баланс ниже порога — деактивируем лоты категории.")
                    deactivate_category(account, CATEGORY_ID)

                if AUTO_REFUND:
                    try:
                        account.refund(state["order_id"])
                    except Exception as e:
                        logger.error(Fore.RED + f"[REFUND] Ошибка возврата: {e}")
                else:
                    account.send_message(chat_id, "⚠️ Автоматический возврат отключён. Свяжитесь с админом для возврата.")

                USER_STATES.pop(user_id, None)
                return

            logger.info(Fore.BLUE + f"💳 Запрос на оплату заказа custom_id={custom_id}")
            pay_res = pay_order(custom_id)

            if pay_res.status_code == 200:
                state["step"] = "await_confirm_topup"
                account.send_message(
                    chat_id,
                    "🎉 Готово!\n\n"
                    f"Мы пополнили баланс *{state['login']}* на *{state['amount']} {state['currency']}* "
                    f"(≈ *{state['usd_amount']:.2f} USD*).\n\n"
                    "Пожалуйста, проверьте зачисление средств в Steam. "
                    "Если всё в порядке, напишите **`подтверждаю`**, и мы завершим заказ."
                )
                logger.info(Fore.GREEN + f"✅ Успешное пополнение: {state['amount']} {state['currency']} для {state['login']}. Ждём подтверждение покупателя.")
            else:
                user_msg = _friendly_http_error(pay_res, "Не удалось оплатить заказ в сервисе пополнения.")
                account.send_message(chat_id, f"❌ {user_msg}")

                balance = check_balance()
                if balance < MIN_BALANCE and AUTO_DEACTIVATE:
                    logger.warning(Fore.YELLOW + "💤 Баланс ниже порога — деактивируем лоты категории.")
                    deactivate_category(account, CATEGORY_ID)

                if AUTO_REFUND:
                    try:
                        account.refund(state["order_id"])
                    except Exception as e:
                        logger.error(Fore.RED + f"[REFUND] Ошибка возврата: {e}")
                else:
                    account.send_message(chat_id, "⚠️ Автоматический возврат отключён. Свяжитесь с админом для возврата.")

                USER_STATES.pop(user_id, None)

        else:
            new_login = text
            if not check_login(new_login):
                account.send_message(
                    chat_id,
                    f"⚠️ Логин *{new_login}* не найден. Попробуйте снова:"
                )
                logger.info(Fore.YELLOW + f"🚫 Новый логин не найден: {new_login}")
                return
            state["login"] = new_login
            account.send_message(
                chat_id,
                "✅ Новый логин найден!\n\n"
                f"Вы указали: *{new_login}*\n"
                f"Сумма: *{state['amount']} {state['currency']}* (≈ *{state['usd_amount']:.2f} USD*)\n\n"
                "Если всё верно — напишите `+`.\n"
                "Если нужно изменить логин — укажите другой."
            )
            logger.info(Fore.GREEN + f"♻️ Логин обновлён: {new_login}")

        return

    if state["step"] == "await_confirm_topup":
        if text.lower() in ("подтверждаю", "подтверждаю.", "ок", "окей", "да", "+", "confirm", "confirmed"):
            try:
                account.complete_order(state["order_id"])
                link = order_link(state["order_id"])
                account.send_message(
                    chat_id,
                    "✅ Спасибо! Заказ подтверждён и завершён.\n\n"
                    "🙏 Если всё понравилось, пожалуйста, оставьте отзыв — это очень помогает!\n"
                    f"Вот ссылка на ваш заказ: {link}"
                )
                logger.info(Fore.GREEN + f"🏁 Заказ {state['order_id']} подтверждён покупателем и закрыт (review requested).")
            except Exception as e:
                logger.error(Fore.RED + f"Не удалось завершить заказ {state['order_id']}: {e}")
                account.send_message(
                    chat_id,
                    "ℹ️ Мы зафиксировали ваше подтверждение, но возникла техническая задержка с завершением в системе. "
                    "Статус обновится автоматически в ближайшее время."
                )
            finally:
                USER_STATES.pop(user_id, None)
        else:
            account.send_message(
                chat_id,
                "Если всё в порядке — напишите **`подтверждаю`** для завершения заказа.\n"
                "Если что-то не так — опишите, пожалуйста, проблему."
            )
            logger.info(Fore.CYAN + f"⌛ Ожидаем подтверждение от покупателя {user_id}. Текст: {text}")

# ==================== RUNNER LOOP ====================
def get_subcategory_id_safe(order, account):
    subcat = getattr(order, "subcategory", None) or getattr(order, "sub_category", None)
    if subcat and hasattr(subcat, "id"):
        return subcat.id, subcat
    try:
        full_order = account.get_order(order.id)
        subcat = getattr(full_order, "subcategory", None) or getattr(full_order, "sub_category", None)
        if subcat and hasattr(subcat, "id"):
            return subcat.id, subcat
    except Exception as e:
        logger.warning(Fore.YELLOW + f"⚠️ Не удалось загрузить полный заказ: {e}")
    return None, None

def main():
    if not FUNPAY_AUTH_TOKEN:
        raise RuntimeError("FUNPAY_AUTH_TOKEN не найден в .env")
    if not (STEAM_API_USER and STEAM_API_PASS):
        raise RuntimeError("STEAM_API_USER/STEAM_API_PASS не найдены в .env")

    logger.info(Fore.CYAN + f"[CFG] AUTO_REFUND={AUTO_REFUND}, AUTO_DEACTIVATE={AUTO_DEACTIVATE}, MIN_BALANCE={MIN_BALANCE}")

    account = Account(FUNPAY_AUTH_TOKEN)
    account.get()
    logger.info(Fore.GREEN + f"🔐 Авторизован как {getattr(account, 'username', '(unknown)')}")

    runner = Runner(account)
    logger.info(Style.BRIGHT + Fore.WHITE + "🚀 SteamBot запущен. Ожидаю события...")

    for event in runner.listen(requests_delay=3.0):
        try:
            if isinstance(event, NewOrderEvent):
                order = account.get_order(event.order.id)
                handle_new_order(account, order)
            elif isinstance(event, NewMessageEvent):
                if getattr(event, "message", None) is None:
                    logger.debug("[Runner] NewMessageEvent без message — пропускаю")
                else:
                    handle_new_message(account, event.message)
        except Exception:
            logger.exception(Fore.RED + "Ошибка в основном цикле")

if __name__ == "__main__":
    main()
