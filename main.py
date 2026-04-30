import discord
from discord.ext import commands, tasks
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus
import re
import asyncio
import shlex
import json
import os
import time

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

LOCATION_ZIP = "40313"
RADIUS_KM = "60"

USERS_FILE = "users.json"
FLIPS_FILE = "flips.json"

CHECK_EVERY_MINUTES = 5

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

DEFAULT_BLACKLIST = [
    "prodáno", "prodano", "rezervace", "rezervováno", "rezervovano",
    "nefunkční", "nefunkcni", "nejde", "vadný", "vadny",
    "poškozené", "poskozene", "poškozený", "poskozeny",
    "na díly", "na dily", "oprava", "rozbitý", "rozbity",
    "jen krabice", "bez dongle", "bez usb", "bez přijímače", "bez prijimace",
    "nabíječka", "nabijecka", "wallet", "peněženka", "penezenka",
    "řemínek", "reminek", "obal", "kryt", "pouzdro",
    "držák", "drzak", "adaptér", "adapter", "kabel",
]


# -------------------------
# JSON HELPERS
# -------------------------

async def update_user_role(ctx, total_profit):
    guild = ctx.guild
    if guild is None:
        return

    roles_map = [
        (20000, "👑 Shark"),
        (10000, "💎 Profit Boss"),
        (5000, "🔥 Flipper"),
        (1000, "⚡ Deal Hunter"),
        (0, "🌱 Rookie"),
    ]

    member = ctx.author

    target_role_name = None
    for threshold, role_name in roles_map:
        if total_profit >= threshold:
            target_role_name = role_name
            break

    if not target_role_name:
        return

    role = discord.utils.get(guild.roles, name=target_role_name)

    if role is None:
        return

    # odeber staré role
    for _, role_name in roles_map:
        r = discord.utils.get(guild.roles, name=role_name)
        if r in member.roles and r != role:
            await member.remove_roles(r)

    # přidej novou
    if role not in member.roles:
        await member.add_roles(role)

def load_json(path, default):
    if not os.path.exists(path):
        save_json(path, default)
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, type(default)):
            save_json(path, default)
            return default

        return data
    except Exception:
        save_json(path, default)
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# -------------------------
# USER SETTINGS
# -------------------------

def load_users():
    return load_json(USERS_FILE, {})


def save_users(data):
    save_json(USERS_FILE, data)


def get_user_id(ctx):
    return str(ctx.author.id)


def get_user_name(ctx):
    return ctx.author.display_name


def ensure_user_settings(user_id, username):
    users = load_users()

    if user_id not in users:
        users[user_id] = {
            "username": username,
            "watchlist": [],
            "blacklist": DEFAULT_BLACKLIST.copy(),
            "seen": []
        }

    users[user_id]["username"] = username

    if "watchlist" not in users[user_id] or not isinstance(users[user_id]["watchlist"], list):
        users[user_id]["watchlist"] = []

    if "blacklist" not in users[user_id] or not isinstance(users[user_id]["blacklist"], list):
        users[user_id]["blacklist"] = DEFAULT_BLACKLIST.copy()

    if "seen" not in users[user_id] or not isinstance(users[user_id]["seen"], list):
        users[user_id]["seen"] = []

    save_users(users)
    return users[user_id]


# -------------------------
# BAZOS HELPERS
# -------------------------

def extract_price(text):
    text = text.replace("\xa0", " ")
    match = re.search(r"(\d[\d ]*)\s*Kč", text)
    if not match:
        return None
    return int(match.group(1).replace(" ", ""))


def normalize(text):
    return text.lower().strip()


def contains_blocked_words(text, blacklist):
    text_l = normalize(text)
    return any(word.lower() in text_l for word in blacklist)


def title_matches(title, keyword):
    title_l = normalize(title)
    keyword_l = normalize(keyword)

    words = [w for w in keyword_l.split() if len(w) > 1]
    matched = sum(1 for w in words if w in title_l)

    return matched >= max(2, len(words) - 1)


