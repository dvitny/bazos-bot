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

DISCORD_BOT_TOKEN = "some_token"

LOCATION_ZIP = "40313"
RADIUS_KM = "60"

WATCH_FILE = "watchlist.json"
SEEN_FILE = "seen.json"
BLACKLIST_FILE = "blacklist.json"

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


def load_json(path, default):
    if not os.path.exists(path):
        save_json(path, default)
        return default

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def extract_price(text):
    text = text.replace("\xa0", " ")
    match = re.search(r"(\d[\d ]*)\s*Kč", text)
    if not match:
        return None
    return int(match.group(1).replace(" ", ""))


def normalize(text):
    return text.lower().strip()


def get_blacklist():
    return load_json(BLACKLIST_FILE, DEFAULT_BLACKLIST)


def contains_blocked_words(text):
    text_l = normalize(text)
    blacklist = get_blacklist()
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


def search_bazos(keyword, max_price, limit=50):
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

        if contains_blocked_words(full_text):
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


@bot.event
async def on_ready():
    print(f"Bot běží jako {bot.user}")

    load_json(WATCH_FILE, [])
    load_json(SEEN_FILE, [])
    load_json(BLACKLIST_FILE, DEFAULT_BLACKLIST)

    if not auto_scan.is_running():
        auto_scan.start()


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    print("ZPRÁVA:", message.content)
    await bot.process_commands(message)


@bot.command()
async def test(ctx):
    await ctx.send("funguje")


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

        await ctx.send(f"🔎 Hledám: **{keyword}**")

        ads = await asyncio.to_thread(search_bazos, keyword, max_buy_price)
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

        watchlist = load_json(WATCH_FILE, [])

        item = {
            "keyword": keyword,
            "max_buy_price": max_buy_price,
            "expected_sell_price": expected_sell_price,
            "min_profit": min_profit,
            "channel_id": ctx.channel.id,
            "created_at": int(time.time())
        }

        watchlist.append(item)
        save_json(WATCH_FILE, watchlist)

        await ctx.send(
            f"✅ Přidáno do hlídání:\n"
            f"**{keyword}** | max {max_buy_price} Kč | prodej {expected_sell_price} Kč | min profit {min_profit} Kč"
        )

    except Exception as e:
        await ctx.send(f"Chyba: `{e}`")


@bot.command()
async def unwatch(ctx, index: int):
    watchlist = load_json(WATCH_FILE, [])

    if index < 1 or index > len(watchlist):
        await ctx.send("Špatné číslo. Použij `!watchlist`.")
        return

    removed = watchlist.pop(index - 1)
    save_json(WATCH_FILE, watchlist)

    await ctx.send(f"🗑️ Odebráno: **{removed['keyword']}**")


@bot.command()
async def watchlist(ctx):
    watchlist = load_json(WATCH_FILE, [])

    if not watchlist:
        await ctx.send("Watchlist je prázdný.")
        return

    msg = "**📋 Watchlist:**\n\n"

    for i, item in enumerate(watchlist, start=1):
        msg += (
            f"`{i}` — **{item['keyword']}**\n"
            f"Max koupě: {item['max_buy_price']} Kč | "
            f"Prodej: {item['expected_sell_price']} Kč | "
            f"Min profit: {item['min_profit']} Kč\n\n"
        )

    await ctx.send(msg)


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
async def blacklist(ctx, action=None, *, word=None):
    words = load_json(BLACKLIST_FILE, DEFAULT_BLACKLIST)

    if action == "add" and word:
        word = word.lower().strip()

        if word not in words:
            words.append(word)
            save_json(BLACKLIST_FILE, words)

        await ctx.send(f"✅ Přidáno do blacklistu: **{word}**")
        return

    if action == "remove" and word:
        word = word.lower().strip()

        if word in words:
            words.remove(word)
            save_json(BLACKLIST_FILE, words)

        await ctx.send(f"🗑️ Odebráno z blacklistu: **{word}**")
        return

    msg = "**🚫 Blacklist slova:**\n"
    msg += ", ".join(words[:80])

    if len(words) > 80:
        msg += f"\n...a dalších {len(words) - 80}"

    await ctx.send(msg)


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


@tasks.loop(minutes=CHECK_EVERY_MINUTES)
async def auto_scan():
    watchlist = load_json(WATCH_FILE, [])
    seen = set(load_json(SEEN_FILE, []))

    if not watchlist:
        return

    print("Auto scan běží...")

    for item in watchlist:
        channel = bot.get_channel(item["channel_id"])

        if channel is None:
            continue

        try:
            ads = await asyncio.to_thread(
                search_bazos,
                item["keyword"],
                item["max_buy_price"]
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
                save_json(SEEN_FILE, list(seen))

                await send_deal(channel, deal)
                await asyncio.sleep(1)

        except Exception as e:
            print(f"Auto scan chyba u {item['keyword']}: {e}")


bot.run(DISCORD_BOT_TOKEN)
