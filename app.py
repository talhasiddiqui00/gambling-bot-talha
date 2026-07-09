"""
Highrise Blackjack Gambling Bot (blackjack_bot.py)
Standalone bot - Blackjack rounds only. No emotes, VIP, DJ, or trivia.

EVERY PLAYER'S GAME RUNS INDEPENDENTLY. There is no shared "room round" -
each person tips gold at their own pace, and when they type !bet, a
personal Blackjack round starts just for them, running concurrently with
anyone else's game. Two, five, ten people can all be mid-hand at the same
time; each game only listens to its own player's commands.

Flow for an individual player:
  1. Tip the bot any amount of gold, any time - it's added to your personal
     pending balance (whispered confirmation of your running total).
  2. Type !bet - your whole pending balance becomes your bet (min 5g, max
     1000g; anything tipped over the max is refunded immediately).
  3. 20 seconds later, your own 2 cards + a dealer hand are dealt, announced
     publicly, and it's just your turn: !hit/!h or !stand/!s (15s per
     decision), or !double/!dd on a starting 9/10/11 (confirm by tipping a
     matching amount within 10s).
  4. Dealer auto-plays (hits on soft 17), your result + payout is announced.
  5. Your game clears - tip and !bet again whenever you like.

Payouts: 1.9x on a win, 2.0x on a natural Blackjack, push returns the exact
bet, loss forfeits the bet. 5-Card Charlie (5 cards, no bust) auto-wins.
Real random cards are dealt every game - nothing is rigged. Before starting
any individual game, the bot checks that a worst-case payout on that bet
wouldn't exceed a safe fraction of its own gold balance, refunding the bet
and asking for a smaller one if it would - honest risk management, not
manipulating outcomes.
"""

import os
import sys
import time
import math
import random
import asyncio
import threading
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from json import load, dump

from highrise import BaseBot, User, Position, AnchorPosition, SessionMetadata, CurrencyItem, Item
from highrise.__main__ import main, BotDefinition

sys.stdout.reconfigure(line_buffering=True)
os.environ["PYTHONUNBUFFERED"] = "1"

# --- ROOM / BOT CREDENTIALS ---
ROOM_ID = "6a28b5b000b6151bd4c9641e"
API_TOKEN = "ca3eb4565417e356e291ea4832d8df1422365d5fa2aa528827cba5bc55655a04"  # <-- VERIFY/REPLACE THIS

DATA_FILE = "./blackjack_data.json"

# --- Optional permanent storage via GitHub Gist (same pattern as your main bot) ---
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GIST_ID = os.environ.get("GIST_ID", "").strip()
GIST_FILENAME = "blackjack_data.json"

# Optional: your own public URL (e.g. https://your-app.onrender.com). If set,
# the bot will ping its own /health endpoint periodically. NOTE: this only
# helps keep a process that is ALREADY awake from going idle on some hosts -
# it does NOT wake a process back up once the host has fully spun it down.
# For real 24/7 uptime on a free-tier host you still need an EXTERNAL
# monitor - see the comment above run_web_server() for details.
SELF_URL = os.environ.get("SELF_URL", "").strip()

TIP_MAP = {
    "1g": "gold_bar_1", "5g": "gold_bar_5", "10g": "gold_bar_10",
    "50g": "gold_bar_50", "100g": "gold_bar_100", "500g": "gold_bar_500",
    "1k": "gold_bar_1k", "5k": "gold_bar_5k", "10k": "gold_bar_10k",
}
DENOMINATION_VALUES = [
    (10000, "10k"), (5000, "5k"), (1000, "1k"), (500, "500g"),
    (100, "100g"), (50, "50g"), (10, "10g"), (5, "5g"), (1, "1g"),
]

WIN_MULTIPLIER = 1.9
BLACKJACK_MULTIPLIER = 2.0
PLAYER_TURN_SECONDS = 15
PERSONAL_ROUND_DELAY_SECONDS = 20
DOUBLE_CONFIRM_SECONDS = 10
DOUBLE_ELIGIBLE_TOTALS = (9, 10, 11)
CHARLIE_CARD_COUNT = 5
MIN_BET = 5
MAX_BET = 1000
MAX_EXPOSURE_FRACTION = 0.5
WELCOME_INTERVAL_SECONDS = 60

# How many times / how often to re-assert the bot's saved position right
# after connecting. Highrise can silently snap a freshly-connected bot back
# to the room's default spawn point moments after the first teleport call
# "wins" the race, which is what causes the visible jump back to the crowd
# spawn even though !gset was already used. Re-teleporting a few times in
# the first several seconds makes the saved position "stick".
PLACEMENT_RETRY_DELAYS = (0.3, 1.5, 3.5, 6.0)

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
RANK_VALUES = {
    "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
    "J": 10, "Q": 10, "K": 10, "A": 11,
}