def get_deal_score(price, safe_sell_price, profit, min_profit):
    profit_score = min(5, profit / max(min_profit, 1) * 2)
    margin = profit / max(price, 1)
    margin_score = min(3, margin * 4)
    cheap_score = 2 if price <= safe_sell_price * 0.65 else 1

    score = profit_score + margin_score + cheap_score
    return round(min(score, 10), 1)


def search_bazos(keyword, max_price, blacklist, limit=50):
    url = (
        f"https://www.bazos.cz/search.php?"
        f"hledat={quote_plus(keyword)}"
        f"&rubriky=www"
        f"&hlokalita={LOCATION_ZIP}"
        f"&humkreis={RADIUS_KM}"
        f"&cenaod="
        f"&cenado={max_price}"
        f"&order="
    )

    headers = {"User-Agent": "Mozilla/5.0"}

    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    ads = soup.select(".inzeraty")

    results = []

    for ad in ads[:limit]:
        title_tag = ad.select_one(".nadpis a")
        if not title_tag:
            continue

        title = title_tag.get_text(strip=True)
        link = title_tag.get("href")

        if not link:
            continue

        if link.startswith("/"):
            link = "https://www.bazos.cz" + link

        text = ad.get_text(" ", strip=True)
        price = extract_price(text)

        desc_tag = ad.select_one(".popis")
        description = desc_tag.get_text(" ", strip=True) if desc_tag else ""

        location_tag = ad.select_one(".lokalita")
        location = location_tag.get_text(" ", strip=True) if location_tag else "Neznámá lokalita"

        full_text = f"{title} {description}"

        if contains_blocked_words(full_text, blacklist):
            continue

        if not title_matches(title, keyword):
            continue

        if price is None or price > max_price:
            continue

        results.append({
            "title": title,
            "price": price,
            "description": description[:700],
            "location": location,
            "link": link
        })

    return results


def filter_good_deals(ads, expected_sell_price, min_profit):
    safe_sell_price = int(expected_sell_price * 0.9)
    good_deals = []

    for ad in ads:
        profit = safe_sell_price - ad["price"]

        if profit >= min_profit:
            score = get_deal_score(ad["price"], safe_sell_price, profit, min_profit)
            good_deals.append({
                **ad,
                "profit": profit,
                "score": score,
                "safe_sell_price": safe_sell_price
            })

    good_deals.sort(key=lambda x: x["score"], reverse=True)
    return good_deals


async def send_deal(channel, deal):
    message = (
        f"🔥 **DEAL {deal['score']}/10**\n\n"
        f"**{deal['title']}**\n"
        f"Cena: **{deal['price']} Kč**\n"
        f"Bezpečný profit: **{deal['profit']} Kč**\n"
        f"Odhad bezpečný prodej: **{deal['safe_sell_price']} Kč**\n"
        f"Lokalita: **{deal['location']}**\n\n"
        f"**Popisek:**\n{deal['description'] or 'Bez popisku'}\n\n"
        f"{deal['link']}"
    )

    await channel.send(message)


# -------------------------
# FLIPPING GAME
# -------------------------

def load_flips():
    return load_json(FLIPS_FILE, {})


def save_flips(data):
    save_json(FLIPS_FILE, data)


def ensure_flip_user(data, user_id, username):
    if user_id not in data:
        data[user_id] = {
            "username": username,
            "items": []
        }

    data[user_id]["username"] = username

    if "items" not in data[user_id] or not isinstance(data[user_id]["items"], list):
        data[user_id]["items"] = []

    return data[user_id]


def get_level(total_profit):
    if total_profit >= 20000:
        return "👑 Ústecký Shark"
    if total_profit >= 10000:
        return "💎 Profit Boss"
    if total_profit >= 5000:
        return "🔥 Marketplace Demon"
    if total_profit >= 1000:
        return "⚡ Deal Hunter"
    return "🌱 Rookie Flipper"


