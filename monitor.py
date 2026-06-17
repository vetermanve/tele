#!/usr/bin/env python3
"""
Мониторинг P2P-цен на wallet.tg (USDT/RUB, TON/RUB)
с отклонением от рыночного курса.

python monitor.py
"""

import os
import sys
import time
import json
import threading
from io import StringIO
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from rich.table import Table
from rich.text import Text
from rich.console import Console, Group
from rich import box

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl, BufferControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import ANSI

# ─── настройки ───────────────────────────────────────────────
load_dotenv()  # подхватывает .env из корня проекта, если есть

API_KEY = os.environ.get("WALLET_TG_API_KEY", "")
P2P_URL = "https://p2p.walletbot.me/p2p/integration-api/v1/item/online"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"

POLL_INTERVAL = 20
TOP_N = 30
SHOW_N = 10

PAIRS = [
    {"crypto": "USDT", "fiat": "RUB", "cg_id": "tether"},
    {"crypto": "TON",  "fiat": "RUB", "cg_id": "the-open-network"},
]

PAYMENT_LABELS = {
    "sbp": "СБП", "tinkoff": "Тинькофф", "sberbankru": "Сбер",
    "alfabank": "Альфа", "raiffeisen": "Райфф", "gazprombank": "Газпром",
    "ozon": "Озон", "otpbank": "ОТП", "russtandart": "Русстандарт",
    "dushanbecitybank": "Душанбе Сити", "youmoney": "ЮMoney", "qiwi": "QIWI",
    "payeer": "Payeer", "rosbank": "Росбанк", "mtsbank": "МТС Банк",
    "pochta": "Почта Банк", "vtb": "ВТБ", "sovcombank": "Совком",
    "uralsib": "Уралсиб", "homecredit": "ХКФ",
}

# ─── фильтры (персистентные) ─────────────────────────────────
FILTERS_FILE = Path(__file__).parent / ".filters.json"

# поля: amount_min, amount_max, rating
FIELD_NAMES = ["amount_min", "amount_max", "rating"]
FIELD_COUNT = len(FIELD_NAMES)


def default_config():
    return {"amount_min": 0, "amount_max": 0, "rating": 98}


def load_config():
    try:
        data = json.loads(FILTERS_FILE.read_text())
        return {
            "amount_min": float(data.get("amount_min", 0)),
            "amount_max": float(data.get("amount_max", 0)),
            "rating": float(data.get("rating", 98)),
        }
    except Exception:
        return default_config()


def save_config(cfg):
    try:
        FILTERS_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


# ─── состояние ────────────────────────────────────────────────
state_lock = threading.Lock()
config = load_config()
active_field = 0  # 0=min, 1=max, 2=rating
market_rates = {}
all_offers = {}
error_msg = ""
RATE_SOURCE = ""

# трекинг лидеров: {crypto: {"price": float, "id": str, "since": float}}
best_leaders = {}
FLASH_DURATION = 4  # секунд мигания

# ─── API ──────────────────────────────────────────────────────