WELCOME_TEXT = (
    "🃏 <color=#FFD700><b>Welcome to the Blackjack Game!</b></color> 🃏 Tip the bot any gold, then type <b>!bet</b> "
    f"(whole balance) or <b>!bet 100g</b> / <b>!bet 1k</b> (a specific amount) to start your OWN round "
    f"(min {MIN_BET}g, max {MAX_BET}g) - runs just for you, even if others are playing too!\n"
    "🂡 <b>!hit</b>/<b>!h</b> draw · ✋ <b>!stand</b>/<b>!s</b> hold · 💰 <b>!double</b>/<b>!dd</b> double a 9-11 · "
    "📖 <b>!rules</b> full rules · 📊 <b>!stats</b> your win ratio · 🏆 <b>!leaderboard</b> top players"
)

RULES_TEXT = (
    "🃏 <color=#FFD700><b>BLACKJACK RULES</b></color> 🃏\n"
    f"💰 Tip ANY amount of gold to the bot, anytime - it's added to your personal balance. Type <b>!bet</b> to bet "
    f"your WHOLE balance, or <b>!bet 100g</b> / <b>!bet 1k</b> to bet just part of it and keep the rest saved "
    f"(min {MIN_BET}g, max {MAX_BET}g per bet - accepted formats: 5g, 10g, 50g, 100g, 500g, 1000g/1k, 5000g/5k).\n"
    "🎮 Your game runs on its own, just for you - your cards are dealt 20 seconds after you !bet, even if other "
    "people are mid-game at the same time.\n"
    "🂡 You'll get 2 cards. Type <b>!hit</b> (or <b>!h</b>) to draw another, or <b>!stand</b> (or <b>!s</b>) to hold.\n"
    "🎯 Get as close to 21 as you can WITHOUT going over - go over 21 and you bust (lose instantly).\n"
    "👑 Beat the dealer's final hand to win <color=#FFD700><b>1.9x</b></color> your bet!\n"
    "🎉 A natural Blackjack (21 with your first 2 cards) pays <color=#FFD700><b>2.0x</b></color>!\n"
    "🤝 Tie the dealer = push, your bet is returned.\n"
    "🂠 Standard house rule: the dealer hits on a soft 17 instead of standing.\n"
    "💵 <b>Double Down:</b> if your first 2 cards total 9, 10, or 11, type <b>!double</b> (or <b>!dd</b>) and tip "
    "a matching bet within 10s - you'll get exactly 1 more card and auto-stand, for double the payout!\n"
    "🃏 <b>5-Card Charlie:</b> draw 5 cards without busting and you win automatically, no matter what the dealer has!\n"
    "⏱️ You only have 15 seconds on your turn - no response means you auto-stand!\n"
    "📊 Type <b>!stats</b> for your personal win ratio, or <b>!leaderboard</b> to see the top players!"
)


def build_shuffled_deck() -> list:
    deck = [(r, s) for r in RANKS for s in SUITS]
    random.shuffle(deck)
    return deck


def format_card(card: tuple) -> str:
    return f"{card[0]}{card[1]}"


def format_hand(cards: list) -> str:
    return " ".join(format_card(c) for c in cards)


def hand_value(cards: list) -> int:
    total = sum(RANK_VALUES[r] for r, _ in cards)
    aces = sum(1 for r, _ in cards if r == "A")
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


def is_soft_hand(cards: list) -> bool:
    hard_total = sum(1 if r == "A" else RANK_VALUES[r] for r, _ in cards)
    return hand_value(cards) != hard_total


def is_blackjack(cards: list) -> bool:
    return len(cards) == 2 and hand_value(cards) == 21


def decompose_amount(amount: int) -> list:
    parts = []
    remaining = int(amount)
    for value, key in DENOMINATION_VALUES:
        while remaining >= value:
            parts.append(key)
            remaining -= value
    return parts


def parse_bet_amount(text: str):
    text = text.strip().lower()
    if not text:
        return None
    multiplier = 1
    if text.endswith("k"):
        multiplier = 1000
        text = text[:-1]
    elif text.endswith("g"):
        text = text[:-1]
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if value <= 0:
        return None
    return int(round(value * multiplier))


class HealthCheckHandler(BaseHTTPRequestHandler):
    # This is the endpoint an external uptime monitor should hit every few
    # minutes to stop a free-tier host from spinning the process down (see
    # the note near run_web_server()).
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Blackjack Bot Live")

    def log_message(self, format, *args):
        return