def get_user_stats(user):
    items = user.get("items", [])

    sold_items = [item for item in items if item.get("status") == "sold"]
    holding_items = [item for item in items if item.get("status") == "holding"]

    total_spent = sum(item.get("buy_price", 0) for item in items)
    money_in_stock = sum(item.get("buy_price", 0) for item in holding_items)
    total_revenue = sum(item.get("sell_price", 0) or 0 for item in sold_items)
    total_profit = sum(item.get("profit", 0) or 0 for item in sold_items)

    avg_profit = round(total_profit / len(sold_items), 1) if sold_items else 0
    win_flips = [item for item in sold_items if (item.get("profit", 0) or 0) > 0]
    win_rate = round((len(win_flips) / len(sold_items)) * 100, 1) if sold_items else 0

    best_flip = max(sold_items, key=lambda x: x.get("profit", 0) or 0, default=None)
    worst_flip = min(sold_items, key=lambda x: x.get("profit", 0) or 0, default=None)

    return {
        "total_spent": total_spent,
        "money_in_stock": money_in_stock,
        "total_revenue": total_revenue,
        "total_profit": total_profit,
        "avg_profit": avg_profit,
        "win_rate": win_rate,
        "bought_count": len(items),
        "sold_count": len(sold_items),
        "holding_count": len(holding_items),
        "best_flip": best_flip,
        "worst_flip": worst_flip,
        "level": get_level(total_profit)
    }


def find_holding_item(user, query):
    query_l = normalize(query)
    holding = [item for item in user.get("items", []) if item.get("status") == "holding"]

    exact = [item for item in holding if normalize(item.get("name", "")) == query_l]
    if exact:
        return exact[0]

    partial = [item for item in holding if query_l in normalize(item.get("name", ""))]
    if partial:
        return partial[0]

    return None


# -------------------------
# EVENTS
# -------------------------

@bot.event
async def on_ready():
    print(f"Bot běží jako {bot.user}")

    load_json(USERS_FILE, {})
    load_json(FLIPS_FILE, {})

    if not auto_scan.is_running():
        auto_scan.start()


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    print("ZPRÁVA:", message.content)
    await bot.process_commands(message)


# -------------------------
# BASIC COMMANDS
# -------------------------

@bot.command()
async def test(ctx):
    await ctx.send("funguje")


@bot.command()
async def helpbot(ctx):
    await ctx.send(
        "**📘 Příkazy bota**\n\n"
        "**Bazoš:**\n"
        '`!find "název" max_koupě prodej min_profit`\n'
        '`!watch "název" max_koupě prodej min_profit`\n'
        "`!watchlist`\n"
        "`!unwatch číslo`\n\n"
        "**Personal settings:**\n"
        "`!blacklist`\n"
        "`!blacklist add slovo`\n"
        "`!blacklist remove slovo`\n\n"
        "**Flipping game:**\n"
        '`!buy "název itemu" cena`\n'
        '`!sell "název itemu" cena`\n'
        "`!inventory`\n"
        "`!stats`\n"
        "`!leaderboard`\n"
        "`!history`\n\n"
        "**Ostatní:**\n"
        "`!profit koupě prodej`\n"
        "`!msg cena`\n"
        "`!clear 50`"
    )


# -------------------------
# BAZOS COMMANDS
# -------------------------

@bot.command()
async def find(ctx, *, args):
    try:
        parts = shlex.split(args)

        if len(parts) < 4:
            await ctx.send(
                "Použití:\n"
                '`!find "logitech g pro superlight" 1200 1700 400`\n\n'
                "1200 = max koupit\n"
                "1700 = za kolik to reálně prodáš\n"
                "400 = minimální profit"
            )
            return

        keyword = parts[0]
        max_buy_price = int(parts[1])
        expected_sell_price = int(parts[2])
        min_profit = int(parts[3])

        user_settings = ensure_user_settings(get_user_id(ctx), get_user_name(ctx))
        blacklist = user_settings["blacklist"]

        await ctx.send(f"🔎 Hledám pro **{ctx.author.display_name}**: **{keyword}**")

        ads = await asyncio.to_thread(search_bazos, keyword, max_buy_price, blacklist)
        good_deals = filter_good_deals(ads, expected_sell_price, min_profit)

        if not good_deals:
            await ctx.send("Nic dobrého jsem nenašel.")
            return

        await ctx.send(f"🔥 Našel jsem **{len(good_deals)}** dealů. Posílám po jednom:")

        for deal in good_deals[:10]:
            await send_deal(ctx.channel, deal)
            await asyncio.sleep(1)

    except Exception as e:
        await ctx.send(f"Chyba: `{e}`")