def fetch_p2p_offers(crypto, fiat):
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    payload = {
        "cryptoCurrency": crypto, "fiatCurrency": fiat,
        "side": "SELL", "page": 1, "pageSize": TOP_N,
    }
    try:
        r = requests.post(P2P_URL, json=payload, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        return data.get("data", data.get("items", []))
    except Exception:
        return None


def _try_coingecko():
    ids = ",".join(p["cg_id"] for p in PAIRS)
    r = requests.get(
        COINGECKO_URL,
        params={"ids": ids, "vs_currencies": "rub"},
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    return {k: v.get("rub", 0) for k, v in data.items()} if data else None


def _try_binance():
    result = {}
    r = requests.get("https://api.binance.com/api/v3/ticker/price",
                     params={"symbol": "USDTRUB"}, timeout=10)
    r.raise_for_status()
    usdt_rub = float(r.json()["price"])
    result["tether"] = usdt_rub
    r = requests.get("https://api.binance.com/api/v3/ticker/price",
                     params={"symbol": "TONUSDT"}, timeout=10)
    r.raise_for_status()
    result["the-open-network"] = float(r.json()["price"]) * usdt_rub
    return result


def _try_cryptocompare():
    r = requests.get("https://min-api.cryptocompare.com/data/pricemulti",
                     params={"fsyms": "USDT,TON", "tsyms": "RUB"}, timeout=10)
    r.raise_for_status()
    data = r.json()
    result = {}
    if "USDT" in data:
        result["tether"] = data["USDT"].get("RUB", 0)
    if "TON" in data:
        result["the-open-network"] = data["TON"].get("RUB", 0)
    return result if result else None


RATE_CACHE_FILE = Path(__file__).parent / ".rates_cache.json"
RATE_CACHE_TTL = 600


def _load_cache():
    try:
        cache = json.loads(RATE_CACHE_FILE.read_text())
        if time.time() - cache.get("ts", 0) < RATE_CACHE_TTL:
            return cache.get("rates"), cache.get("source", "cache")
    except Exception:
        pass
    return None, None


def _save_cache(rates, source):
    try:
        RATE_CACHE_FILE.write_text(json.dumps({
            "ts": time.time(), "source": source, "rates": rates,
        }))
    except Exception:
        pass


def fetch_market_rates_impl():
    global RATE_SOURCE
    cached, src = _load_cache()
    if cached:
        RATE_SOURCE = src + " (cache)"
        return cached
    for name, fn in [("CoinGecko", _try_coingecko),
                     ("Binance", _try_binance),
                     ("CryptoCompare", _try_cryptocompare)]:
        try:
            data = fn()
            if data:
                RATE_SOURCE = name
                _save_cache(data, name)
                return data
        except Exception:
            continue
    try:
        cache = json.loads(RATE_CACHE_FILE.read_text())
        rates = cache.get("rates")
        if rates:
            age = int((time.time() - cache.get("ts", 0)) / 60)
            RATE_SOURCE = "%s (%d мин)" % (cache.get("source", "?"), age)
            return rates
    except Exception:
        pass
    RATE_SOURCE = ""
    return {}


# ─── фоновый поллинг ─────────────────────────────────────────

app_ref = None


def poll_loop():
    global market_rates, all_offers, error_msg
    while True:
        err = ""
        new_market = fetch_market_rates_impl()
        with state_lock:
            if new_market:
                market_rates = new_market
            elif not market_rates:
                err = "Курсы недоступны"

        for pair in PAIRS:
            offers = fetch_p2p_offers(pair["crypto"], pair["fiat"])
            with state_lock:
                if offers is not None:
                    all_offers[pair["crypto"]] = offers
                elif pair["crypto"] not in all_offers:
                    all_offers[pair["crypto"]] = []
                    err = "P2P API недоступен"

        with state_lock:
            error_msg = err

        if app_ref:
            app_ref.invalidate()

        time.sleep(POLL_INTERVAL)


# ─── рендеринг ────────────────────────────────────────────────

def fmt_pay(code):
    return PAYMENT_LABELS.get(code, code)

def fmt_n(n):
    return "{:,.0f}".format(n).replace(",", " ")

def dev_pct(p2p, mkt):
    if mkt <= 0:
        return None
    return (p2p - mkt) / mkt * 100

def dev_text(pct):
    if pct is None:
        return Text("  n/a", style="dim")
    s = "%+.2f%%" % pct
    if pct <= 1.0:
        return Text(s, style="bold green")
    if pct <= 3.0:
        return Text(s, style="yellow")
    if pct <= 5.0:
        return Text(s, style="dark_orange")
    return Text(s, style="bold red")

def rating_text(er):
    if er == "—":
        return Text("—", style="dim")
    val = float(er) * 100
    s = "%.0f%%" % val
    if val >= 99:
        return Text(s, style="bold green")
    if val >= 95:
        return Text(s, style="yellow")
    return Text(s, style="red")


def ad_matches(ad, cfg):
    """Объявление подходит если его диапазон лимита пересекается с нашим диапазоном."""
    er = ad.get("executeRate", "0")
    if er != "—" and float(er) * 100 < cfg["rating"]:
        return False
    lo = float(ad.get("minAmount", 0))
    hi = float(ad.get("maxAmount", 0))
    f_min = cfg["amount_min"]
    f_max = cfg["amount_max"]
    # пересечение диапазонов: [lo, hi] ∩ [f_min, f_max]
    if f_min > 0 and hi < f_min:
        return False
    if f_max > 0 and lo > f_max:
        return False
    return True


MIN_ORDERS = 50


def filter_offers(offers, cfg):
    result = []
    has_range = cfg["amount_min"] > 0 or cfg["amount_max"] > 0
    for ad in (offers or []):
        if ad.get("orderNum", 0) < MIN_ORDERS:
            continue
        if has_range:
            if not ad_matches(ad, cfg):
                continue
        else:
            er = ad.get("executeRate", "0")
            if er != "—" and float(er) * 100 < cfg["rating"]:
                continue
        result.append(ad)
    result.sort(key=lambda a: float(a.get("price", 0)))
    return result[:SHOW_N]


def check_new_leader(crypto, filtered):
    """Проверяет, появился ли новый лидер. Возвращает ad_id если мигание активно."""
    global best_leaders
    if not filtered:
        return None

    top = filtered[0]
    top_price = float(top.get("price", 0))
    top_id = top.get("id", "")
    now = time.time()

    prev = best_leaders.get(crypto)

    if prev is None:
        # первый запуск — запомнить без мигания
        best_leaders[crypto] = {"price": top_price, "id": top_id, "since": 0}
        return None

    if top_price < prev["price"] or (top_price == prev["price"] and top_id != prev["id"]):
        # новый лидер — дешевле или другое объявление по той же цене
        if top_id != prev.get("id") or top_price < prev["price"]:
            best_leaders[crypto] = {"price": top_price, "id": top_id, "since": now}
            return top_id

    # обновить цену если лидер тот же (цена могла измениться)
    if top_id == prev.get("id"):
        prev["price"] = top_price

    # мигание ещё активно?
    if now - prev.get("since", 0) < FLASH_DURATION:
        return prev.get("id")

    return None


def build_pair_table(pair, offers, market_price, cfg, width):
    crypto = pair["crypto"]
    filtered = filter_offers(offers, cfg)
    flash_id = check_new_leader(crypto, filtered)

    cap_parts = []
    if market_price > 0:
        src = " (%s)" % RATE_SOURCE if RATE_SOURCE else ""
        cap_parts.append("рынок %.2f ₽%s" % (market_price, src))
    else:
        cap_parts.append("рынок —")
    cap_parts.append("показано %d / %d" % (len(filtered), len(offers or [])))

    table = Table(
        title=" %s / RUB " % crypto,
        title_style="bold white on dark_blue",
        caption="  ".join(cap_parts),
        caption_style="dim italic",
        box=box.HEAVY_EDGE,
        border_style="blue",
        width=width,
        show_lines=False,
        padding=(0, 1),
    )

    table.add_column("#",      justify="right", style="dim",        width=2,  no_wrap=True, ratio=0)
    table.add_column("Цена",  justify="right", style="bold white", width=8,  no_wrap=True, ratio=0)
    table.add_column("Откл.", justify="right",                     width=8,  no_wrap=True, ratio=0)
    table.add_column("Лимит", justify="center", style="white",    width=21, no_wrap=True, ratio=0)
    table.add_column("Мейкер",justify="left",                      width=16, no_wrap=True, ratio=0)
    table.add_column("Сд",    justify="right", style="dim",        width=4,  no_wrap=True, ratio=0)
    table.add_column("Рт",    justify="right",                     width=4,  no_wrap=True, ratio=0)
    table.add_column("Оплата",justify="left",  style="magenta",                             ratio=1)

    if not filtered:
        table.add_row("", "", "", Text("нет подходящих", style="dim italic"),
                      "", "", "", "")
        return table

    for i, ad in enumerate(filtered, 1):
        price = float(ad.get("price", 0))
        lo = float(ad.get("minAmount", 0))
        hi = float(ad.get("maxAmount", 0))
        nick = ad.get("nickname", "—")[:14]
        pays = ", ".join(fmt_pay(p) for p in ad.get("payments", [])) or "—"
        orders = str(ad.get("orderNum", "—"))
        er = ad.get("executeRate", "—")
        online = ad.get("isOnline", False)
        pct = dev_pct(price, market_price)
        limit_str = "%s – %s" % (fmt_n(lo), fmt_n(hi))

        # мигание нового лидера
        is_flashing = (flash_id and ad.get("id") == flash_id)

        if is_flashing:
            # мигающий зелёный фон на отклонении
            pct_text = dev_text(pct)
            pct_str = pct_text.plain
            pct_cell = Text(pct_str, style="blink bold white on green")
        else:
            pct_cell = dev_text(pct)

        if online:
            nick_t = Text("● ", style="green") + Text(nick, style="bold cyan")
        else:
            nick_t = Text("○ ", style="dim") + Text(nick, style="dim")

        row_style = "on #002200" if is_flashing else ""

        table.add_row(
            str(i), "%.2f" % price, pct_cell,
            limit_str, nick_t, orders, rating_text(er), pays,
            style=row_style,
        )

    return table


def render_display():
    with state_lock:
        cfg = dict(config)
        mkt = dict(market_rates)
        offers = dict(all_offers)
        err = error_msg

    try:
        w = os.get_terminal_size().columns
    except OSError:
        w = 100

    rows = []

    now = datetime.now().strftime("%H:%M:%S")
    hdr = Text()
    hdr.append("  WALLET.TG  P2P  MONITOR  ", style="bold white on blue")
    hdr.append("  %s" % now, style="bold white")
    rows.append(hdr)

    # текущий диапазон
    f = Text()
    f.append("  Диапазон: ", style="bold")
    mn = cfg["amount_min"]
    mx = cfg["amount_max"]
    if mn > 0 or mx > 0:
        if mn > 0:
            f.append("%s" % fmt_n(mn), style="bold yellow")
        else:
            f.append("0", style="dim")
        f.append(" – ", style="dim")
        if mx > 0:
            f.append("%s ₽" % fmt_n(mx), style="bold yellow")
        else:
            f.append("∞", style="dim")
    else:
        f.append("любой", style="dim italic")
    f.append("    рейтинг ", style="dim")
    f.append(">= %.0f%%" % cfg["rating"], style="bold yellow")
    rows.append(f)
    rows.append(Text(""))

    for pair in PAIRS:
        mp = mkt.get(pair["cg_id"], 0)
        pair_offers = offers.get(pair["crypto"])
        rows.append(build_pair_table(pair, pair_offers, mp, cfg, w))

    if err:
        rows.append(Text("  " + err, style="bold red"))

    rows.append(Text(""))

    leg = Text()
    leg.append("  ● онлайн  ", style="green")
    leg.append("○ офлайн    ", style="dim")
    leg.append("Откл: ", style="dim")
    leg.append(" <1% ", style="bold green")
    leg.append(" 1-3% ", style="yellow")
    leg.append(" 3-5% ", style="dark_orange")
    leg.append(" >5% ", style="bold red")
    rows.append(leg)

    buf = StringIO()
    console = Console(file=buf, width=w, force_terminal=True, color_system="truecolor")
    console.print(Group(*rows))
    return buf.getvalue()


def render_fields_panel():
    """Панель из 3 полей: min, max, rating."""
    with state_lock:
        cfg = dict(config)
        act = active_field

    labels = [
        ("MIN сумма ₽", cfg["amount_min"]),
        ("MAX сумма ₽", cfg["amount_max"]),
        ("Рейтинг %%",  cfg["rating"]),
    ]

    lines = []
    for i, (label, val) in enumerate(labels):
        is_active = (i == act)
        if is_active:
            prefix = "\033[1;33m ▶ \033[0m"
        else:
            prefix = "\033[2m   \033[0m"

        if i < 2:
            # суммы
            if val > 0:
                val_s = "\033[1m%s\033[0m" % fmt_n(val)
            else:
                val_s = "\033[2m—\033[0m"
        else:
            # рейтинг
            val_s = "\033[1m>= %.0f%%\033[0m" % val

        lines.append("%s%-14s  %s" % (prefix, label, val_s))

    return "\n".join(lines)


# ─── prompt_toolkit UI ────────────────────────────────────────

def get_display_text():
    return ANSI(render_display())

def get_fields_text():
    return ANSI(render_fields_panel())

def get_hint_text():
    return ANSI(
        "\033[2m ↑↓ поле  │  "
        "число + Enter = задать  │  "
        "0 = сброс  │  "
        "Ctrl+C = выход\033[0m"
    )


def apply_input(text):
    global config
    line = text.strip().lower()
    if not line:
        return

    with state_lock:
        field = FIELD_NAMES[active_field]

        if line in ("0", "off", "reset", "-"):
            if field == "rating":
                config["rating"] = 98
            else:
                config[field] = 0
        else:
            try:
                val = float(line.replace(" ", "").replace(",", ""))
                if field == "rating":
                    if 0 <= val <= 100:
                        config["rating"] = val
                elif val >= 0:
                    config[field] = val
            except ValueError:
                pass

        save_config(config)


def run():
    global active_field, app_ref

    input_buffer = Buffer(
        multiline=False,
        accept_handler=lambda buf: apply_input(buf.text),
    )

    root = HSplit([
        Window(content=FormattedTextControl(get_display_text), wrap_lines=False),
        Window(
            content=FormattedTextControl(lambda: ANSI(
                "\033[34m" + "─" * 80 + "\033[0m"
            )),
            height=1,
        ),
        Window(
            content=FormattedTextControl(get_fields_text),
            height=FIELD_COUNT,
        ),
        Window(content=FormattedTextControl(get_hint_text), height=1),
        Window(
            content=BufferControl(buffer=input_buffer),
            height=1,
            style="bg:#1a1a2e fg:#ffffff bold",
        ),
    ])

    kb = KeyBindings()

    @kb.add("c-c")
    @kb.add("c-q")
    def exit_(event):
        event.app.exit()

    @kb.add("up")
    def prev_field(event):
        global active_field
        with state_lock:
            active_field = (active_field - 1) % FIELD_COUNT
        event.app.invalidate()

    @kb.add("down")
    def next_field(event):
        global active_field
        with state_lock:
            active_field = (active_field + 1) % FIELD_COUNT
        event.app.invalidate()

    app = Application(
        layout=Layout(root, focused_element=input_buffer),
        key_bindings=kb,
        full_screen=True,
        mouse_support=False,
    )

    app_ref = app

    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()

    def tick_refresh():
        while True:
            time.sleep(1)
            try:
                app.invalidate()
            except Exception:
                break

    t2 = threading.Thread(target=tick_refresh, daemon=True)
    t2.start()

    app.run()


if __name__ == "__main__":
    run()