class Bot(BaseBot):
    instance = None

    def __init__(self):
        super().__init__()
        Bot.instance = self
        self.bot_id = None
        self.owner_username = "sadi_key"
        self.is_initialized = False
        self.last_command_time = {}

        # --- per-player independent game state ---
        self.pending_tips = {}
        self.active_games = {}
        self.player_stats = {}

        self.tip_queue = asyncio.Queue()

        self._gist_dirty = False
        self._gist_pending_data = None

        self._load_startup_state()

    # --- persistence (same pattern as the main bot) ---

    def _gist_configured(self) -> bool:
        return bool(GITHUB_TOKEN and GIST_ID)

    def _gist_headers(self) -> dict:
        return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}

    def fetch_gist_data(self):
        try:
            resp = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=self._gist_headers(), timeout=10)
            resp.raise_for_status()
            file_entry = resp.json().get("files", {}).get(GIST_FILENAME)
            if not file_entry:
                return None
            content = file_entry.get("content", "")
            if file_entry.get("truncated") and file_entry.get("raw_url"):
                raw = requests.get(file_entry["raw_url"], timeout=10)
                raw.raise_for_status()
                content = raw.text
            return json.loads(content) if content.strip() else None
        except Exception as e:
            print(f"[GIST ERROR] Fetch failed, falling back to local disk: {e}")
            return None

    def push_gist_data(self, data: dict) -> None:
        try:
            payload = {"files": {GIST_FILENAME: {"content": json.dumps(data, indent=4)}}}
            resp = requests.patch(f"https://api.github.com/gists/{GIST_ID}", headers=self._gist_headers(), json=payload, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            print(f"[GIST ERROR] Push failed (safe on local disk this run): {e}")

    def queue_gist_push(self, data: dict) -> None:
        self._gist_pending_data = data
        self._gist_dirty = True

    async def gist_sync_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            if not self._gist_configured() or not self._gist_dirty:
                continue
            data_to_push = self._gist_pending_data
            self._gist_dirty = False
            try:
                await asyncio.to_thread(self.push_gist_data, data_to_push)
            except Exception as e:
                print(f"[GIST ERROR] Sync loop push failed, will retry: {e}")
                self._gist_pending_data = data_to_push
                self._gist_dirty = True

    def _load_startup_state(self) -> None:
        data = None
        if self._gist_configured():
            data = self.fetch_gist_data()
        if data is None and os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r") as f:
                    data = load(f)
            except Exception:
                data = None
        data = data or {}
        self._saved_bot_position = data.get("bot_position", {"x": 0, "y": 0, "z": 0, "facing": "FrontRight"})
        self.player_stats = data.get("player_stats", {}) or {}
        self._stranded_tips = data.get("pending_tips", {}) or {}
        self._stranded_bets = data.get("active_bets", {}) or {}
        self._save_state()

    def _save_state(self) -> None:
        try:
            data = {}
            if os.path.exists(DATA_FILE):
                try:
                    with open(DATA_FILE, "r") as f:
                        data = load(f)
                except Exception:
                    data = {}
            data["pending_tips"] = self.pending_tips
            data["active_bets"] = {
                uid: {"username": g["username"], "bet": g["bet"]} for uid, g in self.active_games.items()
            }
            data["player_stats"] = self.player_stats
            data.setdefault("bot_position", getattr(self, "_saved_bot_position", {"x": 0, "y": 0, "z": 0, "facing": "FrontRight"}))
            with open(DATA_FILE, "w") as f:
                dump(data, f, indent=4)
            if self._gist_configured():
                self.queue_gist_push(data)
        except Exception as e:
            print(f"[STATE ERROR] Save failed: {e}")

    def get_bot_position(self) -> Position:
        pos = getattr(self, "_saved_bot_position", {"x": 0, "y": 0, "z": 0, "facing": "FrontRight"})
        return Position(pos["x"], pos["y"], pos["z"], pos["facing"])

    def record_result(self, uid: str, username: str, outcome: str) -> None:
        stats = self.player_stats.get(uid, {
            "username": username, "rounds": 0, "wins": 0, "blackjacks": 0, "charlies": 0, "pushes": 0, "losses": 0,
        })
        stats.setdefault("charlies", 0)
        stats["username"] = username
        stats["rounds"] += 1
        if outcome == "win":
            stats["wins"] += 1
        elif outcome == "blackjack":
            stats["wins"] += 1
            stats["blackjacks"] += 1
        elif outcome == "charlie":
            stats["wins"] += 1
            stats["charlies"] += 1
        elif outcome == "push":
            stats["pushes"] += 1
        else:
            stats["losses"] += 1
        self.player_stats[uid] = stats

    @staticmethod
    def win_ratio_pct(stats: dict) -> float:
        rounds = stats.get("rounds", 0)
        if rounds == 0:
            return 0.0
        return round((stats.get("wins", 0) / rounds) * 100, 1)

    # --- tip payout queue ---

    async def process_tip_queue_worker(self):
        while True:
            target_id, gold_bar_tier, username, reason = await self.tip_queue.get()
            try:
                await self.highrise.tip_user(target_id, gold_bar_tier)
                await asyncio.sleep(random.uniform(1.5, 2.5))
            except Exception as e:
                print(f"[TIP ERROR] Failed to pay {username} ({target_id}): {e}")
                await asyncio.sleep(1.0)
            finally:
                self.tip_queue.task_done()

    async def queue_payout(self, user_id: str, username: str, amount: int, reason: str) -> None:
        for part in decompose_amount(amount):
            await self.tip_queue.put((user_id, TIP_MAP[part], username, reason))

    async def connection_watchdog_loop(self) -> None:
        # Only restarts the process when the websocket connection itself
        # looks dead (repeated transport/timeout errors on a lightweight
        # wallet fetch) - not on a fixed schedule, so it won't cause
        # needless restarts (and therefore won't cause needless position
        # flicker) while the bot is healthy and just sitting idle.
        consecutive_failures = 0
        while True:
            await asyncio.sleep(45)
            try:
                await self.highrise.get_wallet()
                consecutive_failures = 0
            except Exception as e:
                if "closing transport" in str(e).lower() or "timeout" in str(e).lower():
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        print("[WATCHDOG] Connection appears dead - restarting process.")
                        os._exit(1)

    async def welcome_announce_loop(self) -> None:
        while True:
            await asyncio.sleep(WELCOME_INTERVAL_SECONDS)
            await self.announce(WELCOME_TEXT)

    async def self_ping_loop(self) -> None:
        # Optional: pings this bot's own public health-check URL every few
        # minutes if SELF_URL is set. This can help keep an already-running
        # process "warm" on some hosts, but it is NOT a substitute for an
        # external uptime monitor - a process that has fully spun down
        # can't ping itself back awake. Set SELF_URL and, more importantly,
        # point an external monitor (UptimeRobot / cron-job.org / etc.) at
        # the same URL for real 24/7 uptime.
        if not SELF_URL:
            return
        while True:
            await asyncio.sleep(240)
            try:
                await asyncio.to_thread(requests.get, SELF_URL, timeout=10)
            except Exception as e:
                print(f"[SELF-PING] Failed (non-fatal): {e}")

    # --- shared helpers ---

    async def announce(self, msg: str) -> None:
        try:
            await self.highrise.chat(msg)
        except Exception as e:
            print(f"[ANNOUNCE ERROR] {e}")

    async def get_wallet_gold(self):
        try:
            wallet = await self.highrise.get_wallet()
            return next((c.amount for c in wallet.content if c.type == "gold"), 0)
        except Exception:
            return None

    async def refund_stranded_gold(self) -> None:
        for uid, info in getattr(self, "_stranded_tips", {}).items():
            await self.queue_payout(uid, info.get("username", "player"), info.get("amount", 0), "bj_stranded_tip_refund")
        for uid, info in getattr(self, "_stranded_bets", {}).items():
            await self.queue_payout(uid, info.get("username", "player"), info.get("bet", 0), "bj_stranded_bet_refund")
        self._stranded_tips = {}
        self._stranded_bets = {}

    # --- personal per-player round ---

    async def run_personal_round(self, uid: str) -> None:
        game = self.active_games.get(uid)
        if not game:
            return
        username = game["username"]
        bet = game["bet"]
        try:
            await asyncio.sleep(PERSONAL_ROUND_DELAY_SECONDS)

            deck = build_shuffled_deck()
            game["deck"] = deck
            game["cards"] = [deck.pop(), deck.pop()]
            game["dealer_cards"] = [deck.pop(), deck.pop()]
            dealer_up = game["dealer_cards"][0]

            await self.announce(
                f"🎴 @{username}'s Blackjack round is live! Bet: <b>{bet}g</b>. Hand: <b>{format_hand(game['cards'])}</b> "
                f"({hand_value(game['cards'])}). Dealer shows <b>{format_card(dealer_up)}</b> and a hidden card."
            )

            if is_blackjack(game["cards"]):
                await self.announce(f"🎉 @{username} has a natural <color=#FFD700><b>BLACKJACK</b></color>! Auto-standing.")
                game["done"] = True
            else:
                double_hint = (
                    " You can also type <b>!double</b>/<b>!dd</b> to double your bet for one extra card!"
                    if hand_value(game["cards"]) in DOUBLE_ELIGIBLE_TOTALS else ""
                )
                await self.announce(
                    f"👉 @{username}, it's your turn! Type <b>!hit</b>/<b>!h</b> or <b>!stand</b>/<b>!s</b>!{double_hint} (15s)"
                )
                game["awaiting_action"] = True
                turn_start = asyncio.get_running_loop().time()
                while not game["done"]:
                    if game.get("pending_double"):
                        if time.time() > game["pending_double"]["deadline"]:
                            game["pending_double"] = None
                            await self.announce(f"⌛ @{username} didn't confirm the double in time - go ahead with !hit or !stand.")
                            turn_start = asyncio.get_running_loop().time()
                        await asyncio.sleep(1)
                        continue
                    if asyncio.get_running_loop().time() - turn_start > PLAYER_TURN_SECONDS:
                        game["done"] = True
                        await self.announce(f"⌛ @{username} ran out of time - auto-standing with {hand_value(game['cards'])}.")
                        break
                    await asyncio.sleep(1)
                game["awaiting_action"] = False

            dealer_cards = game["dealer_cards"]
            await self.announce(f"🂠 Dealer reveals: <b>{format_hand(dealer_cards)}</b> ({hand_value(dealer_cards)})")
            while hand_value(dealer_cards) < 17 or (hand_value(dealer_cards) == 17 and is_soft_hand(dealer_cards)):
                dealer_cards.append(deck.pop())
            dealer_total = hand_value(dealer_cards)
            dealer_bust = dealer_total > 21
            dealer_bj = is_blackjack(dealer_cards)
            await self.announce(
                f"🂠 Dealer's final hand: <b>{format_hand(dealer_cards)}</b> ({dealer_total})"
                + (" - <color=#FF0000><b>BUST!</b></color>" if dealer_bust else "")
            )

            cards = game["cards"]
            player_total = hand_value(cards)
            player_bust = player_total > 21
            player_bj = is_blackjack(cards)

            if player_bust:
                outcome = "lose"
            elif game.get("charlie"):
                outcome = "charlie"
            elif player_bj and dealer_bj:
                outcome = "push"
            elif player_bj:
                outcome = "blackjack"
            elif dealer_bust:
                outcome = "win"
            elif player_total > dealer_total:
                outcome = "win"
            elif player_total == dealer_total:
                outcome = "push"
            else:
                outcome = "lose"

            double_tag = " (doubled!)" if game.get("doubled") else ""

            if outcome == "win":
                payout = math.floor(bet * WIN_MULTIPLIER)
                await self.queue_payout(uid, username, payout, "bj_win")
                await self.announce(f"🎉 @{username} WINS <color=#FFD700><b>{payout}g</b></color>!{double_tag} (bet {bet}g, hand {player_total})")
            elif outcome == "blackjack":
                payout = math.floor(bet * BLACKJACK_MULTIPLIER)
                await self.queue_payout(uid, username, payout, "bj_blackjack")
                await self.announce(f"🃏 @{username} BLACKJACK! Wins <color=#FFD700><b>{payout}g</b></color>! (bet {bet}g)")
            elif outcome == "charlie":
                payout = math.floor(bet * WIN_MULTIPLIER)
                await self.queue_payout(uid, username, payout, "bj_charlie")
                await self.announce(f"🃏 @{username} hit a <color=#FFD700><b>5-CARD CHARLIE</b></color>! Automatic win of <b>{payout}g</b>!{double_tag} (bet {bet}g)")
            elif outcome == "push":
                await self.queue_payout(uid, username, bet, "bj_push")
                await self.announce(f"🤝 @{username} PUSH - {bet}g bet refunded. (hand {player_total})")
            else:
                await self.announce(f"💀 @{username} loses {bet}g.{double_tag} (hand {player_total})")

            self.record_result(uid, username, outcome)

        except Exception as e:
            print(f"[ROUND ERROR] @{username}: {e}")
            await self.announce(f"⚠️ @{username}'s round hit an error and had to be cancelled. Your {bet}g bet has been refunded.")
            await self.queue_payout(uid, username, bet, "bj_round_error_refund")
        finally:
            self.active_games.pop(uid, None)
            self._save_state()

    # --- Highrise event hooks ---

    async def on_start(self, session_metadata: SessionMetadata) -> None:
        print("Blackjack Bot Connected")
        self.bot_id = session_metadata.user_id

        if self.is_initialized:
            # A reconnect within the SAME running process, not a fresh
            # process start - the bot never actually left its spot, so
            # re-teleporting here would just add a pointless extra jump.
            print("[RECONNECT] Session restarted - skipping re-teleport to avoid flicker.")
            return
        self.is_initialized = True

        asyncio.create_task(self.place_bot())
        asyncio.create_task(self.process_tip_queue_worker())
        asyncio.create_task(self.connection_watchdog_loop())
        asyncio.create_task(self.gist_sync_loop())
        asyncio.create_task(self.refund_stranded_gold())
        asyncio.create_task(self.self_ping_loop())
        await self.announce(WELCOME_TEXT)
        asyncio.create_task(self.welcome_announce_loop())

    async def place_bot(self):
        pos = self.get_bot_position()
        if pos == Position(0, 0, 0, "FrontRight"):
            # No custom spot saved yet - nothing to restore, leave the bot
            # wherever Highrise put it and let the owner run !gset once.
            return
        # Re-assert the saved position several times over the first ~6
        # seconds after connecting. A single teleport call right after
        # connect can get silently overwritten by Highrise's own spawn
        # placement for a newly-joined bot; repeating it makes sure the
        # saved spot actually sticks instead of the bot settling back at
        # the crowd spawn point.
        for delay in PLACEMENT_RETRY_DELAYS:
            await asyncio.sleep(delay)
            try:
                await self.highrise.teleport(self.bot_id, pos)
            except Exception:
                pass

    async def on_tip(self, sender: User, receiver: User, tip) -> None:
        if sender.id == self.bot_id or receiver.id != self.bot_id:
            return
        if not isinstance(tip, CurrencyItem):
            return

        game = self.active_games.get(sender.id)

        if game and game.get("pending_double"):
            pending = game["pending_double"]
            if time.time() > pending["deadline"]:
                game["pending_double"] = None
            elif tip.amount == pending["amount"]:
                game["pending_double"] = None
                game["bet"] += tip.amount
                game["doubled"] = True
                if not game["deck"]:
                    game["deck"] = build_shuffled_deck()
                game["cards"].append(game["deck"].pop())
                total = hand_value(game["cards"])
                if total > 21:
                    game["busted"] = True
                await self.announce(
                    f"💰 @{sender.username} DOUBLES DOWN! New bet: <b>{game['bet']}g</b>. "
                    f"Draws {format_card(game['cards'][-1])} - hand: {format_hand(game['cards'])} ({total})"
                    + (" - <color=#FF0000><b>BUST!</b></color>" if total > 21 else " - standing.")
                )
                game["done"] = True
                self._save_state()
                return
            else:
                await self.queue_payout(sender.id, sender.username, tip.amount, "bj_double_mismatch_refund")
                try:
                    await self.highrise.send_whisper(
                        sender.id,
                        f"⚠️ That didn't match the {pending['amount']}g needed to double down, so it's been refunded. "
                        "Double down offer cancelled - just !hit or !stand instead."
                    )
                except Exception:
                    pass
                game["pending_double"] = None
                return

        entry = self.pending_tips.get(sender.id, {"username": sender.username, "amount": 0})
        entry["username"] = sender.username
        entry["amount"] += tip.amount
        self.pending_tips[sender.id] = entry
        self._save_state()
        try:
            await self.highrise.send_whisper(
                sender.id,
                f"✅ You now have <b>{entry['amount']}g</b> ready to bet! Type !bet to start your round "
                f"(min {MIN_BET}g, max {MAX_BET}g)."
            )
        except Exception:
            pass

    async def on_chat(self, user: User, message: str) -> None:
        await self.command_handler(user, message, "chat")

    async def on_whisper(self, user: User, message: str) -> None:
        await self.command_handler(user, message, "whisper")

    async def respond(self, user: User, msg: str, source: str):
        if source == "chat":
            await self.highrise.chat(msg)
        else:
            await self.highrise.send_whisper(user.id, msg)

    async def command_handler(self, user: User, message: str, source: str):
        if not message or not message.strip():
            return
        clean_msg = message.lower().strip()

        now = time.time()
        user_history = self.last_command_time.get(user.id, {})
        last_time = user_history.get(clean_msg, 0)
        if now - last_time < 1.5:
            return
        user_history[clean_msg] = now
        self.last_command_time[user.id] = user_history

        is_owner = user.username.lower() == self.owner_username.lower()

        bet_parts = clean_msg.split()
        if bet_parts and bet_parts[0] == "!bet":
            if user.id in self.active_games:
                await self.respond(user, "⏳ You already have a Blackjack round in progress! Finish it before starting another.", "whisper")
                return
            entry = self.pending_tips.get(user.id)
            if not entry or entry["amount"] < MIN_BET:
                await self.respond(user, f"💰 Tip the bot at least {MIN_BET}g first, then type !bet to start your round!", "whisper")
                return

            username = entry["username"]
            arg = bet_parts[1] if len(bet_parts) > 1 else None
            refund = 0

            if arg is None:
                bet_amount = entry["amount"]
                if bet_amount > MAX_BET:
                    refund = bet_amount - MAX_BET
                    bet_amount = MAX_BET
                self.pending_tips.pop(user.id, None)
            else:
                parsed = parse_bet_amount(arg)
                if parsed is None:
                    await self.respond(
                        user,
                        "⚠️ Couldn't read that bet amount. Try formats like !bet 5g, !bet 100g, !bet 1k, or !bet 5k.",
                        "whisper",
                    )
                    return
                if parsed < MIN_BET or parsed > MAX_BET:
                    await self.respond(user, f"⚠️ Bet must be between {MIN_BET}g and {MAX_BET}g.", "whisper")
                    return
                if parsed > entry["amount"]:
                    await self.respond(
                        user,
                        f"⚠️ You've only got {entry['amount']}g tipped - tip more or bet a smaller amount.",
                        "whisper",
                    )
                    return
                bet_amount = parsed
                entry["amount"] -= bet_amount
                if entry["amount"] <= 0:
                    self.pending_tips.pop(user.id, None)
                else:
                    self.pending_tips[user.id] = entry

            fetched_gold = await self.get_wallet_gold()
            wallet_gold = fetched_gold if fetched_gold is not None else 0
            worst_case = bet_amount * BLACKJACK_MULTIPLIER
            if worst_case > wallet_gold * MAX_EXPOSURE_FRACTION:
                await self.queue_payout(user.id, username, bet_amount + refund, "bj_house_cap_refund")
                self._save_state()
                await self.respond(
                    user,
                    "⚠️ Sorry, the house can't safely cover a bet that size right now - your gold has been refunded. Try a smaller bet!",
                    "whisper",
                )
                return

            if refund > 0:
                await self.queue_payout(user.id, username, refund, "bj_over_max_refund")
                await self.respond(
                    user,
                    f"⚠️ Max bet is {MAX_BET}g, so the extra {refund}g has been refunded. Starting your round with {bet_amount}g!",
                    "whisper",
                )
            elif arg is not None:
                remaining = self.pending_tips.get(user.id, {}).get("amount", 0)
                await self.respond(
                    user,
                    f"✅ {bet_amount}g bet placed! {remaining}g remaining in your balance.",
                    "whisper",
                )

            self.active_games[user.id] = {
                "username": username, "bet": bet_amount, "cards": [], "dealer_cards": [], "deck": [],
                "done": False, "awaiting_action": False, "doubled": False, "charlie": False,
                "busted": False, "pending_double": None,
            }
            self._save_state()
            await self.announce(
                f"🎰 @{username} placed a <b>{bet_amount}g</b> Blackjack bet! Cards will be dealt in "
                f"{PERSONAL_ROUND_DELAY_SECONDS} seconds..."
            )
            asyncio.create_task(self.run_personal_round(user.id))
            return

        if clean_msg in ("!hit", "!h", "!stand", "!s", "!double", "!dd"):
            game = self.active_games.get(user.id)
            if not game or not game.get("awaiting_action"):
                return

            if clean_msg in ("!double", "!dd"):
                if game.get("pending_double"):
                    return
                if len(game["cards"]) != 2:
                    await self.respond(user, "⚠️ You can only double down on your first two cards.", "whisper")
                    return
                total = hand_value(game["cards"])
                if total not in DOUBLE_ELIGIBLE_TOTALS:
                    await self.respond(
                        user,
                        f"⚠️ Double down is only available on a starting total of 9, 10, or 11 (yours is {total}).",
                        "whisper",
                    )
                    return
                game["pending_double"] = {"amount": game["bet"], "deadline": time.time() + DOUBLE_CONFIRM_SECONDS}
                await self.announce(
                    f"💰 @{user.username} wants to DOUBLE DOWN! Tip <b>{game['bet']}g</b> within "
                    f"{DOUBLE_CONFIRM_SECONDS}s to confirm (must match your original bet exactly)."
                )
                return

            if clean_msg in ("!stand", "!s"):
                game["done"] = True
                await self.respond(user, f"✋ @{user.username} stands with {hand_value(game['cards'])}.", "chat")
                return

            if not game["deck"]:
                game["deck"] = build_shuffled_deck()
            game["cards"].append(game["deck"].pop())
            total = hand_value(game["cards"])
            if total > 21:
                game["busted"] = True
                game["done"] = True
                await self.respond(user, f"💥 @{user.username} draws {format_card(game['cards'][-1])} - hand: {format_hand(game['cards'])} ({total}) - BUST!", "chat")
            elif len(game["cards"]) >= CHARLIE_CARD_COUNT:
                game["charlie"] = True
                game["done"] = True
                await self.respond(
                    user,
                    f"🃏 @{user.username} draws {format_card(game['cards'][-1])} - hand: {format_hand(game['cards'])} ({total}) - "
                    f"<b>5-CARD CHARLIE!</b> Automatic win!",
                    "chat",
                )
            else:
                await self.respond(user, f"🂠 @{user.username} draws {format_card(game['cards'][-1])} - hand: {format_hand(game['cards'])} ({total}). Hit or stand?", "chat")
            return

        if clean_msg == "!rules":
            await self.respond(user, RULES_TEXT, source)
            return

        if clean_msg in ("!stats", "!wr", "!winrate"):
            stats = self.player_stats.get(user.id)
            if not stats or stats.get("rounds", 0) == 0:
                await self.respond(user, "📊 You haven't played a round yet - tip the bot and type !bet to get started!", "whisper")
                return
            ratio = self.win_ratio_pct(stats)
            await self.respond(
                user,
                (
                    f"📊 <b>Your Blackjack stats</b>\n"
                    f"Rounds played: {stats['rounds']} | Wins: {stats['wins']} (incl. {stats['blackjacks']} Blackjacks, "
                    f"{stats.get('charlies', 0)} Charlies) | Pushes: {stats['pushes']} | Losses: {stats['losses']}\n"
                    f"🏆 Win ratio: <b>{ratio}%</b>"
                ),
                "whisper",
            )
            return

        if clean_msg in ("!leaderboard", "!lb"):
            qualified = [s for s in self.player_stats.values() if s.get("rounds", 0) >= 3]
            if not qualified:
                await self.respond(user, "📊 Not enough rounds played yet for a leaderboard - play a few rounds first!", source)
                return
            ranked = sorted(qualified, key=lambda s: self.win_ratio_pct(s), reverse=True)[:5]
            lines = ["🏆 <b>Blackjack Leaderboard</b> (min. 3 rounds played)"]
            for i, s in enumerate(ranked, start=1):
                lines.append(f"{i}. @{s['username']} - {self.win_ratio_pct(s)}% win ratio ({s['rounds']} rounds)")
            await self.respond(user, "\n".join(lines), source)
            return

        if not is_owner:
            return

        if clean_msg == "!gset":
            try:
                room_users = await self.highrise.get_room_users()
                position = None
                for u, pos in room_users.content:
                    if u.id == user.id:
                        position = pos
                        break
                if isinstance(position, Position):
                    self._saved_bot_position = {"x": position.x, "y": position.y, "z": position.z, "facing": position.facing}
                    self._save_state()
                    await self.highrise.teleport(self.bot_id, position)
                    await self.respond(user, "📍 Bot position updated successfully!", source)
            except Exception as e:
                print(f"[SET ERROR] {e}")
            return

        if clean_msg == "!gbal":
            gold = await self.get_wallet_gold()
            if gold is None:
                await self.highrise.send_whisper(user.id, "⚠️ Couldn't fetch the wallet balance right now - try again in a moment.")
            else:
                await self.highrise.send_whisper(user.id, f"💰 Blackjack bot balance: {gold}g")
            return


def run_web_server():
    # This tiny HTTP server exists so hosting platforms (Render, Railway,
    # etc.) can health-check the process. On FREE tiers, these platforms
    # spin the service down after a period with no inbound HTTP requests -
    # when that happens the bot disconnects and, on the next request,
    # restarts from scratch (fresh process -> is_initialized=False again),
    # which is what causes it to reappear at the room's default spawn
    # before place_bot() teleports it back.
    #
    # To keep the bot up 24/7:
    #   1. Point an external uptime monitor (UptimeRobot, cron-job.org,
    #      Freshping, etc. - most have free tiers) at this service's public
    #      URL, hitting it every 3-5 minutes. That's what actually prevents
    #      the platform from ever spinning the process down.
    #   2. Optionally set the SELF_URL environment variable to the same
    #      public URL so the bot also pings itself (belt-and-suspenders -
    #      does not replace step 1).
    #   3. If your host offers an "always on" / paid instance tier, that
    #      removes the idle-spindown behavior entirely.
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


if __name__ == "__main__":
    web_worker = threading.Thread(target=run_web_server, daemon=True)
    web_worker.start()
    asyncio.run(main([BotDefinition(Bot(), ROOM_ID, API_TOKEN)]))