@bot.command()
async def watch(ctx, *, args):
    try:
        parts = shlex.split(args)

        if len(parts) < 4:
            await ctx.send(
                "Použití:\n"
                '`!watch "logitech g pro superlight" 1200 1700 400`'
            )
            return

        keyword = parts[0]
        max_buy_price = int(parts[1])
        expected_sell_price = int(parts[2])
        min_profit = int(parts[3])

        users = load_users()
        user_id = get_user_id(ctx)
        user_settings = ensure_user_settings(user_id, get_user_name(ctx))

        item = {
            "keyword": keyword,
            "max_buy_price": max_buy_price,
            "expected_sell_price": expected_sell_price,
            "min_profit": min_profit,
            "channel_id": ctx.channel.id,
            "created_at": int(time.time())
        }

        user_settings["watchlist"].append(item)
        users[user_id] = user_settings
        save_users(users)

        await ctx.send(
            f"✅ Přidáno do **tvého** watchlistu:\n"
            f"**{keyword}** | max {max_buy_price} Kč | prodej {expected_sell_price} Kč | min profit {min_profit} Kč"
        )

    except Exception as e:
        await ctx.send(f"Chyba: `{e}`")


@bot.command()
async def unwatch(ctx, index: int):
    users = load_users()
    user_id = get_user_id(ctx)
    user_settings = ensure_user_settings(user_id, get_user_name(ctx))

    watchlist = user_settings["watchlist"]

    if index < 1 or index > len(watchlist):
        await ctx.send("Špatné číslo. Použij `!watchlist`.")
        return

    removed = watchlist.pop(index - 1)

    user_settings["watchlist"] = watchlist
    users[user_id] = user_settings
    save_users(users)

    await ctx.send(f"🗑️ Odebráno z **tvého** watchlistu: **{removed['keyword']}**")


@bot.command()
async def watchlist(ctx):
    user_settings = ensure_user_settings(get_user_id(ctx), get_user_name(ctx))
    watchlist = user_settings["watchlist"]

    if not watchlist:
        await ctx.send("Tvůj watchlist je prázdný.")
        return

    msg = f"📋 **Watchlist — {ctx.author.display_name}**\n\n"

    for i, item in enumerate(watchlist, start=1):
        msg += (
            f"`{i}` — **{item['keyword']}**\n"
            f"Max koupě: {item['max_buy_price']} Kč | "
            f"Prodej: {item['expected_sell_price']} Kč | "
            f"Min profit: {item['min_profit']} Kč\n\n"
        )

    await ctx.send(msg)


@bot.command()
async def blacklist(ctx, action=None, *, word=None):
    users = load_users()
    user_id = get_user_id(ctx)
    user_settings = ensure_user_settings(user_id, get_user_name(ctx))
    words = user_settings["blacklist"]

    if action == "add" and word:
        word = word.lower().strip()

        if word not in words:
            words.append(word)

        user_settings["blacklist"] = words
        users[user_id] = user_settings
        save_users(users)

        await ctx.send(f"✅ Přidáno do **tvého** blacklistu: **{word}**")
        return

    if action == "remove" and word:
        word = word.lower().strip()

        if word in words:
            words.remove(word)

        user_settings["blacklist"] = words
        users[user_id] = user_settings
        save_users(users)

        await ctx.send(f"🗑️ Odebráno z **tvého** blacklistu: **{word}**")
        return

    msg = f"🚫 **Blacklist — {ctx.author.display_name}**\n"
    msg += ", ".join(words[:80])

    if len(words) > 80:
        msg += f"\n...a dalších {len(words) - 80}"

    await ctx.send(msg)


