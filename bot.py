"""
Telegram-бот: остатки ПФЦ Wildberries по баркоду.

Селлер отправляет баркод -> бот спрашивает у my-pfc.wb.ru остатки и отвечает.
Владелец ПФЦ (ADMIN_IDS) входит в кабинет командой /login (SMS-код), бот
сам хранит и продлевает токен.
"""
import asyncio
import base64
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

# ---------- настройки ----------
BOT_TOKEN = (os.environ.get("BOT_TOKEN") or os.environ.get("TOKEN")
             or os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN"))
if not BOT_TOKEN:
    raise SystemExit("Не задан BOT_TOKEN")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
ADMIN_USERNAMES = {x.strip().lstrip("@").lower() for x in os.environ.get("ADMIN_USERNAMES", "").split(",") if x.strip()}
PHONE = os.environ.get("PFC_PHONE", "").lstrip("+")
STORAGE_ID = os.environ.get("STORAGE_ID", "")
STATE_FILE = Path(os.environ.get("STATE_FILE") or Path(__file__).with_name("state.json"))
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

AUTH_BASE = "https://r-point.wb.ru/auth-api/v4/pvz"
STOCK_URL = "https://darkstore.wb.ru/crowd-storage/s1/api/v1/stock/cells"
COMMON_HEADERS = {
    "Content-Type": "application/json",
    "X-App-Type": "prod-my-pfc",
    "X-Client-Id": "my-pfc",
    "X-Language": "ru",
    "Origin": "https://my-pfc.wb.ru",
    "Referer": "https://my-pfc.wb.ru/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pfc_bot")


# ---------- хранение токенов ----------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(st: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(STATE_FILE)


store = load_state()
store.setdefault("device_uuid", str(uuid.uuid4()))
ADMIN_IDS.update(store.get("admin_ids", []))
save_state(store)


def jwt_payload(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def extract_tokens(data: dict) -> tuple[str | None, str | None]:
    """Ответы auth-api бывают вида {access:{token}, refresh:{token}}."""
    if not isinstance(data, dict):
        return None, None
    acc = data.get("access")
    ref = data.get("refresh")
    a = acc.get("token") if isinstance(acc, dict) else data.get("access_token") or data.get("token")
    r = ref.get("token") if isinstance(ref, dict) else data.get("refresh_token")
    return a, r


class AuthError(Exception):
    pass


# ---------- клиент кабинета ----------
class PfcClient:
    def __init__(self):
        self.http = httpx.AsyncClient(timeout=20, headers=COMMON_HEADERS)
        self.lock = asyncio.Lock()

    async def _post(self, path: str, body: dict, token: str | None = None) -> dict:
        headers = {"X-App-Version": "v0.0.7"}
        if token:
            headers["X-Token"] = token
        r = await self.http.post(f"{AUTH_BASE}/{path}", json=body, headers=headers)
        log.info("auth %s -> %s", path, r.status_code)
        if r.status_code >= 400:
            raise AuthError(f"{path}: HTTP {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}

    # шаг 1: отправить SMS
    async def send_code(self) -> None:
        data = await self._post("login", {
            "country": "RU", "device_uuid": store["device_uuid"], "phone": int(PHONE)})
        store["login_token"] = data.get("token")
        save_state(store)

    # шаг 2: подтвердить код, выбрать организацию
    async def confirm_code(self, code: str) -> str:
        data = await self._post("validate", {
            "app_version": "v0.0.7", "code": code,
            "device_uuid": store["device_uuid"], "token": store.get("login_token")})
        access, refresh = extract_tokens(data)
        if not access:
            raise AuthError(f"validate: нет токена в ответе: {str(data)[:300]}")
        store.update(access=access, refresh=refresh)
        save_state(store)
        return await self._select_org()

    async def _select_org(self) -> str:
        r = await self.http.get(f"{AUTH_BASE}/my-orgs",
                                headers={"X-Token": store["access"], "X-App-Version": "v0.0.7"})
        orgs = r.json() if r.status_code == 200 else []
        log.info("my-orgs -> %s, %d org(s)", r.status_code, len(orgs or []))
        if not orgs:
            return "организация не выбрана (список пуст)"
        want = store.get("org_id")
        org = next((o for o in orgs if o.get("id") == want), orgs[0])
        data = await self._post("enrich", {"org_id": org["id"], "position": org.get("position")},
                                token=store["access"])
        access, refresh = extract_tokens(data)
        if access:
            store.update(access=access, refresh=refresh or store.get("refresh"),
                         org_id=org["id"], org_position=org.get("position"))
            save_state(store)
        return org.get("org_name") or str(org["id"])

    async def refresh(self) -> None:
        if not store.get("refresh"):
            raise AuthError("нет refresh-токена, нужен /login")
        data = await self._post("refresh", {"token": store["refresh"]})
        access, refresh = extract_tokens(data)
        if not access:
            raise AuthError(f"refresh: нет токена в ответе: {str(data)[:300]}")
        store.update(access=access, refresh=refresh or store["refresh"])
        save_state(store)
        if store.get("org_id"):
            data = await self._post("enrich", {"org_id": store["org_id"],
                                               "position": store.get("org_position")},
                                    token=store["access"])
            a2, r2 = extract_tokens(data)
            if a2:
                store.update(access=a2, refresh=r2 or store["refresh"])
                save_state(store)
        log.info("token refreshed, exp=%s", jwt_payload(store["access"]).get("exp"))

    async def ensure_token(self) -> str:
        async with self.lock:
            tok = store.get("access")
            if not tok:
                raise AuthError("бот не авторизован, нужен /login")
            if jwt_payload(tok).get("exp", 0) - time.time() < 3600:
                await self.refresh()
            return store["access"]

    async def stock(self, barcode: str) -> dict | None:
        for attempt in range(2):
            tok = await self.ensure_token()
            r = await self.http.get(
                STOCK_URL, params={"barcode": barcode, "storage_id": STORAGE_ID},
                headers={"X-Token": tok, "X-App-Version": "v0.0.3"})
            if r.status_code == 401 and attempt == 0:
                async with self.lock:
                    await self.refresh()
                continue
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            log.info("stock %s raw: %s", barcode, r.text[:1500])
            return r.json()
        raise AuthError("не удалось обновить вход")


pfc = PfcClient()


# ---------- форматирование ответа ----------
def pick(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def format_stock(barcode: str, data: dict | None) -> str:
    if not data:
        return f"По баркоду <code>{barcode}</code> на складе ничего не найдено."
    card = data.get("card") or {}
    cells = data.get("cells") or []
    if not card and not cells:
        return f"По баркоду <code>{barcode}</code> на складе ничего не найдено."
    total = pick(data, "total", default=None)
    if total is None:
        total = sum(int(pick(c, "quantity", "count", "qty", default=0) or 0) for c in cells)
    name = pick(card, "name", "goods_name", "title", "subject_name", default="Товар")
    nm = pick(card, "nm_id", "nmId", "nm", "article")
    lines = [f"<b>{name}</b>"]
    if nm:
        lines.append(f'Артикул WB: <a href="https://www.wildberries.ru/catalog/{nm}/detail.aspx">{nm}</a>')
    lines.append(f"Баркод: <code>{barcode}</code>")
    lines.append(f"\n<b>Остаток: {total} шт</b> (мест хранения: {len(cells)})")
    for c in cells:
        code = pick(c, "cell_code", "cellCode", "code", "name", default="—")
        qty = pick(c, "quantity", "count", "qty", default="?")
        lines.append(f"• МХ {code} — {qty} шт")
    return "\n".join(lines)


# ---------- Telegram ----------
dp = Dispatcher()
BARCODE_RE = re.compile(r"^\d{6,20}$")
last_query: dict[int, float] = {}


class Login(StatesGroup):
    code = State()


def is_admin(m: Message) -> bool:
    u = m.from_user
    if not u:
        return False
    if u.id in ADMIN_IDS:
        return True
    if u.username and u.username.lower() in ADMIN_USERNAMES:
        # запоминаем числовой ID, чтобы присылать уведомления
        ids = set(store.get("admin_ids", []))
        if u.id not in ids:
            ids.add(u.id)
            store["admin_ids"] = sorted(ids)
            save_state(store)
        ADMIN_IDS.add(u.id)
        return True
    return False


@dp.message(CommandStart())
async def start(m: Message):
    is_admin(m)
    log.info("/start from id=%s @%s", m.from_user.id, m.from_user.username)
    await m.answer("Здравствуйте! Отправьте баркод товара — я покажу, сколько штук "
                   "лежит на ПФЦ (Тольятти, Новозаводская, 25В).")


@dp.message(Command("myid"))
async def myid(m: Message):
    await m.answer(f"Ваш Telegram ID: <code>{m.from_user.id}</code>")


@dp.message(Command("login"))
async def login(m: Message, state: FSMContext):
    if not is_admin(m):
        return
    try:
        await pfc.send_code()
    except Exception as e:
        await m.answer(f"Не удалось запросить SMS: {e}")
        return
    await state.set_state(Login.code)
    await m.answer(f"SMS с кодом отправлена на +{PHONE}. Пришлите код (6 цифр).")


@dp.message(Login.code, F.text.regexp(r"^\s*\d{4,6}\s*$"))
async def login_code(m: Message, state: FSMContext):
    if not is_admin(m):
        return
    await state.clear()
    try:
        org = await pfc.confirm_code(m.text.strip())
    except Exception as e:
        await m.answer(f"Вход не удался: {e}\nПопробуйте /login ещё раз.")
        return
    exp = jwt_payload(store["access"]).get("exp")
    await m.answer(f"Готово, вход выполнен ({org}). Токен до "
                   f"{time.strftime('%d.%m %H:%M', time.localtime(exp)) if exp else '?'}, "
                   "дальше бот продлевает его сам.")


@dp.message(Command("status"))
async def status(m: Message):
    if not is_admin(m):
        return
    tok = store.get("access")
    if not tok:
        await m.answer("Не авторизован. Сделайте /login.")
        return
    exp = jwt_payload(tok).get("exp", 0)
    await m.answer(f"Авторизован. org_id={store.get('org_id')}, токен до "
                   f"{time.strftime('%d.%m %H:%M', time.localtime(exp))}.")


@dp.message(F.text)
async def barcode(m: Message, bot: Bot):
    text = re.sub(r"\s+", "", m.text or "")
    if not BARCODE_RE.match(text):
        await m.answer("Пришлите баркод — только цифры, например <code>2051341177454</code>.")
        return
    now = time.time()
    if now - last_query.get(m.from_user.id, 0) < 2:
        return
    last_query[m.from_user.id] = now
    log.info("query %s from id=%s @%s", text, m.from_user.id, m.from_user.username)
    try:
        data = await pfc.stock(text)
        await m.answer(format_stock(text, data), disable_web_page_preview=True)
    except AuthError as e:
        log.warning("auth problem: %s", e)
        await m.answer("Сервис временно недоступен, попробуйте позже.")
        for a in ADMIN_IDS:
            try:
                await bot.send_message(a, f"⚠️ Бот потерял вход в кабинет ({e}). Сделайте /login.")
            except Exception:
                pass
    except Exception as e:
        log.exception("stock error")
        await m.answer("Не получилось получить остатки, попробуйте ещё раз через минуту.")


async def keepalive(bot: Bot):
    """Раз в 30 минут продлевает токен, чтобы он не протух в тишине."""
    warned = False
    while True:
        await asyncio.sleep(1800)
        if not store.get("access"):
            continue
        try:
            await pfc.ensure_token()
            warned = False
        except Exception as e:
            log.warning("keepalive: %s", e)
            if not warned:
                warned = True
                for a in ADMIN_IDS:
                    try:
                        await bot.send_message(a, f"⚠️ Не удалось продлить вход ({e}). Сделайте /login.")
                    except Exception:
                        pass


async def main():
    from aiogram.client.default import DefaultBotProperties
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    asyncio.create_task(keepalive(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