# -------------------------
# TOOL COMMANDS
# -------------------------

@bot.command()
async def profit(ctx, buy_price: int, sell_price: int):
    safe_sell = int(sell_price * 0.9)
    clean_profit = safe_sell - buy_price
    margin = round((clean_profit / buy_price) * 100, 1) if buy_price > 0 else 0

    await ctx.send(
        f"💰 **Profit kalkulačka**\n\n"
        f"Koupě: **{buy_price} Kč**\n"
        f"Odhad prodej: **{sell_price} Kč**\n"
        f"Bezpečný prodej po rezervě: **{safe_sell} Kč**\n"
        f"Čistý bezpečný profit: **{clean_profit} Kč**\n"
        f"Margin: **{margin}%**"
    )


@bot.command()
async def msg(ctx, price: int):
    await ctx.send(
        f"✉️ **Zpráva pro prodejce:**\n\n"
        f"Dobrý den, měl bych zájem. Bylo by možné to nechat za {price} Kč? "
        f"Můžu přijet osobně a vyzvednout co nejdřív."
    )


@bot.command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx, amount: int = 100):
    await ctx.channel.purge(limit=amount)


@bot.command()
@commands.has_permissions(manage_messages=True)
async def clearall(ctx):
    deleted = 0

    while True:
        msgs = await ctx.channel.purge(limit=100)
        deleted += len(msgs)

        if len(msgs) < 100:
            break

        await asyncio.sleep(1)

    await ctx.send(f"Smazáno {deleted} zpráv.", delete_after=3)


# -------------------------
# FLIPPING GAME COMMANDS
# -------------------------

@bot.command()
async def buy(ctx, *, args):
    try:
        parts = shlex.split(args)

        if len(parts) < 2:
            await ctx.send('Použití: `!buy "Logitech Superlight" 1000`')
            return

        name = parts[0]
        buy_price = int(parts[1])

        data = load_flips()
        user = ensure_flip_user(data, get_user_id(ctx), get_user_name(ctx))

        item = {
            "name": name,
            "buy_price": buy_price,
            "sell_price": None,
            "profit": None,
            "status": "holding",
            "bought_at": int(time.time()),
            "sold_at": None
        }

        user["items"].append(item)
        save_flips(data)

        await ctx.send(
            f"📦 **Koupeno!**\n"
            f"Item: **{name}**\n"
            f"Cena: **{buy_price} Kč**\n\n"
            f"Použij potom: `!sell \"{name}\" prodejní_cena`"
        )

    except Exception as e:
        await ctx.send(f"Chyba: `{e}`")


@bot.command()
async def sell(ctx, *, args):
    try:
        parts = shlex.split(args)

        if len(parts) < 2:
            await ctx.send('Použití: `!sell "Logitech Superlight" 1700`')
            return

        name = parts[0]
        sell_price = int(parts[1])

        data = load_flips()
        user = ensure_flip_user(data, get_user_id(ctx), get_user_name(ctx))

        item = find_holding_item(user, name)

        if not item:
            await ctx.send(
                f"Nemáš žádný aktivní item s názvem **{name}**.\n"
                f"Použij `!inventory`."
            )
            return

        profit = sell_price - item["buy_price"]

        item["sell_price"] = sell_price
        item["profit"] = profit
        item["status"] = "sold"
        item["sold_at"] = int(time.time())

        save_flips(data)

        emoji = "🔥" if profit > 0 else "💀"

        await ctx.send(
            f"{emoji} **Prodáno!**\n"
            f"Item: **{item['name']}**\n"
            f"Koupeno za: **{item['buy_price']} Kč**\n"
            f"Prodáno za: **{sell_price} Kč**\n"
            f"Profit: **{profit:+} Kč**"
        )

    except Exception as e:
        await ctx.send(f"Chyba: `{e}`")

@bot.command()
async def losses(ctx):
    data = load_flips()
    user = ensure_flip_user(data, get_user_id(ctx), get_user_name(ctx))

    sold = [i for i in user["items"] if i["status"] == "sold"]

    losses = [i for i in sold if i["profit"] < 0]

    if not losses:
        await ctx.send("Nemáš žádné ztrátové flipy 🔥")
        return

    losses.sort(key=lambda x: x["profit"])

    msg = f"💀 **Ztrátové flipy — {ctx.author.display_name}**\n\n"

    for item in losses[:5]:
        msg += (
            f"**{item['name']}**\n"
            f"{item['buy_price']} → {item['sell_price']} Kč "
            f"(**{item['profit']} Kč**)\n\n"
        )

    await ctx.send(msg)

@bot.command()
async def inventory(ctx):
    data = load_flips()
    user = ensure_flip_user(data, get_user_id(ctx), get_user_name(ctx))

    holding = [item for item in user.get("items", []) if item.get("status") == "holding"]

    if not holding:
        await ctx.send("📦 Inventory je prázdný.")
        return

    msg = f"📦 **Inventory — {ctx.author.display_name}**\n\n"

    total = 0

    for i, item in enumerate(holding, start=1):
        total += item["buy_price"]
        msg += f"`{i}` — **{item['name']}** | {item['buy_price']} Kč\n"

    msg += f"\n💼 Peníze ve zboží: **{total} Kč**"

    await ctx.send(msg)


@bot.command()
async def stats(ctx, member: discord.Member = None):
    target = member or ctx.author

    data = load_flips()
    user_id = str(target.id)

    if user_id not in data:
        await ctx.send("Tenhle user ještě nemá žádné flipy.")
        return

    user = data[user_id]
    stats_data = get_user_stats(user)

    best = stats_data["best_flip"]
    worst = stats_data["worst_flip"]

    best_text = f"{best['name']} ({best['profit']:+} Kč)" if best else "Zatím nic"
    worst_text = f"{worst['name']} ({worst['profit']:+} Kč)" if worst else "Zatím nic"

    await ctx.send(

        
        f"📊 **Stats — {target.display_name}**\n\n"
        f"🏷️ Level: **{stats_data['level']}**\n"
        f"💰 Celkový profit: **{stats_data['total_profit']:+} Kč**\n"
        f"💸 Celkem utraceno: **{stats_data['total_spent']} Kč**\n"
        f"💵 Celkové tržby: **{stats_data['total_revenue']} Kč**\n"
        f"📦 Koupeno itemů: **{stats_data['bought_count']}**\n"
        f"✅ Prodáno itemů: **{stats_data['sold_count']}**\n"
        f"⏳ Aktuálně drží: **{stats_data['holding_count']}**\n"
        f"💼 Peníze ve zboží: **{stats_data['money_in_stock']} Kč**\n"
        f"📈 Průměrný profit/item: **{stats_data['avg_profit']} Kč**\n"
        f"🏆 Win rate: **{stats_data['win_rate']}%**\n"
        f"🔥 Největší flip: **{best_text}**\n"
        f"📉 Nejhorší flip: **{worst_text}**"

        
    )
    
    await update_user_role(ctx, stats_data["total_profit"])

@bot.command()
async def best(ctx):
    data = load_flips()
    user = ensure_flip_user(data, get_user_id(ctx), get_user_name(ctx))

    sold = [i for i in user["items"] if i["status"] == "sold"]

    if not sold:
        await ctx.send("Nemáš žádné prodané itemy.")
        return

    sold.sort(key=lambda x: x["profit"], reverse=True)

    msg = f"🏆 **Top flipy — {ctx.author.display_name}**\n\n"

    for item in sold[:3]:
        msg += (
            f"**{item['name']}**\n"
            f"{item['buy_price']} → {item['sell_price']} Kč "
            f"(**{item['profit']:+} Kč**)\n\n"
        )

    await ctx.send(msg)

@bot.command()
async def leaderboard(ctx):
    data = load_flips()

    if not data:
        await ctx.send("Leaderboard je zatím prázdný.")
        return

    rows = []

    for user_id, user in data.items():
        stats_data = get_user_stats(user)
        rows.append({
            "username": user.get("username", "Unknown"),
            "profit": stats_data["total_profit"],
            "sold": stats_data["sold_count"],
            "level": stats_data["level"]
        })

    rows.sort(key=lambda x: x["profit"], reverse=True)

    msg = "🏆 **Flipping Leaderboard**\n\n"

    for i, row in enumerate(rows[:10], start=1):
        msg += (
            f"`{i}` — **{row['username']}**\n"
            f"Profit: **{row['profit']:+} Kč** | "
            f"Sold: **{row['sold']}** | "
            f"{row['level']}\n\n"
        )

    await ctx.send(msg)


@bot.command()
async def history(ctx, member: discord.Member = None):
    target = member or ctx.author

    data = load_flips()
    user_id = str(target.id)

    if user_id not in data:
        await ctx.send("Tenhle user ještě nemá historii.")
        return

    items = data[user_id].get("items", [])
    sold = [item for item in items if item.get("status") == "sold"]

    if not sold:
        await ctx.send("Zatím žádné prodané itemy.")
        return

    sold.sort(key=lambda x: x.get("sold_at", 0), reverse=True)

    msg = f"🧾 **Historie prodejů — {target.display_name}**\n\n"

    for item in sold[:10]:
        msg += (
            f"**{item['name']}**\n"
            f"{item['buy_price']} Kč → {item['sell_price']} Kč "
            f"(**{item['profit']:+} Kč**)\n\n"
        )

    await ctx.send(msg)


@bot.command()
async def removeitem(ctx, index: int):
    data = load_flips()
    user = ensure_flip_user(data, get_user_id(ctx), get_user_name(ctx))

    holding = [item for item in user.get("items", []) if item.get("status") == "holding"]

    if index < 1 or index > len(holding):
        await ctx.send("Špatné číslo. Použij `!inventory`.")
        return

    item_to_remove = holding[index - 1]
    user["items"].remove(item_to_remove)
    save_flips(data)

    await ctx.send(f"🗑️ Odebráno z inventory: **{item_to_remove['name']}**")


# -------------------------
# AUTO SCAN
# -------------------------

@tasks.loop(minutes=CHECK_EVERY_MINUTES)
async def auto_scan():
    users = load_users()

    if not users:
        return

    print("Auto scan běží...")

    for user_id, user_settings in users.items():
        watchlist = user_settings.get("watchlist", [])
        blacklist = user_settings.get("blacklist", DEFAULT_BLACKLIST.copy())
        seen = set(user_settings.get("seen", []))

        if not watchlist:
            continue

        for item in watchlist:
            channel = bot.get_channel(item["channel_id"])

            if channel is None:
                continue

            try:
                ads = await asyncio.to_thread(
                    search_bazos,
                    item["keyword"],
                    item["max_buy_price"],
                    blacklist
                )

                good_deals = filter_good_deals(
                    ads,
                    item["expected_sell_price"],
                    item["min_profit"]
                )

                for deal in good_deals:
                    if deal["link"] in seen:
                        continue

                    seen.add(deal["link"])
                    user_settings["seen"] = list(seen)
                    users[user_id] = user_settings
                    save_users(users)

                    await channel.send(f"<@{user_id}> 🔥 nový deal podle tvého watchlistu:")
                    await send_deal(channel, deal)
                    await asyncio.sleep(1)

            except Exception as e:
                print(f"Auto scan chyba u usera {user_id}, item {item['keyword']}: {e}")


# -------------------------
# START BOT
# -------------------------

if not DISCORD_BOT_TOKEN:
    raise RuntimeError("Chybí DISCORD_BOT_TOKEN v Railway Variables")

bot.run(DISCORD_BOT_TOKEN)
